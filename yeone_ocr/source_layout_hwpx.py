"""Editable, page-oriented HWPX composed from verified PDF text geometry."""
from copy import deepcopy
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
import re
from statistics import median
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from lxml import etree as xml

from .hwpx_writer import HP


HH = "{http://www.hancom.co.kr/hwpml/2011/head}"
UNIT_NS = "http://www.hancom.co.kr/hwpml/2016/HwpUnitChar"
FONT = "Source Han Serif K Medium"
FONT_FILE = Path(__file__).parent / "assets" / "fonts" / "SourceHanSerifK-Medium.otf"
MARGIN = 8.5  # points, outside the measured text area


@dataclass
class Fragment:
    left: float
    right: float
    height: float
    text: str


@dataclass
class PageLayout:
    size: tuple[int, int]
    rows: list = field(default_factory=list)
    rules: list = field(default_factory=list)
    artwork: list = field(default_factory=list)


def measured_rows(page):
    """Read editable text from the aligned PDF layer, including physical gaps."""
    import pymupdf

    chunks = []
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    for block in page.get_text("dict", flags=flags)["blocks"]:
        for line in block.get("lines", ()):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = "".join(span["text"] for span in line["spans"]).strip()
            text = "".join(c for c in text if ord(c) >= 32 or c == "\t")
            if not text:
                continue
            left, top, right, bottom = line["bbox"]
            baseline = median(span["origin"][1] for span in spans)
            fragment = Fragment(left, right, max(4, bottom - top), text)
            chunks.append((baseline, fragment))

    rows = []
    tolerance = min(4.6, max(2.5, page.rect.height / 130))
    for baseline, fragment in sorted(chunks, key=lambda item: (item[0], item[1].left)):
        if not rows or baseline - rows[-1][0] > tolerance:
            rows.append((baseline, []))
        rows[-1][1].append(fragment)

    for _, parts in rows:
        grouped = []
        for part in sorted(parts, key=lambda f: f.left):
            previous = grouped[-1] if grouped else None
            if previous is not None and part.left - previous.right < 4:
                spacer = " " if part.left - previous.right > 1.8 and previous.text[-1].isascii() and part.text[0].isascii() else ""
                previous.text += spacer + part.text
                previous.right = max(previous.right, part.right)
                previous.height = max(previous.height, part.height)
            else:
                grouped.append(part)
        parts[:] = grouped
    return rows


def _printed_rules(dark, width, height):
    import cv2
    import numpy as np

    h, w = dark.shape
    found = []
    for horizontal in (True, False):
        kernel = np.ones((1, max(1, int(.25*w))) if horizontal else (max(1, int(.13*h)), 1), dtype=np.uint8)
        bridge = np.ones((1, 5) if horizontal else (5, 1), dtype=np.uint8)
        continuous = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, bridge)
        straight = cv2.morphologyEx(continuous, cv2.MORPH_OPEN, kernel)
        _, _, boxes, _ = cv2.connectedComponentsWithStats(straight)
        for x, y, bw, bh, _area in boxes[1:]:
            if x < .025*w or y < .035*h or x+bw > .975*w or y+bh > .965*h:
                continue
            if horizontal and bh <= 4 and bw > 20*bh and bw >= .25*w:
                center = (y+bh/2)*height/h
                found.append((x*width/w, center, (x+bw)*width/w, center))
            if not horizontal and bw <= 4 and bh > 20*bw and bh >= .13*h:
                center = (x+bw/2)*width/w
                found.append((center, y*height/h, center, (y+bh)*height/h))
    return found


def _artwork(page, box):
    import pymupdf

    rect = pymupdf.Rect(box) & page.rect
    pixmap = page.get_pixmap(dpi=180, clip=rect, alpha=False)
    return tuple(rect), pixmap.tobytes("jpeg", jpg_quality=91)


