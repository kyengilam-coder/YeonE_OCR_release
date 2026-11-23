"""Local evidence for selecting book-format samples without OCR or network calls."""
import re
from collections import Counter, defaultdict
from statistics import median

from .contents_layout import CONTENTS
from .index_layout import index_entry
from .toc_tabs import ENTRY


INDEX = re.compile(r'(?:索[引隐隱]|檢索|检索|색인|index)', re.I)
BIBLIOGRAPHY = re.compile(r'(?:參考文獻|参考文献|참고문헌|문헌목록|bibliography|references)', re.I)
MAX_AUTO_SAMPLES = 8
PRIMARY_TYPES = ('목차', '본문', '색인')
FIELD_PREFIX = re.compile(
    r'^(?:\[([^\]]+)\]|［([^］]+)］|〔([^〕]+)〕|【([^】]+)】|([^:：]+)[:：])\s*(?=\S)')
OCR_FIELD = re.compile(r'^(?:\[([^\]]+)\]|［([^］]+)］|〔([^〕]+)〕|【([^】]+)】)(?:\s|(?=\S)|$)')


def saved_ocr_fields(records):
    """Repeat bracketed labels at real paragraph starts on separate pages."""
    from .schema import PageReading, paragraph_text

    labels = defaultdict(set)
    for record in records:
        reading = PageReading.model_validate(record['merged'])
        for region in reading.regions:
            if region.kind not in ('본문', '설명문') or region.bbox[0] >= 740:
                continue
            for para in region.paragraphs:
                if not (match := OCR_FIELD.match(paragraph_text(para).strip())):
                    continue
                name = next(value for value in match.groups() if value is not None).strip()
                if re.fullmatch(r'[^\W_][\w \-·]{0,23}', name) and any(c.isalpha() for c in name):
                    labels[name].add(record['pdf_page'])
    return [name for name, pages in sorted(labels.items(), key=lambda pair: (-len(pair[1]), pair[0]))
            if len(pages) >= 2][:40]


def _layout_profile(rects, body_margins, labels):
    fields = [name for name, count in labels.most_common(40) if count >= 2]
    if not rects:
        return {'field_labels': fields}
    width, height = rects.most_common(1)[0][0]
    rows = body_margins[(width, height)]
    page = {'width': width, 'height': height,
            'left': round(width * .12, 1), 'right': round(width * .12, 1),
            'top': round(height * .09, 1), 'bottom': round(height * .08, 1)}
    if rows:
        page['left'] = round(max(width * .04, min(width * .25, median(left for left, _ in rows))), 1)
        page['right'] = round(max(width * .04, min(width * .25, median(right for _, right in rows))), 1)
    return {'field_labels': fields, 'paper': page}


def _heading_type(text):
    title = re.sub(r'\s+', '', text)
    if CONTENTS.fullmatch(title):
        return '차례' if title == '차례' else '목차'
    if INDEX.fullmatch(title):
        return '색인'
    if BIBLIOGRAPHY.fullmatch(title):
        return '문헌 목록'
    return None


