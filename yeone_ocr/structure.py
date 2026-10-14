"""내용별 문서의 짜임: 판독 블록마다 쓰임(role)을 붙이고, 쓰임별 글자 범위(cstyles)를 단다. 양식 HWPX 는 쓰임 → 스타일 이름으로 채운다.

쓰임은 세 갈래에서 온다(모두 모델을 부르지 않는다).
1) 판독 기록의 영역 종류·제목 단계: 제목 1·2·3 단계 → chapter·section·subsection, 그림·표 제목 → caption, 소주 → small.
2) 책별 규칙(preset "bencao", 本草 고전 조문 책): 문단 첫머리의 「약재명 + 빈칸 + 味/一名/主…」 → 약재명(entry) 제목을 떼어 내고
   나머지는 조문, 「［陶隐居云］·［唐本注云］·［今按］…」 로 시작하는 문단 → 주석(commentary), 문단 끝의 「唐附·今附…」 → 출처(source).
   개보본초(尚志钧 輯復本) 394·395·396·413쪽 판면에서 확인한 꼴이다.
3) 획 굵기(measure_bold): 쪽 이미지에서 글자마다 획 굵기(먹 넓이 × 2 / 먹 둘레)를 재어, 책 전체 중앙값의 1.25배를 넘는 글자가 4자 이상
   이어지면(앞뒤 일곱 글자 중앙값으로 고른 뒤) 굵은 글씨로 본다. 輯復本은 本經 글을 굵게 찍었다(394쪽 苦瓠 4.6 · 395쪽 水靳 4.6, 보통 글 2.6~3.5).
   조문의 굵은 글자가 반을 넘으면 본경(bonjing), 아니면 별록(bielu). 섞인 문단은 굵은 범위를 bonjing 글자 범위로 남긴다.
글자는 바꾸지 않는다: 약재명을 떼어 낼 때 사이의 빈칸만 빠진다."""
from __future__ import annotations
import re
from dataclasses import replace
import numpy as np
from .blocks import Block, first_reading

ROLES = ("chapter", "section", "subsection", "entry", "body", "bonjing", "bielu", "commentary", "small", "caption", "note")
PRESETS = ("none", "auto", "bencao")   # auto = 주석·출처 규칙은 늘, 약재명 떼기는 그런 문단이 문서에 셋 이상일 때만(다른 책의 우연한 꼴을 떼지 않게)
_HAN = "㐀-䶿一-鿿豈-﫿\U00020000-\U0003134f"
ENTRY_RE = re.compile(rf"^([{_HAN}]{{1,6}})[ \t\n\u00a0\u2002\u2003\u3000]+(?=(?:味|一名|主|生|性|氣|气)[{_HAN}])")   # 사이 빈칸: 보통·전각·em·줄바꿈
COMMENT_RE = re.compile(r"^[［\[〔【（(]\s*[^］\]〕】）)\s]{0,10}?(?:云|曰|按|注|謹按|谨按|又按|今注|今按|說|说)\s*[］\]〕】）)]")
SOURCE_RE = re.compile(r"(?:(?<=[。，、；：\s　])|^)((?:唐|今|宋|新|陳藏器|陈藏器|海藥|海药|唐本)(?:附|餘|余|增|退|移))[ 　]*$")
BOLD_RATIO = 1.25            # 책 전체 중앙값의 이 배수를 넘으면 굵은 글자
BOLD_MIN_RUN = 4             # 이 글자 수 넘게 이어져야 굵은 범위로(낱글자 잡음 제외)


def _split(b: Block, cut: int, rest: int) -> tuple[Block, Block]:
    """블록 b 를 [0, cut) 와 [rest, 끝) 둘로(사이 글자는 버림 — 빈칸만). 등급·메모·쓰임 범위·책갈피를 나눠 옮긴다."""
    def rng(rs, lo, hi):
        return [(max(a, lo) - lo, min(e, hi) - lo, k) for a, e, k in rs if a < hi and e > lo]
    n = len(b.text)
    head = replace(b, text=b.text[:cut], spans=rng(b.spans, 0, cut), cstyles=rng(b.cstyles, 0, cut),
                   memos=[(min(o, cut - 1), m) for o, m in b.memos if o < cut], anchors=[(o, a) for o, a in b.anchors if o < cut])
    tail = replace(b, text=b.text[rest:], spans=rng(b.spans, rest, n), cstyles=rng(b.cstyles, rest, n),
                   memos=[(max(0, o - rest), m) for o, m in b.memos if o >= cut], anchors=[(max(0, o - rest), a) for o, a in b.anchors if o >= cut])
    return head, tail


