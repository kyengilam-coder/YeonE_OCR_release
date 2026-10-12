"""3차 글자 판별(adjudication). 1차·2차 판독이 갈린 자리(〔a/b〕)마다 그 글자 하나를 원본에서 크게 잘라 모델에 객관식으로 묻는다.

왜 따로 하는가
- 1차(쪽 전체)·2차(영역 조각)에서는 글자 하나에 토큰이 거의 배정되지 않아 획 하나 차이(入/人·曰/日·于/干)가 운에 맡겨진다.
- 글자 ±2자 창을 4~6배로 키우면 모델이 갈고리·점 하나를 볼 수 있다(지침 24장: 전체 → 영역 → 열 → 글자 순 확대를 코드로 옮긴 것).
- 자형이 보이면 확정하고, 보이지 않으면 표시를 그대로 둔다. 문맥으로 고르게 하지 않는다(지침 2·21).

자리(조각 좌표)는 세 단계로 잡는다(0.6.2): ① 글자층의 글자별 상자(가장 정확) → ② 쪽 이미지의 먹 띠(줄)·먹 구간(글자) 실측 →
③ 1차 판독의 영역 좌표를 줄 수·글자 수로 균등 분할(이전 방식). 300dpi 한자 렌더 실측(세로·가로, 22pt·10.5pt, 줄 길이 제각각, 영역 상자 잡음 8%)에서
목표 글자가 창 안에 든 비율은 ③ 87% → ① 98%·② 98% 였다(tests GlyphLocate). 어느 방법을 썼는지는 기록(adjudications[].loc)에 남는다.

한 쪽의 자리들을 8장씩 한 호출에 묶어 묻고(adjudicate_many), 빠지거나 창이 안 맞는 자리는 낱개로 다시 묻는다.

눈가림(0.6.5) — 모델이 문맥으로 고르지 못하게 묻는 방식 자체를 바꿨다.
- 앞뒤 글자를 주지 않는다. 판독기에는 GlyphAsk(후보·조각 안 순번·글자 수)만 넘어가고, 앞뒤 글자는 답을 받은 뒤 코드 검사(window_ok)에만 쓴다.
- 자리를 실측했으면(글자층, 또는 먹 셈이 글자 수와 꼭 맞을 때) 조각 바깥에 흰 띠를 덧대 빨간 눈금으로 가리킨다(글자 획은 가리지 않는다).
- 후보 차례는 자리 정보로 만든 고정 씨앗으로 섞는다(먼저 적힌 1차 글자를 고르는 위치 편향을 던다).
- 판별기는 지침 전문 대신 자형 전용 시스템 지시(prompts.glyph_system_prompt)를 쓰고, 답은 w(보인 글) → e(자형) → c(고른 글자) 차례로 쓴다.
- 원본 글자 크기가 문턱(min_px)보다 작으면 묻지 않고 미정으로 둔다(지침 24: 확대로 없는 획이 살아나지 않는다).
- blind=True 면 확정한 자리를 후보 없이 한 번 더 읽게 해(identify) 객관식 답과 같을 때만 확정한다.

합의 판독 의심(0.6.8, suspect_page) — 1차·2차가 같게 틀린 글자는 〔a/b〕 가 생기지 않는다. 표시 밖 글자가 PDF 글자층과 1:1 로 다르고 두 글자가
자형 혼동 묶음이면 같은 방식(후보 [합의 글자, 글자층 글자])으로 묻고, 자형이 글자층 쪽이면 본문은 그대로 두고 확인 항목만 단다(지침 20).
"""
from __future__ import annotations
import functools, re, time, unicodedata
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
import numpy as np
from PIL import Image
from .schema import PageReading, Region, Column, Uncertain, TEXT_KINDS, bbox_frac, GlyphChoice, GlyphAsk, duplicate_region_ids
from .merge import columns_in_reading_order, MARK_RE, nfc_text
from .layout import ink_runs, merge_glyph_runs

MARK_AB = re.compile(r"〔([^〔〕/]{1,2})/([^〔〕]{1,2})〕")


@dataclass
class Site:
    region_id: str
    col_index: int          # 영역 안 읽기 순서 열(가로쓰기면 줄) 번호, 0부터
    n_cols: int
    char_index: int         # 표시를 뺀 글에서 그 글자의 위치, 0부터
    n_chars: int
    a: str                  # 1차 글자
    b: str                  # 2차 글자
    pre: str
    post: str
    mark: str               # 본문에 있는 표시 글 그대로 (〔a/b〕)


@dataclass
class GlyphWindow:
    """자리 창 하나. box 는 쪽 비율 좌표(x0, y0, x1, y1), how 는 잡은 방법(layer·ink·uniform).
    pos·count 는 창 안 읽기 방향 순번(1부터, 대상 첫 글자)과 창의 글자 수, measured 는 그 순번이 실측인지(아니면 어림).
    target 은 대상 글자 상자(실측일 때만 — 눈금을 그린다), em_px 는 원본 이미지에서 잰 글자 크기(픽셀, 0=모름)."""
    box: tuple
    how: str
    pos: int = 0
    count: int = 0
    measured: bool = False
    target: tuple | None = None
    em_px: float = 0.0


def candidate_order(candidates, *salt) -> tuple[str, ...]:
    """후보 차례를 고정 씨앗(자리 정보)으로 섞는다. 먼저 적힌 후보(보통 1차 판독)를 고르는 위치 편향을 덜고, 같은 자리는 늘 같은 차례라
    다시 실행해도 질문이 흔들리지 않는다."""
    import hashlib, random
    out = [c for c in dict.fromkeys(candidates) if c]
    seed = hashlib.sha1("|".join(str(v) for v in (*salt, *out)).encode("utf-8")).hexdigest()
    random.Random(int(seed[:16], 16)).shuffle(out)
    return tuple(out)


def make_ask(site: "Site", win: GlyphWindow, candidates, *salt, tick: bool = False) -> GlyphAsk:
    """모델에 보낼 질문(후보는 섞고, 앞뒤 글자는 넣지 않는다). 후보의 글자 수가 다르면(〔日一/旦〕) 대상 길이를 1차 후보로 정할 수 없으므로
    k=0(1~2자)으로 두고 실측·눈금을 주장하지 않는다(눈금이 이웃 글자까지 덮지 않게)."""
    lens = {len(c) for c in candidates if c} or {max(1, len(site.a))}
    same = len(lens) == 1
    return GlyphAsk(candidates=candidate_order(candidates, *salt, site.region_id, site.col_index, site.char_index) if candidates else (),
                    pos=win.pos, k=max(lens) if same else 0, count=win.count, measured=win.measured and same, tick=tick and same)


def plain(text: str) -> str:
    """표시를 1차 글자로 접은 글(길이·위치 계산용)."""
    def rep(m):
        inner = m.group(0)[1:-1]
        if inner.startswith("疑:"):
            return inner[2:].strip()
        if "/" in inner:
            return inner.split("/")[0]
        return ""
    return MARK_RE.sub(rep, text)


def find_sites(merged: PageReading) -> list[Site]:
    """병합 판독문의 〔a/b〕 자리를 모두 찾는다(한두 글자짜리만)."""
    out: list[Site] = []
    duplicate = duplicate_region_ids(merged)
    for reg in merged.regions:
        if reg.kind not in TEXT_KINDS or reg.id in duplicate:
            continue
        cols = columns_in_reading_order(reg)
        for ci, col in enumerate(cols):
            text = col.text or ""
            for m in MARK_AB.finditer(text):
                before = plain(text[:m.start()])
                after = plain(text[m.end():])
                out.append(Site(reg.id, ci, len(cols), len(before), len(before) + len(m.group(1)) + len(after),
                                m.group(1), m.group(2), before[-4:], after[:4], m.group(0)))
    return out


def glyph_box(bbox: list[int], site: Site, horizontal: bool, span: int = 2) -> tuple[float, float, float, float]:
    """영역 좌표와 줄·글자 번호로 글자 ±span 자 창의 비율 좌표를 추정한다."""
    x0, y0, x1, y1 = bbox_frac(bbox)
    n_cols, L = max(1, site.n_cols), max(1, site.n_chars)
    k = len(site.a)
    if horizontal:                                  # 줄은 위→아래, 글자는 왼→오른쪽
        lh = (y1 - y0) / n_cols; cw = (x1 - x0) / L
        ya, yb = y0 + site.col_index * lh - lh * 0.35, y0 + (site.col_index + 1) * lh + lh * 0.35
        xa, xb = x0 + max(0, site.char_index - span) * cw, x0 + min(L, site.char_index + k + span) * cw
    else:                                           # 열은 오른→왼쪽(col_from_right 1 이 맨 오른쪽), 글자는 위→아래
        cw = (x1 - x0) / n_cols; ch = (y1 - y0) / L
        xb = x1 - site.col_index * cw + cw * 0.35; xa = x1 - (site.col_index + 1) * cw - cw * 0.35
        ya, yb = y0 + max(0, site.char_index - span) * ch, y0 + min(L, site.char_index + k + span) * ch
    return (max(0.0, xa), max(0.0, ya), min(1.0, xb), min(1.0, yb))


def column_plain(merged: PageReading, site: Site) -> str:
    """자리가 속한 열(줄)의 표시를 접은 글."""
    for reg in merged.regions:
        if reg.id == site.region_id:
            cols = columns_in_reading_order(reg)
            if site.col_index < len(cols):
                return plain(cols[site.col_index].text or "")
    return ""


def _nfc(s: str) -> str:
    return "".join(unicodedata.normalize("NFC", ch) for ch in s)


def _map_index(src: str, dst: str, i: int, near: int = 2) -> int | None:
    """src 의 i 번째 글자가 dst 의 몇 번째 글자에 해당하는지. 일치 구간 안이면 그대로, 일치 구간에서 near 글자 안에 붙어 있으면
    앞뒤 일치 구간 사이를 비례로. 더 멀면(어느 구간과도 안 맞는 긴 자리) None — 그때는 글자층을 믿지 않는다."""
    return _map_index_ex(src, dst, i, near)[0]


