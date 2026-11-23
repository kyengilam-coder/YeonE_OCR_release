"""Identify printed herb headings before OCR lines are joined into paragraphs."""
from collections import Counter, defaultdict
from difflib import SequenceMatcher
import re
from statistics import median

from .citation_layout import heading_key
from .schema import paragraph_text
from .toc_tabs import list_entry

_HAN = r'\u3400-\u9fff\uf900-\ufaff'
_NAME = re.compile(rf'[{_HAN}]{{2,12}}')
_SECTION_LABELS = {'總論', '總說', '通論', '概論', '緒論'}
_FIELD = re.compile(r'^(?:[\[［〔【]\s*(?:[氣气]味|主治|修治)\s*[\]］〕】]|一名|又名)')


def _herb_name_shape(key):
    return key not in _SECTION_LABELS and _NAME.fullmatch(key)


def catalogue_names(pdf, records, contents_pages):
    """Index names by the printed folio in the source contents."""
    from .contents_layout import cell_spans
    from .schema import PageReading

    names = defaultdict(set)

    def add(text):
        match = list_entry(text)
        if match:
            name = heading_key(re.split(r'[（(]', text[:match.start()], maxsplit=1)[0])
            if _herb_name_shape(name):
                folio = int(re.search(r'\d+', match['page']).group())
                names[folio].add(name)

    for number in contents_pages:
        if 1 <= number <= len(pdf):
            for line in pdf[number - 1].get_text().splitlines():
                add(line.strip())
    for record in records:
        if not record.get('_source_contents_page'):
            continue
        reading = PageReading.model_validate(record['merged'])
        hints = record.get('_common_layout_regions', {})
        for region in reading.regions:
            layout = hints.get(region.id, {}).get('source_contents', {})
            if layout.get('mode') not in ('outline', 'grid'):
                continue
            line = region.title_text or ' '.join(paragraph_text(p) for p in region.paragraphs)
            add(line.strip())
            if layout['mode'] == 'grid':
                for start, end in cell_spans(line):
                    cell = line[start:end]
                    folio = re.search(r'\d+$', cell)
                    key = heading_key(cell[:folio.start()])
                    if _herb_name_shape(key):
                        names[int(folio[0])].add(key)
    return names


def source_line(text, spans, row):
    """Ignore a detached OCR comma when measuring a short title."""
    if len(spans) > 1 and spans[-1]['text'].strip() in (',', '，'):
        name = text[:-1].strip()
        if len(name) <= 18 and re.fullmatch(rf'[{_HAN}\s]+', name):
            x0, y0, x1, y1 = zip(*(span['bbox'] for span in spans[:-1]))
            return name, (min(x0), min(y0), max(x1), max(y1)), row[1]
    return text, *row


def _one_substitution(a, b):
    return len(a) == len(b) >= 3 and sum(x != y for x, y in zip(a, b)) == 1


def _same_following_text(ocr, printed):
    a, b = heading_key(ocr[:32]), heading_key(printed[:32])
    length = min(len(a), len(b), 16)
    return length >= 5 and SequenceMatcher(None, a[:length], b[:length], autojunk=False).ratio() >= .70


def _stroke_width(ink, x, y, box):
    x0, y0, x1, y1 = box
    area = ink[max(0, round(3*y0)-y-3):round(3*y1)-y+3,
               max(0, round(3*x0)-x-3):round(3*x1)-x+3]
    if not area.size:
        return 0
    perimeter = ((area[:, 1:] != area[:, :-1]).sum() + (area[1:, :] != area[:-1, :]).sum()
                 + area[:, 0].sum() + area[:, -1].sum() + area[0, :].sum() + area[-1, :].sum())
    return 2 * float(area.sum()) / max(1, perimeter)


def source_entry_rows(page, regions, hints, printed, names, left, body_height, prose):
    """Return unambiguous herb boundaries supported by physical source rows.

    OCR supplies every output character. The PDF text and pixels supply only
    the location of a short heading and its distinction from the next line.
    """
    width, height = page.rect.width, page.rect.height
    rows = sorted((row for row in printed if height*.09 <= row[1][1] < height*.90
                   and left-width*.035 <= row[1][0] <= left+width*.12),
                  key=lambda row: (row[1][1], row[1][0]))
    # Ignore narrow punctuation fragments when looking for the adjacent line.
    rows = [row for row in rows if row[1][2]-row[1][0] >= body_height*.8]
    ocr = [(r.id, line) for r in regions if r.kind == '본문' and len(r.paragraphs) == 1
           if (line := paragraph_text(r.paragraphs[0]).strip())]
    matches = []
    for j, (text, box, size) in enumerate(rows[:-1]):
        if (box[2]-box[0] > width*.24 or not .62*body_height <= box[3]-box[1] <= 1.9*body_height
                or not re.fullmatch(rf'[{_HAN}\s]+', text) or not _herb_name_shape(key := heading_key(text))):
            continue
        following, below, _ = rows[j+1]
        if not 0 < below[1]-box[1] <= 2.5*body_height:
            continue
        if j and box[1]-rows[j-1][1][3] < body_height*.70:
            continue
        prefix = re.compile(rf'(?:[{_HAN}][ \t\u3000]*){{{len(key)}}}')
        for i, (region_id, line) in enumerate(ocr):
            hint = hints.get(region_id, {})
            if hint.get('kind') not in (None, 'body') or hint.get('evidence') == 'image-reviewed-layout':
                continue
            match = prefix.match(line)
            if not match:
                continue
            name = heading_key(match[0])
            if not _herb_name_shape(name):
                continue
            exact = name == key
            if not exact and not _one_substitution(name, key):
                continue
            tail = line[match.end():]
            next_text = tail or (ocr[i+1][1] if i+1 < len(ocr) else '')
            listed = name in names
            anchored = _same_following_text(next_text, following)
            if (tail or not exact) and not anchored:
                continue
            if not listed and not (anchored and (any(_FIELD.match(s) for _, s in ocr[i+1:i+6])
                                                  or _FIELD.match(tail)
                                                  or any(_one_substitution(name, n) for n in names))):
                continue
            evidence = 'source-contents-and-heading-row' if listed and exact else 'source-ink-heading'
            matches.append((region_id, box, size, len(match[0].rstrip()) if tail else 0, evidence))
    region_counts = Counter(row[0] for row in matches)
    box_counts = Counter(row[1] for row in matches)
    result = {}
    raster = None
    for region, box, size, cut, evidence in matches:
        if region_counts[region] != 1 or box_counts[box] != 1:
            continue
        if evidence == 'source-ink-heading':
            if raster is None:
                import numpy as np
                import pymupdf
                pix = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), colorspace=pymupdf.csGRAY, alpha=False)
                ink = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, :pix.width] < 145
                sample = prose[::max(1, len(prose)//4)][:4]
                baseline = median(_stroke_width(ink, pix.x, pix.y, row[0]) for row in sample)
                raster = ink, pix.x, pix.y, baseline
            ink, x, y, baseline = raster
            if baseline <= 0 or _stroke_width(ink, x, y, box) < 1.30*baseline:
                continue
        result[region] = (box, size, cut, evidence)
    return result