ENTRY_START = re.compile(rf"(?:味|一名|主|生|性|氣|气)[{_HAN}]")


def _entry_cut(b: Block) -> tuple[int, int] | None:
    """약재명 제목을 떼어 낼 자리 (이름 끝, 조문 시작). 글에 빈칸이 있으면 그것으로, 없으면(판독이 빈칸을 빠뜨림) 쪽 이미지에서 잰
    첫 줄의 큰 틈(measure_bold 의 "gap") 으로 — 틈 앞이 한자 1~6자이고 틈 뒤가 味·一名·主… 로 시작할 때만."""
    if m := ENTRY_RE.match(b.text):
        return m.end(1), m.end()
    for a, _, k in b.cstyles:
        if k == "gap" and 1 <= a <= 6 and re.fullmatch(rf"[{_HAN}]+", b.text[:a]) and ENTRY_START.match(b.text, a):
            return a, a
    return None


def _join(a: Block, b: Block) -> Block:
    """a 의 글을 b 앞에 잇는다(쓰임·종류는 b 의 것). 등급·메모·쓰임 범위·책갈피 자리를 함께 옮긴다."""
    k = len(a.text)
    sh = lambda rs: [(x + k, y + k, z) for x, y, z in rs]
    return replace(b, text=a.text + b.text, spans=list(a.spans) + sh(b.spans), cstyles=list(a.cstyles) + sh(b.cstyles),
                   memos=list(a.memos) + [(o + k, m) for o, m in b.memos], anchors=list(a.anchors) + [(o + k, t) for o, t in b.anchors])


def _heading_entry(blocks: list[Block], i: int) -> tuple | None:
    """제목 블록 i 가 약재명인지(판독이 약재명을 제목으로 읽은 쪽이 많다):
    ("split", 이름 끝, 조문 시작) — 제목 글 안에 「약재명 + 빈칸 + 味·一名·主…」 가 붙음,
    ("name",) — 제목이 한자 1~6자이고 다음 문단이 味·一名 으로 시작(앞에 이름을 되읽은 표시 〔疑: 黄连〕 가 있어도).
    生·主 로 여는 문단은 보지 않는다: 序例의 병증 제목(吐唾血) 뒤 약재 목록(生地黃…)을 약재명으로 잘못 떼지 않게."""
    b = blocks[i]
    if b.type != "heading" or b.kind in ("그림제목", "표제목"):
        return None
    if m := ENTRY_RE.match(b.text):
        return ("split", m.end(1), m.end())
    name = re.sub(r"[\s　·・.,，。]", "", first_reading(b.text))
    if not re.fullmatch(rf"[{_HAN}]{{1,6}}", name):
        return None
    nxt = next((x for x in blocks[i + 1:i + 4] if x.type in ("para", "heading") and x.text.strip()), None)
    if nxt is None or nxt.type != "para" or _entry_cut(nxt):
        return None                                                     # 다음 문단이 제 약재명으로 시작하면(「菜 下」 뒤 「香薷 味辛…」) 이 제목은 위 단계
    t = first_reading(nxt.text).lstrip(" 　")
    if re.match(rf"[{_HAN}]{{0,{len(name)}}}[ 　,，、]*(?:味|一名)[{_HAN}]", t):
        return ("name",)
    return None


def _bold_role(b: Block) -> tuple[str, list]:
    """조문 블록의 본경·별록과 남길 굵은 범위(섞인 문단만). 굵기를 재지 못했으면 별록으로 둔다."""
    bold = [(a, e) for a, e, k in b.cstyles if k == "bold"]
    count = lambda rs: sum(1 for a, e in rs for c in b.text[a:e] if re.match(rf"[{_HAN}]", c))
    total = sum(1 for c in b.text if re.match(rf"[{_HAN}]", c)) or 1
    nb = count(bold)
    if nb == 0:
        return "bielu", []
    if nb >= 0.5 * total:
        return ("bonjing", [] if nb >= 0.9 * total else [(a, e, "bonjing") for a, e in bold])
    return "bielu", [(a, e, "bonjing") for a, e in bold]


