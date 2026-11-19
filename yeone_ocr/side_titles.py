"""Corroborate repeated vertical side titles against the rendered source."""
from collections import defaultdict
from itertools import combinations


def _runs(active, max_step):
    import numpy as np
    positions = np.flatnonzero(active)
    return [(int(group[0]), int(group[-1]) + 1)
            for group in np.split(positions, np.where(np.diff(positions) > max_step)[0] + 1) if len(group)]


def _strips(page):
    import numpy as np
    import pymupdf
    from PIL import Image, ImageFilter

    zoom = 980 / max(page.rect.width, page.rect.height)
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
    pixels = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width)
    ink = pixels < 145
    height, width = ink.shape
    top, bottom = round(height * .10), round(height * .90)
    gutter = max(3, round(width * .012))
    for start, end in ((0, round(width * .17)), (round(width * .83), width)):
        margin = ink[top:bottom, start:end]
        for left, right in _runs(margin.sum(axis=0) > max(4, height * .008), max(2, round(width * .004))):
            if left == 0 or right == end - start or not width * .014 <= right - left <= width * .065:
                continue
            for first, last in _runs(margin[:, left:right].any(axis=1), round(height * .022)):
                if not height * .085 <= last - first <= height * .50:
                    continue
                x0, x1, y0, y1 = start + left, start + right, top + first, top + last
                if (y1 - y0) < 3 * (x1 - x0):
                    continue
                # Require a visible gap on the body side, with a separate body
                # text area spanning the same part of the page.
                if start == 0:
                    gap = ink[y0:y1, x1:x1 + gutter]
                    text = ink[y0:y1, x1 + gutter:round(width * .60)]
                else:
                    gap = ink[y0:y1, x0 - gutter:x0]
                    text = ink[y0:y1, round(width * .40):x0 - gutter]
                if gap.mean() > .018 or text.mean() < .025:
                    continue
                glyphs = len(_runs(margin[first:last, left:right].sum(axis=1) > 1, 3))
                image = Image.fromarray(255 - pixels[y0:y1, x0:x1])
                image = image.resize((32, 128), Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(1.1))
                vector = np.asarray(image, dtype=float).ravel()
                vector /= np.linalg.norm(vector)
                bbox = [round(1000 * coord / scale) for coord, scale in (
                    (y0, height), (x0, width), (y1, height), (x1, width))]
                yield bbox, glyphs, vector


def repeated_side_titles(pdf, readings):
    """Return evidence only when three source pages share OCR text and side ink."""
    from .document_editing import _edge_rows, _header_signature
    from .schema import paragraph_text

    candidates = defaultdict(list)
    for reading in readings:
        for region, text in _edge_rows(reading):
            # Only standalone local lines. Do not classify cover/section titles
            # or remove part of a line carrying ordinary body text.
            if region.kind != '본문' or region.reading_direction != '가로' or not region.id.startswith('local-line-'):
                continue
            signature = _header_signature(text)
            if not 4 <= len(signature) <= 18 or not all(c.isalpha() for c in signature):
                continue
            if not ''.join(text.split()).isalpha():
                continue
            occurrences = sum(_header_signature(paragraph_text(p)) == signature
                              for row in reading.regions for p in row.paragraphs)
            if occurrences == 1:
                candidates[signature].append((reading.pdf_page, region))

    evidence = defaultdict(dict)
    page_cache = {}
    for signature, rows in candidates.items():
        if len({number for number, _ in rows}) < 3:
            continue
        strips = []
        for number, region in rows:
            if not 1 <= number <= len(pdf):
                continue
            if number not in page_cache:
                page_cache[number] = list(_strips(pdf[number - 1]))
            for bbox, glyphs, vector in page_cache[number]:
                if not len(signature) - 1 <= glyphs <= len(signature) + 3:
                    continue
                if region.bbox != [0, 0, 1000, 1000]:
                    y0, x0, y1, x1 = region.bbox
                    if not (bbox[0] - 15 <= (y0 + y1) / 2 <= bbox[2] + 15
                            and bbox[1] - 15 <= (x0 + x1) / 2 <= bbox[3] + 15):
                        continue
                strips.append((number, region.id, bbox, vector))
        matches = defaultdict(set)
        for (i, left), (j, right) in combinations(enumerate(strips), 2):
            if left[0] != right[0] and float(left[3] @ right[3]) >= .94:
                matches[i].add(j)
                matches[j].add(i)
        for i, (number, region_id, bbox, _) in enumerate(strips):
            # A matching pair of other pages must also match one another.
            if any(strips[j][0] != strips[k][0] and k in matches[j]
                   for j, k in combinations(matches[i], 2)):
                evidence[number][region_id] = bbox
    return evidence
