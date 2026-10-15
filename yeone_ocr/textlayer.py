"""검색 가능한 PDF: 판독 기록(readings/pNNN.json 의 merged)의 글을 원본 PDF 쪽 위에 보이지 않는 글자층으로 넣는다.

화면(스캔)은 그대로 두고 글자만 얹으므로 PDF 뷰어에서 찾기·복사·끌어 고르기가 된다. 모델은 부르지 않는다.
1) 자리 잡기(plan_page): 영역 → 줄(세로쓰기는 열) → 글자 상자. 판독 기록에는 영역 상자만 있으므로 글자 자리는 따로 잰다. 줄마다 차례대로
   ① 먹 실측(쪽 이미지의 먹 띠·먹 구간)이 글자 수와 꼭 맞으면 그것 → ② 원본에 있던 다른 OCR 의 글자층에서 글자 상자를 빌림(글은 판독대로,
   자리만) → ③ 먹 범위를 글자 수로 나눔 → ④ 영역 상자를 균등 분할. 3차 판별의 자리 잡기(글자층 → 먹 → 균등)와 같은 재료를 쓰되 먹 실측을
   앞에 둔다: 실제 책(개보본초, PDF-XChange OCR 층)에서 다른 엔진의 글자 상자가 먹 실측보다 덜 맞았다.
2) 쓰기(write_layer): 글리프 없는 CID 글꼴(Identity-H + ToUnicode)로 3 Tr(보이지 않음). 글꼴에 글리프가 없어도 되므로 확장 A·B 한자,
   호환 한자, 한글이 그대로 남는다(내장 CJK 글꼴로 쓰면 㐀·䗪·𠮷 이 빈칸이 된다). 세로 열은 아래로 가는 줄(dir=(0,1))로 써서
   pdfio.text_layer 가 세로 열로 다시 읽는다. 틈 없이 이어진 글자는 한 번에(Tz 로 길이를 맞춰) 써서 추출기가 글자 사이에 빈칸을 넣지 않게 한다.
3) 이 글꼴(FONT_NAME)로 쓴 글자는 연어OCR 자신의 판독이므로 pdfio 가 기존 OCR 글자층(증인)으로 읽지 않는다 — 이 PDF 를 다시 넣어도
   판독을 판독과 대조하는 순환이 생기지 않는다.
원본 PDF 는 고치지 않고 새 파일로 저장한다. 판독 기록이 있는 쪽에서만, 원본의 숨은 글자층(다른 OCR)은 지우고(검색이 두 번 걸리지 않게) 넣는다.
보이는 글자가 있는 쪽(스캔이 아닌 쪽)은 건너뛴다. 도판·표 영역의 글자는 아직 넣지 않는다."""
from __future__ import annotations
import base64
import re, unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
import numpy as np
import pymupdf
from .schema import PageReading, TEXT_KINDS, TITLE_KINDS, SKIP_KINDS, bbox_frac
from .merge import columns_in_reading_order
from .layout import ink_runs, merge_glyph_runs
from .pdfio import OWN_LAYER_FONT as FONT_NAME, is_own_span, layer_chars
from .marks import MARK_RE, mark_kind, mark_reading

FONT_KEY = "FYeonEOCR"               # 쪽 자원(/Font) 안의 이름
ASC, DESC = 0.88, -0.12              # 글꼴의 위·아래 범위(em). 글자 상자 = 1em 정사각형(가운데에 맞춘다)
INK_THR = 110                        # 먹으로 칠 밝기(layout·adjudicate 와 같은 값)


class TextlayerCancelled(Exception):
    """중지 요청: 작업 중인 사본을 버리고 기존 출력 PDF 는 보존한다."""


@dataclass
class Line:
    dir: str                           # "v"(세로 열, 위→아래) | "h"(가로 줄, 왼→오른쪽)
    chars: list[tuple[str | None, tuple]]   # (글자, (x0,y0,x1,y1) 쪽 이미지 비율). 글자 None = 판독불명 자리(상자만, 글은 넣지 않음)
    how: str                           # ink · ink/group · ink/split · layer · ink~ · uniform
    region: str = ""
    index: int = 0                     # 영역 안 줄 번호(line_texts 의 차례) — 판독 글의 어느 열인지 되짚을 때


def out_path(work_dir, pdf) -> Path:
    """기본 저장 자리: 결과 폴더 out/<원본 이름>_글자층.pdf (창 프로그램도 같은 규칙으로 연다)."""
    return Path(work_dir) / "out" / f"{Path(pdf).stem}_글자층.pdf"


def token_positions(text: str) -> list[tuple[str | None, int]]:
    """줄 글 → 쪽에 찍힌 글자 차례와 각 글자의 글 안 위치(공백 뺌). 〔a/b〕→a(1차), 〔疑: X〕→X, 〔판독불명〕→None(먹은 있으나
    글을 모르는 한 자리), 그 밖의 〔…〕(원본 결락 등 먹이 없는 표시)는 뺀다. 표시에서 나온 글자의 위치는 그 표시의 첫 글자."""
    text = text or ""
    out: list[tuple[str | None, int]] = []; pos = 0
    for m in MARK_RE.finditer(text):
        out.extend((c, pos + k) for k, c in enumerate(text[pos:m.start()]) if not c.isspace())
        inner = m.group(0)[1:-1]; kind = mark_kind(inner)
        if kind in ("doubt", "ab"):
            out.extend((c, m.start()) for c in mark_reading(inner) if not c.isspace())
        elif kind == "unknown":
            out.append((None, m.start()))
        pos = m.end()
    out.extend((c, pos + k) for k, c in enumerate(text[pos:]) if not c.isspace())
    return out


def tokens(text: str) -> list[str | None]:
    """줄 글 → 쪽에 찍힌 글자 차례(token_positions 의 글자만)."""
    return [c for c, _ in token_positions(text)]


