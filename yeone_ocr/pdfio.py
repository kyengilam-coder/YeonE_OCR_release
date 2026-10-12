"""PDF 열기, 쪽 이미지 얻기(내장 스캔 이미지 우선), 기존 텍스트 레이어 추출.

PDF 대신 이미지(PNG·JPG·TIFF…)나 이미지 폴더를 주어도 된다. 이미지 한 장(여러 쪽 TIFF 는 쪽마다)을 한 쪽으로 하는
PDF 를 메모리에서 만들어 같은 길로 읽는다. JPEG 는 원래 바이트 그대로, 나머지는 PNG(무손실)로 넣으므로
쪽 이미지는 원본 비트맵 그대로 꺼내진다(재압축·재샘플 없음). 이미지에는 글자층이 없으므로 기존 OCR 비교는 없다.
EXIF 방향 태그가 있는 사진(휴대전화 세로 사진)은 JPEG 를 그대로 넣고 쪽 위에서 돌려 놓으며(뒤집힌 방향만 바로 세운 픽셀을 PNG 로),
16비트 회색조는 실제로 쓰인 비트(12비트 자료 등)의 위 8비트로 넣는다.
"""
from __future__ import annotations
import hashlib, io, os, re
from dataclasses import dataclass, field
from pathlib import Path
import numpy as np
import pymupdf
from PIL import Image, ImageOps

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif", ".jp2", ".j2k", ".jpx")
_IMG_CACHE: dict = {}            # 같은 이미지(들)로 만든 PDF 를 다시 만들지 않게: (경로·내용 지문) → PDF 바이트
_TURN = {3: 180, 6: 270, 8: 90}  # EXIF 방향(순수 회전) → insert_image 의 rotate(시계 반대 방향 각도)

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


def is_image(path: str | os.PathLike) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTS


