"""Measure typography from the matching line in the source PDF's text layer."""
from collections import defaultdict
from difflib import SequenceMatcher
import re
from statistics import median

from .citation_layout import heading_key
from .schema import PageReading, TEXT_KINDS, TITLE_KINDS, bbox_frac, paragraph_text
from .hwpx_output_rules import HwpxOutputRules
from .source_entry_headings import GENERAL_SECTION_LABELS, source_entry_rows, source_line

_IMPRINT = re.compile(r'ISBN|CIP|版权|版權|판권|原著|资助出版|資助出版', re.I)
_NUMBERED_SECTION = re.compile(r'(?:[一二三四五六七八九十]+[、．.]|[0-9]{1,2}[．.])\s*[^。？！,，.;；]{2,42}')
_DEPARTMENT = re.compile(r'[\u3400-\u9fff]{1,7}部')
_ENTRY = re.compile(r'[\u3400-\u9fff]{2,8}')
_CREDIT = re.compile(r'[^。；;：:]{3,24}(?:原著|先生著|校訂|校订|校注|編校|编校)')
_NOTE = re.compile(r'(?:[〔\[［【(（]\s*\d{1,2}\s*[〕\]］】)）]|[①-⑳])')
_FOLIO = re.compile(r'[•·・]\s*(\d{1,4})\s*[•·・]')
_ATTRIBUTION = re.compile(r'[\u3400-\u9fff]{2,8}(?:曰|云|謂)\s*[:：]')


def ruling_y(page, top, bottom, left, right, width_fraction):
    """Locate a printed horizontal rule from a small grayscale page strip."""
    import numpy as np
    import pymupdf

    r = page.rect
    rect = pymupdf.Rect(r.x0 + left*r.width, r.y0 + top*r.height,
                       r.x0 + right*r.width, r.y0 + bottom*r.height)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=rect, colorspace=pymupdf.csGRAY,
                          alpha=False, annots=False)
    if not pix.width or not pix.height:
        return None
    pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    longest = r.width * 2 * width_fraction
    found = None
    for y, row in enumerate(pixels):
        changes = np.flatnonzero(np.diff(np.r_[False, row < 185, False]))
        run = max(changes[1::2] - changes[::2], default=0)
        if run >= longest:
            longest = run
            found = round(((pix.y + y)/2 - r.y0) * 1000 / r.height)
    return found


def _scanned_notes(regions, hints, height, reviewed, rule=None):
    """Use the OCR rectangle only when a numbered note sits in the bottom band."""
    numbered = []
    for region in regions:
        if region.kind not in ('본문', '소주') or not region.paragraphs or 'kind' in hints.get(region.id, {}):
            continue
        x0, y0, x1, y1 = bbox_frac(region.bbox)
        after_rule = rule is not None and region.bbox[0] >= rule - 1
        min_width = .024 if reviewed and after_rule else .06
        if y0 < (.55 if region.kind == '소주' else .76) or y1 > .98 or x1 - x0 < min_width:
            continue
        text = paragraph_text(region.paragraphs[0]).strip()
        below_rule = (after_rule and text and not text.isdecimal()
                      and not re.fullmatch(r'[·.•・。\s]*\d{1,4}[·.•・。\s]*', text))
        if _NOTE.match(text) or reviewed and region.kind == '소주' or below_rule:
            numbered.append((region, y1 - y0))
    if len(numbered) < 2 and not reviewed and not any(region.kind == '소주' for region, _ in numbered):
        return
    supported = []
    for region, fraction in numbered:
        lines = sum(max(1, len(p.columns)) for p in region.paragraphs)
        typical = round(fraction * height / lines * .8, 1)
        if region.kind != '소주' and typical > 13 and not reviewed:
            continue
        supported.append((region, typical))
    if not supported:
        return
    size = min(11, max(9, median(typical for _, typical in supported)))
    hanging = round(size * 100)
    for region, _ in supported:
        hints.setdefault(region.id, {}).update(
            source_layout_para='footnote',
            source_typography={'size': size, 'alignment': 'LEFT', 'left': hanging, 'indent': -hanging})

    # A wrapped note often has no number on its second line. Require it to
    # continue the compact, aligned rows below a recognized note.
    marked = {region.id for region, _ in supported}
    first = supported[0][0]
    baseline = median(r.bbox[2] - r.bbox[0] for r, _ in supported)
    prior = None
    for region in regions:
        top, left, bottom, right = region.bbox
        if region.id in marked:
            prior = region
        elif prior is not None:
            text = paragraph_text(region.paragraphs[0]).strip() if region.paragraphs else ''
            wraps = (0 <= top - prior.bbox[2] <= baseline * .75
                     and first.bbox[1] - 60 <= left <= first.bbox[1] + 100)
            same_line = abs(top - prior.bbox[0]) <= baseline * .5 and abs(left - prior.bbox[3]) <= 25
            if (region.kind in ('본문', '소주') and text and not (text.isdecimal() or _FOLIO.fullmatch(text))
                    and region.id not in hints and (wraps or same_line)
                    and bottom - top <= baseline * 1.25 and right - left >= (24 if reviewed else 120)):
                hints[region.id] = dict(source_layout_para='footnote',
                                        source_typography={'size': size, 'alignment': 'LEFT', 'left': hanging})
                prior = region
            else:
                prior = None


