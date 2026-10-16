"""내용별 문서의 짜임: 판독 블록마다 쓰임(role)을 붙이고, 쓰임별 글자 범위(cstyles)를 단다. 양식 HWPX 는 쓰임 → 스타일 이름으로 채운다.

쓰임은 세 갈래에서 온다(모두 모델을 부르지 않는다).
1) 판독 기록의 영역 종류·제목 단계: 제목 1·2·3 단계 → chapter·section·subsection, 그림·표 제목 → caption, 소주 → small.
2) 책별 규칙은 짜임 규칙(preset) 파일에서 — presets.py, data/preset_kaibao.json(本草 고전 조문 책): 문단 첫머리의 「약재명 + 빈칸 + 味/一名/主…」
   → 약재명(entry) 제목을 떼어 내고 나머지는 조문, 「［陶隐居云］·［唐本注云］·［今按］…」 로 시작하는 문단 → 주석(commentary),
   문단 끝의 「唐附·今附…」 → 출처(source). 개보본초(尚志钧 輯復本) 394·395·396·413쪽 판면에서 확인한 꼴이다.
   기본 규칙 basic 과 예전 regions 는 이 셋도 굵기 해석도 없이 1)의 영역 종류만 쓴다(어느 책에나 안전).
3) 획 굵기(measure_bold): 쪽 이미지에서 글자마다 획 굵기(먹 넓이 × 2 / 먹 둘레)를 재어, 책 전체 중앙값의 1.25배를 넘는 글자가 4자 이상
   이어지면(앞뒤 일곱 글자 중앙값으로 고른 뒤) 굵은 글씨로 본다. 輯復本은 本經 글을 굵게 찍었다(394쪽 苦瓠 4.6 · 395쪽 水靳 4.6, 보통 글 2.6~3.5).
   조문의 굵은 글자가 반을 넘으면 굵은 글씨 쓰임(kaibao: 본경 bonjing), 아니면 보통 글씨 쓰임(별록 bielu) — 짜임 규칙의 emphasis.
   섞인 문단은 굵은 범위를 굵은 글씨 쓰임의 글자 범위로 남긴다. emphasis 가 없는 규칙은 굵기를 보지 않는다.
글자는 바꾸지 않는다: 약재명을 떼어 낼 때 사이의 빈칸만 빠진다."""
from __future__ import annotations
import re
from dataclasses import replace
import numpy as np
from .blocks import Block
from .marks import fold, fold_map
from .presets import ROLES, DEFAULT_PRESET, Preset, resolve_preset, _HAN

__all__ = ["ROLES", "apply_structure", "measure_bold", "bold_spans", "page_strokes"]   # ROLES 는 presets 에서(예전 이름으로도 가져가게)
BOLD_RATIO = 1.25            # 책 전체 중앙값의 이 배수를 넘으면 굵은 글자
BOLD_MIN_RUN = 4             # 이 글자 수 넘게 이어져야 굵은 범위로(낱글자 잡음 제외)
# 예전 상수(ENTRY_RE·ENTRY_START·COMMENT_RE·SOURCE_RE·_OPENERS·_FUNC_END·_UNIT_END·_FUNC_START)는 짜임 규칙 data/preset_kaibao.json 으로 옮겼다
# — 규칙의 entry_re·entry_start_re·comment_re·source_re·name_re 와 entry.name_end_not·name_start_not.


def _is_name(name: str, p: Preset) -> bool:
    """한자 1~max_name 자이면서 글의 토막(…云·…按)·수량(…三兩)·허사 첫머리(凡…)가 아닌 것만 약재명 후보(entry.name_end_not·name_start_not)."""
    return bool(p.name_re.fullmatch(name)) and name[-1] not in p.entry.name_end_not and name[0] not in p.entry.name_start_not


def _entry_match(text: str, p: Preset):
    """글 첫머리의 「약재명 + 빈칸 + 味…」 — 판독 표시(〔a/b〕·〔疑: X〕)는 1차 글자로 접어 맞추고, 자리는 원글 기준으로 돌려준다."""
    folded, pos = fold_map(text, keep_other=True)
    m = p.entry_re.match(folded)
    if m and _is_name(m.group(1), p):
        return pos[m.end(1)], pos[m.end()]
    return None


