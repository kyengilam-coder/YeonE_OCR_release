"""1차 판독 영역 좌표 보정(refit).

Gemini 가 돌려주는 영역 좌표는 글이 빽빽한 쪽에서 아래로 갈수록 밀리는 일이 있다(실측: 중의역대약론선 130쪽, 맨 아래 영역이
7.5% = 글줄 4개만큼 아래로). 그대로 쓰면 2차 조각이 이웃 인용문을 읽어 가짜 차이가 쏟아지고, 3차 글자 판별도 엉뚱한 글자를 본다.
그래서 1차 판독 직후 영역 좌표를 쪽의 실제 글줄 위치에 맞춘다.
  1) 글자층이 있으면: 영역의 첫·끝 줄 글을 글자층 줄과 맞춰 그 줄의 좌표를 쓴다(가장 정확, 이미지 불필요).
  2) 없으면: 쪽 전체의 먹 띠(글줄)를 찾고, 모델 좌표의 밀림을 1차식으로 추정해 바로잡은 뒤 띠에 붙인다.
좌표만 고치고 글은 손대지 않는다. 고친 내역은 기록(bbox_fix)에 남겨 검수 화면에서 볼 수 있다."""
from __future__ import annotations
import re
from difflib import SequenceMatcher
from PIL import Image
from .schema import PageReading, Region, TEXT_KINDS, IMAGE_KINDS, bbox_frac, frac_bbox
from .merge import columns_in_reading_order

_STRIP = re.compile(r"[\s　·ㆍ・•。,，、:：;；!！?？()（）\[\]【】『』「」《》〈〉“”\"'‘’—─―－\-_/／\\|*＊~～…‥\.]")
MAX_MOVE = 0.15          # 이보다 멀리 가는 짝은 잘못 맞춘 것으로 보고 버린다
MIN_RATIO = 0.6


def _key(s: str) -> str:
    return _STRIP.sub("", s or "")


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def region_lines(reg: Region) -> list[str]:
    """영역의 글줄(가로쓰기) 또는 글 열(세로쓰기) 글을 읽기 순서로."""
    return [c.text or "" for c in columns_in_reading_order(reg)]


def _line_count(reg: Region) -> int:
    n = sum(len(p.columns) for p in reg.paragraphs)
    return n if n else (1 if (reg.title_text or reg.kind in ("제목", "쪽번호", "머리글", "꼬리글")) else 0)


# ---------- 1) 글자층으로 ----------
def refit_by_layer(p1: PageReading, layer: dict, horizontal: bool) -> dict[str, dict]:
    """글자층(기존 OCR) 줄의 정확한 좌표로 영역 상자를 다시 잡는다. 돌려주는 사전: {region_id: {"from", "to", "how", "move"}}."""
    W = float(layer.get("page_width") or 0); H = float(layer.get("page_height") or 0)
    if not W or not H:
        return {}
    if horizontal:
        lines = sorted(layer.get("horizontal_lines") or [], key=lambda l: (l["bbox"][1], l["bbox"][0]))
        boxes = [(l["bbox"][0] / W, l["bbox"][1] / H, l["bbox"][2] / W, l["bbox"][3] / H) for l in lines]
    else:
        cols = layer.get("vertical_columns") or []
        if len(cols) < 2:
            return {}
        xs = sorted(c["x"] for c in cols)
        pitch = min((b - a for a, b in zip(xs, xs[1:]) if b - a > 1), default=0) or 0
        half = (pitch / 2 if pitch else 6.0) / W
        lines = sorted(cols, key=lambda c: -c["x"])                      # 오른쪽 열부터
        boxes = [(c["x"] / W - half, c["y0"] / H, c["x"] / W + half, c["y1"] / H) for c in lines]
    keys = [_key(l["text"]) for l in lines]
    if len(keys) < 2:
        return {}
    fixes: dict[str, dict] = {}
    pos = 0
    for reg in p1.regions:
        if reg.kind in IMAGE_KINDS:
            continue
        rl = [t for t in region_lines(reg) if _key(t)] or ([reg.title_text] if reg.title_text else [])
        if not rl:
            continue
        first, last = _key(rl[0])[:12], _key(rl[-1])[-12:]
        old = bbox_frac(reg.bbox)
        # 첫 줄: 앞서 맞춘 자리 다음부터 찾되, 모델 좌표에서 MAX_MOVE 안에 있는 줄만
        best_i, best_s = None, 0.0
        for i in range(pos, len(keys)):
            b = boxes[i]
            if abs((b[1] if horizontal else -b[0]) - (old[1] if horizontal else -old[0])) > MAX_MOVE:
                continue
            s = _ratio(first, keys[i][:12])
            if s > best_s:
                best_i, best_s = i, s
        if best_i is None or best_s < MIN_RATIO:
            continue
        n = max(1, len(rl))
        best_j, best_sj = best_i, (1.0 if n == 1 else 0.0)
        if n > 1:
            for j in range(best_i, min(len(keys), best_i + n + 3)):
                s = _ratio(last, keys[j][-12:])
                if s > best_sj:
                    best_j, best_sj = j, s
            if best_sj < MIN_RATIO:
                best_j = min(len(keys) - 1, best_i + n - 1)             # 끝 줄을 못 찾으면 줄 수로 짐작
        span = boxes[best_i:best_j + 1]
        new = (min(b[0] for b in span), min(b[1] for b in span), max(b[2] for b in span), max(b[3] for b in span))
        pad = 0.003
        new = (max(0.0, new[0] - pad), max(0.0, new[1] - pad), min(1.0, new[2] + pad), min(1.0, new[3] + pad))
        move = max(abs(a - b) for a, b in zip(old, new))
        shift = max(abs(old[1] - new[1]), abs(old[3] - new[3])) if horizontal else max(abs(old[0] - new[0]), abs(old[2] - new[2]))
        if not any(pp.columns for pp in reg.paragraphs):
            shift = 0.0                                                  # 제목 상자는 글자층 글꼴 상자가 커서 밀림의 증거가 못 된다
        if move > MAX_MOVE:
            continue
        reg.bbox = frac_bbox(new)
        fixes[reg.id] = {"from": frac_bbox(old), "to": reg.bbox, "how": "layer", "move": round(move, 4), "shift": round(shift, 4), "lines": [best_i, best_j]}
        pos = best_j + 1
    return fixes


