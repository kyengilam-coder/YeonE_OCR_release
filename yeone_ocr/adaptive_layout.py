"""Conservative document structure inferred from the current PDF and OCR."""
from collections import Counter, defaultdict
from itertools import combinations
import re
from statistics import median

from .citation_layout import heading_key
from .index_layout import index_entry
from .rule_layout_scan import _heading_type
from .schema import PageReading, Region, TableRow, TEXT_KINDS, paragraph_text
from .toc_tabs import ENTRY


def _line(region):
    if region.kind not in TEXT_KINDS or len(region.paragraphs) != 1:
        return ""
    return paragraph_text(region.paragraphs[0]).strip()


def _column_gutters(rows, *, index=False):
    """Find shared blank vertical ranges. Require several lines in every column."""
    boxes = [r.bbox for r in rows if _line(r) and r.bbox[2]-r.bbox[0] <= 100]
    medium = [b for b in boxes if (35 if index else 105) <= b[3]-b[1] <= 515]
    if len(medium) < 9:
        return ()
    areas = []
    for x in range(230, 781, 10):
        count = sum(b[1]+8 < x < b[3]-8 for b in boxes)
        if count <= max(1, len(boxes)//16):
            areas.append(x)
    choices = [(x,) for x in areas] + [(x, y) for x, y in combinations(areas, 2) if y-x >= 180]
    best_score, best = None, ()
    for cuts in choices:
        bounds = (0, *cuts, 1000)
        columns = [[b for b in medium if b[1] >= left-8 and b[3] <= right+8]
                   for left, right in zip(bounds, bounds[1:])]
        if min(map(len, columns)) < (5 if index else 4):
            continue
        # A split must be supported by rows at overlapping heights, which
        # separates true columns from successive, differently indented blocks.
        if min(max(b[2] for b in col) for col in columns) - max(min(b[0] for b in col) for col in columns) < 140:
            continue
        if not index and any(median(b[3]-b[1] for b in col) < .38*(right-left)
                             for col, left, right in zip(columns, bounds, bounds[1:])):
            continue
        clearance = min(min(abs(x-edge) for b in medium for edge in (b[1], b[3])) for x in cuts)
        score = (len(cuts) if index else -len(cuts), sum(map(len, columns)), clearance, min(map(len, columns)))
        if best_score is None or score > best_score:
            best_score, best = score, cuts
    return best


def _read_columns(regions, cuts):
    from bisect import bisect_right

    bands, output = [], []
    for region in sorted(regions, key=lambda r: (r.bbox[0], r.bbox[1])):
        if region.kind == '단순표' or any(region.bbox[1]+8 < x < region.bbox[3]-8 for x in cuts):
            output.extend(sorted(bands, key=lambda r: (bisect_right(cuts, (r.bbox[1]+r.bbox[3])/2), r.bbox[0], r.bbox[1])))
            bands.clear()
            output.append(region)
        else:
            bands.append(region)
    output.extend(sorted(bands, key=lambda r: (bisect_right(cuts, (r.bbox[1]+r.bbox[3])/2), r.bbox[0], r.bbox[1])))
    return output


def _scan_grid(page):
    import numpy as np
    import pymupdf
    from .source_layout_hwpx import _printed_rules

    pix = page.get_pixmap(dpi=105, colorspace=pymupdf.csGRAY, alpha=False)
    gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    rules = _printed_rules((gray < 155).astype(np.uint8), page.rect.width, page.rect.height)
    vertical = [rule for rule in rules if rule[0] == rule[2]]
    horizontal = [rule for rule in rules if rule[1] == rule[3]]
    if min(len(vertical), len(horizontal)) < 3:
        return []
    neighbors = defaultdict(set)
    for v, (x, top, _, bottom) in enumerate(vertical):
        for h, (left, y, right, _) in enumerate(horizontal, len(vertical)):
            if left-3 <= x <= right+3 and top-3 <= y <= bottom+3:
                neighbors[v].add(h)
                neighbors[h].add(v)
    remaining, grids = set(neighbors), []
    while remaining:
        pending = [remaining.pop()]
        component = set(pending)
        while pending:
            adjacent = neighbors[pending.pop()] & remaining
            pending.extend(adjacent)
            remaining.difference_update(adjacent)
            component.update(adjacent)
        cols = [i for i in component if i < len(vertical)]
        rows = component.difference(cols)
        if min(len(cols), len(rows)) < 3 or any(not rows <= neighbors[i] for i in cols):
            continue
        xs = sorted(vertical[i][0] for i in cols)
        ys = sorted(horizontal[i-len(vertical)][1] for i in rows)
        if any(b-a < 6 for axis in (xs, ys) for a,b in zip(axis, axis[1:])):
            continue
        cells = [[(a,c,b,d) for a,b in zip(xs,xs[1:])] for c,d in zip(ys,ys[1:])]
        grids.append(((xs[0],ys[0],xs[-1],ys[-1]), cells))
    return grids


def _table_grids(page, allow_scan):
    drawings = page.get_drawings()
    horizontal = vertical = 0
    for drawing in drawings:
        for item in drawing.get('items', ()):
            if item[0] == 're':
                horizontal += 2
                vertical += 2
            elif item[0] == 'l':
                start, end = item[1:]
                horizontal += abs(start.y-end.y) < 1 and abs(start.x-end.x) > 15
                vertical += abs(start.x-end.x) < 1 and abs(start.y-end.y) > 15
    if min(horizontal, vertical) >= 3:
        try:
            tables = page.find_tables(strategy="lines_strict", paths=drawings).tables
            return [(table.bbox, [row.cells for row in table.rows]) for table in tables]
        except (AttributeError, RuntimeError, ValueError):
            pass
    return _scan_grid(page) if allow_scan and page.get_images() else []


def _infer_tables(page, regions):
    """Use confirmed cell boundaries and the saved OCR text in those cells."""
    import pymupdf

    if page.rotation or any(region.kind in ('단순표', '복잡한표') for region in regions):
        return regions
    width, height = page.rect.width, page.rect.height
    current = list(regions)
    narrow = sum(bool(_line(r)) and r.bbox[3]-r.bbox[1] < 430 for r in regions)
    for bounds, grid_rows in _table_grids(page, allow_scan=narrow >= 4):
        if not grid_rows:
            continue
        height_cells, width_cells = len(grid_rows), len(grid_rows[0])
        if min(height_cells, width_cells) < 2 or height_cells*width_cells > 1000:
            continue
        area = pymupdf.Rect(bounds)
        assigned = defaultdict(list)
        touched = set()
        ambiguous = False
        for index, region in enumerate(current):
            y0, x0, y1, x1 = region.bbox
            rect = pymupdf.Rect(x0*width/1000, y0*height/1000, x1*width/1000, y1*height/1000)
            if not rect or (rect & area).get_area() < .45*rect.get_area():
                continue
            cells = [(r, c) for r, row in enumerate(grid_rows) for c, cell in enumerate(row)
                     if cell and (rect & pymupdf.Rect(cell)).get_area() >= .80*rect.get_area()]
            if len(cells) != 1 or not _line(region):
                ambiguous = True
                break
            assigned[cells[0]].append(region)
            touched.add(index)
        if ambiguous or len(assigned) < max(4, height_cells*width_cells*.5):
            continue
        rows = [TableRow(cells=['\n'.join(_line(r) for r in sorted(assigned[i, j], key=lambda r: (r.bbox[0], r.bbox[1])))
                               for j in range(width_cells)]) for i in range(height_cells)]
        bbox = [round(value*1000/scale) for value, scale in zip((area.y0, area.x0, area.y1, area.x1), (height, width, height, width))]
        bbox = [max(0, min(1000, value)) for value in bbox]
        if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            continue
        grid = Region(id=current[min(touched)].id+'-table', kind='단순표', bbox=bbox, reading_direction='가로', table_rows=rows)
        current = [grid if i == min(touched) else r for i, r in enumerate(current) if i == min(touched) or i not in touched]
    return current


def _page_kind(lines, titles, previous):
    heading = next((kind for title in (*titles, *lines[:5]) if (kind := _heading_type(title))), None)
    entries = sum(bool(index_entry(text)) for text in lines)
    dotted = sum(bool(ENTRY.search(text)) for text in lines)
    prose = sum(len(text) >= 32 and not ENTRY.search(text) for text in lines)
    if (heading == '색인' or previous == '색인' or entries >= .65*len(lines)) and entries >= 6:
        return '색인'
    if dotted >= 4 and (heading in ('목차', '차례') or previous in ('목차','차례') or dotted >= .65*len(lines)):
        return heading if heading in ('목차', '차례') else (previous if previous in ('목차','차례') else '목차')
    if heading in ('목차','차례','문헌 목록') and prose < 4:
        return heading
    return '본문' if lines else '기타'


def analyze_records(records, doc, *, stop=None):
    """Modify only the transient output records; return evidence for the writer."""
    from ab_ocr.ocr import OCRCancelled

    bookmarks, titles_by_page = defaultdict(dict), defaultdict(list)
    for level, name, number in doc.get_toc():
        bookmarks[number][heading_key(name)] = max(1, min(3, level))
        titles_by_page[number].append(name)
    types = defaultdict(list)
    previous, prior_page = None, None
    readings = [PageReading.model_validate(record['merged']) for record in records]
    repeated = Counter()
    for reading in readings:
        repeated.update({heading_key(_line(r)) for r in reading.regions if _line(r)})

    for record, reading in zip(records, readings):
        if stop and stop():
            raise OCRCancelled('문서 구조 분석을 중지했습니다.')
        number = record['pdf_page']
        regions = list(reading.regions)
        lines = [_line(r) for r in regions if _line(r)]
        titles = bookmarks[number]
        kind = _page_kind(lines, titles_by_page[number], previous if prior_page == number-1 else None)
        previous, prior_page = kind, number
        types[kind].append(number)
        body = [r for r in regions if _line(r) and 1 <= r.bbox[2]-r.bbox[0] <= 100]
        heights = [r.bbox[2]-r.bbox[0] for r in body if len(_line(r)) >= 12]
        typical = median(heights) if len(heights) >= 5 else 0
        horizontal = record.get('mode') != 'vertical' and not any('세로' in r.reading_direction for r in body)
        cuts = ()
        if kind == '본문' and horizontal:
            for region in body:
                text = _line(region)
                key = heading_key(text)
                top, left, bottom, right = region.bbox
                size = bottom-top
                separated = all(other is region or other.bbox[2] < top-typical*.18 or other.bbox[0] > bottom+typical*.18
                                for other in body)
                large_heading = (typical and size >= typical*1.55 and 100 <= top <= 790 and
                                 (left <= 230 or abs((left+right)/2-500) < 70) and len(text) <= 60 and separated and
                                 repeated[key] == 1 and not re.search(r'[。!?？！,，;；()（）]|(?<!\d)\.\s*$', text))
                if key and repeated[key] == 1 and (key in titles or large_heading):
                    region.kind, region.title_text = '제목', text
                    region.title_level = titles.get(key, 2)
                    region.paragraphs = []
            notes = [r for r in body if r.bbox[0] >= 675 and r.bbox[2]-r.bbox[0] <= typical*.83]
            markers = [r for r in notes if re.match(r'^[①-⑳⁰-⁹]|^\[\d{1,3}\]|^\d{1,2}[)）]', _line(r))]
            if len(notes) >= 2 and markers:
                start = min(r.bbox[0] for r in markers)
                types['각주'].append(number)
                for r in notes:
                    if r.bbox[0] >= start:
                        r.kind = '소주'
            regions = _infer_tables(doc[number-1], regions)
            cuts = _column_gutters(regions)
            if cuts:
                regions = _read_columns(regions, cuts)
        reading.regions = regions
        record['merged'] = reading.model_dump()
        record['_automatic_layout'] = {'type': kind, 'column_count': len(cuts)+1,
                                       'headings': sum(r.kind == '제목' for r in regions),
                                       'notes': sum(r.kind == '소주' for r in regions),
                                       'tables': sum(r.kind in ('단순표','복잡한표') for r in regions)}
    return dict(types)


def apply_index_geometry(records, page_types):
    """Fall back to OCR coordinates for confirmed index pages without a usable native text layer."""
    from bisect import bisect_right

    numbers = set(page_types.get('색인', ()))
    for record in records:
        if record.get('_source_index_page'):
            layouts = [item['source_index']['columns'] for item in record.get('_common_layout_regions', {}).values()
                       if 'source_index' in item]
            if layouts and '_automatic_layout' in record:
                record['_automatic_layout']['column_count'] = max(layouts)
            continue
        if record['pdf_page'] not in numbers:
            continue
        reading = PageReading.model_validate(record['merged'])
        entries = [r for r in reading.regions if index_entry(_line(r)) and r.bbox[0] >= 100]
        cuts = _column_gutters(entries, index=True)
        if not cuts:
            continue
        first_y = min(r.bbox[0] for r in entries)
        header, columns = [], defaultdict(list)
        for region in reading.regions:
            y, left, _, right = region.bbox
            if y < first_y-8:
                header.append(region)
            elif not any(left+8 < x < right-8 for x in cuts):
                columns[bisect_right(cuts, (left+right)/2)].append(region)
            else:
                break
        else:
            if len(columns) != len(cuts)+1:
                continue
            rows = [r for col in sorted(columns) for r in sorted(columns[col], key=lambda item: (item.bbox[0], item.bbox[1]))]
            hints = record.setdefault('_common_layout_regions', {})
            count = len(columns)
            title = next((r for r in header if re.search(r'索[引隐隱]|색인|\bindex\b', _line(r), re.I)), None)
            if title is not None:
                hints.setdefault(title.id, {}).update(kind='section', level=2, alignment='CENTER', source_index_title=True)
            for col, regions in columns.items():
                for region in regions:
                    for paragraph in region.paragraphs:
                        paragraph.continues_from_previous = False
                    hints.setdefault(region.id, {}).update(source_index=dict(page=record['pdf_page'], column=col,
                        is_entry=bool(index_entry(_line(region))), columns=count, gap_fraction=.045,
                        leader='NONE', has_title=title is not None, allow_plain=False))
            reading.regions = header+rows
            record['merged'] = reading.model_dump()
            record['_source_index_page'] = True
            if '_automatic_layout' in record:
                record['_automatic_layout']['column_count'] = count
