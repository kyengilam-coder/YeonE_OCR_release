"""Separate comparable readings from alignment failures in display grades."""
from difflib import SequenceMatcher

from .collate.zh import script_fold, script_pair


def unsupported_disagreement(base, candidates):
    """Two engines agree, and no other engine supplies a comparable dissent."""
    letters = ''.join(c for c in base if c.isalnum())
    if not letters or len(candidates) < 2:
        return False
    supported = 0
    folded = script_fold(letters)
    for candidate in candidates.values():
        other = ''.join(c for c in candidate if c.isalnum())
        if len(other) == len(letters) and all(a == b or script_pair(a, b) for a, b in zip(letters, other)):
            supported += 1
        elif (abs(len(other) - len(letters)) < 3 or
              SequenceMatcher(None, folded, script_fold(other), autojunk=False).ratio() >= .55):
            # A localized different character or a small omission is real
            # review evidence even when the other engines agree.
            return False
    return supported >= 2


def narrow_saved_grades(record, page):
    """Replay this display rule on old, unchanged baseline pages."""
    from .schema import PageReading, paragraph_text

    if not record.get('local_grades') or (record.get('local_first', {}).get('ai_page') or {}).get('used'):
        return record
    if (page.get('corrected_text') is not None or any(page.get(k) for k in ('edits', 'decisions', 'ai_decisions'))
            or any(review.get('proposals') for review in page.get('ai_reviews', ()))):
        return record
    baseline = next((run for run in page.get('runs', ()) if run['id'] == page.get('baseline_run_id')), None)
    if baseline is None:
        return record
    reading = PageReading.model_validate(record['merged'])
    lines = [paragraph_text(p) for region in reading.regions for p in region.paragraphs]
    current = '\n'.join(lines)
    if current != baseline['raw']['text']:
        return record
    rejected = set()
    protected = set()
    for item in page.get('suggestions', ()):
        if item.get('decision') != 'pending' or item.get('kind') == 'whitespace':
            continue
        start, end = item['start'], item['end']
        positions = rejected if unsupported_disagreement(current[start:end], item['candidates']) else protected
        positions.update(range(start, end))
    rejected.difference_update(protected)
    if not rejected:
        return record
    line_start = {}
    offset = 0
    for index, line in enumerate(lines, 1):
        line_start[f'local-line-{index}'] = offset
        offset += len(line) + 1
    grades = []
    removed = 0
    for item in record['local_grades']:
        offset = line_start.get(item.get('region_id'))
        if offset is None or item.get('para', 0) != 0 or item.get('grade') != 'red':
            grades.append(item)
            continue
        start, end = item['start'], item['end']
        cursor = start
        for position in range(start, end):
            absolute = offset + position
            if absolute in rejected and current[absolute] not in '\ufffd\u25a1':
                if cursor < position:
                    grades.append(dict(item, start=cursor, end=position))
                cursor = position + 1
                removed += int(current[absolute].isalnum())
        if cursor < end:
            grades.append(item if cursor == start else dict(item, start=cursor))
    return dict(record, local_grades=grades, _narrowed_local_disagreements=removed) if removed else record