# ---------- 2) 먹 띠로 ----------
def _fit_linear(pairs: list[tuple[float, float]]) -> tuple[float, float]:
    n = len(pairs)
    sx = sum(x for x, _ in pairs); sy = sum(y for _, y in pairs)
    sxx = sum(x * x for x, _ in pairs); sxy = sum(x * y for x, y in pairs)
    den = n * sxx - sx * sx
    if n < 2 or abs(den) < 1e-9:
        return 1.0, (sy - sx) / n if n else 0.0
    a = (n * sxy - sx * sy) / den; b = (sy - a * sx) / n
    a = max(0.85, min(1.15, a))
    return a, b


def align_runs(mids: list[float], counts: list[int], band_mids: list[float]) -> list[int] | None:
    """영역 k(가운데 mids[k], 줄 수 counts[k])에 띠를 counts[k]개씩 '순서대로·겹치지 않게' 배정한다(띠는 건너뛸 수 있음).
    배정 비용은 영역 가운데와 띠 묶음 가운데의 거리. 돌려주는 값은 영역마다 첫 띠 번호. 배정이 불가능하면 None."""
    K, B = len(counts), len(band_mids)
    if sum(counts) > B:
        return None
    INF = float("inf")
    f = [[INF] * (B + 1) for _ in range(K + 1)]
    back: list[list[int | None]] = [[None] * (B + 1) for _ in range(K + 1)]
    for j in range(B + 1):
        f[0][j] = 0.0
    for k in range(1, K + 1):
        n = counts[k - 1]
        for j in range(1, B + 1):
            if f[k][j - 1] < f[k][j]:
                f[k][j] = f[k][j - 1]; back[k][j] = -1                    # 띠 j-1 을 건너뜀
            if j >= n and f[k - 1][j - n] < INF:
                run_mid = (band_mids[j - n] + band_mids[j - 1]) / 2
                c = f[k - 1][j - n] + abs(mids[k - 1] - run_mid)
                if c < f[k][j]:
                    f[k][j] = c; back[k][j] = j - n                        # 영역 k-1 이 띠 j-n..j-1 을 차지
    if f[K][B] == INF:
        return None
    starts = [0] * K; k, j = K, B
    while k > 0:
        b = back[k][j]
        if b == -1:
            j -= 1
        else:
            starts[k - 1] = b; j = b; k -= 1
    return starts