def _entry_cut(b: Block, p: Preset) -> tuple[int, int] | None:
    """약재명 제목을 떼어 낼 자리 (이름 끝, 조문 시작). 글에 빈칸이 있으면 그것으로, 없으면(판독이 빈칸을 빠뜨림) 쪽 이미지에서 잰
    첫 줄의 큰 틈(measure_bold 의 "gap") 으로 — 틈 앞이 약재명 꼴이고 틈 뒤가 조문 첫 낱말(味·一名·主…)로 시작할 때만."""
    if cut := _entry_match(b.text, p):
        return cut
    for a, _, k in b.cstyles:
        if k == "gap" and 1 <= a <= len(b.text):
            name = fold(b.text[:a], keep_other=True)
            if _is_name(name, p) and p.entry_start_re.match(fold(b.text[a:], keep_other=True)):
                return a, a
    return None


def _comment(text: str, p: Preset) -> bool:
    """주석 문단 첫머리(［陶隱居云］…) — 규칙에 commentary 가 없으면 늘 아니다."""
    return p.comment_re is not None and bool(p.comment_re.match(text))


def _starts_entry(text: str, p: Preset) -> bool:
    """쪽 첫머리에서 넘어온 글이 새 조문(약재명·주석)으로 시작하는가 — cont 로 잘못 이어진 것을 가를 때."""
    return bool(_entry_match(text, p)) or _comment(text, p)


def _like_name(a: str, b: str) -> bool:
    """되읽은 이름 a 가 제목 b 와 같은 약재명인가: 이체자·호환 한자를 접고 간체·번체 차이(雄黄/雄黃)는 OpenCC 로 맞춘 뒤, 글자 절반 이상 같으면(點青/扁青 처럼
    판독이 한 글자를 달리 읽은 것도) 같은 것으로 본다. 「菜 下」와 「香薷」처럼 아예 다른 이름은 아니다."""
    from difflib import SequenceMatcher
    from .merge import _fold
    try:
        from .normalization import _opencc_converter
        conv = _opencc_converter()
    except Exception:
        conv = lambda x: x
    fa, fb = _fold(conv(a)), _fold(conv(b))
    return fa == fb or SequenceMatcher(None, fa, fb).ratio() >= 0.5


def _heading_entry(blocks: list[Block], i: int, p: Preset) -> tuple | None:
    """제목 블록 i 가 약재명인지(판독이 약재명을 제목으로 읽은 쪽이 많다):
    ("split", 이름 끝, 조문 시작) — 제목 글 안에 「약재명 + 빈칸 + 味·一名·主…」 가 붙음,
    ("name",) — 제목이 약재명 꼴이고 다음 문단이 entry.heading_openers(kaibao: 味·一名)로 시작(앞에 이름을 되읽은 표시 〔疑: 黄连〕 가 있어도).
    kaibao 는 生·主 로 여는 문단은 보지 않는다: 序例의 병증 제목(吐唾血) 뒤 약재 목록(生地黃…)을 약재명으로 잘못 떼지 않게."""
    b = blocks[i]
    if b.type != "heading" or b.kind in ("그림제목", "표제목"):
        return None
    if cut := _entry_match(b.text, p):
        return ("split", cut[0], cut[1])
    name = re.sub(r"[\s　·・.,，。]", "", fold(b.text, keep_other=True))
    if not _is_name(name, p):
        return None
    j = next((k for k in range(i + 1, min(len(blocks), i + 4)) if blocks[k].type in ("para", "heading") and blocks[k].text.strip()), None)
    if j is None:
        return None
    nxt = blocks[j]
    if nxt.type == "heading":                                           # 약재명 둘이 잇달아 제목으로 읽혔으면(뒤 것이 약재명이면) 앞 것도
        return ("name",) if len(name) <= 4 and _heading_entry(blocks, j, p) else None
    if _entry_cut(nxt, p):
        return None                                                     # 다음 문단이 제 약재명으로 시작하면(「菜 下」 뒤 「香薷 味辛…」) 이 제목은 위 단계
    t = fold(nxt.text, keep_other=True).lstrip(" 　")
    m = p.heading_next_re.match(t)
    if m and (not m.group(1) or _like_name(m.group(1), name)):          # 앞에 되읽은 이름이 있으면 이 제목과 같아야(다른 약재명이면 아님)
        return ("name",)
    return None