def _page_layout(source, aligned, *, cover_position, record):
    import numpy as np
    import pymupdf

    w, h = source.rect.width, source.rect.height
    layout = PageLayout((round(w*100), round(h*100)))
    pix = source.get_pixmap(dpi=105, colorspace=pymupdf.csGRAY, alpha=False)
    gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    dark = (gray < 165).astype(np.uint8)
    if dark.mean() < .0002:  # confirmed blank scan, even if an OCR provider emitted a notice
        return layout

    layout.rows = measured_rows(aligned)
    images = source.get_image_info()
    whole_scan = any(pymupdf.Rect(image["bbox"]).get_area() > .75*w*h for image in images)
    if cover_position and whole_scan and len(layout.rows) <= 12 and (
            not layout.rows or dark.mean() > .13 or max(f.height for _, row in layout.rows for f in row) > 22):
        layout.rows = []
        layout.artwork.append(_artwork(source, source.rect))
        return layout

    from .schema import IMAGE_KINDS

    boxes = []
    for region in (record.get("merged") or {}).get("regions", ()):
        if region.get("kind") in (*IMAGE_KINDS, "표"):
            top, left, bottom, right = region["bbox"]
            box = pymupdf.Rect(left*w/1000, top*h/1000, right*w/1000, bottom*h/1000)
            if region["kind"] != "표" or not any(
                    box.y0 <= y <= box.y1 and any(f.left < box.x1 and f.right > box.x0 for f in row)
                    for y, row in layout.rows):
                boxes.append(box)
    for image in images:
        box = pymupdf.Rect(image["bbox"]) & source.rect
        if .015*w*h < box.get_area() < .55*w*h and not any((box & other).get_area() > .6*box.get_area() for other in boxes):
            boxes.append(box)
    layout.artwork.extend(_artwork(source, box) for box in boxes if not box.is_empty)
    layout.rules = _printed_rules(dark, w, h)
    return layout


def _compatible_units(parent):
    for label, factor in (("case", 1), ("default", 2)):
        child = xml.SubElement(parent, HP + label)
        if label == "case":
            child.set(HP + "required-namespace", UNIT_NS)
        yield child, factor


class Styles:
    def __init__(self, package, header):
        from PIL import ImageFont

        self.package = package
        self.header = header
        self.paras = header.find(".//" + HH + "paraProperties")
        self.tabs = header.find(".//" + HH + "tabProperties")
        self.paragraphs = {}
        self.tab_ids = {}
        self.runs = {}
        self.fonts = {}
        self.load_font = ImageFont.truetype

    def char(self, size, ratio=100, bold=False):
        key = size, ratio, bold
        if key not in self.runs:
            self.runs[key] = self.package.styles.ensure_run(font=FONT, size=size, ratio=ratio, bold=bold)
        return self.runs[key]

    def fit(self, fragment, limit):
        size = max(4, min(36, round(fragment.height*.87*4)/4))
        width = max(.5, min(fragment.right-fragment.left, limit-fragment.left))
        bold = fragment.height > 12 and width > 60 and bool(re.match(r"^\d{2,4}\s+\D", fragment.text))
        while True:
            if size not in self.fonts:
                self.fonts[size] = self.load_font(str(FONT_FILE), round(size*8))
            advance = max(.01, self.fonts[size].getlength(fragment.text)/8)
            # Leave space for shaping/fallback differences in Hancom, and an
            # extra absolute margin for rows near the physical page edge.
            target = max(.5, min(width, limit-fragment.left-3.5))
            ratio = min(115, int((88 if bold else 92)*target/advance))
            if ratio >= 50 or size <= 4:
                return self.char(size, max(50, ratio), bold)
            size = max(4, round(size*.85*4)/4)

    def _tabs(self, positions):
        if not positions:
            return "0"
        if positions not in self.tab_ids:
            identity = str(len(self.tabs))
            tab = xml.SubElement(self.tabs, HH+"tabPr", id=identity, autoTabLeft="0", autoTabRight="0")
            switch = xml.SubElement(tab, HP+"switch")
            for branch, factor in _compatible_units(switch):
                for pos in dict.fromkeys(positions):
                    item = xml.SubElement(branch, HH+"tabItem", pos=str(pos*factor), type="LEFT", leader="NONE")
                    if factor == 1:
                        item.set("unit", "HWPUNIT")
            self.tab_ids[positions] = identity
        return self.tab_ids[positions]

    def paragraph(self, indent, pitch, positions=(), before=0):
        key = indent, pitch, positions, before
        if key not in self.paragraphs:
            identity = str(len(self.paras))
            p = deepcopy(self.paras[0])
            p.set("id", identity)
            p.set("tabPrIDRef", self._tabs(positions))
            p.set("snapToGrid", "0")
            p.find(HH+"align").set("horizontal", "LEFT")
            breaks = p.find(HH+"breakSetting")
            for name in ("keepWithNext", "keepLines", "widowOrphan", "pageBreakBefore"):
                breaks.set(name, "0")
            for kind, factor in (("case", 1), ("default", 2)):
                for branch in p.iter(HP+kind):
                    for node in branch.iter():
                        tag = xml.QName(node).localname
                        if tag in ("left", "prev", "intent", "right", "next"):
                            node.set("value", str({"left":indent, "prev":before}.get(tag, 0)*factor))
                        if tag == "lineSpacing":
                            node.set("type", "FIXED")
                            node.set("value", str(pitch*factor))
            self.paras.append(p)
            self.paragraphs[key] = identity
        return self.paragraphs[key]

    def finish(self):
        self.paras.set("itemCnt", str(len(self.paras)))
        self.tabs.set("itemCnt", str(len(self.tabs)))
        source = self.package.oxml.headers[0].element
        for name in ("fontfaces", "charProperties"):
            tree = self.header.find(".//"+HH+name)
            updated = source.find(".//"+HH+name)
            tree.getparent().replace(tree, xml.fromstring(ElementTree.tostring(updated)))