def line_texts(reg) -> list[str]:
    """영역의 줄(세로쓰기는 열) 글, 읽기 차례대로. 제목은 제목 글 한 줄."""
    if reg.kind in TITLE_KINDS:
        return [reg.title_text] if reg.title_text.strip() else []
    cols = [c.text or "" for c in columns_in_reading_order(reg)]
    return cols or ([reg.title_text] if reg.title_text.strip() else [])


def adv(c: str | None) -> float:
    """글자 나아감 폭(em): 전각·넓은 글자(한자·한글·전각 부호)는 1, 그 밖(숫자·라틴·반각 부호)은 0.5."""
    return 1.0 if c is None or unicodedata.east_asian_width(c) in ("W", "F") else 0.5


def _grid(chars, s0: float, s1: float) -> list[tuple[float, float]]:
    """[s0, s1] 을 글자 폭(adv) 비율로 나눈 칸들."""
    w = [adv(c) for c in chars]; unit = (s1 - s0) / max(1e-9, sum(w)); out = []; x = s0
    for k in w:
        out.append((x, x + k * unit)); x += k * unit
    return out


# ---- ① 기존 글자층(다른 OCR)의 글자 상자 빌리기 ----
_FOLD: dict = {}
_PUNCT = {"，": ",", "；": ";", "：": ":", "［": "[", "］": "]", "〔": "[", "〕": "]", "（": "(", "）": ")", "！": "!", "？": "?"}


def fold(ch: str | None) -> str:
    """글자층 짝 찾기용 접기: 번체→간체(한 글자씩, 길이 그대로), 전각·반각 부호는 하나로 — 간체 책을 번체로 읽은 층(PDF-XChange 등)도 맞춘다."""
    if ch is None:
        return "\0"
    if ch not in _FOLD:
        try:
            from opencc import OpenCC
            t = _FOLD.setdefault(None, OpenCC("t2s")).convert(ch)
        except Exception:
            t = ch
        t = t if len(t) == 1 else ch
        _FOLD[ch] = _PUNCT.get(t, t)
    return _FOLD[ch]


def by_layer(chars, frac, layer_lines: list[dict], frame, want: str, used: set) -> list[tuple] | None:
    """영역 안(조금 넓혀)의 같은 방향 글자층 줄 가운데 판독 줄과 가장 닮은 것(닮기 0.5 이상, 아직 안 쓴 줄)의 글자 상자를 빌린다.
    글자가 달라도(다른 엔진의 오독) 짝이 맞는 자리는 그 상자를, 짝이 없는 글자는 앞뒤 상자로 보간한다."""
    fx0, fy0, fw, fh = frame
    rx0, ry0, rx1, ry1 = frac
    if want == "v": rx0, rx1, ry0, ry1 = rx0 - 0.03, rx1 + 0.03, ry0 - 0.02, ry1 + 0.02
    else: ry0, ry1, rx0, rx1 = ry0 - 0.03, ry1 + 0.03, rx0 - 0.02, rx1 + 0.02
    src = "".join(fold(c) for c in chars); best = None
    for li, ln in enumerate(layer_lines):
        if ln.get("dir") != want or li in used:
            continue
        cs = [(c, bb) for c, bb in ln.get("chars") or []
              if rx0 <= ((bb[0] + bb[2]) / 2 - fx0) / fw <= rx1 and ry0 <= ((bb[1] + bb[3]) / 2 - fy0) / fh <= ry1]
        if len(cs) < 2:
            continue
        r = SequenceMatcher(None, src, "".join(fold(c) for c, _ in cs), autojunk=False).ratio()
        if r >= 0.5 and (best is None or r > best[0]):
            best = (r, li, cs)
    if best is None:
        return None
    _, li, cs = best
    boxes: list = [None] * len(chars)
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, src, "".join(fold(c) for c, _ in cs), autojunk=False).get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            for k in range(i2 - i1):
                boxes[i1 + k] = list(cs[j1 + k][1])
    known = [i for i, b in enumerate(boxes) if b is not None]
    if not known:
        return None
    used.add(li)
    for i in range(len(boxes)):                                         # 짝 없는 글자: 앞뒤 상자로 보간(끝이면 글자 크기만큼 내밀어)
        if boxes[i] is None:
            a = max((k for k in known if k < i), default=None); b = min((k for k in known if k > i), default=None)
            if a is not None and b is not None:
                t = (i - a) / (b - a); boxes[i] = [boxes[a][q] + (boxes[b][q] - boxes[a][q]) * t for q in range(4)]
            else:
                k = a if a is not None else b; s = boxes[k]
                d = (i - k) * ((s[3] - s[1]) if want == "v" else (s[2] - s[0]))
                boxes[i] = [s[0], s[1] + d, s[2], s[3] + d] if want == "v" else [s[0] + d, s[1], s[2] + d, s[3]]
    return [((b[0] - fx0) / fw, (b[1] - fy0) / fh, (b[2] - fx0) / fw, (b[3] - fy0) / fh) for b in boxes]


# ---- ② 먹 실측 ----
def _merge_split_bands(bands: list, n: int) -> list:
    """흐린 인쇄로 한 줄이 두 띠로 갈린 것(머리말 등)을 잇는다: 띠가 줄 수보다 많을 때만, 틈이 띠 두께 중앙값의 0.5배보다 좁고
    이은 두께가 중앙값의 1.7배를 넘지 않는 이웃부터."""
    bands = [list(b) for b in bands]
    while len(bands) > max(1, n):
        med = float(np.median([b[1] - b[0] for b in bands]))
        g, i = min((bands[i + 1][0] - bands[i][1], i) for i in range(len(bands) - 1))
        if g >= 0.5 * med or bands[i + 1][1] - bands[i][0] > 1.7 * med:
            break
        bands[i:i + 2] = [[bands[i][0], bands[i + 1][1]]]
    return bands


