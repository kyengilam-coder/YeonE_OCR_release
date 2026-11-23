"""Measure typography from the matching line in the source PDF's text layer."""
from collections import defaultdict
from difflib import SequenceMatcher
import re
from statistics import median

from .citation_layout import heading_key
from .schema import PageReading, TEXT_KINDS, TITLE_KINDS, paragraph_text
from .hwpx_output_rules import HwpxOutputRules

_IMPRINT = re.compile(r'ISBN|CIP|版权|版權|판권|原著|资助出版|資助出版', re.I)
_NUMBERED_SECTION = re.compile(r'(?:[一二三四五六七八九十]+[、．.]|[0-9]{1,2}[．.])\s*[^。？！,，.;；]{2,42}')
_DEPARTMENT = re.compile(r'[\u3400-\u9fff]{1,7}部')
_ENTRY = re.compile(r'[\u3400-\u9fff]{2,8}')
_CREDIT = re.compile(r'[^。；;：:]{3,24}(?:原著|先生著|校訂|校订|校注|編校|编校)')
_NOTE = re.compile(r'[〔\[［]\s*\d{1,2}\s*[〕\]］]')
_FOLIO = re.compile(r'[•·・]\s*(\d{1,4})\s*[•·・]')
_ATTRIBUTION = re.compile(r'[\u3400-\u9fff]{2,8}(?:曰|云|謂)\s*[:：]')


def _match_nearby(key, matches, accepts):
    """Use near-matching PDF text for geometry only, never to replace OCR."""
    candidates = [(SequenceMatcher(None, key, printed, autojunk=False).ratio(), row)
                  for printed, rows in matches.items()
                  if abs(len(printed)-len(key)) <= max(3, len(key)//5)
                  for row in rows if accepts(row[0])]
    candidates = sorted(candidates, key=lambda hit: hit[0], reverse=True)
    if candidates and candidates[0][0] >= .72 and (len(candidates) == 1 or candidates[0][0]-candidates[1][0] >= .12):
        return [candidates[0][1]]
    return []


def _front_rows(record, regions, page, lines, matches, hints):
    """Restore measured rows on early title, credit and imprint pages."""
    entries = [(r, (r.title_text or ' '.join(paragraph_text(p) for p in r.paragraphs)).strip())
               for r in regions if r.kind in TEXT_KINDS | TITLE_KINDS]
    entries = [(r, text) for r, text in entries if text]
    if not (record['pdf_page'] <= 16 and len(lines) <= 24 and 1 < len(entries) <= 30
            and any(_IMPRINT.search(text) for _, text in entries)):
        return
    width, height = page.cropbox.width, page.cropbox.height
    if not lines:
        lines = [((x0*width/1000, y0*height/1000, x1*width/1000, y1*height/1000), None)
                 for (region, _) in entries if region.bbox != [0, 0, 1000, 1000]
                 for y0, x0, y1, x1 in (region.bbox,)]
    if len(lines) < 2:
        return
    first_box = min((box for box, _ in lines), key=lambda box: box[1])
    typical = median(box[3] - box[1] for box, _ in lines)
    record['_source_front_page'] = True
    for index, (region, text) in enumerate(entries):
        if region.bbox == [0, 0, 1000, 1000]:
            found = matches.get(heading_key(text), ())
            if not found and index == len(entries) - 1 and len(text) <= 18:
                found = [row for row in lines if row[0][1] > height * .82
                         and row[0][2] - row[0][0] < width * .5]
            if len(found) != 1:
                continue
            box, size = found[0]
        else:
            y0, x0, y1, x1 = (v / 1000 for v in region.bbox)
            box, size = (x0 * width, y0 * height, x1 * width, y1 * height), None
        x0, y0, x1, y1 = box
        title = index == 0 and y1-y0 >= 2 * typical and abs(y0-first_box[1]) <= height*.05 and len(text) <= 24
        if size is None and title:
            size = _printed_title_size(page, box)
        size = size or min(19, max(7, (y1-y0) * .94))
        hint = hints.setdefault(region.id, {})
        if title:
            hint.update(kind='book', level=1, evidence='measured-title-page')
        elif hint.get('evidence') == 'cover-context':
            # Publication credits may have been inferred as the book title.
            hint.update(kind='body', evidence='measured-imprint-row')
        center = abs((x0+x1)/2 - width/2) < width * .045
        hint.update(front_row=(x0, y0, x1, y1), front_top_margin=height*.10,
                    source_typography={'size': size, 'alignment': 'CENTER' if center else 'LEFT',
                                       'left': round(max(0, x0-width*.10)*100) if not center else 0,
                                       'top_fraction': y0/height})


def _printed_title_size(page, box):
    """Estimate a scanned title's point size from the ink within its OCR bounds."""
    import pymupdf

    clip = pymupdf.Rect(box) & page.rect
    if clip.is_empty or clip.height > 85 or clip.width < 5:
        return None
    scale = 2
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, colorspace=pymupdf.csGRAY, alpha=False)
    pixels = pix.samples
    rows = [y for y in range(pix.height)
            if sum(value < 145 for value in pixels[y * pix.stride:y * pix.stride + pix.width]) >= max(2, pix.width * .05)]
    if len(rows) < 5:
        return None
    ink_height = (rows[-1] - rows[0] + 1) / scale
    if .35 * clip.height <= ink_height <= clip.height:
        return round(min(72, ink_height / .84), 1)
    return None