def _positioned_picture(package, artwork, paragraph):
    box, image = artwork
    x0,y0,x1,y1 = (round(value*100) for value in box)
    item = package.media.add_image(image, "jpg")
    picture = paragraph.add_picture(item, width=x1-x0, height=y1-y0, treat_as_char=False,
                                    pos_overrides={"vertRelTo":"PAPER", "horzRelTo":"PAPER",
                                                   "vertAlign":"TOP", "horzAlign":"LEFT",
                                                   "horzOffset":x0, "vertOffset":y0}, text_wrap="BEHIND_TEXT")
    return xml.fromstring(ElementTree.tostring(picture.element))


def _rule(prototype, edges, identity):
    x0,y0,x1,y1 = (round(value*100) for value in edges)
    shape = deepcopy(prototype)
    for name in ("id", "instid"):
        shape.set(name, str(identity))
    shape.set("textWrap", "BEHIND_TEXT")
    for name in ("orgSz", "curSz", "sz"):
        item = shape.find(HP+name)
        item.set("width", str(max(1,x1-x0)))
        item.set("height", str(max(1,y1-y0)))
    end = next(node for node in shape if xml.QName(node).localname == "endPt")
    end.set("x", str(x1-x0))
    end.set("y", str(y1-y0))
    position = shape.find(HP+"pos")
    position.set("vertRelTo", "PAPER")
    position.set("horzRelTo", "PAPER")
    position.set("horzOffset", str(x0))
    position.set("vertOffset", str(y0))
    return shape


