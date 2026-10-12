"""영역 잘라내기, 외곽 잘림 검사, 확대 조각, 도판 자동 감지(OpenCV, 보조)."""
from __future__ import annotations
import io
import numpy as np
from PIL import Image

Frac = tuple[float, float, float, float]   # (x0,y0,x1,y1) 0~1


def to_png_bytes(im: Image.Image) -> bytes:
    """모델에 보낼 PNG(무손실). compress_level=1: 기본(6)보다 ~3배 빠르고 ~15% 클 뿐 — 쪽당 CPU 의 대부분이 PNG 인코딩이다."""
    buf = io.BytesIO(); im.save(buf, "PNG", compress_level=1); return buf.getvalue()


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


def grow_to_components(im: Image.Image, fr: Frac, min_frac: float = 0.12, max_area_mult: float = 4.5) -> Frac:
    """모델 좌표가 도판을 크게 잘랐을 때(원형 도판의 반쪽만 잡는 등) 좌표를 도판 전체로 넓힌다.
    좌표 테두리를 가로지르는 연결 성분(먹 덩어리) 가운데 큰 것(쪽 너비·높이의 min_frac 이상: 도판의 테두리·선)을 모두 품게 한다.
    글자는 성분이 작아 끌려오지 않고, 테두리를 건드리지 않는 판 테두리(版框)도 끌려오지 않는다. 쪽 전체만 한 성분(스캔 검은 가장자리)과
    넓이가 max_area_mult 배를 넘는 확장은 믿지 않고 그대로 둔다. OpenCV 가 없으면 그대로."""
    try:
        import cv2
    except Exception:
        return fr
    g = np.asarray(im.convert("L"))
    H, W = g.shape
    x0, y0 = max(0, int(fr[0] * W)), max(0, int(fr[1] * H))
    x1, y1 = min(W - 1, int(fr[2] * W)), min(H - 1, int(fr[3] * H))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return fr
    _, b = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(b, connectivity=8)
    edge = np.concatenate([labels[y0, x0:x1 + 1], labels[y1, x0:x1 + 1], labels[y0:y1 + 1, x0], labels[y0:y1 + 1, x1]])
    nx0, ny0, nx1, ny1 = x0, y0, x1, y1
    for lab in set(np.unique(edge).tolist()) - {0}:
        x, y, w, h, _area = stats[lab]
        if w < min_frac * W and h < min_frac * H:
            continue                                   # 글자·작은 기호
        if w >= 0.9 * W and h >= 0.9 * H:
            continue                                   # 쪽 전체만 한 덩어리(스캔 가장자리 등)
        nx0, ny0, nx1, ny1 = min(nx0, x), min(ny0, y), max(nx1, x + w), max(ny1, y + h)
    if (nx0, ny0, nx1, ny1) == (x0, y0, x1, y1):
        return fr
    if (nx1 - nx0) * (ny1 - ny0) > max_area_mult * (x1 - x0) * (y1 - y0):
        return fr
    return (round(min(fr[0], nx0 / W), 4), round(min(fr[1], ny0 / H), 4), round(max(fr[2], nx1 / W), 4), round(max(fr[3], ny1 / H), 4))


ALL_SIDES = ("left", "right", "top", "bottom")


def expand_until_clean(im: Image.Image, fr: Frac, pad: float = 0.0, thr: float = 0.0005,
                       step: float = 0.004, max_steps: int = 10, sides: tuple[str, ...] = ALL_SIDES) -> dict:
    """문자 영역 좌표를 바깥으로만 넓혀 네 변에 먹이 닿지 않게 한다(2차 판독 조각이 열의 마지막 글자나 가장자리 열을
    잘라 먹지 않도록). 모델이 준 좌표는 글자 반 개쯤 안쪽으로 들어오는 일이 잦다.
    sides 로 넓힐 변을 제한한다 — 가로쓰기 책은 줄 간격이 좁아 위·아래로 넓히면 이웃 줄을 끌어들이므로 좌우만 넓힌다."""
    bb = list(fr)
    def touching(b):
        e = edge_ink(crop_frac(im, tuple(b), pad))
        return [k for k, v in e.items() if v > thr and k in sides]
    bad = touching(bb); steps = 0
    while bad and steps < max_steps:
        for k in bad:
            if k == "left": bb[0] = max(0.0, bb[0] - step)
            if k == "right": bb[2] = min(1.0, bb[2] + step)
            if k == "top": bb[1] = max(0.0, bb[1] - step)
            if k == "bottom": bb[3] = min(1.0, bb[3] + step)
        steps += 1; bad = touching(bb)
    return {"bbox": tuple(round(v, 4) for v in bb), "touching": bad, "steps": steps}


