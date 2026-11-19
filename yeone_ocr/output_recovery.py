"""Image-checked output repairs and quality gates; never rewrite OCR history."""
from copy import deepcopy
import hashlib
import json
import re

from .schema import PageReading


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf8')).hexdigest()


def unexpected_latin_lines(text):
    """Route whole rows in predominantly Han pages, never translate Latin blindly.

    This is a review signal, including for legitimate Latin names, not an error
    verdict. English pages and Korean mixed prose do not enable this heuristic.
    """
    han = sum('\u3400' <= c <= '\u9fff' for c in text)
    letters = sum(c.isalpha() for c in text)
    if han < 40 or han / max(1, letters) < .65:
        return []
    rows = []
    for match in re.finditer(r'[^\n]+', text):
        if re.search(r'[A-Za-z]{2,}', match[0]):
            rows.append((match.start(), match.end()))
    return rows


def apply_repairs(records, manifest, source_sha256):
    """Apply only explicit, image-checked, exact-snapshot repairs in memory.

    A changed correction snapshot invalidates the entire transaction. Missing
    pages are allowed for partial builds; their patches are not applied.
    """
    if manifest.get('schema') != 1 or manifest.get('source_sha256') != source_sha256:
        raise ValueError('출력 복원 기록의 형식 또는 원본 지문이 다릅니다.')
    output = deepcopy(records)
    by_page = {r['pdf_page']: r for r in output}
    seen_pages = set()
    for patch in manifest.get('pages', []):
        number = patch['pdf_page']
        if number in seen_pages:
            raise ValueError('출력 복원 쪽이 중복되었습니다.')
        seen_pages.add(number)
        if number not in by_page:
            continue
        record = by_page[number]
        if digest(record['merged']) != patch.get('merged_sha256'):
            raise ValueError(f'{number}쪽 교정문이 변경되어 출력 복원을 보류합니다.')
        affected = set()
        preserved_grades = []
        for change in patch['changes']:
            evidence = change.get('evidence', {})
            if (evidence.get('method') != 'source-image-checked' or not evidence.get('note')
                    or not re.fullmatch(r'[0-9a-f]{64}', evidence.get('image_sha256', ''))):
                raise ValueError('원본 이미지 대조 근거가 없는 복원입니다.')
            ids = change['region_ids']
            regions = record['merged']['regions']
            positions = [i for i, r in enumerate(regions) if r['id'] in ids]
            if (not ids or affected.intersection(ids) or len(positions) != len(ids)
                    or positions != list(range(positions[0], positions[-1] + 1))
                    or [regions[i]['id'] for i in positions] != ids):
                raise ValueError('복원 영역의 순서·범위가 일치하지 않습니다.')
            replacements = change['regions']
            if not replacements:
                raise ValueError('빈 복원 영역은 허용하지 않습니다.')
            for region in replacements:
                y0, x0, y1, x1 = region['bbox']
                if not (0 <= y0 < y1 <= 1000 and 0 <= x0 < x1 <= 1000):
                    raise ValueError('복원 영역의 원본 좌표가 올바르지 않습니다.')
                # Splitting a title must not erase evidence on the unchanged
                # body. Only an exact, unique single-paragraph substring maps.
                for old in [regions[i] for i in positions]:
                    if len(old.get('paragraphs', [])) != 1 or len(region.get('paragraphs', [])) != 1:
                        continue
                    before = ''.join(c['text'] for c in old['paragraphs'][0]['columns'])
                    after = ''.join(c['text'] for c in region['paragraphs'][0]['columns'])
                    if not after or before.count(after) != 1:
                        continue
                    offset = before.index(after)
                    for grade in record.get('local_grades', []):
                        if grade['region_id'] != old['id'] or grade.get('para', 0) != 0:
                            continue
                        a, b = max(grade['start'], offset), min(grade['end'], offset + len(after))
                        if a < b:
                            preserved_grades.append(dict(grade, region_id=region['id'], start=a-offset, end=b-offset))
            regions[positions[0]:positions[-1] + 1] = deepcopy(replacements)
            affected.update(ids)
        reading = PageReading.model_validate(record['merged'])
        ids = [r.id for r in reading.regions]
        if len(ids) != len(set(ids)):
            raise ValueError('복원 후 영역 식별자가 중복되었습니다.')
        record['merged'] = reading.model_dump()
        # Offsets from the damaged rows cannot describe their restored text.
        record['local_grades'] = [g for g in record.get('local_grades', []) if g['region_id'] not in affected] + preserved_grades
        record['_output_repairs'] = deepcopy(patch)
    return output


def quality_report(blocks, records):
    pages = {}
    for b in blocks:
        if b.text:
            pages.setdefault(b.page, []).append(b)
    findings = []
    for page, rows in pages.items():
        text = '\n'.join(b.text for b in rows)
        for start, end in unexpected_latin_lines(text):
            findings.append({'page': page, 'kind': 'unexpected-latin', 'text': text[start:end],
                             'action': '원본 행 전체 대조·재판독'})
        previous = None
        for b in rows:
            if b.type == 'heading':
                previous = None
            m = re.match(r'^\s*(\d{1,3})\s*[.．、]\s*[《『]', b.text)
            if m:
                number = int(m[1])
                if previous is not None and number > previous + 1:
                    findings.append({'page': page, 'kind': 'citation-gap',
                                     'missing': list(range(previous + 1, number))})
                previous = number
    unresolved = []
    for r in records:
        first = r.get('local_first', {})
        pending = sum(p.get('decision') == 'pending' and not p.get('stale') for p in first.get('ai_proposals', []))
        if pending:
            unresolved.append({'page': r['pdf_page'], 'pending_proposals': pending,
                               'execution_status': first.get('review', {}).get('status'),
                               'output_repairs': bool(r.get('_output_repairs'))})
    return {'schema': 1, 'status': 'needs-review' if findings or unresolved else 'no-detected-issues',
            'findings': findings, 'unresolved_history': unresolved,
            'repairs': [r['_output_repairs'] for r in records if r.get('_output_repairs')]}