def _write_sections(package, layouts, target, stop):
    from itertools import count, groupby

    groups = [(size, list(pages)) for size, pages in groupby(layouts, key=lambda layout: layout.size)]
    anchor = package.paragraphs[0]
    template_rule = package.shapes.add_line(end_x=14400, treat_as_char=False, paragraph=anchor,
                                            line_color="#646464", line_width="26")
    rule = xml.fromstring(ElementTree.tostring(template_rule.element))
    images = {id(layout): [_positioned_picture(package, picture, anchor) for picture in layout.artwork]
              for layout in layouts if layout.artwork}
    for index, (size, _) in enumerate(groups):
        if index:
            package.add_section()
        package.page.setup(section_index=index, width_mm=size[0]*25.4/7200,
                           height_mm=size[1]*25.4/7200, margins_mm={
                               edge: MARGIN*25.4/72 for edge in ("top", "bottom", "left", "right")},
                           header_margin_mm=0, footer_margin_mm=0)

    data = BytesIO()
    package.save_to_stream(data)
    with ZipFile(data) as template:
        header = xml.fromstring(template.read("Contents/header.xml"))
        styles = Styles(package, header)
        bodies = {}
        ids = count(1)
        shape_ids = count(120000000)
        for index, (size, pages) in enumerate(groups):
            part = f"Contents/section{index}.xml"
            body = xml.fromstring(template.read(part))
            paragraphs = body.findall(HP+"p")
            if not paragraphs:
                raise ValueError("HWPX 구역에 쪽 설정이 없습니다.")
            for paragraph in paragraphs[1:]:
                body.remove(paragraph)
            carrier = paragraphs[0]
            carrier.set("paraPrIDRef", styles.paragraph(0, 50))
            for run in carrier.findall(HP+"run"):
                for child in tuple(run):
                    if child.tag in (HP+"pic", HP+"line", HP+"t"):
                        run.remove(child)
            for page_index, layout in enumerate(pages):
                stop()
                width, height = (value/100 for value in size)
                rows = layout.rows or [(MARGIN+6, [])]
                baselines = [round(point*2)*50 for point, _ in rows]
                last_safe = round((height-MARGIN-15)*100)
                if baselines[-1] > last_safe and baselines[-1] > baselines[0]:
                    scale = max(.5, (last_safe-baselines[0])/(baselines[-1]-baselines[0]))
                    baselines = [round(baselines[0]+(b-baselines[0])*scale) for b in baselines]
                for row_index, (_, fragments) in enumerate(rows):
                    y = baselines[row_index]
                    pitch = max(350, baselines[row_index+1]-y) if row_index+1 < len(rows) else 900
                    before = max(0, y-round((MARGIN+6)*100)) if row_index==0 else 0
                    stops = tuple(max(0, round((f.left-MARGIN)*2)*50) for f in fragments)
                    pid = styles.paragraph(stops[0] if stops else 0, pitch, stops[1:], before)
                    p = xml.SubElement(body, HP+"p", id=str(next(ids)), paraPrIDRef=pid, styleIDRef="0",
                                       pageBreak="1" if page_index and row_index==0 else "0", columnBreak="0", merged="0")
                    if not row_index and (layout.rules or layout.artwork):
                        run = xml.SubElement(p, HP+"run", charPrIDRef=styles.char(5))
                        run.extend(images.get(id(layout), ()))
                        run.extend(_rule(rule, edges, next(shape_ids)) for edges in layout.rules)
                    if not fragments:
                        xml.SubElement(xml.SubElement(p, HP+"run", charPrIDRef=styles.char(5)), HP+"t")
                    for j, fragment in enumerate(fragments):
                        edge = fragments[j+1].left if j+1 < len(fragments) else width-MARGIN
                        # At the final tab, reserve room for the right margin and Hancom line metrics.
                        run = xml.SubElement(p, HP+"run", charPrIDRef=styles.fit(fragment, edge))
                        holder = xml.SubElement(run, HP+"t")
                        if j:
                            tab = xml.SubElement(holder, HP+"tab", width="0", leader="0", type="1")
                            tab.tail = fragment.text
                        else:
                            holder.text = fragment.text
            bodies[part] = body
        styles.finish()
        bodies["Contents/header.xml"] = header
        with ZipFile(target, "w", compression=ZIP_DEFLATED, compresslevel=5) as out:
            for name in template.namelist():
                value = xml.tostring(bodies[name], encoding="UTF-8", xml_declaration=True) if name in bodies else template.read(name)
                out.writestr(name, value, compress_type=ZIP_STORED if name=="mimetype" else ZIP_DEFLATED)


def write_source_hwpx(source, aligned, records, target, *, should_stop=None, step=None):
    """Compose selected source pages atomically. Source and aligned PDF stay read-only."""
    from ab_ocr.ocr import OCRCancelled
    from hwpx import HwpxDocument
    from .state import write_atomic_with

    def stop():
        if should_stop and should_stop():
            raise OCRCancelled("원본 배치 HWPX 만들기를 중지했습니다.")

    layouts = []
    for index, record in enumerate(records, 1):
        stop()
        number = record["pdf_page"]
        layout = _page_layout(source[number-1], aligned[number-1],
                              cover_position=number<=2 or number==len(source), record=record)
        layouts.append(layout)
        if step:
            step(index, len(records)+1)

    def build(path):
        with HwpxDocument.new() as package:
            _write_sections(package, layouts, path, stop)
        with ZipFile(path) as check:
            if check.testzip() is not None:
                raise ValueError("원본 배치 HWPX의 파일 구조가 손상되었습니다.")
            pages = 0
            for name in check.namelist():
                if name.startswith("Contents/section") and name.endswith(".xml"):
                    tree = xml.fromstring(check.read(name))
                    pages += 1 + sum(p.get("pageBreak")=="1" for p in tree.findall(HP+"p"))
            if pages != len(layouts):
                raise ValueError(f"원본 배치 HWPX 쪽 나눔 불일치 ({pages}/{len(layouts)})")
        stop()

    if not layouts:
        raise ValueError("원본 배치 HWPX에 넣을 판독 기록이 없습니다.")
    write_atomic_with(Path(target), build)
    if step:
        step(len(records)+1, len(records)+1)
    return len(layouts)