def _map_index_ex(src: str, dst: str, i: int, near: int = 2) -> tuple[int | None, bool]:
    """_map_index 와 같고, 대응이 꼭 맞는지(일치 구간 안이거나, 어긋난 틈의 길이가 두 글에서 같음)를 함께 돌려준다.
    글자층이 대상 글자를 빠뜨리거나 쪼갠 자리(틈 길이가 다름)는 비례로 어림한 것이라 실측으로 치지 않는다(눈금을 그리지 않게)."""
    if not src or not dst:
        return None, False
    blocks = [b for b in SequenceMatcher(None, src, dst, autojunk=False).get_matching_blocks() if b.size]
    if not blocks:
        return None, False
    before = after = None
    for b in blocks:
        if b.a <= i < b.a + b.size:
            return b.b + (i - b.a), True
        if b.a + b.size <= i:
            before = b
        elif after is None:
            after = b
    d_before = i - (before.a + before.size) if before else None          # 앞 일치 구간 끝에서 떨어진 거리(0=바로 다음)
    d_after = (after.a - i - 1) if after else None                        # 뒤 일치 구간 시작까지 거리(0=바로 앞)
    if (d_before is None or d_before > near) and (d_after is None or d_after > near):
        return None, False
    if before and after:
        a0, b0 = before.a + before.size - 1, before.b + before.size - 1
        a1, b1 = after.a, after.b
        if a1 <= a0:
            return b0, False
        return max(0, min(len(dst) - 1, round(b0 + (i - a0) * (b1 - b0) / (a1 - a0)))), (a1 - a0) == (b1 - b0)
    if before:
        j = before.b + before.size - 1 + (i - (before.a + before.size - 1))
        if j > len(dst) - 1:
            return None, False                                             # 줄 끝을 넘어가면(글자층이 끝 글자를 빠뜨림) 믿지 않는다
        return j, (len(src) - (before.a + before.size)) == (len(dst) - (before.b + before.size))
    j = after.b - (after.a - i)
    return (j, after.a == after.b) if j >= 0 else (None, False)


def _frame(frame) -> tuple[float, float, float, float]:
    """(x0, y0, 너비, 높이). (너비, 높이) 둘만 주면 원점 0."""
    if frame is None:
        return (0.0, 0.0, 0.0, 0.0)
    if len(frame) == 2:
        return (0.0, 0.0, float(frame[0]), float(frame[1]))
    return tuple(float(v) for v in frame)


def locate_by_layer(site: Site, region_bbox: list[int], col_text: str, layer_lines: list[dict], frame,
                    horizontal: bool, span: int = 2, min_ratio: float = 0.5) -> tuple[float, float, float, float] | None:
    """글자층(글자별 상자)에서 자리 창 좌표를 찾는다(_layer_window 의 좌표만). 못 찾으면 None."""
    w = _layer_window(site, region_bbox, col_text, layer_lines, frame, horizontal, span, min_ratio)
    return w.box if w else None


def _layer_window(site: Site, region_bbox: list[int], col_text: str, layer_lines: list[dict], frame,
                  horizontal: bool, span: int = 2, min_ratio: float = 0.5, size=None) -> GlyphWindow | None:
    """글자층(글자별 상자)에서 자리를 찾는다. size=(너비, 높이)는 쪽 이미지 픽셀 크기(글자 크기를 픽셀로 잴 때).
    - frame: 쪽 이미지가 덮는 PDF 좌표 틀 (x0, y0, 너비, 높이) — pdfio.page_frame. 내장 스캔은 쪽과 최대 10% 어긋날 수 있다.
    - 영역 상자(읽기 방향과 직각으로 3% 넓힘)에 든 글자만 쓰고, 열 글과 가장 닮은 줄을 고른다. 닮기가 비슷한 줄(0.1 안)이 여럿이면
      줄 번호의 균등 추정 위치에 가장 가까운 줄(되풀이되는 소주·처방문 대비).
    - 열 글의 글자 번호를 그 줄의 글자 번호로 옮긴다(공백은 빼고 셈). 일치 구간에서 2자 넘게 떨어진 자리는 믿지 않는다.
    창은 ±span 자, 직각 방향 0.35자·읽기 방향 0.15자를 더 둔다. 못 찾으면 None."""
    fx0, fy0, fw, fh = _frame(frame)
    if not fw or not fh or not col_text or not layer_lines:
        return None
    rx0, ry0, rx1, ry1 = bbox_frac(region_bbox)
    if horizontal:
        ry0, ry1 = ry0 - 0.03, ry1 + 0.03
    else:
        rx0, rx1 = rx0 - 0.03, rx1 + 0.03
    want = "h" if horizontal else "v"
    src_all = _nfc(col_text)
    src = "".join(ch for ch in src_all if not ch.isspace())
    idx = site.char_index - sum(1 for ch in src_all[:site.char_index] if ch.isspace())      # 공백을 뺀 글자 번호
    n_cols = max(1, site.n_cols)
    est = (ry0 + (site.col_index + 0.5) / n_cols * (ry1 - ry0)) if horizontal else (rx1 - (site.col_index + 0.5) / n_cols * (rx1 - rx0))
    cands = []
    for ln in layer_lines:
        if ln.get("dir") != want:
            continue
        chars = []
        for ch, bb in ln.get("chars") or []:
            cx, cy = ((bb[0] + bb[2]) / 2 - fx0) / fw, ((bb[1] + bb[3]) / 2 - fy0) / fh
            if rx0 <= cx <= rx1 and ry0 <= cy <= ry1:
                chars.append((ch, bb))
        if len(chars) < 2:
            continue
        dst = "".join(_nfc(c) for c, _ in chars)
        r = SequenceMatcher(None, src, dst, autojunk=False).ratio()
        if r < min_ratio:
            continue
        mid = (sum((bb[1] + bb[3]) / 2 for _, bb in chars) / len(chars) - fy0) / fh if horizontal else (sum((bb[0] + bb[2]) / 2 for _, bb in chars) / len(chars) - fx0) / fw
        cands.append((r, abs(mid - est), chars, dst))
    if not cands:
        return None
    best_r = max(c[0] for c in cands)
    r, _, chars, dst = min((c for c in cands if c[0] >= best_r - 0.1), key=lambda c: c[1])
    j, exact = _map_index_ex(src, dst, idx)
    if j is None:
        return None
    k = max(1, len(site.a))
    lo = max(0, j - span)
    win = chars[lo: min(len(chars), j + k + span)]
    if not win:
        return None
    x0 = min(bb[0] for _, bb in win); y0 = min(bb[1] for _, bb in win); x1 = max(bb[2] for _, bb in win); y1 = max(bb[3] for _, bb in win)
    em = (x1 - x0) if horizontal else (y1 - y0)
    em = em / max(1, len(win))
    px, py = (0.15 * em, 0.35 * em) if horizontal else (0.35 * em, 0.15 * em)
    box = (max(0.0, (x0 - px - fx0) / fw), max(0.0, (y0 - py - fy0) / fh), min(1.0, (x1 + px - fx0) / fw), min(1.0, (y1 + py - fy0) / fh))
    tg = chars[j: j + k] if exact else []
    target = None
    if tg:
        tx0 = min(bb[0] for _, bb in tg); ty0 = min(bb[1] for _, bb in tg); tx1 = max(bb[2] for _, bb in tg); ty1 = max(bb[3] for _, bb in tg)
        target = ((tx0 - fx0) / fw, (ty0 - fy0) / fh, (tx1 - fx0) / fw, (ty1 - fy0) / fh)
    em_px = (em / fw * size[0] if horizontal else em / fh * size[1]) if size else 0.0
    return GlyphWindow(box, "layer", pos=j - lo + 1, count=len(win), measured=exact, target=target, em_px=em_px)


def page_ink(img: Image.Image, thr: int = 110) -> np.ndarray:
    """쪽 이미지의 먹(어두운 픽셀) 배열. 한 쪽에서 여러 자리를 찾을 때 한 번만 만든다."""
    return np.asarray(img.convert("L")) < thr


def locate_by_ink(img, region_bbox: list[int], site: Site, horizontal: bool, span: int = 2,
                  thr: int = 110, density: float = 0.002) -> tuple[float, float, float, float] | None:
    """쪽 이미지의 먹으로 자리 창 좌표를 찾는다(_ink_window 의 좌표만). 못 찾으면 None."""
    w = _ink_window(img, region_bbox, site, horizontal, span, thr, density)
    return w.box if w else None