def _profile(im: Image.Image, fr: Frac, axis: str, thr: int = 110) -> np.ndarray:
    """fr 안의 먹 비율 프로파일. axis='y' 면 줄마다(가로 글줄 찾기), 'x' 면 세로줄마다(세로 글 열 찾기)."""
    a = np.asarray(im.convert("L")) < thr
    H, W = a.shape
    x0, y0, x1, y1 = fr
    xs, xe = max(0, int(x0 * W)), min(W, max(int(x0 * W) + 1, int(x1 * W)))
    ys, ye = max(0, int(y0 * H)), min(H, max(int(y0 * H) + 1, int(y1 * H)))
    if axis == "y":
        return a[:, xs:xe].mean(axis=1) if xe > xs else np.zeros(H)
    return a[ys:ye, :].mean(axis=0) if ye > ys else np.zeros(W)


def snap_to_gaps(im: Image.Image, fr: Frac, axis: str = "y", density: float = 0.002, inset_px: int = 2, max_move: float = 0.03) -> Frac:
    """영역의 두 변(axis='y' 면 위·아래, 'x' 면 왼·오른쪽)을 글줄 사이 공백에 맞춘다.
    각 변에서 안쪽으로 먹이 나올 때까지 간 뒤(첫 글줄), 다시 바깥으로 먹이 없는 줄까지 되돌아간다(그 글줄 바로 밖 공백).
    가로쓰기 책에서 2차 조각이 이웃 줄을 끌어들여 1차 36자 대 2차 252자가 되던 과확장을 막는다. 움직임은 max_move(비율) 안으로 제한."""
    prof = _profile(im, fr, axis)
    n = len(prof)
    x0, y0, x1, y1 = fr
    lo, hi = (y0, y1) if axis == "y" else (x0, x1)
    def edge(pos: float, inward: int) -> float:
        c = i = min(n - 1, max(0, int(pos * n)))
        lim = max(1, int(max_move * n))
        if prof[i] > density:
            # 변이 먹 띠(글줄) 한가운데를 지난다. 그 띠가 주로 영역 바깥에 있으면 이웃 줄이니 건너뛰고, 주로 안쪽이면 영역의 첫 줄이다.
            s0 = e0 = i
            while s0 - 1 >= 0 and prof[s0 - 1] > density: s0 -= 1
            while e0 + 1 < n and prof[e0 + 1] > density: e0 += 1
            inside = (e0 - c + 1) if inward > 0 else (c - s0 + 1)
            if inside < (e0 - s0 + 1) / 2:
                i = e0 + 1 if inward > 0 else s0 - 1
        while 0 <= i < n and prof[i] <= density and abs(i - c) < lim:      # 안쪽으로: 먹이 나올 때까지(영역의 첫 글줄)
            i += inward
        while 0 <= i < n and prof[i] > density and abs(i - c) < lim:       # 바깥으로: 그 글줄 바로 밖 공백까지
            i -= inward
        i = min(n - 1, max(0, i - inward * inset_px))                        # 공백 안쪽으로 몇 px 여유
        if abs(i - c) >= lim:
            return pos
        return i / n
    lo2, hi2 = edge(lo, +1), edge(hi, -1)
    if hi2 - lo2 < 0.002:
        return fr
    return (x0, round(lo2, 4), x1, round(hi2, 4)) if axis == "y" else (round(lo2, 4), y0, round(hi2, 4), y1)


def split_bands(im: Image.Image, axis: str = "y", max_bands: int = 8, density: float = 0.002, min_band_px: int = 4,
                reverse: bool = False) -> list[tuple[int, int]]:
    """잘라낸 조각 안의 글줄(axis='y') 또는 글 열(axis='x')을 투영으로 찾아 max_bands 개씩 묶은 구간(픽셀, 시작·끝)을 돌려준다.
    구간 경계는 글줄 사이 공백의 가운데다. 글줄이 max_bands 이하면 조각 전체 하나. reverse=True 면 끝에서부터 묶는다(세로쓰기: 오른쪽 열부터)."""
    prof = _profile(im, (0.0, 0.0, 1.0, 1.0), axis)
    n = len(prof)
    on = prof > density
    bands: list[list[int]] = []
    i = 0
    while i < n:
        if not on[i]:
            i += 1; continue
        j = i
        while j < n and on[j]:
            j += 1
        if j - i >= min_band_px:
            bands.append([i, j])
        i = j
    if len(bands) <= max_bands:
        return [(0, n)]
    if reverse:
        bands = bands[::-1]
    groups = [bands[k:k + max_bands] for k in range(0, len(bands), max_bands)]
    if len(groups) > 1 and len(groups[-1]) == 1:            # 끝에 한 줄만 남으면 앞 묶음에 붙인다
        groups[-2].extend(groups.pop())
    if reverse:
        groups = [g[::-1] for g in groups][::-1]
    out: list[tuple[int, int]] = []
    for gi, g in enumerate(groups):
        first, last = min(b[0] for b in g), max(b[1] for b in g)
        if gi == 0:
            start = 0
        else:
            prev_last = max(b[1] for b in groups[gi - 1]); start = (prev_last + first) // 2
        if gi == len(groups) - 1:
            end = n
        else:
            next_first = min(b[0] for b in groups[gi + 1]); end = (last + next_first) // 2
        out.append((start, end))
    return out


def _swap(fr: Frac) -> Frac:
    """(x0,y0,x1,y1) ↔ (y0,x0,y1,x1). 가로쓰기용 계산을 세로쓰기 코드로 돌리기 위한 전치."""
    return (fr[1], fr[0], fr[3], fr[2])