def measure_source_typography(records, pdf, *, infer_titles=False, output_rules=HwpxOutputRules()):
    import pymupdf
    from .common_layout import MAJOR, SECTION

    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    for record in records:
        number = record['pdf_page']
        if not 1 <= number <= len(pdf):
            continue
        page = pdf[number - 1]
        width, height = page.cropbox.width, page.cropbox.height
        if page.rotation or not width or not height:
            continue
        lines, matches, folios = [], defaultdict(list), set()
        for block in page.get_text('dict', flags=flags)['blocks']:
            for line in block.get('lines', []):
                if line.get('wmode') or tuple(line.get('dir', ())) != (1, 0):
                    continue
                spans = [span for span in line['spans'] if span['text'].strip()]
                if not spans:
                    continue
                text = ''.join(span['text'] for span in spans).strip()
                if line['bbox'][1] > height * .90 and (folio := _FOLIO.fullmatch(text)):
                    folios.add(folio[1])
                main = max(spans, key=lambda span: sum(c.isalnum() for c in span['text']))
                size = main['size']
                if not 5 <= size <= 72:
                    continue
                row = (line['bbox'], size if main.get('alpha', 255) else None)
                lines.append(row)
                if len(text) <= 200 and (key := heading_key(text)):
                    matches[key].append(row)
        list_page = record.get('_source_contents_page') or record.get('_source_index_page')
        hints = record.setdefault('_common_layout_regions', {})
        regions = PageReading.model_validate(record['merged']).regions
        if infer_titles and not list_page and number <= 16:
            _front_rows(record, regions, page, lines, matches, hints)
            if not output_rules.source_typography:
                for hint in hints.values():
                    if 'front_row' in hint:
                        hint.pop('front_row')
                        hint.pop('source_typography', None)
        if not lines:
            continue
        prose = [row for row in lines if row[0][2] - row[0][0] > width * .48]
        body_height = None
        if len(prose) >= 4:
            left = median(row[0][0] for row in prose)
            right = median(row[0][2] for row in prose)
            font_sizes = [row[1] for row in prose if row[1] is not None]
            body_size = round(median(font_sizes), 1) if len(font_sizes) >= 4 else None
            body_height = median(row[0][3] - row[0][1] for row in prose)
        else:
            left, right, body_size = width * .10, width * .90, None

        if output_rules.source_typography and infer_titles and not list_page and len(prose) >= 8:
            tops = sorted({round(row[0][1], 1) for row in prose})
            pitches = [b-a for a, b in zip(tops, tops[1:]) if body_height*.9 <= b-a <= body_height*2.1]
            if len(pitches) >= 5 and 7 <= body_height <= 18:
                estimated_size = body_size or round(body_height, 1)
                record['_source_body_spacing'] = round(100 * median(pitches) / estimated_size)
                record['_source_body_width'] = round(right - left, 1)
                body_size = estimated_size
        if output_rules.source_typography and body_size is not None and not list_page:
            record['_source_body_size'] = body_size
        opens_volume = infer_titles and not list_page and any(
            hints.get(region.id, {}).get('kind') == 'volume' for region in regions[:4])
        has_numbered_notes = output_rules.footnote_groups and infer_titles and not list_page and sum(
            bool(_NOTE.match(paragraph_text(p).strip())) for region in regions for p in region.paragraphs
        ) >= 2
        department_seen = False
        for region in regions:
            hint = hints.get(region.id, {})
            if 'front_row' in hint:
                continue
            contents = hint.get('source_contents')
            index = hint.get('source_index')
            title = (region.kind in TITLE_KINDS or hint.get('kind') in
                     ('book', 'volume', 'chapter', 'section', 'subsection', 'entry') or
                     (contents and contents['mode'] == 'title'))
            text = region.title_text if region.kind in TITLE_KINDS else (
                paragraph_text(region.paragraphs[0]) if region.kind in TEXT_KINDS and len(region.paragraphs) == 1 else '')
            major = numbered = possible_title = False
            if not (title or contents or index):
                if list_page or body_height is None or not text:
                    continue
                major = infer_titles and bool(MAJOR.fullmatch(text.strip()))
                numbered = infer_titles and bool(_NUMBERED_SECTION.fullmatch(text.strip()))
                possible_title = major or numbered or (infer_titles and bool(SECTION.fullmatch(text.strip())))
            key = heading_key(text.strip()) if 0 < len(text) <= 200 else ''
            found = matches.get(key, ())
            credit = opens_volume and not department_seen and _CREDIT.fullmatch(text.strip())
            note = has_numbered_notes and _NOTE.match(text.strip())
            if not found and credit:
                found = _match_nearby(key, matches, lambda box: box[0] > width * .50 and box[1] < height * .40)
            if not found and note:
                found = _match_nearby(key, matches, lambda box: box[1] > height * .82)
            if not found and hint.get('front_section') and 1 < len(key) <= 12:
                found = [((a[0], min(a[1], b[1]), b[2], max(a[3], b[3])), size)
                         for split in range(1, len(key))
                         for a, size in matches.get(key[:split], ())
                         for b, _ in matches.get(key[split:], ())
                         if abs(a[1]-b[1]) < 4 and a[2] < b[0]]
            if len(found) > 1 and region.bbox != [0, 0, 1000, 1000]:
                top = region.bbox[0] * height / 1000
                found = [row for row in found if abs(row[0][1] - top) < height * .025]
            if len(found) != 1:
                continue
            box, size = found[0]
            x0, y0, x1, y1 = box
            prior = [line_box[3] for line_box, _ in lines if line_box[3] <= y0 and line_box[0] < right and line_box[2] > left]
            gap = y0 - max(prior) if prior else y0 - height * .08
            center = (x0+x1)/2
            centered = min(abs(center-(left+right)/2), abs(center-width/2)) <= width*.04
            if not (title or contents or index):
                department = (infer_titles and _DEPARTMENT.fullmatch(text.strip()) and centered
                              and x1-x0 < width*.35 and gap >= body_height*.75)
                entry = (department_seen and _ENTRY.fullmatch(text.strip()) and not centered
                         and left-width*.04 <= x0 <= left+width*.10 and gap >= body_height)
                if department or entry:
                    hint = hints.setdefault(region.id, {})
                    hint.update(kind='section' if department else 'entry', level=2 if department else 3,
                                evidence='measured-source-heading')
                    department_seen |= bool(department)
                    title = True
                elif ((output_rules.source_typography and credit and x0 > width*.50 and y0 < height*.40)
                      or (note and y0 > height*.82)):
                    hint = hints.setdefault(region.id, {})
                    hint.update(source_layout_para='credit' if credit else 'footnote',
                                source_typography={'size': round((size or body_size or 10) * (1 if credit else .84), 1),
                                                   'alignment': 'LEFT', 'left': round(max(0,x0-left)*100),
                                                   'indent': 0, 'before': round(max(0,gap)*100)})
                    if note and (folio := re.search(r'[•·・]\s*(\d{1,4})\s*[•·・]\s*$', text)) and folio[1] in folios:
                        hint['printed_folio_suffix'] = folio[0]
                    continue
                elif (possible_title and height * .09 <= y0 <= height * .9
                        and (y1 - y0 >= 1.25 * body_height or numbered and centered and y1-y0 >= 1.10*body_height)
                        and (not numbered or centered)):
                    hint = hints.setdefault(region.id, {})
                    hint.update(kind='volume' if major and '卷' in text else 'chapter' if major else 'section',
                                level=1 if major else 2, evidence='measured-source-heading')
                    title = True
                else:
                    offset = x0 - left
                    if (output_rules.source_typography and region.kind == '본문' and 5 <= offset <= width * .12 and abs(x1 - right) <= width * .04
                            and x1 - x0 > width * .45 and .8 <= (y1 - y0) / body_height <= 1.2):
                        hints.setdefault(region.id, {})['source_typography'] = {'indent': round(offset * 100)}
                    elif output_rules.source_typography and department_seen and _ATTRIBUTION.match(text.strip()) and y0 > height * .23 and x0 >= left-width*.025:
                        hints.setdefault(region.id, {})['source_typography'] = {'indent': round(max(0, offset)*100)}
                    continue
            if not output_rules.source_typography:
                continue
            if size is None and title:
                size = _printed_title_size(page, box)
            geometry = {'size': round(size, 1)} if size is not None else {}
            if title:
                center = (x0 + x1) / 2
                if min(abs(center - width / 2), abs(center - (left + right) / 2)) < width * .025:
                    alignment = 'CENTER'
                elif abs(x1 - right) < width * .035 and x0 - left > width * .09:
                    alignment = 'RIGHT'
                else:
                    alignment = 'LEFT'
                geometry['alignment'] = alignment
                if alignment == 'LEFT':
                    geometry['left'] = round(max(0, min(width * .45, x0 - left)) * 100)
                geometry['before'] = round(max(0, min(180, gap)) * 100)
                if hint.get('front_section') or hint.get('kind') == 'volume':
                    geometry['top_fraction'] = y0/height
            if geometry:
                hints.setdefault(region.id, {})['source_typography'] = geometry