def _ink_window(img, region_bbox: list[int], site: Site, horizontal: bool, span: int = 2,
                thr: int = 110, density: float = 0.002, col_text: str = "") -> GlyphWindow | None:
    """쪽 이미지의 먹으로 자리를 찾는다(img 는 PIL 이미지 또는 page_ink 배열).
    - 영역 안에서 줄(세로쓰기는 열) 띠를 실측한다. 띠 수가 줄 수와 같으면 번호로 고르고, 다르면 균등 추정 중심이 든 띠가 보통 두께
      (중앙값의 0.6~1.5배)일 때만 그 띠를 쓴다(영역 가장자리에 걸친 이웃 줄 조각을 고르지 않게). 띠 수가 줄 수와 크게 다르면 None.
    - 줄 안의 먹 구간을 글자로 묶어 수가 글자 수와 꼭 같을 때만 번호로 고르고, 아니면 줄의 먹 범위 안에서 균등 분할하되
      창 가장자리를 먹 구간 경계로 내민다(글자를 가르지 않게)."""
    a_all = img if isinstance(img, np.ndarray) else page_ink(img, thr)
    H, W = a_all.shape
    fx0, fy0, fx1, fy1 = bbox_frac(region_bbox)
    X0, Y0, X1, Y1 = int(fx0 * W), int(fy0 * H), max(int(fx0 * W) + 2, int(fx1 * W)), max(int(fy0 * H) + 2, int(fy1 * H))
    a = a_all[Y0:Y1, X0:X1]
    if a.size == 0:
        return None
    cross = a.mean(axis=1) if horizontal else a.mean(axis=0)            # 줄과 직각 방향 투영 → 줄 띠
    bands = ink_runs(cross, density, 4)
    n_cols = max(1, site.n_cols)
    if not bands or abs(len(bands) - n_cols) > max(1, round(0.34 * n_cols)):
        return None
    if not horizontal:
        bands = bands[::-1]                                              # 세로쓰기: 오른쪽 열이 0
    if len(bands) == n_cols:
        band = bands[site.col_index]
    else:
        est = (site.col_index + 0.5) / n_cols * len(cross)
        if not horizontal:
            est = len(cross) - est
        inside = [b for b in bands if b[0] <= est < b[1]]
        if not inside:
            return None
        band = inside[0]
        med = sorted(b[1] - b[0] for b in bands)[len(bands) // 2]
        if not (0.6 * med <= band[1] - band[0] <= 1.5 * med):
            return None
    thick = max(2, band[1] - band[0])
    sub = a[band[0]:band[1], :] if horizontal else a[:, band[0]:band[1]]
    along = sub.mean(axis=0) if horizontal else sub.mean(axis=1)        # 줄 방향 투영 → 글자 구간
    runs = ink_runs(along, density, 2)
    if not runs:
        return None
    glyphs = merge_glyph_runs(runs, thick)
    L = max(1, site.n_chars); k = max(1, len(site.a)); i = site.char_index
    gi, gL = i, L                                                        # 먹 구간에는 빈칸이 없으므로 글자 셈은 공백을 빼고
    if col_text and len(col_text) == site.n_chars:
        gi = i - sum(1 for ch in col_text[:i] if ch.isspace()); gL = max(1, sum(1 for ch in col_text if not ch.isspace()))
    elif col_text and any(ch.isspace() for ch in col_text):
        gL = -1                                                           # 글과 자리 셈이 안 맞으면 실측으로 치지 않는다
    exact = len(glyphs) == gL
    tgt = None
    if exact:
        g0, g1 = max(0, gi - span), min(len(glyphs) - 1, gi + k - 1 + span)
        lo = glyphs[g0][0]; hi = glyphs[g1][1]
        pos, count = gi - g0 + 1, g1 - g0 + 1
        if gi < len(glyphs):
            tgt = (glyphs[gi][0], glyphs[min(len(glyphs) - 1, gi + k - 1)][1])
    else:
        pos, count = _approx_pos(col_text, site, span)
        s0, s1 = runs[0][0], runs[-1][1]
        pitch = (s1 - s0) / L
        lo = s0 + max(0, i - span) * pitch; hi = s0 + min(L, i + k + span) * pitch
        for r in runs:                                                   # 글자를 가르지 않게 창을 먹 구간 경계로 내민다
            if r[0] <= lo < r[1]: lo = r[0]
            if r[0] < hi <= r[1]: hi = r[1]
    lo, hi = int(lo), int(hi)
    if hi - lo < 2:
        return None
    m = int(0.35 * thick)
    if horizontal:
        box = (X0 + lo, Y0 + band[0] - m, X0 + hi, Y0 + band[1] + m)
    else:
        box = (X0 + band[0] - m, Y0 + lo, X0 + band[1] + m, Y0 + hi)
    target = None
    if tgt:
        if horizontal:
            target = ((X0 + tgt[0]) / W, (Y0 + band[0]) / H, (X0 + tgt[1]) / W, (Y0 + band[1]) / H)
        else:
            target = ((X0 + band[0]) / W, (Y0 + tgt[0]) / H, (X0 + band[1]) / W, (Y0 + tgt[1]) / H)
    box = (max(0.0, box[0] / W), max(0.0, box[1] / H), min(1.0, box[2] / W), min(1.0, box[3] / H))
    return GlyphWindow(box, "ink", pos=pos, count=count, measured=exact, target=target, em_px=float(band[1] - band[0]))


def _approx_pos(col_text: str, site: Site, span: int = 2) -> tuple[int, int]:
    """실측하지 못한 창에서 대상 글자의 어림 순번과 창의 글자 수(공백은 세지 않는다)."""
    L = max(1, site.n_chars); k = max(1, len(site.a)); i = site.char_index
    lo, hi = max(0, i - span), min(L, i + k + span)
    if col_text and len(col_text) >= hi:
        return (sum(1 for ch in col_text[lo:i] if not ch.isspace()) + 1, sum(1 for ch in col_text[lo:hi] if not ch.isspace()))
    return (i - lo + 1, hi - lo)


def _uniform_em(region_bbox: list[int], site: Site, horizontal: bool, size) -> float:
    """균등 분할 창의 글자 크기 어림(픽셀): 읽기 방향 글자 간격과 줄 간격 가운데 작은 쪽(둘 다 실제 글자보다 조금 크다)."""
    if not size:
        return 0.0
    x0, y0, x1, y1 = bbox_frac(region_bbox)
    W, H = size; L = max(1, site.n_chars); n = max(1, site.n_cols)
    along, perp = ((x1 - x0) * W / L, (y1 - y0) * H / n) if horizontal else ((y1 - y0) * H / L, (x1 - x0) * W / n)
    return float(min(along, perp))


def locate_glyph(img, region_bbox: list[int], site: Site, horizontal: bool, col_text: str = "",
                 layer_lines: list[dict] | None = None, frame=None, span: int = 2) -> GlyphWindow:
    """자리 창(GlyphWindow)을 글자층 → 먹 실측 → 균등 분할 순으로 잡는다. img 는 PIL 이미지 또는 page_ink 배열
    (여러 자리면 배열을 한 번 만들어 넘긴다). 창 안 순번·글자 크기도 함께 잰다."""
    size = None
    if img is not None:
        size = (img.shape[1], img.shape[0]) if isinstance(img, np.ndarray) else img.size
    if layer_lines and frame:
        try:
            w = _layer_window(site, region_bbox, col_text, layer_lines, frame, horizontal, span, size=size)
            if w: return w
        except Exception:
            pass
    if img is not None:
        try:
            w = _ink_window(img, region_bbox, site, horizontal, span, col_text=col_text)
            if w: return w
        except Exception:
            pass
    pos, count = _approx_pos(col_text, site, span)
    return GlyphWindow(glyph_box(region_bbox, site, horizontal, span), "uniform", pos=pos, count=count, em_px=_uniform_em(region_bbox, site, horizontal, size))


def glyph_window(img, region_bbox: list[int], site: Site, horizontal: bool, col_text: str = "",
                 layer_lines: list[dict] | None = None, frame=None, span: int = 2) -> tuple[tuple[float, float, float, float], str]:
    """자리 창 좌표와 그 방법("layer"·"ink"·"uniform") — locate_glyph 의 앞 두 값."""
    w = locate_glyph(img, region_bbox, site, horizontal, col_text, layer_lines, frame, span)
    return w.box, w.how


TICK_RGB = (214, 0, 0)


def glyph_crop(img: Image.Image, box: tuple[float, float, float, float], zoom: int = 6, max_side: int = 1800,
               target: tuple | None = None, horizontal: bool = False) -> Image.Image:
    """창을 잘라 키운다. target(대상 글자의 쪽 비율 상자)을 주면 조각 바깥에 흰 띠를 덧대(세로쓰기는 오른쪽, 가로쓰기는 위)
    대상 글자 길이만큼 빨간 눈금(꺾쇠)을 그린다 — 글자 획 위에는 아무것도 그리지 않는다."""
    W, H = img.size
    cx0, cy0 = int(box[0] * W), int(box[1] * H)
    cx1, cy1 = max(cx0 + 4, int(box[2] * W)), max(cy0 + 4, int(box[3] * H))
    crop = img.crop((cx0, cy0, cx1, cy1))
    f = min(zoom, max_side / max(1, crop.width, crop.height))
    if f > 1:
        crop = crop.resize((int(crop.width * f), int(crop.height * f)), Image.LANCZOS)
    if target is None:
        return crop
    sx, sy = crop.width / max(1, cx1 - cx0), crop.height / max(1, cy1 - cy0)
    if horizontal:
        a0, a1, n = (target[0] * W - cx0) * sx, (target[2] * W - cx0) * sx, crop.width
    else:
        a0, a1, n = (target[1] * H - cy0) * sy, (target[3] * H - cy0) * sy, crop.height
    a0, a1 = max(0.0, a0), min(float(n), a1)
    if a1 - a0 < 3:
        return crop
    from PIL import ImageDraw
    im = crop.convert("RGB")
    strip = max(14, round(0.18 * (crop.height if horizontal else crop.width)))
    t = max(2, strip // 6); serif = max(4, strip // 2)
    if horizontal:
        canvas = Image.new("RGB", (im.width, im.height + strip), "white"); canvas.paste(im, (0, strip))
        d = ImageDraw.Draw(canvas); y = strip // 3
        d.rectangle([a0, y, a1, y + t], fill=TICK_RGB)
        d.rectangle([a0, y, a0 + t, y + serif], fill=TICK_RGB); d.rectangle([a1 - t, y, a1, y + serif], fill=TICK_RGB)
    else:
        canvas = Image.new("RGB", (im.width + strip, im.height), "white"); canvas.paste(im, (0, 0))
        d = ImageDraw.Draw(canvas); x = im.width + strip - strip // 3
        d.rectangle([x - t, a0, x, a1], fill=TICK_RGB)
        d.rectangle([x - serif, a0, x, a0 + t], fill=TICK_RGB); d.rectangle([x - serif, a1 - t, x, a1], fill=TICK_RGB)
    return canvas


def _context_after(site: Site, done) -> tuple[str, str]:
    """기록한 앞뒤 글자(pre·post)에 같은 차례에서 이 열에 이미 바꾼 자리(done: [(글자 번호, 1차 글자, 고른 글자)], 찾을 때의 글 기준)를
    고른 글자로 넣은 것 — 지금 글에서 제자리 옆에 보여야 할 글자. find_sites 는 이웃 표시를 1차 글자로 접어 앞뒤 글자를 적으므로
    이웃이 다른 후보(〔於/不〕→不)나 길이가 다른 후보로 정해지면 기록 그대로는 제자리의 이웃이 안 맞아 보인다."""
    ci, la = site.char_index, len(site.a)
    pre, post = site.pre or "", site.post or ""
    p0, q0 = ci - len(pre), ci + la                                     # 앞 글자·뒤 글자 창의 첫 글자 번호
    for j, a, c in sorted(done, reverse=True):                          # 뒤쪽부터 바꿔야 앞쪽 번호가 그대로다
        if p0 < j + len(a) <= ci:
            pre = pre[:max(0, j - p0)] + c + pre[j + len(a) - p0:]
        elif q0 <= j < q0 + len(post):
            post = post[:j - q0] + c + post[j + len(a) - q0:]
    return pre, post


def apply_choice(merged: PageReading, site: Site, choice: str, moved: int = 0, done=()) -> bool:
    """표시 자리를 고른 글자로 바꾼다. 같은 표시가 그 열에 여럿이면 글자 번호(char_index)가 맞는 것을 바꾼다
    (앞 자리가 미정이고 뒤 자리만 확정됐을 때 첫 표시를 바꾸던 잘못 방지). done = 같은 차례에서 이 열에 이미 바꾼 자리
    [(글자 번호, 1차 글자, 고른 글자)]: 글자 번호는 찾을 때의 글에서 셌으므로 앞쪽 자리의 길이 차(〔效效/先〕→先 이면 -1)만큼 고쳐 맞추고
    (같은 표시 둘이 뒤바뀌지 않게), 앞뒤 글자도 그 자리를 고른 글자로 넣어 견준다(_context_after). moved 는 예전 부름 방식(길이 차 합만 —
    앞뒤 글자는 고치지 못한다).
    제자리에 그 표시가 없으면(사람이 고쳤거나 비교를 다시 계산해 글자가 밀림) 가까운 같은 표시를 찾되, grades.locate_choice 처럼
    기록한 앞뒤 글자(pre·post) 가운데 하나는 그 자리 옆에 있어야 받는다 — 같은 열의 다른 〔日/曰〕 을 바꾸지 않게.
    바로 옆 글자가 다른 후보로 확정된 표시였을 수 있으므로(〔於/不〕→不) 그 옆 글자(pre[-2]·post[1])가 맞아도 약하게 쳐 준다.
    제자리의 표시는 이웃이 안 맞아도 받되 이웃이 맞는 자리가 있으면 그쪽을 앞세운다. 앞뒤 글자 기록이 둘 다 없으면 가장 가까운 표시.
    맞는 자리가 없으면 False(표시를 그대로 둔다 — apply_adjudications 가 못 찾음으로 센다)."""
    if sum(r.id == site.region_id for r in merged.regions) != 1:
        return False                                      # 과거 기록의 중복 id 는 첫 영역에 잘못 반영하지 않는다.
    def side(seen: str, want: str, back: bool) -> int:
        """이웃 한쪽의 맞음: 바로 옆 글자가 같으면 2, 그 옆 글자만 같으면 1, 아니면 0."""
        if not want:
            return 0
        if back:
            return 2 if seen.endswith(want[-1]) else (1 if len(want) > 1 and len(seen) > 1 and seen[-2] == want[-2] else 0)
        return 2 if seen.startswith(want[0]) else (1 if len(want) > 1 and len(seen) > 1 and seen[1] == want[1] else 0)
    for reg in merged.regions:
        if reg.id != site.region_id:
            continue
        cols = columns_in_reading_order(reg)
        if site.col_index >= len(cols):
            return False
        col = cols[site.col_index]; text = col.text or ""
        want = site.char_index + moved + sum(len(c) - len(a) for j, a, c in done if j < site.char_index)
        pre, post = _context_after(site, done)
        best = None
        for m in MARK_RE.finditer(text):
            if m.group(0) != site.mark:
                continue
            before = plain(text[:m.start()]); after = plain(text[m.end():])
            dist = abs(len(before) - want)
            fit = side(before, pre, True) + side(after, post, False)
            if (pre or post) and dist and not fit:
                continue                                                  # 제자리가 아니면 앞뒤 글자 하나는 맞아야 한다
            key = (dist == 0 and fit > 0, fit, -dist)
            if best is None or key > best[0]:
                best = (key, m.start(), m.end())
        if best is None:
            return False
        _, a, b = best
        col.text = text[:a] + choice + text[b:]
        return True


def apply_adjudications(merged: PageReading, adjudications: list[dict] | None, stats: dict | None = None) -> int:
    """저장된 판별 결과를 병합 판독문에 다시 적용한다(비교 다시 계산 뒤에도 판별이 남게). 적용한 수를 돌려준다.
    판별 때처럼 차례(grades.adjudication_runs)마다 같은 열 앞쪽에서 이미 바꾼 글자의 길이 차만큼 글자 번호를 고쳐 표시를 찾는다.
    stats 를 주면 자리를 못 찾아 적용하지 못한 확정 기록 수를 stats["unmapped"] 에 적는다(등급의 '3차 확정 위치 못 찾음' 과 같은 뜻)."""
    from .grades import adjudication_runs
    n = 0; lost = 0; done: dict[tuple, list[tuple[int, str, str]]] = {}  # (영역 id, 열 번호, 차례) → [(글자 번호, 1차 글자, 고른 글자)]
    for a, run in zip(adjudications or [], adjudication_runs(adjudications)):
        if not a.get("choice"):
            continue
        site = Site(**{k: a[k] for k in Site.__dataclass_fields__ if k in a})
        key = (site.region_id, site.col_index, run)
        if apply_choice(merged, site, a["choice"], done=done.get(key, [])):
            n += 1; done.setdefault(key, []).append((site.char_index, site.a, a["choice"]))
        else:
            lost += 1
    if stats is not None:
        stats["unmapped"] = lost
    return n


def window_matches(window: str, pre: str, post: str) -> bool:
    """고른 글자와 무관하게, 조각에 보인 글(window)에 앞 글자나 뒤 글자가 들어 있는지. 비어 있거나 앞뒤 글자가 없으면 검사할 수 없으므로 True.
    묶음 응답에서 c="" 인 자리가 정말 못 가린 것인지, 조각 번호가 섞여 엉뚱한 조각을 본 것인지 가른다."""
    if not window:
        return True
    w = _PUNCT.sub("", window); pk = _PUNCT.sub("", pre or ""); nk = _PUNCT.sub("", post or "")
    if not pk and not nk:
        return True
    try:
        from .collate import zh
        fold = zh.compare_key if zh.available() else (lambda s: s)
    except Exception:
        fold = lambda s: s
    w, pk, nk = fold(w), fold(pk), fold(nk)
    if pk and nk and pk[-1] in w and nk[0] in w:
        return _around(w, pk, nk)                                      # 둘 다 보이면 자리를 사이에 두고 이어져야 한다(흩어져 보이면 다른 조각)
    return bool((pk and pk[-1] in w) or (nk and nk[0] in w))


def _around(w: str, pk: str, nk: str) -> bool:
    """앞 글자 뒤 0~2자(표시 글자의 길이) 안에 뒤 글자가 오는지 — 앞뒤 글자가 창 어디엔가 흩어져 있는 것만으로는 자리를 믿지 않는다.
    사이의 부호(〔〕·︑︒·▲○ 처럼 _PUNCT 에 없는 것 — w 는 부호 포함으로 적게 한다)는 세지 않고 글자와 □(못 읽은 글자)만 센다."""
    return bool(re.search(re.escape(pk[-1]) + r"(?:[^\w□]*[\w□]){0,2}[^\w□]*" + re.escape(nk[0]), w))


def _window_parts(window: str, pre: str, choice: str, post: str):
    w = _PUNCT.sub("", window or ""); pk = _PUNCT.sub("", pre or ""); nk = _PUNCT.sub("", post or "")
    try:
        from .collate import zh
        fold = zh.compare_key if zh.available() else (lambda s: s)
    except Exception:
        fold = lambda s: s
    return fold(w), fold(pk), fold(nk), fold(choice or "")


def adjacent_seen(window: str, pre: str, choice: str, post: str) -> bool:
    """조각에 보인 글(window)에 '앞 글자+고른 글자' 또는 '고른 글자+뒤 글자'가 실제로 있는지. 창이 비었거나 앞뒤 글자가 없으면 False
    (자리를 확인할 수 없음). 확인 등급에서 초록(3차 자형 판별을 조각 위치까지 확인)과 파랑을 가르는 데 쓴다."""
    if not window or not choice:
        return False
    w, pk, nk, ch = _window_parts(window, pre, choice, post)
    return bool((pk and (pk[-1] + ch) in w) or (nk and (ch + nk[0]) in w))


def window_ok(window: str, pre: str, choice: str, post: str) -> bool:
    """모델이 조각에서 본 글(window)이 기대한 앞뒤 글자와 이어지는지. 조각이 엉뚱한 자리(좌표 밀림)를 잘랐으면 False.
    window 가 비었거나 앞뒤 글자가 없으면 검사할 수 없으므로 True."""
    if not window or not choice:
        return True
    w, pk, nk, ch = _window_parts(window, pre, choice, post)
    if not pk and not nk:
        return True
    if adjacent_seen(window, pre, choice, post):
        return True
    if pk and nk and _around(w, pk, nk):                        # 가운데 글자를 다르게 읽었어도 앞뒤가 그 자리를 사이에 두고 보이면 자리는 맞다
        return True
    return False


def _fatal(e: Exception) -> bool:
    return bool(getattr(e, "fatal", False))


def even_chunks(items: list, batch: int) -> list[list]:
    """items 를 ⌈n/batch⌉ 묶음으로 거의 같은 크기(batch 를 넘지 않게)로 나눈다 — 9장을 8장씩이면 5+4(8+1 이 아니라: 한 장짜리 호출도
    묶음만큼 비싸다). 한 장짜리 묶음은 batch 가 2 이고 홀수 장일 때 마지막에만 생기며, 부르는 쪽이 낱개로 묻는다."""
    n = len(items)
    if n == 0:
        return []
    m = max(1, -(-n // max(1, batch)))
    base, extra = divmod(n, m)
    out = []; k = 0
    for c in range(m):
        size = base + (1 if c < extra else 0)
        out.append(list(items[k:k + size])); k += size
    return out


def fold_glyph(s: str) -> str:
    """눈가림 답과 객관식 답을 맞춰 볼 때 쓰는 접기(부호·공백 빼고, 간체·번체·이체를 compare_key 로)."""
    t = _PUNCT.sub("", s or "")
    try:
        from .collate import zh
        return zh.compare_key(t) if zh.available() else t
    except Exception:
        return t


def blind_agrees(got: str, choice: str, candidates) -> bool:
    """눈가림 답(got)이 객관식 답(choice)과 같은가. 빈 답은 다르다고 본다. 간체·번체·이체는 접어서 맞춰 보되,
    두 후보가 접으면 같아지는 자리(黃/黄·苡/苢 같은 이체자 쌍)와 접으면 빈 글이 되는 숫자·부호 후보는 글자 그대로 맞춰 본다
    (그래야 인쇄된 꼴을 고르는 이 자리에서 재확인이 뜻을 가진다)."""
    g, c = nfc_text((got or "").strip()), nfc_text((choice or "").strip())
    if not g or not c:
        return False
    if g == c:
        return True
    fc = fold_glyph(c)
    if not fc or any(o and o != c and fold_glyph(o) == fc for o in candidates):
        return False
    return fold_glyph(g) == fc


LOWRES_NOTE = "저해상"
MIN_GLYPH_PX = 16          # 저해상 문턱(원본 글자 크기, 픽셀). 300dpi 렌더 10pt 글자는 약 42px, 150dpi 스캔 8pt 소주는 약 17px. 잠정값 — 측정으로 고친다


def _prepare_asks(sites: list[Site], merged: PageReading, bboxes: dict, ink, horizontal: bool, pdf_page, layer_lines, frame,
                  min_px: float, ticks: bool, stats: dict) -> tuple[list, dict, set]:
    """자리마다 창(locate_glyph)과 질문(make_ask)을 만든다. 원본 글자가 min_px 보다 작은 자리는 저해상(묻지 않음)으로.
    돌려주는 값: ([(자리 번호, 자리, 창)], {자리 번호: 질문}, 저해상 자리 번호들). 창을 잡은 방법은 stats["loc"] 에 센다."""
    prepared: list[tuple[str, Site, GlyphWindow]] = []
    asks: dict[str, GlyphAsk] = {}
    lowres: set[str] = set()
    for idx, site in enumerate(sites, 1):
        win = locate_glyph(ink, bboxes.get(site.region_id, [0, 0, 1000, 1000]), site, horizontal, column_plain(merged, site), layer_lines, frame)
        sid = str(idx)
        stats["loc"][win.how] = stats["loc"].get(win.how, 0) + 1
        asks[sid] = make_ask(site, win, [site.a, site.b], pdf_page, tick=bool(ticks and win.measured and win.target))
        if min_px and win.em_px and win.em_px < min_px:
            lowres.add(sid)                                                # 묻지 않는다
        prepared.append((sid, site, win))
    return prepared, asks, lowres


@dataclass
class _Quiz:
    """한 쪽 자리들을 판독기에 물은 결과(_ask_glyphs). results: 자리 번호 → (답, 모델, 오류), by: 답을 낸 판독기,
    asked: 한 번이라도 보낸 자리, blind_ans: 눈가림 답(읽은 글자 또는 None=실패, 오류), unsupported: 눈가림을 못 하는 판독기가 답한 자리."""
    results: dict
    by: dict
    asked: set
    blind_ans: dict
    unsupported: set


def _ask_glyphs(prepared: list, asks: dict, lowres: set, img: Image.Image, reader, horizontal: bool, pdf_page, *, zoom: int, batch: int,
                fallback_reader, stop, blind: bool, stats: dict) -> _Quiz:
    """3차 묻기(〔a/b〕 자리·합의 의심 자리 공용): 묶음(batch 장씩) → 빠졌거나 창이 안 맞는 자리는 낱개 → 보완 판독기 → (blind 면) 후보 없이 재확인.
    키·권한 오류(fatal)는 그대로 올리고, stop() 이 참이면 남은 묶음·보완을 건너뛴다. 묶음·보완·재확인 수는 stats 에 적는다."""
    crops: dict[str, Image.Image] = {}
    win_of = {sid: win for sid, _, win in prepared}

    def crop_of(sid: str) -> Image.Image:
        if sid not in crops:
            win = win_of[sid]
            crops[sid] = glyph_crop(img, win.box, zoom=zoom, target=win.target if asks[sid].tick else None, horizontal=horizontal)
        return crops[sid]

    results: dict[str, tuple[GlyphChoice | None, str, str]] = {}        # sid → (답, 모델, 오류)
    batched: set[str] = set()                                            # 묶음 호출로 답을 받은 자리(보완 판독기가 다시 받아도 한 번만 셈)
    rechecked: set[str] = set()                                          # 묶음 응답의 창이 안 맞아 낱개로 다시 물은 자리
    asked: set[str] = set()                                              # 한 번이라도 모델에 보낸 자리(중지로 못 보낸 자리는 실패로 세지 않는다)
    site_of = {sid: site for sid, site, _ in prepared}
    by: dict[str, object] = {}                                           # sid → 답을 낸 판독기(눈가림 재확인도 그 판독기에)
    to_ask = [it for it in prepared if it[0] not in lowres]

    def norm(r: GlyphChoice, site: Site) -> GlyphChoice:
        """후보 밖 글자를 답하면 미정으로 다룬다(그래야 낱개 재질문·보완 판독기 대상이 된다)."""
        c = (r.choice or "").strip()
        if c and c not in (site.a, site.b):
            return GlyphChoice(choice="", evidence=f"후보 밖 답({c}) — " + (r.evidence or ""), window=r.window or "")
        return r

    def decided(sid: str) -> bool:
        r = results.get(sid)
        return bool(r and r[0] is not None and (r[0].choice or "").strip() in (site_of[sid].a, site_of[sid].b))

    def ask(rd, items: list) -> None:
        model = getattr(rd, "model", "")
        pending = [it for it in items if not decided(it[0])]
        if batch > 1 and len(pending) > 1 and hasattr(rd, "adjudicate_many"):
            for chunk in even_chunks(pending, batch):                     # 9장을 5+4 로(8+1 이 아니라)
                if len(chunk) == 1:
                    continue                                              # 하나 남은 조각은 낱개로
                if stop():
                    return
                asked.update(sid for sid, *_ in chunk)
                try:
                    got = rd.adjudicate_many([(sid, crop_of(sid), asks[sid]) for sid, _, _ in chunk], horizontal, pdf_page)
                except Exception as e:
                    if _fatal(e):
                        raise
                    for sid, *_ in chunk:
                        if not decided(sid):
                            results[sid] = (None, model, f"묶음 호출 실패: {str(e)[:60]}")
                    continue
                for sid, site, _ in chunk:
                    r = got.get(sid)
                    if r is None:
                        results.pop(sid, None); continue                  # 빠진 자리 → 낱개
                    r = norm(r, site); choice = (r.choice or "").strip()
                    if (choice and not window_ok(r.window or "", site.pre, choice, site.post)) or (not choice and not window_matches(r.window or "", site.pre, site.post)):
                        results.pop(sid, None); rechecked.add(sid); continue      # 창이 안 맞음(조각 번호가 섞였을 수 있음) → 낱개로 다시
                    results[sid] = (r, model, ""); batched.add(sid); by[sid] = rd
        for sid, site, _ in pending:
            if sid in results and (decided(sid) or results[sid][0] is not None or results[sid][2].startswith("묶음 호출 실패")):
                continue                                                  # 묶음에서 답했거나(미정 포함) 묶음이 실패한 자리는 낱개로 더 묻지 않는다
            if stop():
                return
            asked.add(sid)
            try:
                r = norm(rd.adjudicate(crop_of(sid), asks[sid], horizontal, pdf_page), site)
                results[sid] = (r, model, ""); by[sid] = rd
            except Exception as e:
                if _fatal(e):
                    raise
                results[sid] = (None, model, str(e)[:80])

    ask(reader, to_ask)
    if fallback_reader is not None and not stop():
        again = []
        for it in to_ask:
            r = results.get(it[0])
            if r is None or decided(it[0]):
                continue
            if r[0] is not None and (r[0].choice or "").strip():
                continue
            again.append(it)                                              # 미정(c="")·실패한 자리. 창이 어긋난 자리는 choice 가 있어 제외
        if again:
            before = {sid for sid in results if decided(sid)}
            prev = {it[0]: results.pop(it[0], None) for it in again}
            ask(fallback_reader, again)
            for sid, old in prev.items():                                 # 보완 호출이 실패했으면 첫 판독기의 미정 답(근거 메모)을 살린다
                new = results.get(sid)
                if old and old[0] is not None and (new is None or new[0] is None):
                    r0 = old[0]; note = (new[2] if new else "")
                    results[sid] = (GlyphChoice(choice="", evidence=(r0.evidence or "") + (f" · 보완 실패: {note[:40]}" if note else ""), window=r0.window or ""), old[1], "")
            stats["fallback_decided"] = sum(1 for sid in results if sid not in before and decided(sid))

    # 눈가림 확인: 확정할 자리(후보 안 답 + 창 검사 통과)만 후보 없이 한 번 더 읽힌다. 그 자리의 답을 낸 판독기에 묻는다
    # (보완 판독기가 정한 자리를 그 자리를 못 가린 주 판독기가 뒤집지 않게).
    blind_ans: dict[str, tuple[str | None, str]] = {}                   # sid → (읽은 글자 또는 None=실패, 오류)
    unsupported: set[str] = set()
    if blind:
        cand = [it for it in to_ask if decided(it[0]) and window_ok(results[it[0]][0].window or "", it[1].pre, results[it[0]][0].choice.strip(), it[1].post)]
        open_ask = {}
        for sid, _, _ in cand:
            a = asks[sid]; ch = results[sid][0].choice.strip()             # 대상 길이는 고른 답의 길이(후보 길이가 다른 자리)
            open_ask[sid] = GlyphAsk(candidates=(), pos=a.pos, k=max(1, len(ch)) if a.k == 0 else a.k, count=a.count, measured=a.measured, tick=a.tick)
        for rd in [r for r in (reader, fallback_reader) if r is not None]:
            mine = [it for it in cand if by.get(it[0]) is rd]
            if not mine:
                continue
            if not (hasattr(rd, "identify") or hasattr(rd, "identify_many")):
                unsupported.update(sid for sid, *_ in mine); continue
            rest = list(mine)
            if batch > 1 and len(rest) > 1 and hasattr(rd, "identify_many"):
                left = []
                for chunk in even_chunks(rest, batch):
                    if len(chunk) == 1 or stop():
                        left += chunk; continue
                    try:
                        got = rd.identify_many([(sid, crop_of(sid), open_ask[sid]) for sid, _, _ in chunk], horizontal, pdf_page)
                    except Exception as e:
                        if _fatal(e):
                            raise
                        for sid, *_ in chunk:
                            blind_ans[sid] = (None, f"묶음 호출 실패: {str(e)[:40]}")
                        continue
                    for it in chunk:
                        r = got.get(it[0])
                        if r is None:
                            left.append(it)
                        else:
                            blind_ans[it[0]] = ((r.choice or "").strip(), "")
                rest = left
            for sid, _, _ in rest:
                if stop():
                    break
                try:
                    if hasattr(rd, "identify"):
                        r = rd.identify(crop_of(sid), open_ask[sid], horizontal, pdf_page)
                    else:
                        r = rd.identify_many([(sid, crop_of(sid), open_ask[sid])], horizontal, pdf_page).get(sid)
                    blind_ans[sid] = ((r.choice or "").strip(), "") if r is not None else (None, "응답 없음")
                except Exception as e:
                    if _fatal(e):
                        raise
                    blind_ans[sid] = (None, str(e)[:40])
        if unsupported:
            stats["blind_unsupported"] = len(unsupported)
        if blind_ans:
            stats["blind_checked"] = sum(1 for sid in blind_ans if blind_ans[sid][0] is not None)

    if batched:
        stats["batched"] = len(batched)
    if rechecked:
        stats["batch_rechecked"] = len(rechecked)
    if lowres:
        stats["lowres"] = len(lowres)
    return _Quiz(results, by, asked, blind_ans, unsupported)


def _verdict(q: _Quiz, sid: str, site: Site, stop, blind: bool, stats: dict) -> tuple[str, dict]:
    """물은 자리 하나의 판정: ("skipped" | "failed" | "judged", 기록 칸). judged 의 choice 는 창 검사(window_ok)와
    눈가림 재확인(blind)까지 통과한 후보만(아니면 빈 글 — 근거에 까닭). 어긋남·재확인 실패·불일치·중지 수는 stats 에 센다."""
    if sid not in q.results and (stop() or sid not in q.asked):
        stats["skipped"] = stats.get("skipped", 0) + 1                     # 중지로 묻지 못한 자리: 기록을 남기지 않는다(다음 실행에서 다시)
        return "skipped", {}
    res, model, err = q.results.get(sid, (None, "", "응답 없음"))
    base = {"model": model} if model else {}
    if res is None:
        return "failed", {**base, "choice": "", "evidence": f"판별 실패: {err}"}
    choice = (res.choice or "").strip()
    entry = {**base, "choice": choice if choice in (site.a, site.b) else "", "evidence": (res.evidence or "")[:200],
             "window": (getattr(res, "window", "") or "")[:40]}
    if entry["choice"] and not window_ok(entry["window"], site.pre, entry["choice"], site.post):
        entry["choice"] = ""; entry["evidence"] = f"조각 위치가 어긋남(보인 글: {entry['window'][:12]}) — " + entry["evidence"]; stats["misplaced"] = stats.get("misplaced", 0) + 1
    if entry["choice"] and blind and sid not in q.unsupported:
        got_b, berr = q.blind_ans.get(sid, (None, "중지로 묻지 못함" if stop() else "응답 없음"))
        if got_b is None and stop() and not berr.startswith("묶음"):
            stats["skipped"] = stats.get("skipped", 0) + 1                 # 눈가림 확인 전에 중지: 확정하지 않고 기록도 남기지 않는다(다음 실행에서 다시)
            return "skipped", {}
        entry["blind"] = got_b if got_b is not None else ""
        if got_b is None:
            entry["choice"] = ""; entry["evidence"] = f"눈가림 확인 실패({berr}) — 확정 보류 · " + entry["evidence"]
            stats["blind_failed"] = stats.get("blind_failed", 0) + 1
        elif not blind_agrees(got_b, entry["choice"], (site.a, site.b)):
            entry["evidence"] = f"눈가림 답 「{got_b or '?'}」 ≠ 객관식 「{entry['choice']}」 — 확정 보류 · " + entry["evidence"]
            entry["choice"] = ""; stats["blind_rejected"] = stats.get("blind_rejected", 0) + 1
    return "judged", entry


def adjudicate_page(rec: dict, img: Image.Image, reader, horizontal: bool, max_sites: int = 0, zoom: int = 6, batch: int = 8,
                    fallback_reader=None, layer_lines: list[dict] | None = None, frame=None, should_stop=None, page_size_pt=None,
                    min_px: float = 0, blind: bool = False, ticks: bool = True) -> dict:
    """한 쪽의 〔a/b〕 자리를 판별한다. rec 을 고쳐 돌려준다(merged 글, adjudications, uncertain 메모).

    - 자리마다 창을 글자층 → 먹 실측 → 균등 분할 순으로 잡는다(기록 loc). frame 은 쪽 이미지의 PDF 좌표 틀(pdfio.page_frame).
    - 눈가림: 판독기에는 GlyphAsk(섞은 후보·조각 안 순번)만 넘긴다. 앞뒤 글자(site.pre/post)는 답의 창(w) 검사에만 쓴다.
      ticks 면 실측한 자리의 조각 바깥에 눈금을 그린다.
    - min_px 를 주면 원본에서 잰 글자 크기가 그보다 작은 자리는 묻지 않고 미정으로 둔다(지침 24).
    - reader 에 adjudicate_many 가 있으면 batch 장씩 한 호출로 묻고, 응답에서 빠졌거나 창(w)이 앞뒤 글자와 안 맞는 자리만 낱개로 다시 묻는다.
      묶음 호출 자체가 실패하면(한도 초과 등) 그 자리들은 '판별 실패' 로 남기고 낱개로 번지지 않는다(표시가 남으니 다음 실행에서 다시).
    - fallback_reader 가 있으면 첫 판독기가 못 가린 자리(c="")와 실패한 자리를 그 판독기로 다시 묻는다(예: flash 먼저, pro 보완).
    - blind 면 확정할 자리를 주 판독기에 후보 없이 한 번 더 읽게 해(identify / identify_many) 객관식 답과 같을 때만 확정한다.
    - should_stop() 이 참이면 남은 묶음·보완을 건너뛴다(끝난 자리는 적용). 키·권한 오류(fatal)는 그대로 올려 전체를 멈춘다.
    묻는 방식·검사는 합의 의심 자리(suspect_page)와 같다(_prepare_asks → _ask_glyphs → _verdict)."""
    merged = PageReading.model_validate(rec["merged"])
    p1 = PageReading.model_validate(rec["pass1"]) if rec.get("pass1") else merged
    bboxes = {r.id: r.bbox for r in p1.regions}
    sites = find_sites(merged)
    if max_sites:
        sites = sites[:max_sites]
    done = list(rec.get("adjudications") or [])
    stats = {"sites": len(sites), "decided": 0, "undecided": 0, "a": 0, "b": 0, "failed": 0, "loc": {}}
    pdf_page = rec.get("pdf_page", 0)
    frame = frame or page_size_pt
    stop = should_stop or (lambda: False)
    ink = page_ink(img) if (sites and img is not None) else None
    prepared, asks, lowres = _prepare_asks(sites, merged, bboxes, ink, horizontal, pdf_page, layer_lines, frame, min_px, ticks, stats)
    q = _ask_glyphs(prepared, asks, lowres, img, reader, horizontal, pdf_page, zoom=zoom, batch=batch, fallback_reader=fallback_reader,
                    stop=stop, blind=blind, stats=stats)
    confirmed: set[str] = set()
    from .grades import adjudication_runs
    run = 1 + max(adjudication_runs(done), default=0)                   # 이번 3차 차례 번호(기록마다 적는다 — 등급·다시 적용이 차례별 길이 차를 가른다)
    changed: dict[tuple, list[tuple[int, str, str]]] = {}               # (영역 id, 열 번호) → [(글자 번호, 1차 글자, 고른 글자)] 이번 차례에 바꾼 표시
    for sid, site, win in prepared:
        a = asks[sid]
        base = {**asdict(site), "run": run, "box": [round(v, 4) for v in win.box], "loc": win.how, "pos": win.pos, "asked": "/".join(a.candidates)}
        if win.em_px:
            base["em_px"] = round(win.em_px, 1)
        if sid in lowres:
            stats["undecided"] += 1
            done.append({**base, "choice": "", "evidence": f"{LOWRES_NOTE}: 원본 글자 약 {win.em_px:.0f}px(문턱 {min_px:g}px) — 확대해도 획이 살아나지 않아 묻지 않음(지침 24)"})
            for u in merged.uncertain:
                if site.mark in (u.context or "") and "3차 판별" not in u.context:
                    u.context = u.context + " — 3차 판별: 저해상이라 묻지 않음(원본 확인)"
            continue
        kind, got = _verdict(q, sid, site, stop, blind, stats)
        if kind == "skipped":
            continue
        entry = {**base, **got}
        if kind == "failed":
            stats["failed"] += 1
            done.append(entry)
            continue
        if entry["choice"]:
            key = (site.region_id, site.col_index)
            if apply_choice(merged, site, entry["choice"], done=changed.get(key, [])):
                changed.setdefault(key, []).append((site.char_index, site.a, entry["choice"]))
            else:
                entry["choice"] = ""
                entry["evidence"] = "확정 위치를 찾지 못해 반영하지 않음 — " + entry["evidence"]
                stats["undecided"] += 1
                stats["unmapped"] = stats.get("unmapped", 0) + 1
                done.append(entry)
                continue
            stats["decided"] += 1; stats["a" if entry["choice"] == site.a else "b"] += 1; confirmed.add(sid)
            for u in merged.uncertain:
                if site.mark in (u.context or "") and "3차 판별" not in u.context:
                    u.context = u.context + f" — 3차 판별: 「{entry['choice']}」 확정({entry['evidence'][:40]})"
        else:
            stats["undecided"] += 1
            for u in merged.uncertain:
                if site.mark in (u.context or "") and "3차 판별" not in u.context:
                    u.context = u.context + " — 3차 판별: 자형으로 가리지 못함(원본 확인)"
        done.append(entry)
    if "fallback_decided" in stats:                                      # 보완 확정은 재확인·창 검사 뒤에도 남은 자리만 센다
        stats["fallback_decided"] = sum(1 for sid in confirmed if q.by.get(sid) is fallback_reader)
    rec["merged"] = merged.model_dump()
    rec["adjudications"] = done
    rec["adjudicate_stats"] = stats
    return rec


# ---------- 합의 판독 의심(단계 3 신호 ①): 1차·2차가 같게 읽었는데 글자층과 자형 혼동 쌍 한 글자가 다른 자리 ----------
SUSPECT_NOTE = "합의 판독 의심"           # 확인 항목 머리. remerge 가 이 머리로 남기고(또는 rec['suspects'] 로 다시 만들고) 등급은 쪽 고지로 다룬다
SUSPECT_MAX = 8                          # 쪽당 묻는 자리 수 상한(--suspect-max). 이미 물은 자리도 센다
SUSPECT_MIN_RATIO = 0.5                  # 글자층과의 일치율이 이보다 낮은 쪽은 보지 않는다(지침 28: 판독이나 글자층이 크게 흔들린 쪽)
SUSPECT_SOURCE = "합의↔자형"              # 혼동 장부 출처: 1차·2차 합의 글자 → 자형 판별이 고른 글자층 글자(outcome differs)


# 번체 자형 혼동 묶음: 간체 묶음(CONFUSABLES 등)을 번체로 옮겨도 헷갈리는 부분이 그대로 남는 것(한쪽 부수·성부만 간화된 글자)만 손으로 적는다.
# 於子·無元·幣市·滅火·屬届·膿脹·囉噦 처럼 간화가 글자를 통째로 바꾼 쌍은 넣지 않는다 — 간체로만 닮았고 번체(인쇄된 꼴)로는 닮지 않았다.
TRAD_CONFUSABLES = ("問間聞", "烏鳥", "綱網", "宮官", "師帥", "鳳風", "鎮慎", "攝懾", "嘗常", "瘌癩")


@functools.lru_cache(maxsize=1)
def confusable_groups() -> tuple[frozenset, ...]:
    """자형이 헷갈리기 쉬운 묶음(인쇄된 꼴 그대로): 3차 자형 단서(prompts.GLYPH_HINTS) ∪ 책갈피 교정의 CONFUSABLES·WEAK_WITNESS ∪ TRAD_CONFUSABLES.
    compare_key(번체→간체)로 접지 않는다 — 접으면 於→于·無→无 처럼 생김새가 전혀 다른 번체가 간체 묶음(于子·元无)에 들어간다.
    접으면 한 글자가 되는 묶음(苡苢 — 이체자)은 뺀다."""
    from .prompts import GLYPH_HINTS
    from .collate.collate import CONFUSABLES, WEAK_WITNESS
    from .collate import zh
    out: list[frozenset] = []
    for g in [*GLYPH_HINTS, *CONFUSABLES, *WEAK_WITNESS, *TRAD_CONFUSABLES]:
        k = frozenset(unicodedata.normalize("NFC", c) for c in g)
        if len({zh.compare_key(c) for c in k}) > 1 and k not in out:
            out.append(k)
    return tuple(out)


@functools.lru_cache(maxsize=4096)
def _printed_forms(ch: str) -> frozenset:
    """글자 하나가 가리킬 수 있는 인쇄된 꼴: 그 글자 · 이체자 대표 글자(지침 21 — 같은 꼴로 본다) · 간체면 그 번체들(번체 책의 간체 글자층 间 은 인쇄된 間).
    번체를 간체로 접은 꼴은 넣지 않는다(於 는 于 로 인쇄되지 않는다)."""
    from .collate import zh
    out: set = set()
    for f in {ch, unicodedata.normalize("NFC", ch), zh.fold_variants(unicodedata.normalize("NFC", ch))}:
        out.add(f); out.update(c for c in zh.candidates(f) if len(c) == 1)
    return frozenset(out)


def variant_pair(a: str, b: str) -> bool:
    """이체자·간번체·호환 한자 쌍(지침 21): 접으면 같은 글자. 합의 의심으로 묻지 않는다(인쇄된 꼴을 고르는 일이 아니다)."""
    from .collate import zh
    from .merge import _fold
    return a == b or zh.compare_key(a) == zh.compare_key(b) or zh.fold_variants(a) == zh.fold_variants(b) or _fold(a) == _fold(b)


def confusable_pair(a: str, b: str) -> bool:
    """두 글자가 인쇄된 꼴로 한 자형 혼동 묶음에 드는가(접어 같으면 — 간번체·이체자 — 아님).
    규칙: 두 글자의 인쇄된 꼴(_printed_forms)에서 서로 다른 두 글자가 한 묶음(confusable_groups)에 들어야 한다. 접은 열쇠(compare_key)만 같은 묶음이면
    모자란다 — 於/子·無/元·幣/市·滅/火·屬/届 은 간체(于子·无元·币市·灭火·属届)로만 닮아 번체 책에서는 자형을 헷갈릴 일이 없는데 3차 질문·합의 의심 알림·장부를 만든다.
    번체로 읽은 問 ↔ 간체 글자층 间 은 间 이 가리키는 인쇄된 꼴 間 이 번체 묶음 問間聞 에 들어 묻는다."""
    from .collate import zh
    if zh.compare_key(a) == zh.compare_key(b):
        return False
    pa, pb = _printed_forms(a), _printed_forms(b)
    return any((pa & g) and (pb & g) and len((pa | pb) & g) > 1 for g in confusable_groups())


def _pair_key(a: str, b: str) -> frozenset:
    from .collate import zh
    return frozenset((zh.compare_key(a), zh.compare_key(b)))


def suspect_freq(work_dir) -> dict:
    """혼동 장부(confusion.json)의 글자 쌍 빈도 {접은 두 글자: 건수}(출처를 합침). 장부가 없거나 못 읽으면 빈 사전 — 그때는 글 차례로 묻는다."""
    import json
    from pathlib import Path
    try:
        led = json.loads((Path(work_dir) / "confusion.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict = {}
    for r in (led.get("pairs") if isinstance(led, dict) else None) or []:
        if isinstance(r, dict) and r.get("a") and r.get("b"):
            k = _pair_key(r["a"], r["b"]); out[k] = out.get(k, 0) + int(r.get("count") or 0)
    return out


def find_suspects(merged: PageReading, layer_plain: str, adjudications: list[dict] | None = None, regions=None, freq: dict | None = None) -> tuple[list[Site], dict]:
    """합의 판독 의심 자리: 〔〕 표시 밖(1차·2차가 같게 읽은 글자)에서 PDF 글자층과 1:1 로 한 글자만 다르고, 두 글자가 자형 혼동 묶음인 자리.
    글자층 맞춤은 compare_textlayer 와 같은 글(표시는 1차 글자로·공백 없이·전각 부호와 간체 모드 접기)로 새로 한다(저장된 차이는 60항목에서 잘림).
    빼는 것: 글자층 일치율 SUSPECT_MIN_RATIO 미만인 쪽 전체(지침 28) · 표시 안과 표시 바로 곁(1자) · 3차가 정한 글자와 그 곁 · 숫자·부호·한글 ·
    이체자 쌍(지침 21) · regions(물을 영역 id — 2차 판독이 있는 영역)에 없는 영역. 차례: freq(장부 쌍 빈도) 높은 것 먼저, 같으면 글 차례.
    돌려주는 값: (자리 — a=합의 글자, b=글자층 글자, mark="", 글자 번호는 표시를 접은 열 글 기준 / {"ratio", "layer_chars", "low_ratio"})."""
    import difflib
    from .merge import _fold, strip_marks_for_compare, WS
    from .collate import zh
    from .context import NUMERALS, HANGUL
    near_dec: set = set()                                                # 3차가 정한 글자와 그 곁(영역 id, 문단 번호, 문단 글 위치)
    if adjudications:
        from .grades import decided_positions
        for L in decided_positions(merged, adjudications)[0]:
            near_dec.update((L.region_id, L.para_index, i) for i in range(L.start - 1, L.end + 1))
    chars: list[str] = []; spots: list = []                              # 맞출 글자와 그 자리((영역, 열 번호, 열 수, 글자 번호, 열) 또는 None=묻지 않음)
    duplicate = duplicate_region_ids(merged)
    for reg in merged.regions:
        if reg.kind not in TEXT_KINDS:
            continue
        ok_reg = reg.id not in duplicate and (regions is None or reg.id in regions); n_cols = len(columns_in_reading_order(reg)); ci = 0
        for pi, p in enumerate(reg.paragraphs):
            off = 0
            for col in sorted(p.columns, key=lambda c: c.col_from_right):   # columns_in_reading_order 와 같은 차례
                text = col.text or ""; k = 0; last = 0; prev_end = -1
                for m in [*MARK_RE.finditer(text), None]:
                    s = m.start() if m else len(text)
                    for i in range(last, s):
                        if not text[i].isspace():                       # compare_textlayer 처럼 공백은 맞추지 않는다(글자 번호에는 센다)
                            fine = ok_reg and i != prev_end and not (m and i == s - 1) and (reg.id, pi, off + i) not in near_dec
                            chars.append(text[i]); spots.append((reg.id, ci, n_cols, k, col) if fine else None)
                        k += 1
                    if m is None:
                        break
                    for ch in WS.sub("", strip_marks_for_compare(m.group(0))):
                        chars.append(ch); spots.append(None)             # 표시 안 글자(1차·2차가 갈린 자리)
                    k += len(plain(m.group(0))); last = prev_end = m.end()
                off += len(text); ci += 1
    a, b = "".join(chars), WS.sub("", layer_plain or "")
    info: dict = {"ratio": None, "layer_chars": len(b)}
    if not a or not b:
        return [], info
    sm = difflib.SequenceMatcher(None, _fold(a), _fold(b), autojunk=False)   # 접기는 글자 수를 바꾸지 않는다 — 위치는 a·b 와 1:1
    info["ratio"] = round(sm.ratio(), 4)
    if sm.ratio() < SUSPECT_MIN_RATIO:
        info["low_ratio"] = True
        return [], info
    found = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace" or i2 - i1 != 1 or j2 - j1 != 1 or spots[i1] is None:
            continue
        x, y = a[i1], b[j1]
        if not (zh.is_cjk(x) and zh.is_cjk(y)) or x in NUMERALS or y in NUMERALS or HANGUL.search(x + y):
            continue
        if variant_pair(x, y) or not confusable_pair(x, y):
            continue
        rid, ci, n_cols, k, col = spots[i1]
        pl = plain(col.text or "")
        found.append(((-(freq or {}).get(_pair_key(x, y), 0), i1), Site(rid, ci, n_cols, k, len(pl), x, y, pl[:k][-4:], pl[k + 1:k + 5], "")))
    found.sort(key=lambda t: t[0])
    return [s for _, s in found], info


def _asked_before(prev: list[dict], s: Site) -> int | None:
    """rec['suspects'] 에서 같은 자리(영역·열·글자 번호, 또는 앞뒤 글자)·같은 두 글자를 물은 마지막 기록 번호.
    가까운 앞 〔a/b〕 가 뒤 실행에서 길이가 다른 후보로 정해지면 글자 번호와 앞 글자(pre)가 함께 바뀐다 — 뒤 글자·바로 앞 한 자(표시 곁이 아니라 늘 판독 글자)가
    같고 글자 번호가 4자 안에서 움직였으면 같은 자리로 본다(다시 묻고 기록·알림을 겹쳐 달지 않게)."""
    for i in range(len(prev) - 1, -1, -1):
        e = prev[i]
        if (e.get("region_id"), e.get("col"), e.get("consensus"), e.get("layer")) == (s.region_id, s.col_index, s.a, s.b) \
                and (e.get("char_index") == s.char_index or (e.get("pre") == s.pre and e.get("post") == s.post)
                     or (e.get("post") == s.post and (e.get("pre") or "")[-1:] == s.pre[-1:] and abs(int(e.get("char_index") or 0) - s.char_index) <= 4)):
            return i
    return None


def _to_ask(prev: list[dict], s: Site, force: bool) -> tuple[bool, int | None]:
    """이 의심 자리를 (다시) 물을지와 앞 기록 번호. 판별 실패로 끝난 앞 기록은 다시 묻는다."""
    k = _asked_before(prev, s)
    return force or k is None or prev[k].get("outcome") == "failed", k


def _suspect_regions(rec: dict) -> set:
    """합의 의심을 볼 영역: 2차 판독이 (조각 일부 실패 없이) 있는 영역 — 표시 밖 글자가 1차·2차 합의인 곳.
    1차만 들어간 긴 구간(merge.LONG_SEGMENT: 2차가 못 읽었거나 크게 달라 1차를 따름)이 있는 영역은 뺀다 — 그 글은 합의가 아니어서
    알림·메모의 '1차·2차 「x」'와 장부의 합의↔자형이 틀린 말이 된다(context.settled_segments 와 같은 기준)."""
    from .grades import _pass2_regions
    from .merge import LONG_SEG_NO_P2, LONG_SEG_DIFFER
    p2 = rec.get("pass2") or {}
    one_sided = {u.get("region_id") for u in (rec.get("merged") or {}).get("uncertain") or []
                 if isinstance(u, dict) and (u.get("context") or "").startswith((LONG_SEG_NO_P2, LONG_SEG_DIFFER))}
    return {r for r in _pass2_regions(rec) if not (p2.get(r) or {}).get("pass2_failed") and r not in one_sided}


def suspects_pending(rec: dict, layer_plain: str, max_n: int = SUSPECT_MAX, force: bool = False, freq: dict | None = None) -> int:
    """suspect_page 가 물을 새 자리 수(저해상은 아직 빼지 않은 어림) — 묻기 전에 셀 때(쪽 고르기·mock 셈)."""
    if not max_n or not layer_plain:
        return 0
    cands, _ = find_suspects(PageReading.model_validate(rec["merged"]), layer_plain, rec.get("adjudications"), _suspect_regions(rec), freq)
    prev = rec.get("suspects") or []
    again = sum(1 for s in cands if _to_ask(prev, s, force)[0])
    return max(0, min(again, max_n - (len(cands) - again)))           # 상한에는 이미 물은 자리를 모두 센다(suspect_page 와 같이)


def suspect_uncertain(e: dict) -> Uncertain:
    """합의 의심 기록(outcome differs) 하나의 확인 항목. 같은 기록이면 늘 같은 항목이다(remerge 가 다시 만들어도 겹치지 않게)."""
    from .merge import _pos
    x, y = e.get("consensus", ""), e.get("layer", "")
    return Uncertain(region_id=e.get("region_id", ""), col_from_right=int(e.get("col_from_right") or 0),
                     position=_pos(int(e.get("char_index") or 0), int(e.get("n_chars") or 0)),
                     context=f"{SUSPECT_NOTE} — 1차·2차 「{x}」, 자형 판별 「{y}」(글자층도 「{y}」) · 「{e.get('pre', '')}{x}{e.get('post', '')}」 — 원본 확인")


def suspect_notices(rec: dict) -> list[Uncertain]:
    """rec['suspects'] 에서 다시 만든 확인 항목(자형이 글자층 쪽인 자리만)."""
    return [suspect_uncertain(e) for e in rec.get("suspects") or [] if e.get("outcome") == "differs"]


def suspect_page(rec: dict, img: Image.Image, reader, horizontal: bool, layer_plain: str, max_n: int = SUSPECT_MAX, force: bool = False,
                 freq: dict | None = None, zoom: int = 6, batch: int = 8, fallback_reader=None, layer_lines: list[dict] | None = None, frame=None,
                 should_stop=None, min_px: float = 0, blind: bool = False, ticks: bool = True) -> dict:
    """한 쪽의 합의 판독 의심 자리(find_suspects)를 〔a/b〕 자리와 똑같이 묻는다: 창·눈가림·섞은 후보 [합의 글자, 글자층 글자]·묶음·창 검사·보완·재확인.
    본문은 바꾸지 않는다(지침 20: 기존 OCR 은 의심만 올린다 — 자형 판별이 두 판독과 다르면 사람이 정한다). 결과(outcome):
    - confirmed: 자형이 합의 글자 → 알림 없음
    - differs: 자형이 글자층 글자(창 검사·재확인 통과) → 확인 항목(SUSPECT_NOTE) 하나. 쪽은 '판독 확인 필요'(명령이 붙임)
    - undecided: 못 가림·창 어긋남·재확인 불일치 → 알림 없음 · failed: 호출 실패 → 다음 실행에서 다시 묻는다
    물은 자리는 모두 rec['suspects'] 에, 이번 셈은 rec['suspect_stats'] 에. 같은 자리·같은 두 글자는 다시 묻지 않는다(force 면 다시 묻고 앞 기록을 바꾼다 —
    다시 물은 호출이 실패하면 앞 답과 그 확인 항목을 둔다).
    상한 max_n 에는 이미 물은 자리도 센다. 원본 글자가 min_px 보다 작은 자리는 묻지 않고 기록하지 않는다(지침 24). 중지로 못 물은 자리도 기록하지 않는다."""
    merged = PageReading.model_validate(rec["merged"])
    p1 = PageReading.model_validate(rec["pass1"]) if rec.get("pass1") else merged
    bboxes = {r.id: r.bbox for r in p1.regions}
    cands, info = find_suspects(merged, layer_plain, rec.get("adjudications"), _suspect_regions(rec), freq)
    prev = list(rec.get("suspects") or [])
    stats = {"candidates": len(cands), "asked": 0, "confirmed": 0, "differs": 0, "undecided": 0, "failed": 0, "already": 0, "lowres": 0, "loc": {}, "ratio": info.get("ratio")}
    if info.get("low_ratio"):
        stats["low_ratio"] = 1
    pdf_page = rec.get("pdf_page", 0)
    stop = should_stop or (lambda: False)
    ink = None; pick = []; asks: dict[str, GlyphAsk] = {}; old_of: dict[str, int | None] = {}
    plan = [(idx, s, *_to_ask(prev, s, force)) for idx, s in enumerate(cands, 1)]
    stats["already"] = sum(1 for p in plan if not p[2])                 # 상한에는 이미 물은 자리를 모두 센다 — 장부 빈도로 차례가 바뀌어도 쪽당 max_n 곳을 넘지 않게
    for idx, s, again, k in plan:
        if not again:
            continue
        if stats["already"] + len(pick) >= max_n:
            break
        if ink is None and img is not None:
            ink = page_ink(img)
        prepared, one, low = _prepare_asks([s], merged, bboxes, ink, horizontal, pdf_page, layer_lines, frame, min_px, ticks, stats)
        if low:
            stats["lowres"] += 1; continue                                  # 저해상: 묻지 않고 상한에도 세지 않는다
        sid = str(idx); _, site, win = prepared[0]
        asks[sid] = one["1"]; old_of[sid] = k; pick.append((sid, site, win))
    q = _ask_glyphs(pick, asks, set(), img, reader, horizontal, pdf_page, zoom=zoom, batch=batch, fallback_reader=fallback_reader,
                    stop=stop, blind=blind, stats=stats)
    cols = {}
    for reg in merged.regions:
        for ci, c in enumerate(columns_in_reading_order(reg)):
            cols.setdefault((reg.id, ci), c)
    drop: set[int] = set(); new: list[dict] = []; kept_by_fallback = 0
    for sid, site, win in pick:
        kind, got = _verdict(q, sid, site, stop, blind, stats)
        if kind == "skipped":
            continue
        choice = got.get("choice", "")
        outcome = "failed" if kind == "failed" else ("confirmed" if choice == site.a else "differs" if choice == site.b else "undecided")
        c = cols.get((site.region_id, site.col_index))
        e = {"region_id": site.region_id, "col": site.col_index, "char_index": site.char_index, "n_chars": site.n_chars,
             "col_from_right": c.col_from_right if c is not None else 0, "pre": site.pre, "post": site.post, "consensus": site.a, "layer": site.b,
             "choice": choice, "evidence": got.get("evidence", ""), "model": got.get("model", ""), "window": got.get("window", ""), "outcome": outcome,
             "asked_at": time.strftime("%Y-%m-%d %H:%M:%S"), "asked": "/".join(asks[sid].candidates), "loc": win.how, "box": [round(v, 4) for v in win.box]}
        if "blind" in got:
            e["blind"] = got["blind"]
        if win.em_px:
            e["em_px"] = round(win.em_px, 1)
        stats["asked"] += 1; stats[outcome] += 1
        if outcome in ("confirmed", "differs") and q.by.get(sid) is fallback_reader and fallback_reader is not None:
            kept_by_fallback += 1
        k = old_of.get(sid)
        if outcome == "failed" and k is not None and prev[k].get("outcome") != "failed":
            continue                                                     # 다시 물었는데 답이 없음(호출 실패): 앞 답과 그 확인 항목을 그대로 둔다
        if k is not None:                                                # 다시 물은 자리: 앞 기록과 그 확인 항목을 바꾼다
            drop.add(k); old_u = suspect_uncertain(prev[k])
            if prev[k].get("outcome") == "differs" and (outcome != "differs" or old_u != suspect_uncertain(e)):
                merged.uncertain = [u for u in merged.uncertain if u != old_u]
        if outcome == "differs":
            u = suspect_uncertain(e)
            if u not in merged.uncertain:
                merged.uncertain.append(u)
        new.append(e)
    if "fallback_decided" in stats:
        stats["fallback_decided"] = kept_by_fallback
    rec["merged"] = merged.model_dump()
    rec["suspects"] = [e for i, e in enumerate(prev) if i not in drop] + new
    rec["suspect_stats"] = stats
    return rec


_PUNCT = re.compile(r"[\s　·ㆍ・•\.。,，、:：;；!！?？()（）\[\]【】『』「」《》〈〉“”\"'‘’—─―－\-_/／\\|*＊~～…‥0-9０-９]")


def locate_in_reading(merged: PageReading, pre: str, ch: str, post: str, max_ctx: int = 4) -> Site | None:
    """책갈피 OCR 대조 항목(판독 앞뒤 글자 pre「ch」post)의 자리를 병합 판독문에서 찾는다. 부호·숫자는 빼고 맞춘다.
    찾으면 글자 창 좌표 추정에 쓸 Site(표시 글은 빈 글)를 돌려준다."""
    pre_k, post_k = _PUNCT.sub("", pre)[-max_ctx:], _PUNCT.sub("", post)[:max_ctx]
    for a, b in ((4, 4), (3, 3), (2, 2), (4, 1), (1, 4), (4, 0), (0, 4), (2, 1), (1, 2), (3, 0), (0, 3), (1, 1), (2, 0), (0, 2), (1, 0), (0, 1)):   # 문단 첫·끝 글자는 한쪽 문맥만으로
        pk, nk = pre_k[-a:] if a else "", post_k[:b] if b else ""
        if (a and len(pk) < a) or (b and len(nk) < b):
            continue
        needle = pk + ch + nk
        hits: list[Site] = []
        for reg in merged.regions:
            if reg.kind not in TEXT_KINDS:
                continue
            cols = columns_in_reading_order(reg)
            for ci, col in enumerate(cols):
                text = plain(col.text or "")
                stripped = []; idx = []
                for i, c in enumerate(text):
                    if not _PUNCT.match(c):
                        stripped.append(c); idx.append(i)
                st = "".join(stripped)
                j = st.find(needle)
                while j >= 0:
                    pos = j + len(pk)
                    hits.append(Site(reg.id, ci, len(cols), idx[pos], len(text), ch, "", text[max(0, idx[pos] - 4):idx[pos]], text[idx[pos] + 1:idx[pos] + 5], ""))
                    j = st.find(needle, j + 1)
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1 and a + b >= 4:
            return hits[0]
    return None