def _bold_role(b: Block, measured: bool, p: Preset) -> tuple[str, list]:
    """조문 블록의 쓰임(kaibao: 본경·별록)과 남길 굵은 범위(섞인 문단만) — 짜임 규칙의 emphasis = (굵은 글씨 쓰임, 보통 글씨 쓰임).
    굵기를 재지 않았거나 책에 굵은 글자가 없으면(measured=False) 가르지 않고 본문으로 둔다. emphasis 가 없는 규칙은 늘 본문."""
    if p.emphasis is None:
        return "body", []
    strong, plain = p.emphasis
    bold = [(a, e) for a, e, k in b.cstyles if k == "bold"]
    count = lambda rs: sum(1 for a, e in rs for c in b.text[a:e] if re.match(rf"[{_HAN}]", c))
    total = sum(1 for c in b.text if re.match(rf"[{_HAN}]", c)) or 1
    nb = count(bold)
    if nb == 0:
        return (plain if measured else "body"), []
    if nb >= 0.5 * total:
        return (strong, [] if nb >= 0.9 * total else [(a, e, strong) for a, e in bold])
    return plain, [(a, e, strong) for a, e in bold]


def apply_structure(blocks: list[Block], preset: Preset | str = DEFAULT_PRESET, bold_measured: bool = True) -> tuple[list[Block], dict]:
    """블록에 쓰임을 붙이고(필요하면 약재명 제목을 떼어 내고) 쓰임별 글자 범위를 단다. 돌려주는 값: (새 블록 목록, 쓰임별 수).
    약재명은 문단 첫머리(_entry_cut)에서도, 판독이 약재명을 제목으로 읽은 제목 블록(_heading_entry)에서도 찾는다.
    제목 안에 붙은 조문 앞부분(「孔公孽 味辛，溫，無毒。」의 味…)은 떼어 다음 문단 앞에 잇는다.
    쪽 첫머리(책갈피 자리)에서 새 조문·주석이 시작되는데 앞 문단에 이어진 것(판독의 cont 가 틀림)은 거기서 가른다(stats['page_split']).
    bold_measured=False(획 굵기를 재지 않음)거나 문서 어디에도 굵은 범위가 없으면(本經을 굵게 찍지 않은 책) 本經·別錄을 가르지 않고 조문을 본문으로 둔다
    — '굵은 글자 없음' 을 '別錄' 으로 읽으면 本經 조문까지 별록 꼬리표가 붙는다.
    preset: 짜임 규칙(Preset, 또는 이름 — 내장 id·예전 --structure 값·규칙 JSON 경로, presets.resolve_preset). 기본 basic 은 영역 종류만:
    규칙에 없는 칸(entry·commentary·source·emphasis = null)의 일은 하지 않는다. 약재명 떼기는 entry.when 이 always 이거나
    그런 문단(제목으로 읽힌 약재명 포함)이 min_hits 이상이고 문단의 min_ratio 이상일 때만."""
    p = preset if isinstance(preset, Preset) else resolve_preset(preset)
    bold_measured = bold_measured and p.emphasis is not None and any(k == "bold" for b in blocks for _, _, k in b.cstyles)
    heads = {i: h for i in range(len(blocks)) if p.entry and (h := _heading_entry(blocks, i, p))}
    n_para = sum(1 for b in blocks if b.type == "para")
    hits = (sum(1 for b in blocks if b.type == "para" and _entry_cut(b, p)) + len(heads)) if p.entry else 0
    entries = p.entry is not None and (p.entry.when == "always" or (hits >= p.entry.min_hits and hits >= p.entry.min_ratio * n_para))   # 우연한 꼴 몇 개로 다른 책에서 켜지지 않게
    out: list[Block] = []; stats: dict = {}
    tail: Block | None = None                                           # 제목에서 떼어 낸 조문 앞부분 — 다음 문단 앞에 잇는다

    def count(role: str) -> None:
        stats[role] = stats.get(role, 0) + 1

    def para(b: Block) -> None:
        if entries:                                                     # 쪽 첫머리에서 새 조문·주석이 시작되면 거기서 가른다
            for o, _ in sorted(b.anchors):
                if 0 < o < len(b.text) and _starts_entry(b.text[o:], p):
                    head, rest = b.cut(o)
                    stats["page_split"] = stats.get("page_split", 0) + 1
                    para(head); para(rest); return
        nb = replace(b, cstyles=list(b.cstyles))
        if b.kind == "소주":
            nb.role = "small"
        elif _comment(b.text, p):                                       # 〔陶隱居云〕 꼴도 주석(판독이 책의 ［］ 를 〔〕 로 적기도 한다)
            nb.role = "commentary"
        elif entries and (cut := _entry_cut(b, p)):
            head, nb = b.cut(*cut)
            head.type, head.role, head.level = "heading", "entry", 3
            head.cstyles = []
            out.append(head); count("entry")
            nb.role, extra = _bold_role(nb, bold_measured, p)
            nb.cstyles = [c for c in nb.cstyles if c[2] != "bold"] + extra
        elif entries and out and out[-1].type == "heading" and out[-1].role == "entry":
            nb.role, extra = _bold_role(nb, bold_measured, p)           # 약재명 제목 바로 뒤 조문 — 문단 첫머리에서 떼어 낸 조문과 같은 규칙(한 문서 안에서 같은 모양)
            nb.cstyles = [c for c in nb.cstyles if c[2] != "bold"] + extra
        else:
            nb.role = "body"
            if entries:                                                 # 약재 조문을 이어 쓰는 문단(앞 쪽에서 넘어온 것 등)도 굵기로
                role, extra = _bold_role(nb, bold_measured, p)
                if p.emphasis and role == p.emphasis[0]:
                    nb.role, nb.cstyles = role, [c for c in nb.cstyles if c[2] != "bold"] + extra
        nb.cstyles = [c for c in nb.cstyles if c[2] not in ("bold", "gap")]   # 잰 굵은 범위·틈은 여기까지 — 굵기로 정한 조문은 위에서 쓰임 범위(extra)로 바꿨다
        if p.source_re is not None and (m := p.source_re.search(nb.text)):
            nb.cstyles.append((m.start(1), m.end(1), "source"))
        out.append(nb); count(nb.role)

    for i, b in enumerate(blocks):
        if tail is not None and not (b.type == "para" and b.kind != "소주" and not _comment(b.text, p) and not _entry_cut(b, p)):
            para(tail); tail = None                                     # 이어 붙일 조문 문단이 아니면 따로 둔다
        if b.type == "heading":
            h = heads.get(i) if entries else None
            if h and h[0] == "split":
                head, rest = b.cut(h[1], h[2])
                out.append(replace(head, role="entry", level=3, cstyles=[])); count("entry")
                tail = replace(rest, type="para", kind="본문", level=0, role="")
                continue
            if h:
                role = "entry"
            else:
                role = "caption" if b.kind in ("그림제목", "표제목") else ("chapter", "section", "subsection")[min(3, max(1, b.level)) - 1]
            out.append(replace(b, role=role, level=3 if role == "entry" else b.level, cstyles=[c for c in b.cstyles if c[2] != "bold"])); count(role)
        elif b.type == "para":
            if tail is not None:
                b, tail = Block.joined(tail, b), None
            para(b)
        else:
            out.append(replace(b, role=b.role or "note") if b.type in ("note", "uncertain", "report", "meta") else b)
    if tail is not None:
        para(tail)
    return out, stats


