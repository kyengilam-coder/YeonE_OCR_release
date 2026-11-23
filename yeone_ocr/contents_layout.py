"""Preserve horizontal contents rows using stored OCR and source-page positions."""
from collections import Counter
from dataclasses import replace
from statistics import median
import re
from unicodedata import east_asian_width

from .schema import PageReading, TEXT_KINDS, TITLE_KINDS, paragraph_text
from .source_lists import _pdf_lines, separator_shape, visible_gap_shape
from .toc_tabs import ENTRY, list_entry
from .collate.hwpx_edit import HP

CONTENTS = re.compile(r'^(?:tableofcontents|contents|목차|차례|목록|총목|[總总]目|目[次錄录])$', re.I)
CELL = re.compile(r'[^\d\s][^\d\n]*?\d{1,4}(?!\d)')
SENTENCE = re.compile(r'[。！？!?；;：:〔〕\n]')
VOLUME = re.compile(r'卷\s*第|^第.{1,5}卷|^제.{1,5}권')


def bookmark_contents(doc):
    bookmarks = doc.get_toc()
    pages = set()
    for index, (level, title, start) in enumerate(bookmarks):
        if start < 1 or not CONTENTS.fullmatch(re.sub(r'\s', '', title)):
            continue
        end = next((n for depth, _, n in bookmarks[index + 1:] if depth <= level and n > start), len(doc) + 1)
        pages.update(range(start, min(end, len(doc) + 1)))
    return pages


def cell_spans(text):
    """Recognize short label/folio cells, including OCR with no intervening space."""
    matches = list(CELL.finditer(text))
    end = 0
    for match in matches:
        cell = match[0]
        if (text[end:match.start()].strip() or len(cell) > 30 or SENTENCE.search(cell)
                or not cell[0].isalpha()):
            return []
        end = match.end()
    return [m.span() for m in matches] if not text[end:].strip() else []


def _physical_rows(items):
    rows = []
    for item in sorted(items, key=lambda row: row[1][0]):
        box = item[1]
        mid = (box[0] + box[2]) / 2
        if rows and abs(mid - rows[-1][0]) <= .010:
            rows[-1][1].append(item)
        else:
            rows.append((mid, [item]))
    return [sorted(items, key=lambda row: row[1][1]) for _, items in rows]


def _column_starts(rows):
    candidates = []
    for row in rows:
        starts = []
        for _, box in row:
            x = box[1]
            if not starts or x - starts[-1] >= .15:
                starts.append(x)
        if 2 <= len(starts) <= 4:
            candidates.append(starts)
    counts = Counter(map(len, candidates))
    if not counts:
        return ()
    count, occurrences = counts.most_common(1)[0]
    if occurrences < 3:
        return ()
    rows = [row for row in candidates if len(row) == count]
    typical = tuple(median(column) for column in zip(*rows))
    if sum(all(abs(a - b) <= .06 for a, b in zip(row, typical)) for row in rows) < 3:
        return ()
    return typical


def _outline_match(text):
    match = list_entry(text)
    if match and match.start() and any(c.isalpha() for c in text[:match.start()]):
        return match
    return None