def ink_bands(ink: np.ndarray, frac, want: str, n_lines: int) -> tuple[list, tuple]:
    """영역의 줄 띠(세로쓰기는 열 띠, 오른쪽부터). 영역 상자를 줄과 직각으로 0.6줄, 읽기 방향으로 4% 넓혀 띠를 찾고, 줄 수만큼 이어진 띠 묶음
    가운데 원래 상자와 가장 많이 겹치는 것을 고른다(모델 좌표가 조금 밀려도 줄을 놓치지 않게). 띠가 줄 수보다 적으면 빈 목록."""
    H, W = ink.shape
    x0, y0, x1, y1 = frac; n = max(1, n_lines)
    if want == "v":
        m = 0.6 * (x1 - x0) / n; x0, x1, y0, y1 = x0 - m, x1 + m, y0 - 0.04, y1 + 0.04
    else:
        m = 0.6 * (y1 - y0) / n; y0, y1, x0, x1 = y0 - m, y1 + m, x0 - 0.04, x1 + 0.04
    X0, Y0 = max(0, int(x0 * W)), max(0, int(y0 * H))
    X1, Y1 = min(W, max(X0 + 2, int(x1 * W))), min(H, max(Y0 + 2, int(y1 * H)))
    a = ink[Y0:Y1, X0:X1]
    bands = _merge_split_bands(ink_runs(a.mean(axis=0) if want == "v" else a.mean(axis=1), 0.002, 4), n)
    if len(bands) < n:
        return [], (X0, Y0, X1, Y1)
    lo, hi = ((frac[0] * W - X0, frac[2] * W - X0) if want == "v" else (frac[1] * H - Y0, frac[3] * H - Y0))
    ov = lambda b: max(0.0, min(b[1], hi) - max(b[0], lo))
    k = max(range(len(bands) - n + 1), key=lambda k: sum(ov(b) for b in bands[k:k + n]))
    sel = bands[k:k + n]
    return (sel[::-1] if want == "v" else sel), (X0, Y0, X1, Y1)


def _own_runs(runs: list, lo: float, hi: float, thick: float) -> list:
    """읽기 방향 먹 구간 가운데 이 영역 것: 원래 범위(lo~hi)에 걸친 구간에서 시작해, 줄 두께 1.2배보다 좁은 틈으로 이어진 구간까지."""
    inside = [i for i, r in enumerate(runs) if r[1] > lo and r[0] < hi]
    if not inside:
        return []
    a, b = inside[0], inside[-1]
    while a > 0 and runs[a][0] - runs[a - 1][1] < 1.2 * thick and runs[a - 1][1] > lo - 2 * thick: a -= 1
    while b < len(runs) - 1 and runs[b + 1][0] - runs[b][1] < 1.2 * thick and runs[b + 1][0] < hi + 2 * thick: b += 1
    return runs[a:b + 1]