# ---- 획 굵기 ----
def _ink(img) -> np.ndarray:
    """쪽 그림 → 먹(1)/바탕(0). 문턱은 쪽의 Otsu."""
    import cv2
    g = np.asarray(img.convert("L"))
    thr, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return (g < thr).astype(np.uint8)


def _stroke_widths(ink: np.ndarray, lines) -> list[tuple[object, int, float]]:
    """줄(textlayer.Line)마다 글자 상자의 획 굵기 = 2 × 먹 넓이 / 먹 둘레(px). 실측하지 못한 줄(균등 분할)은 뺀다.
    돌려주는 값: [(줄, 글자 번호, 굵기)]."""
    import cv2
    H, W = ink.shape
    out = []
    for ln in lines:
        if ln.how in ("uniform",):
            continue
        for k, (c, b) in enumerate(ln.chars):
            if not c or not re.match(rf"[{_HAN}]", c):
                continue
            x0, y0, x1, y1 = (max(0, int(b[0] * W)), max(0, int(b[1] * H)), min(W, int(b[2] * W) + 1), min(H, int(b[3] * H) + 1))
            a = ink[y0:y1, x0:x1].copy()
            area = float(a.sum())
            if area < 15:
                continue
            cs, _ = cv2.findContours(a, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
            per = sum(cv2.arcLength(ct, True) for ct in cs)
            if per > 0:
                out.append((ln, k, 2 * area / per))
    return out


def _box_gap(ln, first: int = 6) -> int | None:
    """실측한 글자 상자에서 앞 first 자 안의, 글자 사이가 0.8 자 넘게 빈 첫 자리 → 그 앞 글자 수."""
    ax = (1, 3) if ln.dir == "v" else (0, 2)
    for k in range(min(first, len(ln.chars) - 1)):
        a, b = ln.chars[k][1], ln.chars[k + 1][1]
        em = a[ax[1]] - a[ax[0]]
        if em > 0 and b[ax[0]] - a[ax[1]] > 0.8 * em:
            return k + 1
    return None


def _ink_gap(ink: np.ndarray, ln, first: int = 6) -> int | None:
    """글자 상자를 실측하지 못한 줄(ink~ — 굵은 획이 붙어 글자 수가 맞지 않음): 줄 띠의 먹 구간에서 바로 빈칸을 잰다.
    한 자 폭(pitch)은 (줄 길이 − 빈칸) / 글자 수, 빈칸 앞 글자 수는 빈칸까지의 길이 / pitch 의 반올림."""
    from .layout import ink_runs, merge_glyph_runs
    H, W = ink.shape
    bs = [b for _, b in ln.chars]
    if len(bs) < 3:
        return None
    x0, y0 = max(0, int(min(b[0] for b in bs) * W)), max(0, int(min(b[1] for b in bs) * H))
    x1, y1 = min(W, int(max(b[2] for b in bs) * W) + 1), min(H, int(max(b[3] for b in bs) * H) + 1)
    a = ink[y0:y1, x0:x1]
    if a.size == 0:
        return None
    v = ln.dir == "v"
    runs = ink_runs(a.mean(axis=1) if v else a.mean(axis=0), 0.002, 2)
    thick = (x1 - x0) if v else (y1 - y0)
    gl = merge_glyph_runs(runs, max(2, thick)) if runs else []
    if len(gl) < 2:
        return None
    span = gl[-1][1] - gl[0][0]
    for g0, g1 in zip(gl, gl[1:]):
        gap = g1[0] - g0[1]
        pitch = (span - gap) / len(bs)
        pos = g0[1] - gl[0][0]
        if pitch <= 0 or pos > (first + 0.5) * pitch:
            break
        if gap > 0.8 * pitch:
            return max(1, round(pos / pitch))
    return None


def page_strokes(pr, img, frame, vertical: bool) -> tuple[dict, dict]:
    """쪽 하나의 글자별 획 굵기를 판독 문단 자리로: ({(영역 id, 문단 번호): [(문단 글 안 위치, 굵기)]}, {(영역, 문단): 첫 줄 큰 틈 뒤 글자 위치}).
    제목·도판 영역은 재지 않는다."""
    from .textlayer import plan_page, token_positions
    from .schema import TITLE_KINDS, SKIP_KINDS
    lines = plan_page(pr, img, frame, None, vertical)
    regs = {r.id: r for r in pr.regions}
    ink = _ink(img)
    widths = _stroke_widths(ink, [ln for ln in lines if ln.region in regs and regs[ln.region].kind not in TITLE_KINDS | SKIP_KINDS])
    out: dict = {}
    where: dict = {}
    for rid, reg in regs.items():                                     # 줄 번호 → (문단 번호, 문단 글 안에서 그 열이 시작하는 자리)
        if reg.kind in TITLE_KINDS | SKIP_KINDS:
            continue
        k = 0
        for pi, p in enumerate(reg.paragraphs):
            off = 0
            for c in sorted(p.columns, key=lambda c: c.col_from_right):
                where[(rid, k)] = (pi, off, c.text or ""); off += len(c.text or ""); k += 1
    pos_cache: dict = {}
    gaps: dict = {}
    for ln in lines:                                                  # 문단 첫 줄에서 글자 사이가 한 칸 넘게 빈 첫 자리(약재명 뒤 빈칸)
        hit = where.get((ln.region, ln.index))
        if hit is None or hit[1] != 0 or ln.how in ("uniform", "layer"):
            continue
        k = _ink_gap(ink, ln) if ln.how == "ink~" else _box_gap(ln)
        tp = token_positions(hit[2])
        if k is not None and 0 < k < len(tp):
            gaps[(ln.region, hit[0])] = tp[k][1]
    for ln, k, w in widths:
        hit = where.get((ln.region, ln.index))
        if hit is None:
            continue
        pi, off, text = hit
        if id(ln) not in pos_cache:
            pos_cache[id(ln)] = token_positions(text)
        tp = pos_cache[id(ln)]
        if k < len(tp):
            out.setdefault((ln.region, pi), []).append((off + tp[k][1], w))
    return out, gaps


def bold_spans(strokes: dict, median: float, ratio: float = BOLD_RATIO, min_run: int = BOLD_MIN_RUN, window: int = 7) -> dict:
    """{(영역, 문단): [(위치, 굵기)]} → {(영역, 문단): [(시작, 끝)]} 굵은 범위. 글자 하나의 굵기는 상자 자리가 조금 어긋나도 흔들리므로
    앞뒤 window 자의 중앙값으로 고르게 한 뒤 문턱(중앙값 × ratio)과 견주고, min_run 자보다 짧은 구간은 이웃 구간에 합친다
    (本經·別錄이 한두 글자씩 바뀌는 일은 없다)."""
    out = {}
    if not median:
        return out
    h = window // 2
    for key, seq in strokes.items():
        seq = sorted(seq)
        ws = [w for _, w in seq]
        flags = [float(np.median(ws[max(0, i - h):i + h + 1])) > ratio * median for i in range(len(ws))]
        runs = []                                                     # [시작 번호, 끝 번호(포함), 굵은가]
        for i, f in enumerate(flags):
            if runs and runs[-1][2] == f:
                runs[-1][1] = i
            else:
                runs.append([i, i, f])
        changed = True
        while changed and len(runs) > 1:                              # 가장 짧은 짧은 구간부터 이웃에 합친다
            changed = False
            k = min(range(len(runs)), key=lambda k: runs[k][1] - runs[k][0])
            if runs[k][1] - runs[k][0] + 1 < min_run:
                runs[k][2] = not runs[k][2]
                merged = []
                for r in runs:
                    if merged and merged[-1][2] == r[2]:
                        merged[-1][1] = r[1]
                    else:
                        merged.append(list(r))
                runs = merged; changed = True
        spans = [(seq[i][0], seq[j][0] + 1) for i, j, f in runs if f and j - i + 1 >= min_run]
        if spans:
            out[key] = spans
    return out


def measure_bold(pages, log=None) -> tuple[dict, dict]:
    """pages = (쪽, PageReading, 쪽 이미지, 이미지 틀, 세로쓰기인가) 들(생성기도 됨 — 이미지를 한 장씩) → ({(쪽, 영역, 문단): [(시작, 끝, 종류)]}, 요약).
    종류 "bold" = 굵은 글자 범위, "gap" = 첫 줄 큰 틈 뒤 글자 위치(시작=끝, 약재명 떼기에). 굵기 문턱은 책(넘겨준 쪽 전체)의 글자 굵기 중앙값에서
    잡는다 — 한 쪽이 통째로 굵어도 그 쪽을 보통으로 잘못 보지 않게."""
    per_page = {}; gaps = {}
    import importlib.util
    if importlib.util.find_spec("cv2") is None:                       # 선택 설치 — 없으면 굵기를 재지 않는다(조문은 본문으로)
        if log:
            log("  주의: opencv-python-headless 가 없어 획 굵기를 재지 않습니다 — 본경·별록을 가르지 않습니다.")
        return {}, {"pages": 0, "chars": 0, "median": 0.0, "bold_paragraphs": 0}
    for n, pr, img, frame, vertical in pages:
        try:
            per_page[n], gaps[n] = page_strokes(pr, img, frame, vertical)
        except Exception as e:                                          # 재지 못한 쪽은 굵기 없이 — 문서는 계속
            if log:
                log(f"  주의: {n}쪽 획 굵기를 재지 못했습니다({type(e).__name__}: {str(e)[:60]}) — 그 쪽은 본경·별록을 가르지 않습니다.")
    allw = [w for st in per_page.values() for seq in st.values() for _, w in seq]
    median = float(np.median(allw)) if allw else 0.0
    out: dict = {}
    for n, st in per_page.items():
        for key, spans in bold_spans(st, median).items():
            out.setdefault((n, key[0], key[1]), []).extend((a, e, "bold") for a, e in spans)
        for key, pos in gaps[n].items():
            out.setdefault((n, key[0], key[1]), []).append((pos, pos, "gap"))
    nb = sum(1 for v in out.values() if any(k == "bold" for _, _, k in v))
    return out, {"pages": len(per_page), "chars": len(allw), "median": round(median, 2), "bold_paragraphs": nb}