def _scanned_titles(regions, hints):
    from .common_layout import MAJOR

    for region in regions:
        if region.kind != '설명문' or len(region.paragraphs) != 1:
            continue
        text = paragraph_text(region.paragraphs[0]).strip()
        top, left, bottom, right = region.bbox
        colophon = re.fullmatch(r'《[^》]{2,15}》\s*卷[之第]?[一二三四五六七八九十百上下]+[终終]', text)
        if left >= 560 and bottom - top <= 50 and (top < 350 and _CREDIT.fullmatch(text) or colophon):
            hints.setdefault(region.id, {}).update(source_layout_para='credit',
                                                    source_typography={'alignment': 'RIGHT'})

    rows = [(r, paragraph_text(r.paragraphs[0]).strip()) for r in regions
            if r.kind == '본문' and len(r.paragraphs) == 1 and '세로' not in r.reading_direction
            and 90 <= r.bbox[0] < r.bbox[2] <= 855]
    prose = [r.bbox for r, text in rows if text and r.bbox[3] - r.bbox[1] >= 470]
    if len(prose) >= 4:
        body_height = median(box[2] - box[0] for box in prose)
        body_left = min(median(box[1] for box in prose), min(box[1] for box in prose) + 10)
        for i, (region, text) in enumerate(rows[:-1]):
            existing = hints.get(region.id, {})
            if existing.get('kind') not in (None, 'body') or existing.get('source_layout_para'):
                continue
            top, left, bottom, right = region.bbox
            gap_before = top - rows[i - 1][0].bbox[2] if i else top - 90
            following_region, following_text = rows[i + 1]
            following = following_region.bbox
            gap_after = following[0] - bottom
            larger = bottom - top >= body_height * 1.14
            if top < 310 and larger and MAJOR.fullmatch(text):
                kind, level, alignment = 'volume', 1, 'LEFT' if left < 220 else 'CENTER'
            elif (_DEPARTMENT.fullmatch(text) and larger and abs((left + right) / 2 - 500) <= 45
                  and min(gap_before, gap_after) >= body_height):
                kind, level, alignment = 'section', 2, 'CENTER'
            elif (re.fullmatch(r'(?:[\u3400-\u9fff]\s*){2,12}(?:[（(][^）)\n]{1,24}[）)])?', text)
                  and 20 <= right - left <= 310 and body_left + 15 <= left <= body_left + 100
                  and (larger or bottom - top >= body_height * .92 and following_text.startswith(('[', '［', '【')))
                  and gap_before >= body_height * 1.05 and -body_height * .25 <= gap_after <= body_height * .82
                  and body_left - 10 <= following[1] <= body_left + 100
                  and following[3] - following[1] >= 140):
                kind, level, alignment = 'entry', 3, 'LEFT'
            else:
                continue
            hints[region.id] = dict(kind=kind, level=level, alignment=alignment, evidence='measured-scan-heading',
                                    source_typography={'alignment': alignment})
    for region in regions:
        hint = hints.get(region.id, {})
        if ('세로' in region.reading_direction or 'source_typography' in hint
                or (region.kind != '제목' and hint.get('kind') not in ('book', 'volume', 'chapter', 'section', 'subsection', 'entry'))):
            continue
        x0, y0, x1, y1 = bbox_frac(region.bbox)
        if not (.02 <= x1 - x0 <= .72 and .005 <= y1 - y0 <= .09):
            continue
        if abs((x0 + x1) / 2 - .5) <= .045:
            alignment = 'CENTER'
        elif x0 >= .46 and x1 >= .75:
            alignment = 'RIGHT'
        elif x0 <= .17 and x1 < .70:
            alignment = 'LEFT'
        else:
            continue
        hints.setdefault(region.id, {})['source_typography'] = {'alignment': alignment}


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


