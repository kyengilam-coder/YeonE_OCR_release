"""PDF 열기, 쪽 이미지 얻기(내장 스캔 이미지 우선), 기존 텍스트 레이어 추출."""
from __future__ import annotations
import io, re
from dataclasses import dataclass, field
import pymupdf
from PIL import Image

PAGE_LABEL_RE = re.compile(r"^\s*[-–—ー一]\s*(\d{1,4})\s*[-–—ー一]\s*$")


def parse_pages(spec: str | None, n: int) -> list[int]:
    """'1-5,8' → [1,2,3,4,5,8]. 비면 전체."""
    if not spec:
        return list(range(1, n + 1))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b or n)
            out.extend(range(max(1, a), min(n, b) + 1))
        else:
            v = int(part)
            if 1 <= v <= n:
                out.append(v)
    return sorted(set(out))


def open_pdf(path: str) -> pymupdf.Document:
    return pymupdf.open(path)


def page_image(doc: pymupdf.Document, pdf_page: int, dpi: int = 300, prefer_embedded: bool = True) -> tuple[Image.Image, str]:
    """쪽 이미지. 쪽이 스캔 이미지 한 장으로 되어 있으면 그 원본 비트맵을 그대로 꺼내고(재압축·재샘플 없음),
    아니면 dpi로 렌더한다. 돌려주는 설명 문자열은 기록용."""
    page = doc[pdf_page - 1]
    imgs = page.get_images(full=True)
    if prefer_embedded and len(imgs) == 1:
        info = doc.extract_image(imgs[0][0])
        im = Image.open(io.BytesIO(info["image"]))
        im.load()
        # 스캔이 쪽 전체를 덮는지(이미지 한 장이 판면 전체인지) 확인. 아니면 렌더로 되돌아간다.
        rects = page.get_image_rects(imgs[0][0])
        if rects and rects[0].width >= page.rect.width * 0.9 and rects[0].height >= page.rect.height * 0.9:
            if im.mode not in ("L", "RGB"):
                im = im.convert("RGB")
            return im, f"내장 스캔 {im.width}x{im.height} {info['ext']}"
    pix = page.get_pixmap(dpi=dpi)
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return im, f"렌더 {dpi}dpi {im.width}x{im.height}"


@dataclass
class TextLayer:
    char_count: int = 0
    vertical_columns: list[dict] = field(default_factory=list)   # [{"x":pt, "y0":pt, "y1":pt, "text":...}] 오른쪽→왼쪽
    horizontal_lines: list[dict] = field(default_factory=list)   # [{"bbox":[...], "text":...}]
    page_label: str = ""
    page_width: float = 0
    page_height: float = 0

    def plain(self) -> str:
        return "\n".join(c["text"] for c in self.vertical_columns)


def text_layer(doc: pymupdf.Document, pdf_page: int, col_tol_pt: float = 4.0) -> TextLayer:
    """기존 OCR 텍스트 레이어를 세로 열 단위로 모은다. 비교·보조 자료일 뿐 정답이 아니다(지침 20)."""
    page = doc[pdf_page - 1]
    tl = TextLayer(page_width=page.rect.width, page_height=page.rect.height)
    d = page.get_text("dict")
    vlines, hlines = [], []
    for b in d["blocks"]:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            txt = "".join(s["text"] for s in l["spans"])
            if not txt.strip():
                continue
            if tuple(l["dir"]) == (0.0, 1.0):
                vlines.append((l["bbox"], txt))
            else:
                hlines.append((l["bbox"], txt))
    tl.char_count = sum(len(t.strip()) for _, t in vlines + hlines)
    # 세로 줄을 x 중심으로 묶어 열을 만든다.
    vlines.sort(key=lambda v: -(v[0][0] + v[0][2]) / 2)
    cols: list[dict] = []
    for bb, txt in vlines:
        xc = (bb[0] + bb[2]) / 2
        for c in cols:
            if abs(c["x"] - xc) <= col_tol_pt:
                c["lines"].append((bb[1], txt)); c["y0"] = min(c["y0"], bb[1]); c["y1"] = max(c["y1"], bb[3])
                c["x"] = (c["x"] * (len(c["lines"]) - 1) + xc) / len(c["lines"])
                break
        else:
            cols.append({"x": xc, "y0": bb[1], "y1": bb[3], "lines": [(bb[1], txt)]})
    cols.sort(key=lambda c: -c["x"])
    for c in cols:
        c["text"] = "".join(t.replace("\n", "") for _, t in sorted(c["lines"]))
        del c["lines"]
    tl.vertical_columns = cols
    for bb, txt in hlines:
        tl.horizontal_lines.append({"bbox": [round(v, 1) for v in bb], "text": txt})
        m = PAGE_LABEL_RE.match(txt)
        if m and bb[1] > page.rect.height * 0.8:
            tl.page_label = txt.strip()
    return tl
