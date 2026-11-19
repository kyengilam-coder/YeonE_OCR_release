"""Prepare short source-backed index rows before prose assembly."""
import re
from collections import Counter, defaultdict

from .schema import PageReading, TEXT_KINDS, paragraph_text
from .source_lists import _measure_columns, _pdf_lines, key, page_lists, separator_shape, visible_gap_shape
from .toc_tabs import list_entry

ATTACHED_NUMBER = re.compile(r'(?P<leader>[ \u3000]*)(?P<page>[（(]\s*\d{1,4}\s*[)）])\s*$')
GROUP = re.compile(r'(?:[A-Z]|\d{1,2}\s*[畫画劃划획])\Z')


def index_entry(text, *, allow_plain=False):
    match = list_entry(text)
    if match is None or not match['page'].startswith(('(', '（')):
        match = ATTACHED_NUMBER.search(text) or (match if allow_plain else None)
    if match is None or (not allow_plain and not match['page'].endswith((')', '）'))):
        return None
    label = text[:match.start()].strip()
    if 1 <= len(label) <= 30 and label[0].isalpha() and not re.search(r'[。!?！？;；:：\n]', label):
        return match
    return None


def _source_rows(page, number, *, allow_plain=False):
    lines, chars = _pdf_lines(page)
    entries = []
    for text, x, y, right, bottom in lines:
        if not .08 < y < .93 or (m := index_entry(text, allow_plain=allow_plain)) is None:
            continue
        leader = separator_shape(m['leader'])
        glyphs = chars[(x, y)]
        a, b = m.span('leader')
        gap = None
        if 0 < a <= b < len(glyphs):
            label_end, number_start = glyphs[a-1]['bbox'], glyphs[b]['bbox']
            w, h = page.rect.width, page.rect.height
            gap = (label_end[2]/w, label_end[1]/h, number_start[0]/w, number_start[3]/h)
            if (gap[2]-gap[0])*w >= 10:
                leader = visible_gap_shape(page, gap) or leader
        label = text[:m.start()].strip()
        entries.append(dict(kind='index', page=number, x=x, y=y, right=right, bottom=bottom,
                            label=key(label), target=key(m['page']), text=label, leader=leader, gap=gap))
    if len(entries) < 8:
        return [], []
    rows = _measure_columns(entries, 0)
    if len(rows) < 8:
        return [], []
    headings = [(text.strip(), x, y) for text, x, y, _, _ in lines if GROUP.fullmatch(text.strip())]
    return rows, headings


