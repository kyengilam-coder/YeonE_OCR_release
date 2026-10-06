"""영역 잘라내기, 외곽 잘림 검사, 확대 조각, 도판 자동 감지(OpenCV, 보조)."""
from __future__ import annotations
import io
import numpy as np
from PIL import Image

Frac = tuple[float, float, float, float]   # (x0,y0,x1,y1) 0~1


def to_png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()


def upscale(im: Image.Image, factor: float) -> Image.Image:
    if factor == 1:
        return im
    return im.resize((round(im.width * factor), round(im.height * factor)), Image.LANCZOS)


def crop_frac(im: Image.Image, fr: Frac, pad: float = 0.0) -> Image.Image:
    x0, y0, x1, y1 = fr
    W, H = im.size
    box = (max(0, int((x0 - pad) * W)), max(0, int((y0 - pad) * H)),
           min(W, int((x1 + pad) * W)), min(H, int((y1 + pad) * H)))
    if box[2] - box[0] < 4 or box[3] - box[1] < 4:
        box = (0, 0, min(W, 4), min(H, 4))
    return im.crop(box)


def edge_ink(im: Image.Image, band: int = 6, thr: int = 110) -> dict[str, float]:
    """잘라낸 그림 네 변의 먹(어두운 픽셀) 비율. 0이 아니면 글자·선이 잘렸을 수 있다."""
    a = np.asarray(im.convert("L")) < thr
    if a.size == 0:
        return {"top": 0, "bottom": 0, "left": 0, "right": 0}
    band = max(1, min(band, a.shape[0] // 2, a.shape[1] // 2))
    return {"top": float(a[:band].mean()), "bottom": float(a[-band:].mean()),
            "left": float(a[:, :band].mean()), "right": float(a[:, -band:].mean())}


def fit_figure_bbox(im: Image.Image, fr: Frac, pad: float = 0.01, thr: float = 0.0005,
                    expand_steps: int = 10, shrink_steps: int = 10, step: float = 0.003) -> dict:
    """도판 좌표를 외곽 잘림이 없도록 맞춘다.
    먼저 먹이 닿은 변을 바깥으로 넓혀 본다(잘린 외곽 글자·기호를 되살림). 그래도 닿으면 안쪽으로 당긴다
    (이웃 영역의 글자가 들어온 경우). 끝까지 닿으면 touching에 남겨 사람 확인으로 넘긴다."""
    bb = list(fr)
    def touching(b):
        e = edge_ink(crop_frac(im, tuple(b), pad))
        return [k for k, v in e.items() if v > thr]
    history = []
    bad = touching(bb)
    for _ in range(expand_steps):
        if not bad: break
        for k in bad:
            if k == "left": bb[0] = max(0, bb[0] - step)
            if k == "right": bb[2] = min(1, bb[2] + step)
            if k == "top": bb[1] = max(0, bb[1] - step)
            if k == "bottom": bb[3] = min(1, bb[3] + step)
        history.append(("넓힘", list(bad))); bad = touching(bb)
    for _ in range(shrink_steps):
        if not bad: break
        for k in bad:
            if k == "left": bb[0] += step
            if k == "right": bb[2] -= step
            if k == "top": bb[1] += step
            if k == "bottom": bb[3] -= step
        history.append(("좁힘", list(bad))); bad = touching(bb)
    return {"bbox": tuple(round(v, 4) for v in bb), "touching": bad, "steps": len(history), "history": history}


def expand_until_clean(im: Image.Image, fr: Frac, pad: float = 0.0, thr: float = 0.0005,
                       step: float = 0.004, max_steps: int = 10) -> dict:
    """문자 영역 좌표를 바깥으로만 넓혀 네 변에 먹이 닿지 않게 한다(2차 판독 조각이 열의 마지막 글자나 가장자리 열을
    잘라 먹지 않도록). 모델이 준 좌표는 글자 반 개쯤 안쪽으로 들어오는 일이 잦다."""
    bb = list(fr)
    def touching(b):
        e = edge_ink(crop_frac(im, tuple(b), pad))
        return [k for k, v in e.items() if v > thr]
    bad = touching(bb); steps = 0
    while bad and steps < max_steps:
        for k in bad:
            if k == "left": bb[0] = max(0.0, bb[0] - step)
            if k == "right": bb[2] = min(1.0, bb[2] + step)
            if k == "top": bb[1] = max(0.0, bb[1] - step)
            if k == "bottom": bb[3] = min(1.0, bb[3] + step)
        steps += 1; bad = touching(bb)
    return {"bbox": tuple(round(v, 4) for v in bb), "touching": bad, "steps": steps}


def uncovered_ink_boxes(im: Image.Image, regions: list[Frac], thr: int = 110, margin: float = 0.004,
                        min_w: float = 0.006, min_h: float = 0.03, density: float = 0.004) -> list[Frac]:
    """어느 영역에도 들지 않은 먹 기둥(세로 글 열)을 찾는다. 모델이 영역 좌표를 좁게 잡아 열 하나를 통째로 빠뜨린 경우를
    잡기 위한 것으로, 지침 36 '원본에 있는 본문이 누락되지 않아야 한다' 의 기계적 보조 수단이다."""
    a = np.asarray(im.convert("L")) < thr
    H, W = a.shape
    mask = np.zeros_like(a)
    for (x0, y0, x1, y1) in regions:
        mask[max(0, int((y0 - margin) * H)):min(H, int((y1 + margin) * H) + 1),
             max(0, int((x0 - margin) * W)):min(W, int((x1 + margin) * W) + 1)] = True
    rest = a & ~mask
    col = rest.mean(axis=0)
    on = col > density
    boxes: list[Frac] = []
    x = 0
    while x < W:
        if not on[x]:
            x += 1; continue
        x2 = x
        while x2 < W and on[x2]:
            x2 += 1
        if (x2 - x) >= min_w * W:
            rows = np.where(rest[:, x:x2].any(axis=1))[0]
            if len(rows) and (rows[-1] - rows[0]) >= min_h * H:
                boxes.append((round(x / W, 4), round(rows[0] / H, 4), round(x2 / W, 4), round((rows[-1] + 1) / H, 4)))
        x = x2
    return boxes


def attach_uncovered(boxes: list[Frac], text_regions: list[tuple[str, Frac]], max_gap: float = 0.06, min_overlap: float = 0.5) -> tuple[dict[str, Frac], list[Frac]]:
    """빠진 먹 기둥을 세로로 겹치고 가로로 가까운 문자 영역에 붙인다. 붙일 곳이 없으면 따로 돌려준다."""
    grown: dict[str, Frac] = {}
    orphans: list[Frac] = []
    for b in boxes:
        best, best_gap = None, None
        for rid, r in text_regions:
            r = grown.get(rid, r)
            oy = max(0.0, min(b[3], r[3]) - max(b[1], r[1]))
            if oy / max(1e-9, b[3] - b[1]) < min_overlap:
                continue
            gap = max(0.0, max(b[0], r[0]) - min(b[2], r[2]))
            if gap <= max_gap and (best_gap is None or gap < best_gap):
                best, best_gap = rid, gap
        if best is None:
            orphans.append(b); continue
        r = grown.get(best, dict(text_regions)[best])
        grown[best] = (min(r[0], b[0]), min(r[1], b[1]), max(r[2], b[2]), max(r[3], b[3]))
    return grown, orphans


def detect_figures(im: Image.Image, min_w: float = 0.22, min_h: float = 0.15) -> list[Frac]:
    """큰 연결 성분(원형 나경도의 테두리, 계통도의 선)으로 도판 후보 좌표를 찾는다. OpenCV가 없으면 빈 목록.
    모델이 준 좌표를 교차 확인하거나, 모델 없이(mock) 돌릴 때 쓰는 보조 수단이다."""
    try:
        import cv2
    except Exception:
        return []
    g = np.asarray(im.convert("L"))
    _, b = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(b, connectivity=8)
    H, W = g.shape
    cands = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if w >= min_w * W and h >= min_h * H:
            cands.append([x / W, y / H, (x + w) / W, (y + h) / H])
    # 겹치는 후보 합치기
    merged: list[list[float]] = []
    for c in sorted(cands, key=lambda c: -(c[2] - c[0]) * (c[3] - c[1])):
        for m in merged:
            if not (c[2] < m[0] or c[0] > m[2] or c[3] < m[1] or c[1] > m[3]):
                m[0], m[1], m[2], m[3] = min(m[0], c[0]), min(m[1], c[1]), max(m[2], c[2]), max(m[3], c[3])
                break
        else:
            merged.append(c)
    return [tuple(round(v, 4) for v in m) for m in merged]


def overlap_ratio(a: Frac, b: Frac) -> float:
    """a가 b와 겹치는 넓이 / a의 넓이."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max(1e-9, (a[2] - a[0]) * (a[3] - a[1]))
    return ix * iy / area
