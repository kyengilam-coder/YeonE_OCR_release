"""Measure typography from the matching line in the source PDF's text layer."""
from collections import defaultdict
from statistics import median

from .citation_layout import heading_key
from .schema import PageReading, TEXT_KINDS, TITLE_KINDS, paragraph_text


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


def measure_source_typography(records, pdf, *, infer_titles=False):
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
        lines, matches = [], defaultdict(list)
        for block in page.get_text('dict', flags=flags)['blocks']:
            for line in block.get('lines', []):
                if line.get('wmode') or tuple(line.get('dir', ())) != (1, 0):
                    continue
                spans = [span for span in line['spans'] if span['text'].strip()]
                if not spans:
                    continue
                text = ''.join(span['text'] for span in spans).strip()
                main = max(spans, key=lambda span: sum(c.isalnum() for c in span['text']))
                size = main['size']
                if not 5 <= size <= 72:
                    continue
                row = (line['bbox'], size if main.get('alpha', 255) else None)
                lines.append(row)
                if len(text) <= 200 and (key := heading_key(text)):
                    matches[key].append(row)
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

        list_page = record.get('_source_contents_page') or record.get('_source_index_page')
        if body_size is not None and not list_page:
            record['_source_body_size'] = body_size
        hints = record.setdefault('_common_layout_regions', {})
        for region in PageReading.model_validate(record['merged']).regions:
            hint = hints.get(region.id, {})
            contents = hint.get('source_contents')
            index = hint.get('source_index')
            title = (region.kind in TITLE_KINDS or hint.get('kind') in
                     ('book', 'volume', 'chapter', 'section', 'subsection', 'entry') or
                     (contents and contents['mode'] == 'title'))
            text = region.title_text if region.kind in TITLE_KINDS else (
                paragraph_text(region.paragraphs[0]) if region.kind in TEXT_KINDS and len(region.paragraphs) == 1 else '')
            major = False
            possible_title = False
            if not (title or contents or index):
                if list_page or body_height is None or not text:
                    continue
                major = infer_titles and bool(MAJOR.fullmatch(text.strip()))
                possible_title = major or (infer_titles and bool(SECTION.fullmatch(text.strip())))
            found = matches.get(heading_key(text.strip()), ()) if 0 < len(text) <= 200 else ()
            if len(found) > 1 and region.bbox != [0, 0, 1000, 1000]:
                top = region.bbox[0] * height / 1000
                found = [row for row in found if abs(row[0][1] - top) < height * .025]
            if len(found) != 1:
                continue
            box, size = found[0]
            x0, y0, x1, y1 = box
            if not (title or contents or index):
                if possible_title and height * .09 <= y0 <= height * .9 and y1 - y0 >= 1.25 * body_height:
                    hint = hints.setdefault(region.id, {})
                    hint.update(kind='volume' if major and '卷' in text else 'chapter' if major else 'section',
                                level=1 if major else 2, evidence='measured-source-heading')
                    title = True
                else:
                    offset = x0 - left
                    if (region.kind == '본문' and 5 <= offset <= width * .12 and abs(x1 - right) <= width * .04
                            and x1 - x0 > width * .45 and .8 <= (y1 - y0) / body_height <= 1.2):
                        hints.setdefault(region.id, {})['source_typography'] = {'indent': round(offset * 100)}
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
                prior = [box[3] for box, _ in lines if box[3] <= y0 and box[0] < right and box[2] > left]
                gap = y0 - max(prior) if prior else y0 - height * .08
                geometry['before'] = round(max(0, min(180, gap)) * 100)
            if geometry:
                hints.setdefault(region.id, {})['source_typography'] = geometry