def scan_layout(doc, *, stop=None, progress=lambda message: None, profile=None):
    from .rule_generation import _check_stop

    count = len(doc)
    if not count:
        raise ValueError('PDF에 쪽이 없습니다.')
    bookmarks = defaultdict(list)
    for level, title, number in doc.get_toc():
        if 1 <= number <= count:
            bookmarks[number].append((level, title))

    types, layouts = defaultdict(list), defaultdict(list)
    uncertain = []
    list_kind = None
    rects, labels = Counter(), Counter()
    body_margins = defaultdict(list)
    pt_to_mm = 25.4 / 72
    for number, page in enumerate(doc, 1):
        _check_stop(stop)
        if number == 1 or number % 50 == 0:
            progress(f'PDF 구조 훑는 중 · {number}/{count}쪽 · 로컬 검사')
        lines = [line.strip() for line in page.get_text().splitlines() if line.strip()]
        headings = [title for _, title in bookmarks[number]] + lines[:4]
        heading = next((kind for title in headings if (kind := _heading_type(title))), None)
        leader_count = sum(bool(ENTRY.search(line)) for line in lines)
        index_count = sum(bool(index_entry(line, allow_plain=True)) for line in lines)
        prose_count = sum(sum(c.isalpha() for c in line) >= 25 and not ENTRY.search(line) for line in lines)
        if heading in ('차례', '목차') and leader_count < 4 and prose_count >= 4:
            heading = None
        if heading:
            kind = heading
        elif list_kind == '색인' and index_count >= 8 and prose_count < 3:
            kind = list_kind
        elif list_kind in ('차례', '목차') and leader_count >= 4:
            kind = list_kind
        elif prose_count >= 4 and leader_count < 4:
            kind = '본문'
        else:
            kind = None
        if kind:
            types[kind].append(number)
        else:
            uncertain.append(number)
        # A repeated running header without list rows does not carry into the next page.
        list_kind = kind if kind in ('차례', '목차', '색인') else None

        rect = page.rect
        if profile is not None:
            size = tuple(round(v * pt_to_mm, 1) for v in (rect.width, rect.height))
            if all(50 <= v <= 600 for v in size):
                rects[size] += 1
                if kind == '본문' and not page.rotation:
                    wide = [b for b in page.get_text('blocks', flags=0)
                            if b[2] - b[0] >= rect.width * .50 and b[6] == 0]
                    if wide:
                        body_margins[size].append((
                            min(b[0] - rect.x0 for b in wide) * pt_to_mm,
                            min(rect.x1 - b[2] for b in wide) * pt_to_mm))
            if kind not in ('차례', '목차', '색인', '문헌 목록'):
                for line in lines:
                    match = FIELD_PREFIX.match(line)
                    if match:
                        name = next(part for part in match.groups() if part is not None).strip()
                        if re.fullmatch(r'[^\W_][\w \-·]{0,23}', name) and any(c.isalpha() for c in name):
                            labels[name] += 1
        shape = (round(rect.width / max(rect.height, 1), 1), page.rotation)
        if len(''.join(lines)) < 30:
            import pymupdf
            scale = 96 / max(rect.width, rect.height, 1)
            thumb = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csGRAY, alpha=False)
            # Compare ink distribution at low resolution. This is a sampling cue, never a page type.
            pixels = thumb.samples
            middle = len(pixels) // 2
            ink = tuple(round(sum(value < 190 for value in band) / max(len(band), 1) * 12)
                        for band in (pixels[:middle], pixels[middle:]))
            shape += ink
        layouts[shape].append(number)

    selected = []

    def add(number):
        if number not in selected and len(selected) < MAX_AUTO_SAMPLES:
            selected.append(number)

    for kind in PRIMARY_TYPES + ('차례',):
        numbers = types.get(kind, [])
        if numbers:
            if kind == '본문':
                primary = [number for number in numbers if any(level == 1 for level, _ in bookmarks.get(number, []))]
                add((primary or numbers)[0])
            else:
                add(numbers[0])
    if not types.get('목차') and not types.get('차례'):
        add(min(3, count))
        add(1 + round((count - 1) * .08))
    if not types.get('본문'):
        add(1 + round((count - 1) * .5))
    if not types.get('색인'):
        add(count)
        add(1 + round((count - 1) * .95))
    for kind in PRIMARY_TYPES + ('차례',):
        numbers = types.get(kind, [])
        if len(numbers) > 1:
            add(numbers[len(numbers) // 2])
    if profile is not None:
        profile.update(_layout_profile(rects, body_margins, labels))
    if all(types.get(kind) for kind in PRIMARY_TYPES) or len(selected) >= 6:
        return dict(types), uncertain, sorted(selected)
    for group in sorted(layouts.values(), key=len):
        add(group[len(group) // 2])
    if bookmarks:
        divisions = sorted(bookmarks)
        add(divisions[len(divisions) // 2])
    for part in (2, 1, 3, 0, 4):
        add(1 + round((count - 1) * part / 4))
    return dict(types), uncertain, sorted(selected)