def group_runs(runs: list, chars, thick: float, max_parts: int = 8) -> list | None:
    """먹 구간들을 글자 수만큼의 이어진 묶음으로 나눈다(동적 계획). 획 사이가 벌어진 글자(三·二·川)가 여러 구간일 때.
    비용 = 묶음 중심과 칸(글자 폭 비율 격자) 중심의 거리² + 한 묶음이 글자 하나(줄 두께의 1.15배)보다 길면 큰 벌점. 한 글자는 구간 max_parts 개까지."""
    r, n = len(runs), len(chars)
    if r < n or n == 0:
        return None
    cells = _grid(chars, runs[0][0], runs[-1][1])
    INF = float("inf")
    cost = [[INF] * (r + 1) for _ in range(n + 1)]; back = [[0] * (r + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for i in range(1, n + 1):
        c_i = (cells[i - 1][0] + cells[i - 1][1]) / 2; p = max(1e-9, cells[i - 1][1] - cells[i - 1][0])
        for j in range(i, r - (n - i) + 1):
            for k in range(max(i - 1, j - max_parts), j):
                if cost[i - 1][k] == INF:
                    continue
                a, b = runs[k][0], runs[j - 1][1]
                over = max(0.0, (b - a) - 1.15 * thick)
                c = cost[i - 1][k] + (((a + b) / 2 - c_i) / p) ** 2 + 50 * (over / thick) ** 2
                if c < cost[i][j]:
                    cost[i][j], back[i][j] = c, k
    if cost[n][r] == INF:
        return None
    out = []; j = r
    for i in range(n, 0, -1):
        k = back[i][j]; out.append([runs[k][0], runs[j - 1][1]]); j = k
    return out[::-1]


def by_ink_in_band(chars, ink: np.ndarray, box_px, band, want: str, frac) -> tuple[list[tuple], str]:
    """띠 하나 안의 글자 상자와 방법. 먹 구간을 글자로 묶어 수가 맞으면 실측(ink), 획이 갈린 글자를 묶어 맞으면(ink/group),
    붙은 글자를 간격으로 갈라 맞으면(ink/split), 아니면 먹 범위를 글자 폭 비율로 나눈다(ink~)."""
    H, W = ink.shape
    X0, Y0, _, _ = box_px
    a = ink[Y0:box_px[3], X0:box_px[2]]
    sub = a[:, band[0]:band[1]] if want == "v" else a[band[0]:band[1], :]
    thick = max(2, band[1] - band[0])
    lo, hi = ((frac[1] * H - Y0, frac[3] * H - Y0) if want == "v" else (frac[0] * W - X0, frac[2] * W - X0))
    runs = _own_runs(ink_runs(sub.mean(axis=1) if want == "v" else sub.mean(axis=0), 0.002, 2), lo, hi, thick)
    if not runs:
        return [], ""
    glyphs = merge_glyph_runs(runs, thick)
    n = len(chars); how = "ink"
    if len(glyphs) > n:
        g2 = group_runs(glyphs, chars, thick)
        if g2:
            glyphs, how = g2, "ink/group"
    if len(glyphs) != n:
        pitch = (runs[-1][1] - runs[0][0]) / max(1e-9, sum(adv(c) for c in chars))
        ks = [max(1, round((g[1] - g[0]) / pitch)) for g in glyphs]
        if sum(ks) == n:                                                # 붙은 글자(먹 구간 하나에 두세 글자)를 똑같이 가른다
            glyphs = [[g[0] + (g[1] - g[0]) * j / k, g[0] + (g[1] - g[0]) * (j + 1) / k] for g, k in zip(glyphs, ks) for j in range(k)]
            how = "ink/split"
    if len(glyphs) == n:
        mids = [(g[0] + g[1]) / 2 for g in glyphs]
        pitch = float(np.median(np.diff(mids))) if n > 1 else max(thick, glyphs[0][1] - glyphs[0][0])
        segs = [(c - pitch / 2, c + pitch / 2) for c in mids]
    else:
        segs = _grid(chars, runs[0][0], runs[-1][1]); how = "ink~"
    if want == "v":
        return [((X0 + band[0]) / W, (Y0 + s) / H, (X0 + band[1]) / W, (Y0 + e) / H) for s, e in segs], how
    return [((X0 + s) / W, (Y0 + band[0]) / H, (X0 + e) / W, (Y0 + band[1]) / H) for s, e in segs], how


# ---- ③ 영역 상자 균등 분할 ----
def by_uniform(chars, frac, li: int, n_lines: int, want: str) -> list[tuple]:
    x0, y0, x1, y1 = frac; n_lines = max(1, n_lines)
    if want == "v":
        cw = (x1 - x0) / n_lines; xa, xb = x1 - (li + 1) * cw, x1 - li * cw
        return [(xa, s, xb, e) for s, e in _grid(chars, y0, y1)]
    lh = (y1 - y0) / n_lines; ya, yb = y0 + li * lh, y0 + (li + 1) * lh
    return [(s, ya, e, yb) for s, e in _grid(chars, x0, x1)]


def _direction(reg, vertical: bool) -> str:
    d = reg.reading_direction or ""
    if "가로" in d: return "h"
    if "세로" in d: return "v"
    return "h" if reg.kind == "쪽번호" or not vertical else "v"


def plan_page(pr: PageReading, img, frame, layer_lines: list[dict] | None = None, vertical: bool = True) -> list[Line]:
    """쪽 판독 → 글자 자리 계획. img 는 판독에 쓴 쪽 이미지(영역 상자의 기준), frame 은 그 이미지가 덮는 PDF 좌표 틀(pdfio.page_frame),
    layer_lines 는 원본의 다른 OCR 글자층(pdfio.layer_chars, 없으면 None). 쪽번호·머리말도 쪽에 보이는 글이므로 넣는다(문서에는 안 넣는 것들)."""
    ink = (np.asarray(img.convert("L")) < INK_THR) if not isinstance(img, np.ndarray) else img
    out: list[Line] = []; used: set = set()
    for reg in pr.regions:
        if reg.kind in SKIP_KINDS:
            texts = [t for t in (line_texts(reg) or [pr.page_label if reg.kind == "쪽번호" else pr.running_header]) if t.strip()]
        elif reg.kind in TEXT_KINDS or reg.kind in TITLE_KINDS or reg.kind == "기타":
            texts = line_texts(reg)
        else:
            continue                                                    # 도판(글자를 적지 않음)·표(다음 단계)
        want = _direction(reg, vertical)
        frac = bbox_frac(reg.bbox)
        items = [(k, tk) for k, tk in ((k, tokens(t)) for k, t in enumerate(texts)) if tk]
        toks = [tk for _, tk in items]
        if not toks:
            continue
        bands, box_px = ink_bands(ink, frac, want, len(toks))
        for li, (index, chars) in enumerate(items):
            boxes, how = None, ""
            got = by_ink_in_band(chars, ink, box_px, bands[li], want, frac) if bands else ([], "")
            if got[0] and got[1] != "ink~":
                boxes, how = got
            if boxes is None and layer_lines:
                boxes = by_layer(chars, frac, layer_lines, frame, want, used); how = "layer"
            if boxes is None and got[0]:
                boxes, how = got
            if boxes is None:
                boxes, how = by_uniform(chars, frac, li, len(toks), want), "uniform"
            out.append(Line(want, list(zip(chars, boxes)), how, reg.id, index))
    return out


# ---- 쓰기 ----
def _u16(c: str) -> str:
    return c.encode("utf-16-be").hex().upper()


def drop_bad_chars(lines: list[Line]) -> int:
    """홀로 남은 UTF-16 대리 코드(U+D800~U+DFFF: 판독 JSON 에서 반 쪽만 남은 이모지 등)는 utf-16 으로 적을 수 없어 글꼴에 못 넣는다.
    판독불명처럼 자리(상자)만 두고 글자는 None 으로 바꾼다. 돌려주는 값: 바꾼 글자 수."""
    bad = 0
    for ln in lines:
        for k, (c, b) in enumerate(ln.chars):
            if c and "\ud800" <= c <= "\udfff":
                ln.chars[k] = (None, b); bad += 1
    return bad


# 글리프 하나(.notdef, 빈 모양)만 든 768바이트 TrueType — 글꼴을 내장하지 않은 Identity 인코딩 CID 글꼴은 Acrobat 이 열 때마다
# "글꼴을 찾거나 만들 수 없습니다" 창을 띄우므로(검토에서 지적) Tesseract 처럼 빈 글꼴을 내장하고 모든 CID 를 글리프 0 으로 보낸다.
GLYPHLESS_TTF = base64.b64decode("".join((
    "AAEAAAAKAIAAAwAgT1MvMkTfRiQAAAEoAAAAYGNtYXAADABGAAABjAAAACxnbHlmAAAAAAAAAbwAAAABaGVhZC0B3dAAAACsAAAA",
    "NmhoZWEDcgNyAAAA5AAAACRobXR4A+gAAAAAAYgAAAAEbG9jYQAAAAAAAAG4AAAABG1heHAAAgACAAABCAAAACBuYW1lOToKEgAA",
    "AcAAAAEacG9zdAADAAAAAALcAAAAJAABAAAAAQAAQIw4el8PPPUAAwPoAAAAAOb1zngAAAAA5vXOeAAAAAAAAAAAAAAAAwACAAAA",
    "AAAAAAEAAANw/4gAAAPoAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAEAAAABAAAAAAAAAAAAAgAAAAAAAAAAAAAAAAAAAAAAAwPo",
    "AZAABQAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPz8/PwAAAAAAAANw/4gAAANwAHgA",
    "AAAAAAAAAAAAAAAAAAAgAAAD6AAAAAAAAgAAAAMAAAAUAAMAAQAAABQABAAYAAAAAgACAAAAAP//AAD//wABAAAAAAAAAAAAAAAA",
    "AAwAlgABAAAAAAABAAgAAAABAAAAAAACAAcACAABAAAAAAADABIADwABAAAAAAAEAAgAAAABAAAAAAAFAAsAIQABAAAAAAAGAAgA",
    "AAADAAEECQABABAALAADAAEECQACAA4APAADAAEECQADACQASgADAAEECQAEABAALAADAAEECQAFABYAbgADAAEECQAGABAALFll",
    "b25FT0NSUmVndWxhclllb25FT0NSIGdseXBobGVzc1ZlcnNpb24gMS4wAFkAZQBvAG4ARQBPAEMAUgBSAGUAZwB1AGwAYQByAFkA",
    "ZQBvAG4ARQBPAEMAUgAgAGcAbAB5AHAAaABsAGUAcwBzAFYAZQByAHMAaQBvAG4AIAAxAC4AMAAAAAIAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAQAA",
)))


def add_font(doc: pymupdf.Document, chars) -> tuple[int, dict]:
    """문서에 글리프 없는 Type0 글꼴 하나를 넣는다. CID 는 글자마다 1부터, ToUnicode 로 유니코드에 잇는다(확장 B 는 UTF-16 대리 쌍).
    폭은 전각 1000·반각 500. 글꼴 파일(GLYPHLESS_TTF)을 내장하고 CIDToGIDMap 으로 모든 CID 를 글리프 0 에 보낸다(보이지 않는 글이라 모양은 없어도 된다).
    돌려주는 값: (글꼴 xref, {글자: CID})."""
    cids = {c: i + 1 for i, c in enumerate(dict.fromkeys(c for c in chars if c))}
    cmap = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n/CMapName /YeonEOCR-UCS def\n/CMapType 2 def\n"
            "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n")
    items = list(cids.items())
    for k in range(0, len(items), 100):                                 # bfchar 한 묶음은 100개까지
        part = items[k:k + 100]
        cmap += f"{len(part)} beginbfchar\n" + "\n".join(f"<{cid:04X}> <{_u16(c)}>" for c, cid in part) + "\nendbfchar\n"
    cmap += "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
    half = " ".join(f"{cid}[500]" for c, cid in items if adv(c) != 1.0)
    tu = doc.get_new_xref(); doc.update_object(tu, "<<>>"); doc.update_stream(tu, cmap.encode())
    ff = doc.get_new_xref(); doc.update_object(ff, f"<</Length1 {len(GLYPHLESS_TTF)}>>"); doc.update_stream(ff, GLYPHLESS_TTF)
    c2g = doc.get_new_xref(); doc.update_object(c2g, "<<>>"); doc.update_stream(c2g, bytes(2 * (len(cids) + 1)))   # CID 0..n → 글리프 0
    desc = doc.get_new_xref()
    doc.update_object(desc, f"<</Type/FontDescriptor/FontName/{FONT_NAME}/Flags 4/FontBBox[0 {int(DESC * 1000)} 1000 {int(ASC * 1000)}]"
                            f"/ItalicAngle 0/Ascent {int(ASC * 1000)}/Descent {int(DESC * 1000)}/CapHeight {int(ASC * 1000)}/StemV 80/FontFile2 {ff} 0 R>>")
    cidf = doc.get_new_xref()
    doc.update_object(cidf, f"<</Type/Font/Subtype/CIDFontType2/BaseFont/{FONT_NAME}/CIDSystemInfo<</Registry(Adobe)/Ordering(Identity)/Supplement 0>>"
                            f"/FontDescriptor {desc} 0 R/DW 1000{f'/W[{half}]' if half else ''}/CIDToGIDMap {c2g} 0 R>>")
    f0 = doc.get_new_xref()
    doc.update_object(f0, f"<</Type/Font/Subtype/Type0/BaseFont/{FONT_NAME}/Encoding/Identity-H/DescendantFonts[{cidf} 0 R]/ToUnicode {tu} 0 R>>")
    return f0, cids


def _em(spans, chars) -> float:
    """줄의 글자 크기(전각 한 칸, pt): 이웃한 전각 글자 중심 간격의 중앙값(칸 = 글자 + 글자 사이). 없으면 전각 글자 상자 크기."""
    full = [s for s, c in zip(spans, chars) if adv(c) == 1.0] or spans
    size = float(np.median([e - s for s, e in full]))
    mids = [(s + e) / 2 for s, e in full]
    d = [b - a for a, b in zip(mids, mids[1:]) if 0.6 * size < b - a < 1.6 * size]
    return float(np.median(d)) if d else size


def line_ops(ln: Line, frame, to_pdf: pymupdf.Matrix, cids: dict) -> str:
    """한 줄의 내용 연산. 틈(반 칸 넘게 빈 자리) 없이 이어진 글자 구간마다 Tm 으로 시작점에 서서 구간 길이에 꼭 맞게 Tz(가로 비율)를 정해
    한 번에 쓴다 — 글자 사이가 0.1칸만 벌어져도 추출기(poppler 등)가 빈칸을 넣는다. 구간 사이의 큰 틈은 추출기가 빈칸으로 읽는다.
    글자 공간의 x 축(읽기 방향)과 y 축(글자 위쪽)은 쪽에 보이는 좌표에서 정한 뒤 to_pdf(/Rotate 되돌림·y 뒤집기)로 옮긴다."""
    fx0, fy0, fw, fh = frame
    kept = [(c, (fx0 + b[0] * fw, fy0 + b[1] * fh, fx0 + b[2] * fw, fy0 + b[3] * fh)) for c, b in ln.chars if c]
    if not kept:
        return ""
    cs = [c for c, _ in kept]; bx = [b for _, b in kept]
    v = ln.dir == "v"
    sp = [((b[1], b[3]) if v else (b[0], b[2])) for b in bx]
    fs = _em(sp, cs)
    if v:                                                               # 아래로 가는 줄, 글자 위쪽이 오른쪽
        cx = float(np.median([(b[0] + b[2]) / 2 for b in bx]))
        origin = lambda along: (cx - (ASC + DESC) / 2 * fs, along); u, w = (0, 1), (1, 0)
    else:                                                               # 오른쪽으로 가는 줄, 글자 위쪽이 위
        cy = float(np.median([(b[1] + b[3]) / 2 for b in bx]))
        origin = lambda along: (along, cy + (ASC + DESC) / 2 * fs); u, w = (1, 0), (0, -1)

    def tm(along: float) -> str:
        o = pymupdf.Point(origin(along)); p = o * to_pdf
        U = (o + pymupdf.Point(u)) * to_pdf - p; V = (o + pymupdf.Point(w)) * to_pdf - p
        return f"{U.x * fs:.3f} {U.y * fs:.3f} {V.x * fs:.3f} {V.y * fs:.3f} {p.x:.3f} {p.y:.3f} Tm"
    mids = [(a + b) / 2 for a, b in sp]
    wid = [adv(c) * fs for c in cs]
    for i in range(len(cs)):                                            # 반각 글자(숫자·라틴)가 전각 간격으로 찍혔으면(卷第12) 한 칸을 다 준다 —
        near = [abs(mids[j] - mids[i]) for j in (i - 1, i + 1) if 0 <= j < len(cs)]   # 반 칸만 주면 글자 사이 틈이 반 칸이 되어 '1 2' 로 읽힌다
        if wid[i] < fs and near and min(near) > 0.75 * fs:
            wid[i] = fs
    scale = [w / (adv(c) * fs) for w, c in zip(wid, cs)]                # 칸 / 글꼴 폭: 1(제 폭) 또는 2(반각을 전각 칸에)
    starts = [m - w / 2 for m, w in zip(mids, wid)]
    ends = [m + w / 2 for m, w in zip(mids, wid)]
    runs, cur = [], [0]
    for i in range(1, len(cs)):
        if starts[i] - ends[i - 1] > 0.5 * fs or scale[i] != scale[i - 1]:   # 틈, 또는 Tz 가 달라져야 하는 자리
            runs.append(cur); cur = []
        cur.append(i)
    runs.append(cur)
    ops = []
    for r in runs:
        natural = sum(adv(cs[i]) for i in r) * fs
        tz = max(50.0, min(200.0, (ends[r[-1]] - starts[r[0]]) / natural * 100)) if natural > 0 else 100.0
        ops.append(f"{tm(starts[r[0]])} {tz:.1f} Tz <{''.join(f'{cids[cs[i]]:04X}' for i in r)}> Tj")
    return " ".join(ops)


def text_state(page: pymupdf.Page) -> str:
    """쪽의 글자 상태: "none"(글자 없음) · "hidden"(보이지 않는 글자만 — 스캔 위 OCR 층) · "visible"(보이는 글자가 있음)."""
    spans = page.get_texttrace()
    if not spans:
        return "none"
    return "visible" if any(s.get("type", 0) != 3 and s.get("opacity", 1) > 0 for s in spans) else "hidden"


def strip_hidden_text(page: pymupdf.Page) -> tuple[bool, int]:
    """보이지 않는 글자(다른 OCR 층·이전에 넣은 이 글자층)를 지운다. 그림·선은 그대로. 쪽 전체를 가리는 지움(redaction)은 링크(목차·주석의
    바로가기)까지 지우므로 미리 받아 두고 다시 넣는다. 돌려주는 값: (지웠는가, 다시 넣은 링크 수). 지우는 법을 모르는 PyMuPDF 면 지우지 않고 False."""
    links = page.get_links()
    page.add_redact_annot(page.rect)
    try:
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=getattr(pymupdf, "PDF_REDACT_TEXT_REMOVE", 0))
    except (TypeError, AttributeError):                                # 선(graphics)을 지키는 선택이 없는 판: 선까지 지울 수 있어 하지 않는다
        for a in list(page.annots(types=[pymupdf.PDF_ANNOT_REDACT])):
            page.delete_annot(a)
        return False, 0
    fix = visible_to_pdf(page) * page.transformation_matrix             # insert_link 는 'from' 에 ~transformation_matrix 를 곱해 /Rect 로 삼는데,
    kept = 0                                                            #   돌린 쪽에서 그 행렬은 회전을 모른다(PyMuPDF 1.28) — 미리 되돌려 준다
    for link in links:
        try:
            link["from"] = link["from"] * fix; page.insert_link(link); kept += 1
        except Exception:                                               # PyMuPDF 가 다시 못 넣는 종류의 링크는 둘 수 없다
            pass
    return True, kept