def _natural_key(p: Path):
    """page2 가 page10 앞에 오게(숫자는 수로 비교)."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def image_sources(path: str | os.PathLike) -> list[Path]:
    """이미지 파일이면 그 한 장, 폴더면 그 안의 이미지 전부(이름의 숫자 순서), 그 밖은 빈 목록."""
    p = Path(path)
    if p.is_dir():
        return sorted((c for c in p.iterdir() if c.is_file() and is_image(c)), key=_natural_key)
    return [p] if is_image(p) else []


def is_image_source(path: str | os.PathLike) -> bool:
    return bool(image_sources(path)) or (Path(path).is_dir())


def _first_jpeg(im: Image.Image, raw: bytes) -> bytes:
    """넣을 JPEG 바이트. MPO(미리보기·HDR 이득 지도가 뒤에 붙은 사진)는 첫 그림의 바이트만(뒤에 붙은 것은 쪽에 쓰이지 않고 PDF 만 키운다)."""
    if im.format == "MPO":
        try:
            size = int(im.mpinfo[0xB002][0]["Size"])
        except Exception:
            size = 0
        if 0 < size < len(raw) and raw[size - 2:size] == b"\xff\xd9":     # 첫 그림이 EOI 로 끝나는지 확인하고 자른다
            return raw[:size]
    return raw


def images_to_pdf(paths: list[str | os.PathLike], default_dpi: int = 300, progress=None) -> bytes:
    """이미지들을 한 장에 한 쪽인 PDF 로. 여러 쪽 TIFF·GIF 는 프레임마다 한 쪽(MPO 의 미리보기·HDR 이득 지도는 쪽이 아니므로 첫 그림만).
    JPEG(한 장짜리)는 원래 바이트를 그대로 넣고, 나머지는 PNG(무손실)로 바꿔 넣는다. 쪽 크기는 이미지의 dpi 정보(없으면 300dpi)로.
    EXIF 방향 태그가 있으면 쪽은 바로 선 크기로 만들고, JPEG 의 순수 회전(3·6·8)은 원래 바이트를 쪽 위에서 돌려 놓고
    (page_image 가 놓인 방향대로 바로 세워 꺼낸다), 뒤집힌 방향(2·4·5·7)이나 JPEG 아닌 것은 바로 세운 픽셀을 PNG 로 넣는다.
    progress(끝난 장 수, 전체 장 수) 를 주면 이미지 한 장마다 부른다."""
    out = pymupdf.open()
    for i, path in enumerate(paths):
        raw = Path(path).read_bytes()
        with Image.open(io.BytesIO(raw)) as im:
            n = 1 if im.format == "MPO" else (getattr(im, "n_frames", 1) or 1)       # MPF 로 붙은 미리보기·이득 지도는 쪽이 아니다
            for k in range(n):
                im.seek(k)
                dpi = im.info.get("dpi") or (default_dpi, default_dpi)
                try:
                    dx, dy = float(dpi[0]) or default_dpi, float(dpi[1]) or default_dpi
                except (TypeError, ValueError, IndexError):
                    dx = dy = default_dpi
                if dx < 50 or dy < 50:                                   # 잘못 적힌 dpi(1·72 미만 등)는 무시
                    dx = dy = default_dpi
                try:
                    orient = im.getexif().get(0x0112, 1)
                except Exception:
                    orient = 1
                jpeg = im.format in ("JPEG", "MPO") and n == 1 and im.mode in ("L", "RGB", "CMYK")
                turn = _TURN.get(orient, 0) if jpeg else 0               # 휴대전화 세로 사진: JPEG 는 그대로 두고 쪽 위에서 돌린다(PNG 로 풀면 열 배 넘게 커진다)
                fr = ImageOps.exif_transpose(im) if orient in (2, 3, 4, 5, 6, 7, 8) and not turn else im   # 뒤집힌 방향(2·4·5·7)은 바로 세운 픽셀로
                if orient in (5, 6, 7, 8):                              # 90·270도면 가로·세로 dpi 도 바꾼다
                    dx, dy = dy, dx
                w, h = (im.height, im.width) if turn in (90, 270) else fr.size
                if jpeg and fr is im:
                    stream = _first_jpeg(im, raw)                       # 다시 압축하지 않는다(MPO 는 첫 그림의 바이트만)
                else:
                    fr = fr.convert("RGBA") if fr.mode in ("P", "LA", "PA") else fr
                    if fr.mode == "RGBA":                               # 투명 부분은 흰 바탕으로
                        bg = Image.new("RGB", fr.size, (255, 255, 255)); bg.paste(fr, mask=fr.split()[3]); fr = bg
                    elif fr.mode.startswith("I;16") or fr.mode == "I":  # 16·32비트 정수 회색조: convert("L") 은 255 에서 잘라 글자가 하얗게 사라진다
                        rows = [(y, min(fr.height, y + 128)) for y in range(0, fr.height, 128)]   # 128줄 띠씩(쪽 전체의 원시 바이트·정수 사본을 만들지 않게: 32비트면 화소당 12바이트 넘게 들었다)
                        band = lambda y0, y1: np.asarray(fr.crop((0, y0, fr.width, y1)))
                        hi = max([int(band(*r).max()) for r in rows] + [0]); s = max(8, hi.bit_length()) - 8
                        a = np.empty((fr.height, fr.width), np.uint8)
                        for y0, y1 in rows:
                            b = band(y0, y1)
                            a[y0:y1] = (np.clip(b, 0, None) if b.dtype.kind == "i" else b) >> s   # 32비트(I)의 음수는 0 으로. 실제로 쓰인 비트의 위 8비트(16비트 그릇에 담은 12비트 자료도 제 밝기로). 실수 임시 배열 없이 정수로만
                        fr = Image.fromarray(a)
                    elif fr.mode == "F":                                # 실수 회색조
                        a = np.asarray(fr, dtype=np.float32); hi = float(a.max()) if a.size else 0.0
                        if 0 < hi <= 1.0:
                            a = a * 255.0                               # 0~1 실수
                        elif hi > 255:
                            a = a * (255.0 / hi)                        # 그 밖의 넓은 범위는 최댓값에 맞춰
                        fr = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
                    elif fr.mode == "1":
                        fr = fr.convert("L")
                    elif fr.mode not in ("L", "RGB"):
                        fr = fr.convert("RGB")
                    buf = io.BytesIO(); fr.save(buf, format="PNG"); stream = buf.getvalue()
                page = out.new_page(width=w * 72.0 / dx, height=h * 72.0 / dy)
                page.insert_image(page.rect, stream=stream, rotate=turn)
        if progress:
            progress(i + 1, len(paths))
    if len(out) == 0:
        raise ValueError("이미지가 없습니다")
    data = out.tobytes(garbage=0, deflate=False)
    out.close()
    return data


def save_images_as_pdf(paths: list[str | os.PathLike], out_path: str | os.PathLike, progress=None) -> str:
    """여러 이미지를 고른 순서대로 한 PDF 파일로 저장한다(창 프로그램에서 이미지를 여러 장 고를 때). progress 는 images_to_pdf 와 같다."""
    Path(out_path).write_bytes(images_to_pdf(paths, progress=progress))
    return str(out_path)


def open_pdf(path: str) -> pymupdf.Document:
    """PDF 를 연다. 이미지 파일·이미지 폴더면 이미지로 만든 PDF 를 메모리에서 연다."""
    srcs = image_sources(path)
    if not srcs:
        if Path(path).is_dir():
            raise ValueError(f"폴더에 이미지가 없습니다: {path} (읽는 형식: {', '.join(IMAGE_EXTS)})")
        return pymupdf.open(path)
    key = tuple((str(p.resolve()), hashlib.sha256(p.read_bytes()).digest()) for p in srcs)
    data = _IMG_CACHE.get(key)
    if data is None:
        data = images_to_pdf(srcs)
        _IMG_CACHE.clear(); _IMG_CACHE[key] = data                    # 하나만 기억(큰 책을 메모리에 여럿 두지 않게)
    return pymupdf.open("pdf", data)


def _turn(m) -> int | None:
    """내장 이미지가 놓인 행렬로 본 방향: 비트맵을 시계 반대 방향으로 몇 도 돌려야 쪽에 보이는 대로인지(0·90·180·270).
    뒤집히거나 기울어 놓였으면 None(그 쪽은 렌더한다)."""
    a, b, c, d = (float(v) for v in tuple(m)[:4])
    e = 1e-3 * max(abs(a), abs(b), abs(c), abs(d), 1e-9)
    if abs(b) <= e and abs(c) <= e:
        return 0 if a > 0 and d > 0 else 180 if a < 0 and d < 0 else None
    if abs(a) <= e and abs(d) <= e:
        return 90 if b < 0 < c else 270 if c < 0 < b else None
    return None


def _embedded(page: pymupdf.Page, prefer_embedded: bool = True) -> tuple[int, pymupdf.Rect, int] | None:
    """쪽이 판면 전체를 덮는 스캔 이미지 한 장이고 돌려서만 놓였으면(뒤집힘·기울임 없이) (xref, 놓인 사각형, 돌림 각도), 아니면 None(렌더).
    돌림 각도에는 쪽의 /Rotate 도 넣는다(Acrobat 의 '쪽 돌리기' 로 바로 세운 거꾸로 된 스캔도 화면에 보이는 대로). /Rotate 가 90·270 인 쪽은 렌더하고,
    놓인 사각형은 쪽에 보이는 좌표(렌더하는 쪽의 page.rect 와 같은 틀)로 준다. page_image·page_frame 이 같은 판단을 쓴다(쪽 이미지와 그 틀이 늘 짝이 맞게)."""
    imgs = page.get_images(full=True)
    if not (prefer_embedded and len(imgs) == 1) or page.rotation % 180 or imgs[0][1]:
        return None
    rects = page.get_image_rects(imgs[0][0], transform=True)               # 놓인 행렬은 /Rotate 를 빼고 본 것
    if len(rects) == 1:
        ir, m = rects[0]
        shown = ir * page.rotation_matrix
        # CropBox 밖의 숨은 이미지, 작은 삽입 그림, 그림 위에 그린 선·주석은 전체 페이지 렌더로 보존한다.
        bounds = page.rect
        covered = (shown.x0 >= bounds.x0 - 0.5 and shown.y0 >= bounds.y0 - 0.5 and
                   shown.x1 <= bounds.x1 + 0.5 and shown.y1 <= bounds.y1 + 0.5 and
                   shown.width >= bounds.width * 0.9 and shown.height >= bounds.height * 0.9)
        if covered and not page.get_drawings(extended=True) and not page.first_annot and (t := _turn(m)) is not None:
            # 기존 OCR 숨은 글자층은 허용하되 실제로 보이는 PDF 글자는 합성해야 한다.
            if any(span.get("type", 0) != 3 and span.get("opacity", 1) > 0 for span in page.get_texttrace()):
                return None
            return imgs[0][0], ir * page.rotation_matrix, (t - page.rotation) % 360     # /Rotate 는 시계 방향
    return None


def page_image(doc: pymupdf.Document, pdf_page: int, dpi: int = 300, prefer_embedded: bool = True) -> tuple[Image.Image, str]:
    """쪽 이미지. 쪽이 스캔 이미지 한 장으로 되어 있으면 그 원본 비트맵을 그대로 꺼내고(재압축·재샘플 없음, 쪽 위에서
    돌려 놓였으면 놓인 방향대로 바로 세워서), 아니면 dpi로 렌더한다. 돌려주는 설명 문자열은 기록용."""
    page = doc[pdf_page - 1]
    emb = _embedded(page, prefer_embedded)                  # 스캔이 쪽 전체를 덮는지(이미지 한 장이 판면 전체인지). 아니면 렌더
    if emb:
        info = doc.extract_image(emb[0])
        im = Image.open(io.BytesIO(info["image"]))
        im.load()
        if im.format == "MPO":                              # 첫 그림만 넣었어도 MPF 표지가 남아 여러 장으로 보이지 않게 한 장으로
            im = im.copy()
        if emb[2]:                                          # 휴대전화 세로 사진 등: 쪽 위에서 돌려 놓인 JPEG(또는 /Rotate 180 인 쪽)
            im = im.transpose({90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_270}[emb[2]])
            if emb[2] != 180 and "dpi" in im.info:
                im.info["dpi"] = tuple(im.info["dpi"])[::-1]   # 가로·세로 dpi 도 돌린 대로
        for k in ("exif", "xmp", "XML:com.adobe.xmp"):      # PDF 는 EXIF 방향을 쓰지 않는다: 쪽에 보이는 대로인 이 비트맵을 받는 쪽이 방향 태그로 한 번 더 돌리지 않게
            im.info.pop(k, None)
        if im.mode not in ("L", "RGB"):
            im = im.convert("RGB")
        return im, f"내장 스캔 {im.width}x{im.height} {info['ext']}" + (f" ({emb[2]}도 돌려 놓임)" if emb[2] else "")
    pix = page.get_pixmap(dpi=dpi)
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return im, f"렌더 {dpi}dpi {im.width}x{im.height}"


def page_frame(doc: pymupdf.Document, pdf_page: int, prefer_embedded: bool = True) -> tuple[float, float, float, float]:
    """쪽 이미지(page_image 가 돌려주는 것)가 PDF 좌표(pt)의 어느 틀을 덮는지: (x0, y0, 너비, 높이).
    내장 스캔을 그대로 쓰는 쪽이면 그 이미지가 놓인 사각형(쪽과 최대 10% 다를 수 있음, 돌려 놓였거나 /Rotate 180 이면 쪽에 보이는 대로의 사각형), 렌더하는 쪽이면 쪽 전체.
    글자층 좌표를 이미지 비율로 바꿀 때 이 틀로 나눠야 글자 하나(쪽의 2~3%)만큼 어긋나지 않는다."""
    page = doc[pdf_page - 1]
    emb = _embedded(page, prefer_embedded)
    r = emb[1] if emb else page.rect
    return (float(r.x0), float(r.y0), float(r.width), float(r.height))


@dataclass
class TextLayer:
    char_count: int = 0
    vertical_columns: list[dict] = field(default_factory=list)   # [{"x":pt, "y0":pt, "y1":pt, "text":...}] 오른쪽→왼쪽
    horizontal_lines: list[dict] = field(default_factory=list)   # [{"bbox":[...], "text":...}]
    page_label: str = ""
    page_width: float = 0
    page_height: float = 0

    def plain(self) -> str:
        """비교용 평문. 세로 열이 있으면 열 순서(오른쪽→왼쪽), 없으면(가로쓰기 책) 가로 줄을 위→아래 순서로."""
        if self.vertical_columns:
            return "\n".join(c["text"] for c in self.vertical_columns)
        lines = sorted(self.horizontal_lines, key=lambda l: (round(l["bbox"][1] / 3), l["bbox"][0]))
        return "\n".join(l["text"] for l in lines)


def _shown(page: pymupdf.Page):
    """글자층 좌표는 /Rotate 를 뺀 쪽 좌표로 나온다. 그것을 쪽에 보이는 좌표(page.rect·렌더·page_frame 과 같은 틀)로 바꾸는 함수 둘
    (상자 [x0,y0,x1,y1], 줄 방향 벡터). /Rotate 가 없으면 그대로."""
    if not page.rotation:
        return (lambda bb: list(bb)), (lambda d: tuple(d))
    m = page.rotation_matrix; o = pymupdf.Point(0, 0) * m
    def box(bb):
        r = pymupdf.Rect(bb) * m
        return [r.x0, r.y0, r.x1, r.y1]
    def way(d):
        q = pymupdf.Point(d) * m - o
        return (round(q.x, 6) + 0.0, round(q.y, 6) + 0.0)
    return box, way


def text_layer(doc: pymupdf.Document, pdf_page: int, col_tol_pt: float = 4.0) -> TextLayer:
    """기존 OCR 텍스트 레이어를 세로 열 단위로 모은다. 비교·보조 자료일 뿐 정답이 아니다(지침 20)."""
    page = doc[pdf_page - 1]
    tl = TextLayer(page_width=page.rect.width, page_height=page.rect.height)
    d = page.get_text("dict")
    box, way = _shown(page)                                  # /Rotate 쪽: 쪽 이미지와 같은 틀로
    vlines, hlines = [], []
    for b in d["blocks"]:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            txt = "".join(s["text"] for s in l["spans"])
            if not txt.strip():
                continue
            if way(l["dir"]) == (0.0, 1.0):
                vlines.append((box(l["bbox"]), txt))
            else:
                hlines.append((box(l["bbox"]), txt))
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


def layer_chars(doc: pymupdf.Document, pdf_page: int, col_tol_pt: float = 4.0) -> list[dict]:
    """글자층을 글자 단위 상자까지 모은다(3차 글자 판별이 조각을 정확한 자리에서 자르는 데 쓴다).
    돌려주는 목록의 원소: {"dir": "v"|"h", "bbox": [x0,y0,x1,y1](pt), "chars": [[글자, [x0,y0,x1,y1]], …]}.
    세로 줄(dir=(0,1))은 text_layer 처럼 x 중심으로 묶어 열 하나로(글자는 위→아래), 가로 줄은 y 중심으로 묶어 줄 하나로(왼→오른쪽)."""
    page = doc[pdf_page - 1]
    d = page.get_text("rawdict")
    box, way = _shown(page)                                  # /Rotate 쪽: 쪽 이미지·page_frame 과 같은 틀로(3차 조각이 반대편 구석을 자르지 않게)
    vparts: list[tuple[float, list]] = []; hparts: list[tuple[float, list]] = []
    for b in d["blocks"]:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            chars = [[c["c"], [round(v, 2) for v in box(c["bbox"])]] for s in l["spans"] for c in s["chars"] if c["c"] and not c["c"].isspace()]
            if not chars:
                continue
            bb = box(l["bbox"])
            if way(l["dir"]) == (0.0, 1.0):
                vparts.append(((bb[0] + bb[2]) / 2, chars))
            else:
                hparts.append(((bb[1] + bb[3]) / 2, chars))
    out: list[dict] = []
    groups: list[dict] = []
    for xc, chars in sorted(vparts, key=lambda v: -v[0]):              # 오른쪽 열부터
        for g in groups:
            if abs(g["c"] - xc) <= col_tol_pt:
                g["chars"].extend(chars); g["c"] = (g["c"] * g["n"] + xc) / (g["n"] + 1); g["n"] += 1
                break
        else:
            groups.append({"c": xc, "n": 1, "chars": list(chars)})
    for g in sorted(groups, key=lambda g: -g["c"]):
        chars = sorted(g["chars"], key=lambda c: c[1][1])
        out.append({"dir": "v", "chars": chars, "bbox": [min(c[1][0] for c in chars), min(c[1][1] for c in chars), max(c[1][2] for c in chars), max(c[1][3] for c in chars)]})
    groups = []
    for yc, chars in sorted(hparts, key=lambda v: v[0]):               # 위 줄부터
        for g in groups:
            if abs(g["c"] - yc) <= col_tol_pt:
                g["chars"].extend(chars); g["c"] = (g["c"] * g["n"] + yc) / (g["n"] + 1); g["n"] += 1
                break
        else:
            groups.append({"c": yc, "n": 1, "chars": list(chars)})
    for g in sorted(groups, key=lambda g: g["c"]):
        chars = sorted(g["chars"], key=lambda c: c[1][0])
        out.append({"dir": "h", "chars": chars, "bbox": [min(c[1][0] for c in chars), min(c[1][1] for c in chars), max(c[1][2] for c in chars), max(c[1][3] for c in chars)]})
    return out