def uncovered_ink_boxes(im: Image.Image, regions: list[Frac], thr: int = 110, margin: float = 0.008,
                        min_w: float = 0.006, min_h: float = 0.03, density: float = 0.004, axis: str = "x") -> list[Frac]:
    """어느 영역에도 들지 않은 먹 기둥(axis='x': 세로 글 열)·먹 띠(axis='y': 가로 글줄)를 찾는다. 모델이 영역 좌표를 좁게 잡아
    열(줄) 하나를 통째로 빠뜨린 경우를 잡기 위한 것으로, 지침 36 '원본에 있는 본문이 누락되지 않아야 한다' 의 기계적 보조 수단이다.
    가로쓰기 문헌은 axis='y' 로 불러야 머리글 한 줄이 글자 수만큼의 '기둥'으로 쪼개져 잡음이 되지 않는다."""
    a = np.asarray(im.convert("L")) < thr
    if axis == "y":
        a = a.T; regions = [_swap(r) for r in regions]
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
    if axis == "y":
        boxes = [_swap(b) for b in boxes]
    return boxes


def attach_uncovered(boxes: list[Frac], text_regions: list[tuple[str, Frac]], max_gap: float = 0.06, min_overlap: float = 0.5,
                     axis: str = "x") -> tuple[dict[str, Frac], list[Frac]]:
    """빠진 먹 기둥을 세로로 겹치고 가로로 가까운 문자 영역에 붙인다(axis='y' 면 가로 글줄을 가로로 겹치고 세로로 가까운 영역에).
    붙일 곳이 없으면 따로 돌려준다."""
    if axis == "y":
        grown_t, orphans_t = attach_uncovered([_swap(b) for b in boxes], [(rid, _swap(r)) for rid, r in text_regions], max_gap, min_overlap, "x")
        return {rid: _swap(fr) for rid, fr in grown_t.items()}, [_swap(b) for b in orphans_t]
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


def page_bands(im: Image.Image, axis: str = "y", exclude: list[Frac] | None = None, thr: int = 110, density: float = 0.003,
               min_band_px: int = 6, min_len: float = 0.02) -> list[Frac]:
    """쪽 전체에서 글줄(axis='y')·글 열(axis='x') 띠를 투영으로 찾는다. exclude(도판 영역)는 먹에서 뺀다.
    띠마다 (x0,y0,x1,y1) 비율 상자를 돌려준다(다른 축 범위는 그 띠의 먹이 있는 범위). 좌표 보정(refit)의 바탕."""
    a = np.asarray(im.convert("L")) < thr
    H, W = a.shape
    for (x0, y0, x1, y1) in (exclude or []):
        a[max(0, int(y0 * H)):min(H, int(y1 * H) + 1), max(0, int(x0 * W)):min(W, int(x1 * W) + 1)] = False
    if axis == "y":
        a = a.T
    Ht, Wt = a.shape                 # 전치 뒤: 띠는 열 방향(x)으로 찾는다
    prof = a.mean(axis=0)
    on = prof > density
    out: list[Frac] = []
    x = 0
    while x < Wt:
        if not on[x]:
            x += 1; continue
        x2 = x
        while x2 < Wt and on[x2]:
            x2 += 1
        if x2 - x >= min_band_px:
            rows = np.where(a[:, x:x2].any(axis=1))[0]
            if len(rows) and (rows[-1] - rows[0]) >= min_len * Ht:
                b = (round(x / Wt, 4), round(rows[0] / Ht, 4), round(x2 / Wt, 4), round((rows[-1] + 1) / Ht, 4))
                out.append(_swap(b) if axis == "y" else b)
        x = x2
    return out


def ink_runs(prof: np.ndarray, density: float = 0.002, min_px: int = 2) -> list[list[int]]:
    """먹 비율 프로파일에서 먹이 이어지는 구간 [시작, 끝) 목록."""
    on = prof > density
    out: list[list[int]] = []; i = 0; n = len(on)
    while i < n:
        if not on[i]:
            i += 1; continue
        j = i
        while j < n and on[j]:
            j += 1
        if j - i >= min_px:
            out.append([i, j])
        i = j
    return out


def merge_glyph_runs(runs: list[list[int]], pitch: float, gap_frac: float = 0.25, max_len: float = 1.0) -> list[list[int]]:
    """글자 방향 먹 구간을 글자 단위로 합친다. 한 글자 안의 획 사이 틈(三·川·二)은 글자 크기(pitch, 줄 두께)의 gap_frac 보다 좁고,
    합친 길이가 pitch 의 max_len 배를 넘지 않으면 같은 글자로 본다. 300dpi 렌더 실측(세로·가로, 명조·고딕 4벌)에서 이 값이 가장 잘 맞았다."""
    out: list[list[int]] = []
    for r in runs:
        if out and (r[0] - out[-1][1]) < gap_frac * pitch and (r[1] - out[-1][0]) <= max_len * pitch:
            out[-1][1] = r[1]
        else:
            out.append([r[0], r[1]])
    return out