def has_own_layer(page: pymupdf.Page) -> bool:
    """이 글자층(FONT_NAME 글꼴)의 글자가 쪽에 있는가."""
    return any(is_own_span(s) for s in page.get_texttrace())


def content_refs(doc: pymupdf.Document, page: pymupdf.Page) -> list[int]:
    """쪽 /Contents 의 내용 흐름 xref 목록. 흐름 하나(N 0 R)·직접 배열([a 0 R b 0 R])·간접 배열(N 0 R → [..]) 모두 흐름 번호로 푼다.
    간접 배열을 흐름으로 잘못 알고 `[N 0 R 새 0 R]` 로 쓰면 MuPDF 가 'object is not a stream' 으로 쪽을 백지로 그린다(검토에서 재현)."""
    kind, val = doc.xref_get_key(page.xref, "Contents")
    if kind == "null":
        return []
    if kind == "xref":
        n = int(val.split()[0])
        if doc.xref_is_stream(n):
            return [n]
        val = doc.xref_object(n)                                             # 간접 배열 — 그 안의 흐름들
    return [int(x) for x in re.findall(r"(\d+) 0 R", val)]


def _set_contents(doc: pymupdf.Document, page: pymupdf.Page, refs: list[int]) -> None:
    doc.xref_set_key(page.xref, "Contents", f"[{' '.join(f'{x} 0 R' for x in refs)}]" if refs else "null")