def prepare_list_pages(records, pdf, index_pages=(), *, contents_pages=()):
    for record in records:
        reading = PageReading.model_validate(record['merged'])
        items = [(i, r, paragraph_text(r.paragraphs[0]).strip()) for i, r in enumerate(reading.regions)
                 if r.kind in TEXT_KINDS and len(r.paragraphs) == 1 and paragraph_text(r.paragraphs[0]).strip()]
        number = record['pdf_page']
        allow_plain = number in index_pages
        if number in contents_pages and not allow_plain:
            continue
        indexed = [(i, r, text, m) for i, r, text in items
                   if (m := index_entry(text, allow_plain=allow_plain)) is not None]
        if not 1 <= number <= len(pdf):
            continue
        explicit = [(r, text, m) for _, r, text in items if (m := list_entry(text)) is not None]
        if len(explicit) >= 4:
            lines, _ = _pdf_lines(pdf[number-1])
            proofs = {row['key'] for row in page_lists(lines, number) if row['kind'] == 'toc'}
            matched = {r.id for r, text, m in explicit if key(text[:m.start()])+key(m['page']) in proofs}
            if len(matched) >= 4:
                record['_source_list_regions'] = [r.id for r, _, _ in explicit]
        if len(indexed) < 8 or len(indexed) < len(items)*.6:
            continue
        rows, headings = _source_rows(pdf[number-1], number, allow_plain=allow_plain)
        if not rows:
            continue
        exact = defaultdict(list)
        targets = defaultdict(list)
        for j, row in enumerate(rows):
            exact[row['label'], row['target']].append(j)
            targets[row['target']].append(j)
        pairs = {i: (key(text[:m.start()]), key(m['page'])) for i, _, text, m in indexed}
        frequency = Counter(pairs.values())
        if sum(frequency[pair] == 1 and len(exact[pair]) == 1 for pair in pairs.values()) < max(5, len(indexed)*.4):
            continue
        matched = {i: exact[pair][0] for i, pair in pairs.items() if len(exact[pair]) == 1 and frequency[pair] == 1}
        used = set(matched.values())
        starts = {row['column']: row['column_start'] for row in rows}

        def position(x, y):
            col = min(starts, key=lambda c: abs(x-starts[c]))
            return (col, y) if abs(x-starts[col]) <= .065 else None

        positions = {}
        for i, region, _, _ in indexed:
            y, x, bottom, _ = region.bbox
            xy = position(x/1000, y/1000) if bottom-y < 50 else None
            candidates = [j for j in targets[pairs[i][1]] if j not in used]
            if i not in matched and xy:
                nearby = [j for j, row in enumerate(rows) if j not in used and row['column'] == xy[0] and abs(row['y']-xy[1]) <= .012]
                if len(nearby) == 1:
                    candidates = nearby
            if i not in matched and len(candidates) == 1:
                matched[i] = candidates[0]
                used.add(candidates[0])
            if i in matched:
                row = rows[matched[i]]
                positions[i] = (row['column'], row['y'])
            elif xy:
                positions[i] = xy

        for i, region, text in items:
            if i in positions:
                continue
            if GROUP.fullmatch(text):
                options = [p for label, x, y in headings if label == text and (p := position(x, y))]
                if len(options) == 1:
                    positions[i] = options[0]
                    continue
                y, x, bottom, _ = region.bbox
                if bottom-y < 50 and (xy := position(x/1000, y/1000)):
                    positions[i] = xy
                    continue
            if i in pairs or GROUP.fullmatch(text):
                before = next((positions[k] for k in range(i-1, -1, -1) if k in positions), None)
                after = next((positions[k] for k in range(i+1, len(reading.regions)) if k in positions), None)
                anchor = before or after
                if anchor:
                    y = (before[1]+after[1])/2 if before and after and before[0] == after[0] and before[1] <= after[1] else anchor[1] + (.0001 if before else -.0001)
                    positions[i] = anchor[0], y
        if not positions or len({c for c, _ in positions.values()}) != len(starts):
            continue
        hints = record.setdefault('_common_layout_regions', {})
        order = sorted(positions, key=lambda i: (*positions[i], i))
        first = min(positions)
        prefix, postfix = [], []
        for i, region in enumerate(reading.regions):
            if i not in positions:
                (prefix if i < first else postfix).append(region)
            if region.kind in TEXT_KINDS:
                for paragraph in region.paragraphs:
                    paragraph.continues_from_previous = False
        title_parts = [r for r in prefix if r.kind in TEXT_KINDS and len(r.paragraphs) == 1
                       and re.search(r'索引|색인|附[錄录]\s*[A-Z]$', paragraph_text(r.paragraphs[0]).strip())]
        has_title = bool(title_parts and any(re.search(r'索引|색인', paragraph_text(r.paragraphs[0]))
                                            for r in title_parts))
        if has_title:
            for region in title_parts:
                hints.setdefault(region.id, {}).update(kind='section', level=2, alignment='CENTER',
                    group=f'source-index-title-{number}', source_index_title=True)
        for i in order:
            region = reading.regions[i]
            col, _ = positions[i]
            row = rows[matched[i]] if i in matched else None
            proven_leaders = {r['leader'] for r in rows if r['column'] == col}
            leader = (row or {}).get('leader') or ('NONE' if proven_leaders == {'NONE'} else 'DASH')
            hints.setdefault(region.id, {}).update(source_index=dict(page=number, column=col, is_entry=i in pairs,
                columns=len(starts), gap_fraction=rows[0]['gap_fraction'], leader=leader,
                has_title=has_title, allow_plain=allow_plain))
        reading.regions = prefix + [reading.regions[i] for i in order] + postfix
        record['merged'] = reading.model_dump()
        record['_source_index_page'] = True


def apply_index_columns(doc, index_rows):
    from .collate.hwpx_edit import HP
    from .source_lists import _columns, _prior_columns, _restore_columns, measured_layout

    count = 0
    for section in doc.sections:
        root = section.element
        pp = root.find('.//'+HP+'pagePr')
        if pp is None or (margin := pp.find(HP+'margin')) is None:
            continue
        body = int(pp.get('width'))-sum(int(margin.get(k,'0')) for k in ('left','right','gutter'))
        paragraphs = list(root.findall(HP+'p'))
        prior = None
        previous = None
        for i, p in enumerate(paragraphs):
            row = index_rows.get(p)
            if row is None:
                if previous is not None:
                    _restore_columns(p, prior)
                    section.mark_dirty()
                previous = None
                continue
            if previous is None:
                prior = _prior_columns(paragraphs, i)
            if previous is None or row['page'] != previous['page']:
                layout = measured_layout([row], body)
                if layout is not None:
                    columns, gap, _ = layout
                    _columns(p, columns, gap)
                    if not row['has_title']:
                        p.set('pageBreak', '1')
                    section.mark_dirty()
                    count += 1
            elif row['column'] != previous['column']:
                p.set('columnBreak', '1')
                section.mark_dirty()
            previous = row
    return count