def apply_structure(blocks: list[Block], preset: str = "bencao") -> tuple[list[Block], dict]:
    """블록에 쓰임을 붙이고(필요하면 약재명 제목을 떼어 내고) 쓰임별 글자 범위를 단다. 돌려주는 값: (새 블록 목록, 쓰임별 수).
    약재명은 문단 첫머리(_entry_cut)에서도, 판독이 약재명을 제목으로 읽은 제목 블록(_heading_entry)에서도 찾는다.
    제목 안에 붙은 조문 앞부분(「孔公孽 味辛，溫，無毒。」의 味…)은 떼어 다음 문단 앞에 잇는다."""
    if preset not in PRESETS:
        raise ValueError(f"preset 은 {PRESETS} 중 하나: {preset!r}")
    rules = preset != "none"
    heads = {i: h for i in range(len(blocks)) if rules and (h := _heading_entry(blocks, i))}
    entries = preset == "bencao" or (preset == "auto" and sum(1 for b in blocks if b.type == "para" and _entry_cut(b)) + len(heads) >= 3)
    out: list[Block] = []; stats: dict = {}
    tail: Block | None = None                                           # 제목에서 떼어 낸 조문 앞부분 — 다음 문단 앞에 잇는다

    def count(role: str) -> None:
        stats[role] = stats.get(role, 0) + 1

    def para(b: Block) -> None:
        nb = replace(b, cstyles=list(b.cstyles))
        if b.kind == "소주":
            nb.role = "small"
        elif rules and COMMENT_RE.match(b.text):
            nb.role = "commentary"
        elif entries and (cut := _entry_cut(b)):
            head, nb = _split(b, *cut)
            head.type, head.role, head.level = "heading", "entry", 3
            head.cstyles = []
            out.append(head); count("entry")
            nb.role, extra = _bold_role(nb)
            nb.cstyles = [c for c in nb.cstyles if c[2] != "bold"] + extra
        else:
            nb.role = "body"
            if entries:                                                 # 약재 조문을 이어 쓰는 문단(앞 쪽에서 넘어온 것 등)도 굵기로
                role, extra = _bold_role(nb)
                if role == "bonjing":
                    nb.role, nb.cstyles = role, [c for c in nb.cstyles if c[2] != "bold"] + extra
        if nb.role != "bonjing" and nb.role != "bielu":
            nb.cstyles = [c for c in nb.cstyles if c[2] != "bold"]
        nb.cstyles = [c for c in nb.cstyles if c[2] != "gap"]
        if rules and (m := SOURCE_RE.search(nb.text)):
            nb.cstyles.append((m.start(1), m.end(1), "source"))
        out.append(nb); count(nb.role)

    for i, b in enumerate(blocks):
        if tail is not None and not (b.type == "para" and b.kind != "소주" and not COMMENT_RE.match(b.text) and not _entry_cut(b)):
            para(tail); tail = None                                     # 이어 붙일 조문 문단이 아니면 따로 둔다
        if b.type == "heading":
            h = heads.get(i) if entries else None
            if h and h[0] == "split":
                head, rest = _split(b, h[1], h[2])
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
                b, tail = _join(tail, b), None
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
    if importlib.util.find_spec("cv2") is None:                       # 선택 설치 — 없으면 굵기를 재지 않고 전부 별록으로
        if log:
            log("  주의: opencv-python-headless 가 없어 획 굵기를 재지 않습니다 — 본경·별록을 가르지 않습니다.")
        return {}, {"pages": 0, "chars": 0, "median": 0.0, "bold_paragraphs": 0}
    for n, pr, img, frame, vertical in pages:
        try:
            per_page[n], gaps[n] = page_strokes(pr, img, frame, vertical)
        except Exception as e:                                          # 재지 못한 쪽은 굵기 없이(별록) — 문서는 계속
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