def strip_own_layer(doc: pymupdf.Document, page: pymupdf.Page) -> bool:
    """이전에 넣은 이 글자층만 지운다(다른 OCR 층은 그대로). 이 층은 /Contents 배열에 따로 붙인 내용 흐름(머리 'q BT 3 Tr /<이름> 1 Tf')이라
    그 흐름만 뺀다. 글꼴 자원은 둔다 — 다른 쪽과 같이 쓰는(물려받은) 자원이면 그 쪽의 글자가 깨진다. 다른 도구가 흐름을 하나로 합쳐 가려낼 수
    없으면 지우지 않고 False."""
    names = {f[4].encode() for f in page.get_fonts(full=True) if f[3] == FONT_NAME}
    refs = content_refs(doc, page)
    keep = [x for x in refs if not ((m := re.match(rb"\s*q BT 3 Tr /(\S+) 1 Tf", doc.xref_stream(x) or b"")) and m.group(1) in names)]
    if len(keep) == len(refs):
        return False
    _set_contents(doc, page, keep)
    return not has_own_layer(doc[page.number])


def _add_font_resource(doc: pymupdf.Document, page: pymupdf.Page, font_xref: int) -> str:
    """쪽 자원의 /Font 에 이 글꼴을 넣고 그 이름을 돌려준다. 자원이 위(Pages)에서 물려받은 것이면 먼저 쪽에 붙인다 — 쪽에 새 /Resources 를 만들면
    물려받던 스캔 그림을 잃는다. 경로 중간이 간접 객체면 그 객체에 직접 쓴다(xref_set_key 의 경로는 간접 객체를 건너지 못한다).
    이름은 FONT_KEY. 그 이름이 벌써 다른 글꼴(이전에 넣은 층, 자원을 같이 쓰는 다른 쪽의 층)을 가리키면 FYeonEOCR2·3…로 비켜 간다 —
    같은 이름에 새 글꼴을 덧쓰면 그 이름으로 쓴 옛 글자가 전부 다른 글자로 읽힌다."""
    if doc.xref_get_key(page.xref, "Resources")[0] == "null":
        x = page.xref
        while (p := doc.xref_get_key(x, "Parent"))[0] == "xref":
            x = int(p[1].split()[0])
            kind, val = doc.xref_get_key(x, "Resources")
            if kind != "null":
                doc.xref_set_key(page.xref, "Resources", val)
                break
    holder, key = page.xref, "Resources/Font"
    kind, val = doc.xref_get_key(holder, "Resources")
    if kind == "xref":
        holder, key = int(val.split()[0]), "Font"
    kind, val = doc.xref_get_key(holder, key)
    if kind == "xref":
        holder, key = int(val.split()[0]), ""
    name, k = FONT_KEY, 1
    while (cur := doc.xref_get_key(holder, f"{key}/{name}" if key else name))[0] != "null" and cur[1] != f"{font_xref} 0 R":
        k += 1; name = f"{FONT_KEY}{k}"
    doc.xref_set_key(holder, f"{key}/{name}" if key else name, f"{font_xref} 0 R")
    return name