def _classify_rows(items, page, suggested):
    named = any(re.search(r'[總总]目$|目[錄录次]$|목차$|차례$|\bcontents$', text, re.I)
                for text, _ in list(items.values())[:4] if len(text) < 40)
    if not (suggested or named):
        return []
    explicit = sum(bool(ENTRY.search(text)) for text, _ in items.values())
    cell_counts = {i: len(cell_spans(text)) for i, (text, _) in items.items()}
    packed = Counter(cell_counts.values())
    if explicit < 3 and packed[1] < 8 and not any(count >= 2 and n >= 3 for count, n in packed.items()):
        return []
    located = [(i, box) for i, (_, box) in items.items() if box[2] - box[0] < .065]
    rows = _physical_rows(located) if len(located) == len(items) else []
    if rows:
        grids = [[(i, box) for i, box in row if cell_counts[i] == 1
                  and not ENTRY.search(items[i][0]) and not VOLUME.search(items[i][0])] for row in rows]
        starts = _column_starts(grids)
        groups = [[i for i, _ in row] for row in rows]
    else:
        groups = [[i] for i in items]
        starts = ()
    packed_counts = [(count, n) for count, n in packed.items() if count >= 2 and n >= 3]
    if not starts and packed_counts:
        count = max(packed_counts, key=lambda pair: pair[1])[0]
        lines, _ = _pdf_lines(page)
        fragments = [(i, (y, x, bottom, right)) for i, (text, x, y, right, bottom) in enumerate(lines)
                     if right - x < .25 and sum(c.isalpha() for c in text) >= 2]
        measured = _column_starts(_physical_rows(fragments))
        starts = measured if len(measured) == count else tuple(.12 + i * .76 / count for i in range(count))
    # Include the right-hand gutter in the available width. The first cell
    # starts at the list inset, with subsequent cells at physical offsets.
    stops = tuple(round((x - starts[0]) / max(.90 - starts[0], starts[-1] - starts[0] + .18), 3)
                  for x in starts[1:])
    layouts = []
    first_entry = False
    total = any(re.sub(r'\s', '', text) in ('總目', '总目', '총목') for text, _ in items.values())
    top = [min(items[i][1][0] for i in group) for group in groups] if rows else []
    pitch = median(b - a for a, b in zip(top, top[1:])) if len(top) > 1 else 0
    for group in groups:
        text = ' '.join(items[i][0] for i in group)
        cells = cell_spans(text)
        positioned = [min(range(len(starts)), key=lambda c: abs(items[i][1][1] - starts[c]))
                      for i in group] if rows and starts else []
        grid = bool(stops and not VOLUME.search(text) and not ENTRY.search(text) and
                    ((positioned and (len(group) >= 2 or cells) and
                      len(set(positioned)) == len(group) and
                      all(abs(items[i][1][1] - starts[c]) <= .065 and items[i][1][3] - items[i][1][1] < .28
                          for i, c in zip(group, positioned)))
                     or (not rows and 2 <= len(cells) <= len(starts))))
        match = _outline_match(text)
        if grid:
            mode, leader = 'grid', 'NONE'
        elif match and (ENTRY.search(text) or VOLUME.search(text) or (not stops and len(cells) == 1)):
            mode = 'outline'
            leader = separator_shape(match['leader']) or 'NONE'
            if leader == 'NONE' and rows and len(group) > 1 and items[group[-1]][0].isdigit():
                _, (_, _, _, left) = items[group[-2]]
                _, (folio_top, right, bottom, _) = items[group[-1]]
                leader = visible_gap_shape(page, (left, folio_top - .01, right, bottom)) or 'NONE'
        else:
            mode = 'title' if not first_entry and len(text) <= 45 else 'plain'
            leader = 'NONE'
        first_entry |= mode in ('grid', 'outline')
        preceding = layouts[-1] if layouts else None
        before = 0
        if preceding:
            if top:
                n = len(layouts)
                before = round(max(0, min(2400, (top[n] - top[n - 1] - pitch - .008) * 40000)))
            elif group[0] > preceding[0][-1] + 1:
                before = 850
            if preceding[1]['mode'] == 'title' and mode == 'outline':
                before = max(before, 1500)
        layouts.append((group, dict(mode=mode, stops=stops, leader=leader, center=total,
                                    columns=positioned if grid else [], before=before)))
    return layouts if first_entry else []


def prepare_contents_pages(records, pdf, page_types=None):
    """Tag an output view. Every confirmed list page retains its OCR row breaks."""
    page_types = page_types or {}
    from .common_layout import is_front_heading
    candidates = bookmark_contents(pdf) | set(page_types.get('목차', ())) | set(page_types.get('차례', ()))
    for record in records:
        number = record['pdf_page']
        reading = PageReading.model_validate(record['merged'])
        items = {}
        for i, region in enumerate(reading.regions):
            if region.kind in TEXT_KINDS and len(region.paragraphs) == 1:
                text = paragraph_text(region.paragraphs[0]).strip()
            elif region.kind in TITLE_KINDS:
                text = region.title_text.strip()
            else:
                continue
            if text and not text.isdecimal():
                items[i] = (text, tuple(v / 1000 for v in region.bbox))
            elif text and .08 < region.bbox[0] / 1000 < .96 and region.bbox[2] - region.bbox[0] < 65:
                items[i] = (text, tuple(v / 1000 for v in region.bbox))
        if not items:
            continue
        layouts = _classify_rows(items, pdf[number - 1], number in candidates)
        if not layouts:
            continue
        hints = record.setdefault('_common_layout_regions', {})
        for row, (group, layout) in enumerate(layouts):
            for slot, i in enumerate(group):
                region = reading.regions[i]
                hints[region.id] = {'source_contents': dict(layout, row=(number, row),
                                                          start=row == 0, slot=slot)}
                if row == 0 and layout['mode'] == 'title' and is_front_heading(items[i][0]):
                    hints[region.id].update(kind='section', level=2, front_section=True)
        for region in reading.regions:
            for para in region.paragraphs:
                para.continues_from_previous = False
        # Output follows physical row order even when extracted OCR is column-major.
        order = {i: (row, col) for row, (group, _) in enumerate(layouts) for col, i in enumerate(group)}
        slots = sorted(order)
        original = list(reading.regions)
        for slot, index in zip(slots, sorted(order, key=order.get)):
            reading.regions[slot] = original[index]
        record['merged'] = reading.model_dump()
        record['_source_contents_page'] = True


