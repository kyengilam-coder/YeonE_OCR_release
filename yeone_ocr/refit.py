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


def span_slack(n: int) -> int:
    """영역 줄 수 n 과 글자층에서 맞춘 줄 수의 차이로 봐주는 줄 수: 2줄, 짧은 영역은 절반(1~3줄 영역은 1줄).
    모델이 줄을 붙이거나 글자층이 줄을 쪼갠 한두 줄은 봐주되, 이웃 영역의 줄까지 덮은 짝은 거른다."""
    return min(2, max(1, n // 2))


def _last_line(keys: list[str], i: int, n: int, last: str) -> tuple[int, bool]:
    """첫 줄 i 에서 시작하는 영역의 끝 줄 번호와 그 줄을 글로 찾았는지: 줄 수 +3 안에서 끝 줄 글과 가장 닮은 줄,
    못 찾으면 줄 수로 짐작(False — 짐작한 줄 수는 늘 영역 줄 수와 맞으니 줄 수 검사의 증거가 못 된다)."""
    if n == 1:
        return i, True
    best_j, best_s = i, 0.0
    for j in range(i, min(len(keys), i + n + 3)):
        s = _ratio(last, keys[j][-12:])
        if s > best_s:
            best_j, best_s = j, s
    return (best_j, True) if best_s >= MIN_RATIO else (min(len(keys) - 1, i + n - 1), False)


# ---------- 1) 글자층으로 ----------
def _choose(rows: list[tuple[float, float, list[tuple]]]) -> dict[int, int]:
    """영역마다 쓸 짝을 쪽 전체로 정한다. rows[k] = (모델 좌표의 첫 줄 가운데, 끝 줄 가운데, 짝 [(첫 줄, 끝 줄, 첫 줄 가운데, 끝 줄 가운데, 점수)]).
    돌려주는 사전: {영역 번호 k: 짝 번호}. 영역 순서대로·겹치지 않게(앞 영역의 끝 줄 < 다음 영역의 첫 줄) 고르되 점수 합이 가장 큰 배정 —
    맞춘 영역 수 → 끝 줄을 글로 찾은 수 → 첫 줄 → 둘째 줄 → 자리 차례로 견준다. 자리는 바로 앞에 맞춘 영역의 끝 줄에서 잰 거리를 모델 좌표의
    같은 거리와 견준다: 모델 좌표의 밀림은 쪽 아래로 갈수록 쌓이지만(실측 7.5%) 이웃 영역 사이에서는 거의 같다. 모델 좌표 그대로에 가까운 줄을
    고르면 밀림이 줄 간격의 절반을 넘을 때 다음 약재의 같은 줄을 고르고, 영역을 하나씩 차례로 맞추면 한 영역이 다음 영역의 줄을 가져가
    뒤 영역이 줄줄이 어긋난다(맞춘 영역 수를 먼저 견주는 까닭)."""
    best: list[list[tuple]] = []; most: list[int] = []                    # best[k][c] = (점수 합, 앞 짝 (k, c) 또는 None), most[k] = 영역 k 까지 맞춘 수의 최대
    for k, (at, _, cands) in enumerate(rows):
        row = []
        for i, _, mi, _, sc in cands:
            opt = ((*sc, -abs(mi - at)), None)                           # 앞에 맞춘 영역이 없음: 모델 좌표 그대로와의 거리
            for k2 in range(k - 1, -1, -1):
                if most[k2] + 1 < opt[0][0]:
                    break                                                # 더 앞 영역에서 이어서는 맞춘 영역 수가 모자란다
                end2 = rows[k2][1]
                for c2, (_, j2, _, mj2, _) in enumerate(rows[k2][2]):
                    if j2 < i:
                        acc = best[k2][c2][0]
                        v = tuple(x + y for x, y in zip(acc[:-1], sc)) + (acc[-1] - abs((mi - mj2) - (at - end2)),)
                        if v > opt[0]:
                            opt = (v, (k2, c2))
            row.append(opt)
        best.append(row); most.append(max([v[0] for v, _ in row] + most[-1:] + [0]))
    ends = [(best[k][c][0], (k, c)) for k in range(len(rows)) for c in range(len(rows[k][2]))]
    pick: dict[int, int] = {}; node = max(ends)[1] if ends else None
    while node is not None:
        pick[node[0]] = node[1]; node = best[node[0]][node[1]][1]
    return pick


def refit_by_layer(p1: PageReading, layer: dict, horizontal: bool) -> dict[str, dict]:
    """글자층(기존 OCR) 줄의 정확한 좌표로 영역 상자를 다시 잡는다. 돌려주는 사전: {region_id: {"from", "to", "how", "move", "lines"}}.
    첫 줄이 되풀이되는 쪽(약재마다 「【性味】…」로 시작)에서 1차가 가운데 영역을 빠뜨리면 다음 영역의 첫 줄이 앞 영역의 같은 줄에
    붙어 상자가 빠진 영역까지 넓어지고, 2차 조각이 그 글을 읽어 '2차에만 있음' 으로 쏟아진다. 그래서 영역마다 첫 줄 후보를 모두 모아
    맞춘 줄 수가 영역 줄 수와 맞는(span_slack) 짝만 남기고, 쪽 전체에서 영역 순서대로·겹치지 않게 짝을 고른다(_choose).
    맞는 짝이 없거나 고른 배정에 들지 못한 영역은 손대지 않고 {"fixed": False, "why": …} 로 남긴다.
    자리는 줄 가운데로 잰다(세로쓰기 영역의 첫 열은 오른쪽 끝 열 — 영역 왼쪽 가장자리는 마지막 열이다)."""
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
    lo, hi = (lambda b: b[1], lambda b: b[3]) if horizontal else (lambda b: -b[2], lambda b: -b[0])   # 읽기 방향 앞·뒤 가장자리(세로쓰기는 오른쪽이 앞)
    mids = [(lo(b) + hi(b)) / 2 for b in boxes]                         # 줄 가운데(글자층 상자와 모델 상자는 너비가 달라 가장자리끼리는 어긋난다)
    fixes: dict[str, dict] = {}
    regs = []; rows = []                                                 # regs[k] = (영역, 줄 수, 모델 상자, 짝, 줄 수가 안 맞은 짝의 줄 수)
    for reg in p1.regions:
        if reg.kind in IMAGE_KINDS:
            continue
        rl = [t for t in region_lines(reg) if _key(t)] or ([reg.title_text] if reg.title_text else [])
        if not rl:
            continue
        first, last = _key(rl[0])[:12], _key(rl[-1])[-12:]
        second = _key(rl[1])[:12] if len(rl) > 1 else ""
        old = bbox_frac(reg.bbox); n = max(1, len(rl))
        hl = (hi(old) - lo(old)) / n / 2; at, end = lo(old) + hl, hi(old) - hl           # 모델 좌표의 첫·끝 줄 가운데(hl = 줄 반 칸)
        # 첫 줄 후보: 모델 좌표에서 MAX_MOVE 안에 있고 첫 줄 글과 닮은(MIN_RATIO 이상) 줄 모두
        firsts = [(i, _ratio(first, keys[i][:12])) for i in range(len(keys)) if abs(mids[i] - at) <= MAX_MOVE]
        firsts = [(i, r) for i, r in firsts if r >= MIN_RATIO]
        if not firsts:
            continue
        top = max(r for _, r in firsts); cands = []; bad = []
        for i, r in firsts:
            j, found = _last_line(keys, i, n, last)
            if abs((j - i + 1) - n) > span_slack(n):
                bad.append(j - i + 1); continue
            span = boxes[i:j + 1]
            new = (min(b[0] for b in span), min(b[1] for b in span), max(b[2] for b in span), max(b[3] for b in span))
            pad = 0.003
            new = (max(0.0, new[0] - pad), max(0.0, new[1] - pad), min(1.0, new[2] + pad), min(1.0, new[3] + pad))
            move = max(abs(x - y) for x, y in zip(old, new))
            if move > MAX_MOVE:
                continue
            r2 = _ratio(second, keys[i + 1][:12]) if second and i + 1 < len(keys) else 0.0
            # 점수(클수록 좋음): 맞춤, 끝 줄을 글로 찾음(짐작한 끝 줄은 줄 수가 늘 맞으니 증거가 못 된다), 첫 줄이 가장 닮은 축(0.05 안), 둘째 줄
            cands.append((i, j, new, move, (1, int(found), -int(r < top - 0.05), r2)))
        regs.append((reg, n, old, cands, bad)); rows.append((at, end, [(i, j, mids[i], mids[j], sc) for i, j, _, _, sc in cands]))
    pick = _choose(rows)
    for k, (reg, n, old, cands, bad) in enumerate(regs):
        if k not in pick:
            if cands:
                fixes[reg.id] = {"from": reg.bbox, "how": "layer", "fixed": False,
                                 "why": "글자층에서 맞는 줄이 앞뒤 영역이 차지한 줄과 겹침 — 영역 순서나 줄 수가 틀렸을 수 있어 손대지 않음"}
            elif bad:
                fixes[reg.id] = {"from": reg.bbox, "how": "layer", "fixed": False,
                                 "why": f"글자층에서 맞춘 줄 수({'·'.join(map(str, bad))})가 영역 줄 수({n})와 안 맞음 — 되풀이되는 첫 줄이나 빠진 영역일 수 있어 손대지 않음"}
            continue
        i, j, new, move, _ = cands[pick[k]]
        shift = max(abs(old[1] - new[1]), abs(old[3] - new[3])) if horizontal else max(abs(old[0] - new[0]), abs(old[2] - new[2]))
        if not any(pp.columns for pp in reg.paragraphs):
            shift = 0.0                                                  # 제목 상자는 글자층 글꼴 상자가 커서 밀림의 증거가 못 된다
        reg.bbox = frac_bbox(new)
        fixes[reg.id] = {"from": frac_bbox(old), "to": reg.bbox, "how": "layer", "move": round(move, 4), "shift": round(shift, 4), "lines": [i, j]}
    return fixes


def is_fixed(f: dict) -> bool:
    """좌표를 실제로 고친 항목인지(확신이 없어 손대지 않은 항목은 fixed=False 로 남긴다)."""
    return bool(f.get("fixed", True))


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
            fixes.update(refit_by_ink(img, p1, horizontal, skip={k for k, f in fixes.items() if is_fixed(f)}))
        except Exception:
            pass
    done = [f for f in fixes.values() if is_fixed(f)]                    # 손대지 않은 항목(fixed=False, 사유만)은 세지 않는다
    hows = sorted({f["how"] for f in done})
    return {"fixed": len(done), "max_move": max((f["move"] for f in done), default=0.0),
            "max_shift": max((f.get("shift", 0.0) for f in done), default=0.0), "how": "+".join(hows), "regions": fixes}


def describe(fix: dict) -> str:
    names = {"layer": "글자층 줄 위치", "ink": "먹 띠(글줄)", "ink+layer": "글자층·먹 띠"}
    return f"좌표 보정 — 영역 {fix['fixed']}개를 {names.get(fix['how'], fix['how'])}에 맞춤 · 읽기 방향 최대 밀림 {fix['max_shift']:.1%}"