def visible_to_pdf(page: pymupdf.Page) -> pymupdf.Matrix:
    """쪽에 보이는 좌표(CropBox 왼쪽 위 원점, y 아래로, /Rotate 적용 뒤) → PDF 사용자 공간. /Rotate 는 derotation_matrix 로 되돌리고,
    CropBox 기준 좌표 → PDF 는 CropBox 로 직접 만든다 — page.transformation_matrix 는 돌린 쪽에서 CropBox 위치를 빼고 준다(PyMuPDF 1.28)."""
    cb, mb = page.cropbox, page.mediabox
    return page.derotation_matrix * pymupdf.Matrix(1, 0, 0, -1, cb.x0, mb.y1 - cb.y0)


def write_lines(doc: pymupdf.Document, page: pymupdf.Page, lines: list[Line], frame, font_xref: int, cids: dict) -> None:
    """쪽에 글자층을 덧붙인다(기존 내용은 q/Q 로 감싸 그대로)."""
    to_pdf = visible_to_pdf(page)
    ops = [o for o in (line_ops(ln, frame, to_pdf, cids) for ln in lines) if o]
    if not ops:
        return
    key = _add_font_resource(doc, page, font_xref)                       # wrap 보다 먼저: 물려받은 자원을 쪽에 붙이기 전에 내용을 읽으면
    old = content_refs(doc, page)                                        #   MuPDF 가 그림 자원을 못 찾았다고 알린다(결과는 같아도 로그가 어지럽다)
    _set_contents(doc, page, old)                                        # 간접 배열(N 0 R → [..])은 흐름 배열로 바로 적는다 — 그대로 두면 wrap 뒤 백지
    page.wrap_contents()
    sx = doc.get_new_xref(); doc.update_object(sx, "<<>>")
    doc.update_stream(sx, ("q BT 3 Tr /" + key + " 1 Tf\n" + "\n".join(ops) + "\nET Q\n").encode())
    _set_contents(doc, page, content_refs(doc, page) + [sx])


