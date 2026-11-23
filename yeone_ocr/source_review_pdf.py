"""Find positioned OCR evidence in a searchable copy of the same scan."""

from pathlib import Path
import re

import pymupdf


def _family(doc):
    software = ((doc.metadata or {}).get('producer') or '').casefold()
    if 'abbyy' in software or 'finereader' in software:
        return 'abbyy'
    if 'adobe' in software or 'acrobat' in software:
        return 'adobe'
    if 'tesseract' in software or 'ocrmypdf' in software:
        return 'tesseract'
    return None


def _ink(page):
    import numpy as np
    from PIL import Image, ImageFilter

    width, height = 160, 220
    rect = page.rect
    pix = page.get_pixmap(matrix=pymupdf.Matrix(width / rect.width, height / rect.height),
                          colorspace=pymupdf.csGRAY, alpha=False, annots=False)
    image = Image.frombytes('L', (pix.width, pix.height), pix.samples).filter(ImageFilter.GaussianBlur(.7))
    return 255.0 - np.asarray(image, dtype='float32')


def same_scan_page(source, copy):
    """Compare visible ink with a small translation allowance for rescanned page margins."""
    import numpy as np

    a, b = source.rect, copy.rect
    if abs((a.width / a.height) / (b.width / b.height) - 1) > .025:
        return False
    if not source.get_images() or not copy.get_images():
        return False
    original, duplicate = _ink(source), _ink(copy)
    height, width = map(min, zip(original.shape, duplicate.shape))
    pad = 3
    core = original[pad:height - pad, pad:width - pad]
    left = core - core.mean()
    left_norm = np.sum(left * left)
    if left_norm < core.size * 9:
        return False  # A blank or nearly blank scan is not enough to identify a page.
    for y in (0, -1, 1, -2, 2, -3, 3):
        for x in (0, -1, 1, -2, 2, -3, 3):
            crop = duplicate[pad+y:height-pad+y, pad+x:width-pad+x]
            right = crop - crop.mean()
            denom = left_norm * np.sum(right * right)
            if denom > 0 and np.sum(left * right) >= .90 * np.sqrt(denom):
                return True
    return False


def find_scan_copy(source_path, source, stack):
    """Prefer the plain OCR copy, and consider one copy from a known OCR family."""
    path = Path(source_path)
    if not path.is_file() or path.suffix.lower() != '.pdf' or len(source) == 0:
        return None
    name = re.compile(re.escape(path.stem) + r'\s*\([^)]*(?:인식|OCR)[^)]*\)(?:\s*-\s*[^.]+)?', re.I)
    try:
        candidates = sorted((p for p in path.parent.iterdir() if p.suffix.lower() == '.pdf'
                             and name.fullmatch(p.stem) and p.is_file()),
                            key=lambda p: (len(p.stem), p.name.casefold()))
    except OSError:
        return None
    samples = sorted({min(len(source) - 1, n) for n in (1, len(source) // 4, len(source) // 2, len(source) * 3 // 4, len(source) - 2) if n >= 0})
    for candidate in candidates:
        try:
            if candidate.samefile(path):
                continue
            with pymupdf.open(candidate) as doc:
                family = _family(doc)
                if not family or len(doc) != len(source):
                    continue
                tested = [n for n in samples if len(doc[n].get_text().strip()) >= 15]
                if not tested or sum(same_scan_page(source[n], doc[n]) for n in tested) < max(1, len(tested) - 1):
                    continue
            return candidate.name, family, stack.enter_context(pymupdf.open(candidate))
        except (OSError, ValueError, RuntimeError):
            continue
    return None


def positioned_reading(page, direction):
    """Return text lines with page coordinates for the regular source-review matcher."""
    from .local_reading import _normalized

    px, py = page.rect.width, page.rect.height
    lines = []
    for block in page.get_text('dict', flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)['blocks']:
        for line in block.get('lines', ()):
            text = ''.join(''.join(s['text'].split()) for s in line['spans'])
            if not text or line['bbox'][2] <= line['bbox'][0] or line['bbox'][3] <= line['bbox'][1]:
                continue
            box = _normalized(line['bbox'], px, py)
            lines.append((box, text))
    if direction == 'horizontal':
        # PDF exporters may split an ink line into several text runs.
        lines.sort(key=lambda item: (item[0][0], item[0][1]))
        rows = []
        for box, text in lines:
            if rows and abs(box[0] - rows[-1][0][0][0]) <= 4:
                rows[-1].append((box, text))
            else:
                rows.append([(box, text)])
        lines = [item for row in rows for item in sorted(row, key=lambda item: item[0][1])]
    else:
        lines.sort(key=lambda item: (-item[0][1], item[0][0]))
    return {'located_lines': lines}