def source_line_blocks(page):
    import pymupdf
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    return page.get_text('dict', flags=flags)['blocks']


def measure_source_typography(records, pdf, *, infer_titles=False, output_rules=HwpxOutputRules(),
                              entry_catalogue=None, contents_pages=(), adaptive=False, footnote_pages=(),
                              source_lines=None):
    from .common_layout import MAJOR, SECTION

    for record in records:
        number = record['pdf_page']
        if not 1 <= number <= len(pdf):
            continue
        page = pdf[number - 1]
        width, height = page.cropbox.width, page.cropbox.height
        if page.rotation or not width or not height:
            continue
        lines, matches, folios, printed = [], defaultdict(list), set(), []
        blocks = source_lines.get(number) if source_lines is not None else None
        for block in source_line_blocks(page) if blocks is None else blocks:
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
                if entry_catalogue is not None:
                    printed.append(source_line(text, spans, row))
                if len(text) <= 200 and (key := heading_key(text)):
                    matches[key].append(row)
        list_page = record.get('_source_contents_page') or record.get('_source_index_page')
        hints = record.setdefault('_common_layout_regions', {})
        reading = PageReading.model_validate(record['merged'])
        regions = reading.regions
        if infer_titles and not list_page and number <= 16:
            _front_rows(record, regions, page, lines, matches, hints)
            if not output_rules.source_typography:
                for hint in hints.values():
                    if 'front_row' in hint:
                        hint.pop('front_row')
                        hint.pop('source_typography', None)
        if adaptive and infer_titles and output_rules.footnote_groups:
            note_regions = [r for r in regions if r.kind == '소주'] if list_page else regions
            reviewed = number in footnote_pages
            rule = ruling_y(page, .68, .94, .06, .51, .17) if reviewed and not lines and not list_page else None
            _scanned_notes(note_regions, hints, height, reviewed, rule=rule)
        if not lines:
            if adaptive and infer_titles and output_rules.source_typography and not list_page:
                _scanned_titles(regions, hints)
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
        entries = {}
        if entry_catalogue is not None and not list_page and number not in contents_pages and body_height is not None:
            if not folios and re.fullmatch(r'[0-9]{1,4}', reading.print_page):
                folios.add(reading.print_page)
            names = entry_catalogue.get(int(next(iter(folios)), 10), set()) if len(folios) == 1 else set()
            entries = source_entry_rows(page, regions, hints, printed, names, left, body_height, prose)
        department_seen = False
        for region in regions:
            hint = hints.get(region.id, {})
            if 'front_row' in hint:
                continue
            source_entry = entries.get(region.id)
            if source_entry:
                entry_box, entry_size, cut, evidence = source_entry
                hint = hints.setdefault(region.id, {})
                hint.update(kind='entry', level=3, alignment='LEFT', evidence=evidence, source_entry_end=cut)
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
            found = [(entry_box, entry_size)] if source_entry else matches.get(key, ())
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
                department_heading = (department_seen and _ENTRY.fullmatch(text.strip()) and not centered
                                      and left-width*.04 <= x0 <= left+width*.10 and gap >= body_height)
                if department or department_heading:
                    section = department or key in GENERAL_SECTION_LABELS
                    hint = hints.setdefault(region.id, {})
                    hint.update(kind='section' if section else 'entry', level=2 if section else 3,
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