def own_text(doc: pymupdf.Document, pno: int) -> list[str]:
    """쪽에서 이 글자층(FONT_NAME 글꼴)의 글만, 줄마다(검증용)."""
    out = []
    for b in doc[pno - 1].get_text("rawdict")["blocks"]:
        for l in b.get("lines", []):
            t = "".join(c["c"] for s in l["spans"] if is_own_span(s) for c in s["chars"])
            if t.strip():
                out.append(t)
    return out


def searchable_pdf(doc: pymupdf.Document, pages: list[int], page_data, out: str | Path, strip_old: bool = True, log=print, step=None,
                   should_stop=None) -> dict:
    """pages 의 쪽에 글자층을 넣어 out 에 저장한다(doc 을 바꾼다 — 버릴 사본을 넘길 것). page_data(쪽) → (PageReading, 쪽 이미지, 이미지 틀, 세로쓰기인가).
    쪽 이미지는 쪽마다 불러 자리만 잡고 버린다(700쪽 책도 메모리에 이미지를 쌓지 않게). step(끝난 수, 전체 수) 를 주면 쪽마다 부른다.
    should_stop() 이 참이면 쪽 사이·저장 전후에 TextlayerCancelled 를 낸다. 검증까지 임시 파일에서 마쳐 기존 출력은 보존한다.
    이전에 넣은 이 글자층은 strip_old 와 상관없이 지운다(두 번 넣을 까닭이 없고, 글꼴 이름이 겹치면 옛 글자가 깨진다). 넣은 쪽이 없으면 저장하지 않는다.
    돌려주는 값: {"pages": 넣은 쪽 수, "chars": 글자 수, "how": {방법: 줄 수}, "skipped": {쪽: 까닭}, "stripped": 옛 글자층을 지운 쪽 수,
    "own_stripped": 이전의 이 글자층만 지운 쪽 수, "links_kept": 지우면서 다시 넣은 링크 수, "bad_chars": 글꼴에 못 넣어 뺀 글자 수(깨진 대리 코드),
    "mismatch": [다시 읽은 글이 넣은 글과 다른 쪽]}."""
    def check_stop():
        if should_stop and should_stop():
            raise TextlayerCancelled()

    check_stop()
    plans: dict[int, list[Line]] = {}; frames: dict[int, tuple] = {}
    stats = {"pages": 0, "chars": 0, "how": {}, "skipped": {}, "stripped": 0, "own_stripped": 0, "links_kept": 0, "bad_chars": 0, "mismatch": []}
    total = len(pages) * 2
    for i, n in enumerate(sorted(pages)):
        check_stop()
        state = text_state(doc[n - 1])
        if state == "visible":
            stats["skipped"][n] = "보이는 글자가 있는 쪽(스캔이 아님)"
        else:
            pr, img, frame, vertical = page_data(n)
            plans[n] = plan_page(pr, img, frame, layer_chars(doc, n) if state == "hidden" else None, vertical); frames[n] = frame
            stats["bad_chars"] += drop_bad_chars(plans[n])
            del img
        if step: step(i + 1, total)
    check_stop()
    if plans:
        font_xref, cids = add_font(doc, [c for ls in plans.values() for ln in ls for c, _ in ln.chars])
    for i, (n, lines) in enumerate(list(plans.items())):
        check_stop()
        page = doc[n - 1]
        if strip_old and text_state(page) == "hidden":
            ok, kept = strip_hidden_text(page)
            if ok:
                stats["stripped"] += 1; stats["links_kept"] += kept
            else:
                log("  주의: 이 PyMuPDF 판은 숨은 글자층만 골라 지우지 못해 원래 글자층을 그대로 둡니다(pip install -U pymupdf).")
                strip_old = False
        if has_own_layer(page):                                        # 남아 있는 이전의 이 글자층(--keep-old-layer): 그것만 지우고 넣는다
            if strip_own_layer(doc, page):
                stats["own_stripped"] += 1; page = doc[n - 1]
            else:
                stats["skipped"][n] = "이전에 넣은 이 글자층을 가려내 지우지 못함(--keep-old-layer 없이 다시 실행)"; del plans[n]
                if step: step(len(pages) + i + 1, total)
                continue
        write_lines(doc, page, lines, frames[n], font_xref, cids)
        stats["pages"] += 1; stats["chars"] += sum(1 for ln in lines for c, _ in ln.chars if c)
        for ln in lines:
            stats["how"][ln.how] = stats["how"].get(ln.how, 0) + 1
        if step: step(len(pages) + i + 1, total)
    check_stop()
    if not stats["pages"]:                                              # 넣은 쪽이 없으면 사본을 만들지 않는다(cli 가 '넣은 쪽 없음'으로 알린다)
        if step: step(total, total)
        return stats
    from .state import write_atomic_with
    out = Path(out); out.parent.mkdir(parents=True, exist_ok=True)

    def save_and_check(tmp):
        check_stop()
        doc.save(tmp, garbage=3, deflate=True)
        check_stop()
        with pymupdf.open(tmp) as check:                              # 검증·중지가 끝난 뒤에만 최종 이름으로 바꾼다
            for n, lines in plans.items():
                check_stop()
                want = "".join(c for ln in lines for c, _ in ln.chars if c)
                if re.sub(r"\s", "", "".join(own_text(check, n))) != want:  # 빈칸은 추출기가 큰 틈에 넣는 것이라 세지 않는다
                    stats["mismatch"].append(n)
        check_stop()

    write_atomic_with(out, save_and_check)
    if step: step(total, total)
    return stats