def refit_by_ink(img: Image.Image, p1: PageReading, horizontal: bool, skip: set[str] | None = None) -> dict[str, dict]:
    """쪽의 먹 띠(글줄·글 열)에 영역 상자를 맞춘다. 영역 순서와 줄 수(1차 판독의 열 수)를 지키며 띠를 배정하고,
    배정으로 추정한 모델 좌표의 밀림(a·y+b)을 넣어 다시 배정하기를 되풀이한다. 끝에 남는 어긋남이 글줄 간격의 6할을 넘으면
    확신이 없는 것이므로 아무것도 고치지 않는다(잘못 고치는 쪽이 더 나쁘다)."""
    from .layout import page_bands
    skip = skip or set()
    axis = "y" if horizontal else "x"
    bands = page_bands(img, axis=axis, exclude=[bbox_frac(r.bbox) for r in p1.regions if r.kind in IMAGE_KINDS])
    regs = [r for r in p1.regions if r.kind not in IMAGE_KINDS and _line_count(r) > 0]
    if len(bands) < 3 or not regs:
        return {}
    lo, hi = (1, 3) if horizontal else (0, 2)
    if not horizontal:
        bands = sorted(bands, key=lambda b: -b[0])                      # 세로쓰기: 오른쪽 열부터
        bands = [(1 - b[2], b[1], 1 - b[0], b[3]) for b in bands]        # 오른쪽→왼쪽을 증가 축으로
    counts = [_line_count(r) for r in regs]
    if len(bands) > sum(counts) + 8:
        return {}                                                        # 띠가 너무 많으면(표·두 단) 손대지 않는다
    def edges(r):
        fr = bbox_frac(r.bbox)
        return (1 - fr[2], 1 - fr[0]) if not horizontal else (fr[1], fr[3])
    band_mids = [(b[lo] + b[hi]) / 2 for b in bands]
    pitch = sorted(m2 - m1 for m1, m2 in zip(band_mids, band_mids[1:]) if m2 > m1)
    pitch = pitch[len(pitch) // 2] if pitch else 0.02
    a, b = 1.0, 0.0
    starts = None
    for _ in range(4):
        mids = [a * (e0 + e1) / 2 + b for e0, e1 in (edges(r) for r in regs)]
        starts = align_runs(mids, counts, band_mids)
        if starts is None:
            return {}
        pairs = []
        for r, n, st in zip(regs, counts, starts):
            e0, e1 = edges(r); pairs.append((e0, bands[st][lo])); pairs.append((e1, bands[st + n - 1][hi]))
        a, b = _fit_linear(pairs)
    res = [abs(a * (sum(edges(r)) / 2) + b - (band_mids[st] + band_mids[st + n - 1]) / 2) for r, n, st in zip(regs, counts, starts)]
    if max(res) > 0.9 * pitch or sum(res) / len(res) > 0.4 * pitch:
        return {}                                                        # 한 줄 어긋난 배정은 평균이 글줄 간격만큼 커진다
    top = edges(regs[0])[0]
    if abs(a * top + b - top) > 0.6 * pitch:
        return {}                                                        # 밀림은 아래로 갈수록 쌓인다. 맨 위 영역부터 한 줄 넘게 움직여야 맞는 배정은 믿지 않는다
    fixes: dict[str, dict] = {}
    for r, n, st in zip(regs, counts, starts):
        if r.id in skip:
            continue
        span = bands[st:st + n]
        new = [min(s[0] for s in span), min(s[1] for s in span), max(s[2] for s in span), max(s[3] for s in span)]
        if not horizontal:
            new = [1 - new[2], new[1], 1 - new[0], new[3]]
        pad = 0.003
        new = (max(0.0, new[0] - pad), max(0.0, new[1] - pad), min(1.0, new[2] + pad), min(1.0, new[3] + pad))
        old = bbox_frac(r.bbox)
        move = max(abs(x - y) for x, y in zip(old, new))
        shift = max(abs(old[1] - new[1]), abs(old[3] - new[3])) if horizontal else max(abs(old[0] - new[0]), abs(old[2] - new[2]))
        if not any(pp.columns for pp in r.paragraphs):
            shift = 0.0
        if move > MAX_MOVE or move < 0.002:
            continue
        r.bbox = frac_bbox(new)
        fixes[r.id] = {"from": frac_bbox(old), "to": r.bbox, "how": "ink", "move": round(move, 4), "shift": round(shift, 4)}
    return fixes


def refit_regions(img: Image.Image | None, p1: PageReading, horizontal: bool, layer: dict | None = None) -> dict:
    """글자층이 있으면 그것으로, 남는 영역은 먹 띠로 좌표를 맞춘다. 요약 {"fixed", "max_move", "how", "regions"} 를 돌려준다."""
    fixes: dict[str, dict] = {}
    if layer and (layer.get("horizontal_lines") if horizontal else layer.get("vertical_columns")):
        try:
            fixes.update(refit_by_layer(p1, layer, horizontal))
        except Exception:
            pass
    if img is not None:
        try:
            fixes.update(refit_by_ink(img, p1, horizontal, skip=set(fixes)))
        except Exception:
            pass
    hows = sorted({f["how"] for f in fixes.values()})
    return {"fixed": len(fixes), "max_move": max((f["move"] for f in fixes.values()), default=0.0),
            "max_shift": max((f.get("shift", 0.0) for f in fixes.values()), default=0.0), "how": "+".join(hows), "regions": fixes}


def describe(fix: dict) -> str:
    names = {"layer": "글자층 줄 위치", "ink": "먹 띠(글줄)", "ink+layer": "글자층·먹 띠"}
    return f"좌표 보정 — 영역 {fix['fixed']}개를 {names.get(fix['how'], fix['how'])}에 맞춤 · 읽기 방향 최대 밀림 {fix['max_shift']:.1%}"