def shape_contents_blocks(blocks):
    """Join a physical row while retaining original run offsets and anchors."""
    shaped = []
    for block in blocks:
        layout = block.layout.get('source_contents')
        if layout is None:
            shaped.append(block)
            continue
        grid = layout['mode'] == 'grid'
        columns = layout['columns']
        previous = shaped[-1].layout.get('source_contents') if shaped else None
        if previous and previous['row'] == layout['row']:
            left = shaped.pop()
            left.extend('\t' * (columns[layout['slot']] - columns[previous['slot']]) if grid else ' ')
            block = type(block).joined(left, block)
        elif grid and columns and columns[0]:
            block = type(block).joined(type(block)(type='para', text='\t' * columns[0]), block)
        shaped.append(block)
    previous_page = None
    for i, block in enumerate(shaped):
        layout = block.layout.get('source_contents')
        if layout is None:
            if previous_page is not None and block.page > previous_page and block.type in ('para', 'heading'):
                shaped[i] = replace(block, layout=dict(block.layout, page_break_before=True))
                previous_page = None
            continue
        previous_page = None if block.layout.get('continue_contents') else block.page
        mode = layout['mode']
        if mode == 'grid' and not layout['columns']:
            cells = cell_spans(block.text)
            gaps = [(a, b) for (_, a), (b, _) in zip(cells, cells[1:])]
        elif mode == 'outline' and (match := _outline_match(block.text)):
            gaps = [match.span('leader')]
        else:
            gaps = []
        for a, b in reversed(gaps):
            text = block.text
            block.remap(text[:a] + '\t' + text[b:], [*range(a), a, *range(b, len(text))])
        shaped[i] = replace(block, type='heading' if mode == 'title' else 'para',
                            role='section' if mode == 'title' else 'body', level=2)
    return shaped


def paragraph_format(doc, base, text, layout, width, cache, source_shape=None):
    """Use real Hancom tab stops, with font size fitted to the available columns."""
    mode = layout['mode']
    source_shape = source_shape or {}
    inset = round(width * .045) if mode == 'grid' else 0
    available = width - inset
    if mode == 'grid':
        positions = tuple(round(available * stop) for stop in layout['stops'])
        edges = (0, *positions, available)
        size = min(source_shape.get('size', 10), *(max(5, (right - left - 380) // (100 * max(1, sum(
            1 if east_asian_width(c) in 'WF' else .55 for c in cell))))
            for left, right, cell in zip(edges, edges[1:], text.split('\t'))))
        align, tab_type, after = 'LEFT', 'LEFT', 50
    elif mode == 'outline':
        positions = (available,)
        align, tab_type, size, after = 'LEFT', 'RIGHT', 11, 130
        size = source_shape.get('size', size)
    else:
        positions = ()
        align = 'CENTER' if mode == 'title' and layout['center'] else 'LEFT'
        size = (12 if text.startswith(('[', '［', '〔')) else 16) if mode == 'title' else 10
        size = source_shape.get('size', size)
        tab_type, after = 'LEFT', 650 if mode == 'title' else 100
        if mode == 'title':
            align = source_shape.get('alignment', align)
            inset = min(source_shape.get('left', inset), width // 2)
    before = source_shape.get('before', layout['before']) if mode == 'title' else layout['before']
    key = (base, mode, inset, positions, align, layout['leader'], before)
    if key not in cache:
        header = doc.oxml.headers[0]
        tabs = header.ensure_tab_definition(tab_stops=[dict(pos=pos, type=tab_type, leader=layout['leader'])
                                                       for pos in positions], auto_tab_right=tab_type == 'RIGHT')
        cache[key] = header.ensure_paragraph_format(
            base_para_pr_id=base, alignment=align, line_spacing_percent=133,
            heading={'type': 'NONE'}, tab_pr_id_ref=tabs,
            margins=dict(intent=0, left=inset, right=0, prev=before, next=after),
            break_setting={'keep_lines': True, 'keep_with_next': mode == 'title', 'page_break_before': False})
    return cache[key], size


def add_contents_run(paragraph, text, char_id, layout):
    parts = text.split('\t')
    run = paragraph.add_run(parts[0], char_pr_id_ref=char_id)
    if len(parts) > 1:
        holder = run.element.find(HP + 't')
        for part in parts[1:]:
            tab = holder.makeelement(HP + 'tab', {'width': '0',
                'type': '2' if layout['mode'] == 'outline' else '0',
                'leader': '3' if layout['leader'] == 'DASH' else '0'})
            tab.tail = part
            holder.append(tab)
    return run
