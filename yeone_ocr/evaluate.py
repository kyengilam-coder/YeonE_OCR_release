"""평가 체계(단계 0): 저장된 쪽 판독 기록을 사람이 입력한 HWPX 와 맞춰 단계별 글자 오류율(CER)·표시 밖 오류율·3차 정밀도 등을 잰다.
읽기 전용 — 모델을 부르지 않고 기록·HWPX 를 고치지 않는다. 기준은 명령줄에 준 원본 HWPX(교정본 <이름>_교정.hwpx 는 받지 않는다 — 순환 평가 방지).

명령줄:  yeone-ocr eval <pdf> --hwpx <원본.hwpx> [--pages 1-5] [--work …] [--out <폴더>] [--mode auto|vertical|horizontal_zh] [--sample 200] [--labels eval/labels.json]
결과:    <work>/eval/summary.json(숫자 전부·방법·표본 id) · summary.md(한글 표) · diffs.html(사람이 판정할 차이 표본)
단계별 글: 글자층 · 1차 · 2차(2차가 없거나 일부 조각만 읽힌 영역은 1차 글, coverage 로 가름) · 병합(첫 후보) · 병합(oracle) · 3차 뒤(기록의 merged 그대로 — 3차 판별이 적용돼 있다고 본다).
  3차 전 병합문은 merged 에서 확정 글자를 표시로 되돌려 만든다(grades.decided_positions). 3차 기록이 없으면 병합 = 3차 뒤."""
from __future__ import annotations
import difflib, hashlib, html, json, random, re, time
from array import array
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from . import merge
from .merge import MARK_RE, INVISIBLE, strip_marks_for_compare, _fold, columns_in_reading_order, nfc_text
from .schema import PageReading, RegionReread, Paragraph, Column, TEXT_KINDS
from .collate.run import index_both, trim_to_section, reading_text
from .collate.collate import hwpx_cells, ocr_cells, note_spans

STAGES = ["layer", "pass1", "pass2", "merged_default", "merged_oracle", "final"]
STAGE_KO = {"layer": "글자층", "pass1": "1차", "pass2": "2차", "merged_default": "병합(첫 후보)", "merged_oracle": "병합(oracle)", "final": "3차 뒤"}
ANCHOR = 4             # difflib 일치 구간이 이 길이 이상이면 닻(닻 정렬은 거리의 상한을 얻는 데 쓴다)
CELLS = 4_000_000      # 정확한 편집 거리 DP 한 번에 채울 칸 수 상한(칸마다 역추적 1바이트). 넘으면 어림
CTX = 6                # 차이 표본에 보일 앞뒤 글자 수
LABELS = ("판독 틀림", "HWPX 틀림", "인쇄 오자", "이체자")
_SENT0 = 0xF0000       # 보조 사용자 영역: 3차 기록 번호 k → chr(_SENT0 + k) 를 그 표시 앞에 둔다(flatten 이 떼어 내며 표시 ↔ 기록을 잇는다)
_COVER = chr(0xFFFFD)  # 2차 조각이 읽은 문단의 머리 표지
TOKEN_RE = re.compile(r"([\U000F0000-\U000FFFFD])|(〔[^〔〕]*〕)|(\S)")
SENT_RE = re.compile(r"[\U000F0000-\U000FFFFD]")
WS_RE = re.compile(r"\s+")
METHOD = ("정규화: 이체자 호환 한자를 보존하는 NFC → 폭 없는 글자 제거 → 공백 제거 → 접기(전각 부호 → 반각; horizontal_zh 모드는 compare_key 로 간체·이체자 대표 글자). 원글 CER 은 이체자 호환 한자를 구분하며 접기는 글자 수를 보존한다.\n"
          f"정렬: 정확한 편집 거리(Levenshtein) — difflib 의 {ANCHOR}자 이상 일치 구간을 닻으로 한 정렬로 상한 U 를 얻고, 대각선마다 가장 멀리 닿는 방법(Ukkonen, 보통 O(n+D²))"
          f" 또는 너비 ~U 의 띠 DP 로 전체를 다시 맞춘다. 둘 다 {CELLS:,}칸을 넘으면 닻 정렬(상한, 닻 사이도 그 칸 수를 넘으면 difflib 로 어림)을 쓰고 summary 의 align 에 센다."
          " CER = 편집 수 ÷ 기준 글자 수.\n"
          "단계: 글자층 = textlayer plain · 1차 = pass1 · 2차 = pass2 열 글(2차가 없거나 일부 조각만 읽힌 영역은 1차 글, coverage 로 가름) · 병합(첫 후보) = 3차 전 병합문(기록의 merged 에서 확정 글자를 표시로 되돌림)"
          " · oracle = 표시마다 앞뒤 1자 포함 국소 편집 거리가 가장 작은 후보(탐욕) · 3차 뒤 = 기록의 merged 그대로(3차 판별이 적용돼 있다고 본다).\n"
          "표시 밖 오류율 = 어느 표시의 ±1자 안도 아닌 오류 ÷ 기준 글자 수. 표시 정밀도 = 기준이 첫 후보와 다른 표시 ÷ 표시. 3차 정밀도 = 확정 글자가 맞선 기준 글(표시 가장자리에서 빠진 기준 글자까지)과 같은 수 ÷ 확정, 결정률 = 확정 ÷ 물음(저해상 제외).\n"
          "오류 상관비 = P(2차 틀림|1차 틀림) ÷ P(2차 틀림), 2차 조각이 읽은 기준 자리만(전체는 단위들의 센 수를 모아서). 구획: HWPX 기준 ↔ PDF 책갈피 짝이 있으면 쪽 기록이 다 있는 구획마다, 없으면 요청 쪽 전체 ↔ HWPX 전체 한 단위.")


def log(msg: str) -> None:
    from .cli import log as _log
    _log(msg)


def fold_text(s: str, zh_fold: bool) -> str:
    """비교 전용 접기(글자 수 보존): 전각 부호 → 반각, zh_fold 면 한자를 간체·이체자 대표 글자(compare_key)로."""
    old, merge.FOLD_ZH = merge.FOLD_ZH, zh_fold
    try:
        return _fold(s)
    finally:
        merge.FOLD_ZH = old


@dataclass
class Flat:
    """공백을 뺀 한 줄 글. 글자마다 쪽(기준 글은 HWPX 문단 번호), 2차 조각이 읽은 글자인지, 〔〕 표시 목록(위치는 text 기준)."""
    text: str = ""
    page: list[int] = field(default_factory=list)
    cov: list[bool] = field(default_factory=list)
    marks: list[dict] = field(default_factory=list)      # {start, end, mark, cands, entry(3차 기록 번호|None), page}


def flatten(paras: list[tuple[int, str]], expand: bool = True) -> Flat:
    """[(쪽, 문단 글)] → Flat. 이체자를 보존하는 NFC·폭 없는 글자·공백 제거. expand 면 〔〕 표시를 첫 후보로 접고 후보를 남긴다."""
    f = Flat(); out: list[str] = []; n = 0
    for pg, text in paras:
        covered = False; entry = None
        for m in TOKEN_RE.finditer(INVISIBLE.sub("", nfc_text(text))):
            if m.group(1):
                covered = covered or m.group(1) == _COVER; entry = entry if m.group(1) == _COVER else ord(m.group(1)) - _SENT0; continue
            seg = m.group(0)
            if m.group(2) and expand:
                inner = seg[1:-1]; seg = WS_RE.sub("", strip_marks_for_compare(m.group(2)))
                cands = [inner[2:].strip(), ""] if inner.startswith("疑:") else ([c.strip() for c in inner.split("/")] if seg else [""])
                f.marks.append({"start": n, "end": n + len(seg), "mark": m.group(2), "cands": [WS_RE.sub("", c) for c in cands], "entry": entry, "page": pg}); entry = None
            out.append(seg); f.page += [pg] * len(seg); f.cov += [covered] * len(seg); n += len(seg)
    f.text = "".join(out)
    return f


# ---------- 정렬 ----------
def _band_dp(a: str, b: str, i0: int, j0: int, U: int) -> list[tuple[str, int, int]] | None:
    """a(기준)·b(가설)의 정확한 편집 정렬(Levenshtein). U 는 거리의 상한: 대각선 d = j - i 가 |d| + |Δ - d| > U 인 칸을 지나는 길은 비용이 U 를 넘으므로
    그 띠만 채운다(U = 긴 쪽 길이면 사실상 전부). 짧은 쪽을 열로 두어 비용은 행 두 줄(O(짧은 쪽)), 역추적은 칸마다 1바이트.
    채울 칸이 CELLS 를 넘으면 표를 만들지 않고 None. 동점이면 같음·바꿈 → 행 글자만 → 열 글자만 차례로 고른다."""
    sw = len(a) < len(b); A, B = (b, a) if sw else (a, b)
    n, m = len(A), len(B); dl = m - n; U = max(U, -dl); lo, hi = -((U - dl) // 2), (U + dl) // 2
    if not m or (n + 1) * min(m + 1, hi - lo + 1) > CELLS:
        return None
    INF = n + m + 1; prev = [INF] * (m + 2); cur = [INF] * (m + 2)          # [m + 1] = [-1] 은 늘 INF(j = 0 의 대각·왼쪽)
    h = min(m, hi); prev[:h + 1] = range(h + 1); tb = bytearray(b"\2" * (h + 1))
    for i in range(1, n + 1):
        ai = A[i - 1]; jl, jh = max(0, i + lo), min(m, i + hi); row = bytearray(jh - jl + 1); left = INF
        for j in range(jl, jh + 1):
            dg = prev[j - 1] + (ai != B[j - 1]); up = prev[j] + 1; left += 1
            if dg <= up and dg <= left:
                left = dg
            elif up <= left:
                left = up; row[j - jl] = 1
            else:
                row[j - jl] = 2
            cur[j] = left
        if jh < m:
            cur[jh + 1] = INF                                                # 다음 행의 띠 오른쪽 끝(위 칸)은 띠 밖
        tb += row; prev, cur = cur, prev
    width = lambda i: min(m, i + hi) - max(0, i + lo) + 1
    out: list = []; i, j = n, m; off = len(tb) - width(n)
    while i or j:
        t = tb[off + j - max(0, i + lo)]
        if t == 0:
            op = "=" if A[i - 1] == B[j - 1] else "R"; out.append((op, i0 + j - 1, j0 + i - 1) if sw else (op, i0 + i - 1, j0 + j - 1)); i -= 1; j -= 1; off -= width(i)
        elif t == 1:
            out.append(("I", i0 + j, j0 + i - 1) if sw else ("D", i0 + i - 1, j0 + j)); i -= 1; off -= width(i)
        else:
            out.append(("D", i0 + j - 1, j0 + i) if sw else ("I", i0 + i, j0 + j - 1)); j -= 1
    return out[::-1]


def _fr_align(a: str, b: str, U: int) -> list[tuple[str, int, int]] | None:
    """a(기준)·b(가설)의 정확한 편집 정렬 — 비용 d 마다 대각선 k = j - i 에서 가장 멀리 닿는 행만 적는다(Ukkonen·Landau–Vishkin).
    같은 글자가 이어지는 동안은 공짜로 미끄러지므로 보통 O(n + D²)(D = 거리 ≤ U) — 긴 글이라도 오류가 적으면 빠르다.
    적을 칸 (U+1)² 이 CELLS 를 넘거나 미끄럼이 길어지면(되풀이 글) None. 동점이면 바꿈 → 빠짐 → 덧남 차례로 고른다."""
    n, m = len(a), len(b); end = m - n
    if (U + 1) ** 2 > CELLS or abs(end) > U:
        return None
    FR: list = []; work = 0                                              # FR[d][k + d] = 비용 d 로 대각선 k 에서 닿는 가장 먼 행(못 닿으면 -1)
    for d in range(U + 1):
        P = FR[-1] if d else None; C = array("i", [-1]) * (2 * d + 1)
        for k in range(max(-d, -n), min(d, m) + 1):
            i = 0 if not d else -1
            if d and -d < k < d and 0 <= (r := P[k + d - 1]) < n and r + k < m:
                i = r + 1                                                # 바꿈(같은 대각선)
            if d and k + 1 < d and 0 <= (r := P[k + d]) < n and r + 1 > i:
                i = r + 1                                                # 빠짐(대각선 k+1 에서 기준 글자 하나)
            if d and k - 1 > -d and (r := P[k + d - 2]) > i and r + k <= m:
                i = r                                                    # 덧남(대각선 k-1 에서 가설 글자 하나)
            if i < 0:
                continue
            i1 = i
            while i < n and i + k < m and a[i] == b[i + k]:
                i += 1
            C[k + d] = i; work += i - i1 + 1
        FR.append(C)
        if work > 4 * CELLS:
            return None
        if -d <= end <= d and C[end + d] >= n:
            break
    else:
        return None
    out: list = []; i, k = n, end
    for d in range(len(FR) - 1, -1, -1):
        if not d:
            out += [("=", t, t + k) for t in range(i - 1, -1, -1)]; break
        P = FR[d - 1]
        cand = [((r + 1) if -d < k < d and 0 <= (r := P[k + d - 1]) < n and r + k < m else -1, "R"),
                ((r + 1) if k + 1 < d and 0 <= (r := P[k + d]) < n else -1, "D"),
                (r if k - 1 > -d and (r := P[k + d - 2]) >= 0 and r + k <= m else -1, "I")]
        p = max(c for c, _ in cand); op = next(o for c, o in cand if c == p)
        out += [("=", t, t + k) for t in range(i - 1, p - 1, -1)]
        if op == "R":
            out.append(("R", p - 1, p - 1 + k)); i = p - 1
        elif op == "D":
            out.append(("D", p - 1, p + k)); i = p - 1; k += 1
        else:
            out.append(("I", p, p + k - 1)); i = p; k -= 1
    return out[::-1]


def _gap(a: str, b: str, i0: int, j0: int, st: dict | None = None) -> list[tuple[str, int, int]]:
    """닻 사이 구간의 정확한 편집 정렬(_band_dp). 칸이 CELLS 를 넘으면 표를 만들지 않고 difflib 정렬로 어림하며 st['approx_gaps'] 에 센다."""
    n, m = len(a), len(b)
    if not n or not m:
        return [("I", i0, j0 + k) for k in range(m)] if not n else [("D", i0 + k, j0) for k in range(n)]
    ops = _band_dp(a, b, i0, j0, max(n, m))
    if ops is not None:
        return ops
    if st is not None:
        st["approx_gaps"] = st.get("approx_gaps", 0) + 1
    out: list = []
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        k = min(a2 - a1, b2 - b1) if op != "equal" else a2 - a1
        out += [("=" if op == "equal" else "R", i0 + a1 + t, j0 + b1 + t) for t in range(k)] + _gap(a[a1 + k:a2], b[b1 + k:b2], i0 + a1 + k, j0 + b1 + k)
    return out


def _lower(a: str, b: str) -> int:
    """편집 거리의 하한: 글자별 개수 차(바꿈·빠짐은 a 쪽 남는 글자를, 바꿈·덧남은 b 쪽 남는 글자를 하나씩만 없앤다)."""
    ca, cb = Counter(a), Counter(b)
    return max(sum((ca - cb).values()), sum((cb - ca).values()))


def align(ref: str, hyp: str, st: dict | None = None) -> list[tuple[str, int, int]]:
    """글자 단위 편집 정렬 [(op, i, j)]: '=' 같음 · 'R' 바꿈 · 'D' 기준에만 있음(j 는 가설에서 끼울 자리) · 'I' 가설에만 있음(i 는 기준에서 끼울 자리).
    정확한 편집 거리: difflib 의 ANCHOR 자 이상 일치 구간을 닻으로 한 정렬(닻 사이는 정확한 DP)로 상한 U 를 얻고, U 가 하한(_lower)이면 그대로,
    아니면 _fr_align(없으면 _band_dp)으로 전체를 다시 맞춘다(닻이 되풀이 구절을 잘못 잡아도 정확). 둘 다 CELLS 를 넘을 때만 닻 정렬(상한)을 쓰고
    st 에 센다: alignments(정렬 수) · inexact(상한으로 남은 정렬) · approx_gaps(그 정렬에서 difflib 로 어림한 닻 사이)."""
    ops: list = []; i = j = 0; g: dict = {}
    for b in [b for b in difflib.SequenceMatcher(None, ref, hyp, autojunk=False).get_matching_blocks() if b.size >= ANCHOR] + [difflib.Match(len(ref), len(hyp), 0)]:
        ops += _gap(ref[i:b.a], hyp[j:b.b], i, j, g) + [("=", b.a + k, b.b + k) for k in range(b.size)]
        i, j = b.a + b.size, b.b + b.size
    if st is not None:
        st["alignments"] = st.get("alignments", 0) + 1
    U = _dist(ops)
    if U > _lower(ref, hyp):
        ex = _fr_align(ref, hyp, U)
        if ex is None:
            ex = _band_dp(ref, hyp, 0, 0, U)                               # 길이가 크게 다르면 띠가 더 좁다
        if ex is not None:
            return ex
        if st is not None:
            st["inexact"] = st.get("inexact", 0) + 1; st["approx_gaps"] = st.get("approx_gaps", 0) + g.get("approx_gaps", 0)
    return ops


def _dist(ops) -> int: return sum(1 for op, _, _ in ops if op != "=")
def _lev(a: str, b: str) -> int: return _dist(_gap(a, b, 0, 0))
def _wrong_ref(ops) -> set[int]: return {i for op, i, _ in ops if op != "="}        # 틀린 기준 자리(가설에만 있는 글자는 끼울 자리의 기준 글자에)
def _stratum(d: dict) -> tuple: return (d["kind"], d["marked"], d["layer"])
def _pct(v, nd: int = 2) -> str: return "—" if v is None else f"{v * 100:.{nd}f}%"
def _corr(both: int, p1: int, p2: int, cov: int): return (both / p1) / (p2 / cov) if p1 and p2 else None     # P(2차 틀림|1차 틀림) ÷ P(2차 틀림)


def _in_span(ops, s: int, e: int) -> list:
    """가설 글자 [s, e) 에 걸린 정렬 항목. 빈 구간(s == e)이면 그 자리에 끼울 기준 글자만."""
    return [(op, i, j) for op, i, j in ops if (op != "D" and s <= j < e) or (op == "D" and (s < j < e or s == e == j))]


def _ref_span(ops, s: int, e: int, n_ref: int) -> tuple[int, int]:
    """가설 글자 [s, e) 에 맞선 기준 구간 [lo, hi)."""
    sel = _in_span(ops, s, e)
    if not sel:
        lo = next((i for op, i, j in ops if op != "D" and j >= e), n_ref); return lo, lo
    return min(i for _, i, _ in sel), max(i + (op != "I") for op, i, _ in sel)


def _choice_ok(ops, s: int, e: int, c: str, ref_f: str) -> bool:
    """3차가 고른 글자 c 가 표시 [s, e)(첫 후보가 든 자리)에 맞선 기준 글과 같은가. c 가 첫 후보보다 길면(〔已/己入〕→己入) 남는 기준 글자가
    표시 가장자리의 빠짐(D, j == s 또는 e)으로 정렬되므로(어느 쪽에 붙을지는 동점 처리) 맞선 구간을 그 빠진 글자까지 넓혀 본다."""
    lo, hi = _ref_span(ops, s, e, len(ref_f))
    L = sum(1 for op, i, j in ops if op == "D" and j == s and i < lo); R = sum(1 for op, i, j in ops if op == "D" and j == e and i >= hi)
    return any(ref_f[lo - x:hi + y] == c for x in range(L + 1) for y in range(R + 1))


# ---------- 단계별 글 ----------
def _pass2_partial(rr: dict | None) -> bool:
    """2차 조각 가운데 일부만 읽힌 영역: 일시 오류(pass2_failed)가 났거나 읽은 조각(chunks_read)이 나눈 조각(chunks)보다 적다.
    기록의 열은 읽힌 조각 것뿐이라 영역 글로 쓰면 못 읽은 조각이 2차 빠짐으로 잡힌다."""
    return bool(rr) and bool(rr.get("pass2_failed") or int(rr.get("chunks_read", rr.get("chunks", 1)) or 0) < int(rr.get("chunks", 1) or 1))


def _pass2_reading(rec: dict) -> PageReading:
    """1차 쪽 판독의 문자 영역을 2차 조각 판독(열 글)으로 바꾼 쪽 판독. 2차가 없거나 일부 조각만 읽힌 영역(제목·2차 실패·_pass2_partial)은
    1차 글 그대로 두고 머리 표지로 가른다."""
    pr = PageReading.model_validate(rec["pass1"])
    for reg in pr.regions:
        rr = (rec.get("pass2") or {}).get(reg.id)
        if reg.kind in TEXT_KINDS and rr and rr.get("columns") and not _pass2_partial(rr):
            cols = sorted(RegionReread.model_validate(rr).columns, key=lambda c: c.col_from_right)
            reg.paragraphs = [Paragraph(columns=[Column(col_from_right=k + 1, text=(_COVER if k == 0 else "") + c.text) for k, c in enumerate(cols)])]
    return pr


def _revert_adjudications(pr: PageReading, adj: list[dict], base: int) -> tuple[PageReading, int]:
    """3차 뒤 병합문에서 확정 글자를 표시 〔a/b〕로 되돌려 3차 전 병합문을 만든다. 되돌린 표시와 미정 표시 앞에 기록 번호 표지를 둔다.
    미정 자리는 apply_choice 처럼 (영역, 열, 표시 글)이 같고 글자 번호가 가장 가까운 표시로 잡는다.
    두 확정 기록이 겹치는 자리로 찾아지면(이웃 표시가 같은 후보로 달리 정해짐) 앞 기록만 되돌린다 — 한 자리에 두 번 되돌리면 표시가 덧난다.
    잇지 못한 기록 수(못 찾음·겹침·미정 표시 없음)도 돌려준다: 그만큼 병합(첫 후보) 글·물음 수가 어림이다."""
    from .grades import decided_positions
    from .adjudicate import plain
    pr = pr.model_copy(deep=True); found, missing = decided_positions(pr, adj)
    ids = {id(a): k for k, a in enumerate(adj)}; edits: dict[tuple, list] = {}; lost = len(missing)
    for L in found:
        taken = edits.setdefault((L.region_id, L.para_index), [])
        if any(L.start < e and s < L.end for s, e, _ in taken):
            lost += 1; continue
        taken.append((L.start, L.end, chr(_SENT0 + base + ids[id(L.entry)]) + L.entry["mark"]))
    for reg in pr.regions:
        for pi, p in enumerate(reg.paragraphs):
            off = 0
            for c in sorted(p.columns, key=lambda c: c.col_from_right):
                n = len(c.text or "")
                for s, e, t in sorted((s - off, e - off, t) for s, e, t in edits.get((reg.id, pi), []) if off <= s < off + n)[::-1]:
                    c.text = c.text[:s] + t + c.text[e:]
                off += n
    for k, a in enumerate(adj):
        if a.get("choice"):
            continue
        reg = next((r for r in pr.regions if r.id == a.get("region_id")), None)
        cols = columns_in_reading_order(reg) if reg else []; ci = int(a.get("col_index", -1)); hits = []
        if 0 <= ci < len(cols):
            text = cols[ci].text or ""
            hits = [(abs(len(plain(SENT_RE.sub("", text[:m.start()]))) - int(a.get("char_index", 0))), m.start()) for m in MARK_RE.finditer(text)
                    if m.group(0) == a.get("mark") and not (m.start() and SENT_RE.match(text[m.start() - 1]))]
            if hits:
                at = min(hits)[1]; cols[ci].text = text[:at] + chr(_SENT0 + base + k) + text[at:]
        lost += not hits
    return pr, lost


def _stage_paras(work, doc, n: int, rec: dict, adj_base: int) -> tuple[dict[str, list], list[dict], int]:
    """한 쪽의 단계별 [(쪽, 문단 글)]. 3차 기록은 adj_base 부터 번호를 매긴 표지로 병합(3차 전) 글의 표시에 잇는다(잇지 못한 기록 수도 돌려준다)."""
    adj = list(rec.get("adjudications") or []); final = reading_text(rec); lost = 0
    st = {"final": final, "pass1": final, "pass2": final, "merged_default": final}
    if rec.get("pass1"):
        st["pass1"] = reading_text({"merged": rec["pass1"]}); st["pass2"] = reading_text({"merged": _pass2_reading(rec).model_dump()})
    if adj:
        pr, lost = _revert_adjudications(PageReading.model_validate(rec["merged"]), adj, adj_base); st["merged_default"] = reading_text({"merged": pr.model_dump()})
    try:
        tlp = work.textlayer_path(n)
        if tlp.exists():
            plain = json.loads(tlp.read_text(encoding="utf-8")).get("plain", "")
        else:
            from .pdfio import text_layer
            plain = text_layer(doc, n).plain() if doc is not None else ""
    except Exception:
        plain = ""
    st["layer"] = [(n, line) for line in plain.split("\n") if line.strip()]
    return st, adj, lost


class Records:
    """eval 이 결과 폴더를 읽는 창구 — 읽기만 한다. Work 와 달리 하위 폴더를 만들지 않고 깨진 기록·state.json 을 옮기지 않는다.
    못 읽는 기록(쓰다 만 JSON 등)은 '주의:' 한 줄을 남기고 None."""
    def __init__(self, work_dir):
        self.dir = Path(work_dir); self.readings_dir = self.dir / "readings"; self._bad: set[int] = set()

    def pages(self) -> set[int]:
        return {int(p.stem[1:]) for p in self.readings_dir.glob("p*.json") if p.stem[1:].isdigit()}

    def load_record(self, n: int) -> dict | None:
        p = self.readings_dir / f"p{n:03d}.json"
        try:
            rec = json.loads(p.read_text(encoding="utf-8-sig"))
            if isinstance(rec, dict) and isinstance(rec.get("merged"), dict):
                return rec
        except (OSError, ValueError):
            pass
        if n not in self._bad:
            self._bad.add(n); log(f"주의: 판독 기록 {p.name} 을(를) 읽지 못해(쓰다 만 JSON 등) 평가에서 뺍니다 — 기록은 그대로 둡니다.")
        return None

    def textlayer_path(self, n: int) -> Path:
        return self.dir / "textlayer" / f"p{n:03d}.json"

    def load_state(self) -> dict:
        try:
            st = json.loads((self.dir / "state.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return st if isinstance(st, dict) else {}


def build_unit(work, doc, name: str, pages: list[int], hw_paras: list[str], hw_first: int = 0, title: str | None = None, next_title: str | None = None) -> dict:
    """평가 단위 하나: 쪽 기록들의 단계별 글(title 이 있으면 trim_to_section 으로 구획만)과 기준 HWPX 문단(연어OCR 작업 표시 note_spans 제거).
    work 는 load_record(n)·textlayer_path(n) 이 있는 것(명령줄은 읽기 전용 Records). 못 읽는 쪽 기록은 건너뛴다."""
    stages: dict[str, list] = {s: [] for s in ("layer", "pass1", "pass2", "merged_default", "final")}; adj: list[dict] = []; dpi: dict[int, object] = {}
    longs = partial = lost = 0
    for n in pages:
        rec = work.load_record(n)
        if rec is None:
            continue
        st, a, k = _stage_paras(work, doc, n, rec, len(adj)); adj += a; lost += k
        for s in stages:
            stages[s] += st[s]
        longs += int((rec.get("merge_stats") or {}).get("long_segments", 0) or 0)
        partial += sum(1 for rr in (rec.get("pass2") or {}).values() if isinstance(rr, dict) and rr.get("columns") and _pass2_partial(rr)) if rec.get("pass1") else 0
        if rec.get("src_dpi") or rec.get("dpi"):
            dpi[n] = rec.get("src_dpi") or rec.get("dpi")
    unit = {"name": name, "pages": pages, "hw_paras": hw_paras, "adjudications": adj, "adj_unlinked": lost, "pass2_partial": partial, "long_segments": longs, "trim_notes": {}, "dpi": dpi}
    for s, paras in stages.items():
        if title:
            paras, note = trim_to_section(paras, title, next_title)
            if note:
                unit["trim_notes"][s] = note
        unit[s] = flatten(paras)
        if s == "final":
            unit["final_paras"] = paras
    hw = []
    for k, t in enumerate(hw_paras):
        for a, b in sorted(note_spans(t), reverse=True):
            t = t[:a] + t[b:]
        hw.append((hw_first + k, t))
    unit["ref"] = flatten(hw, expand=False)
    return unit


# ---------- 지표 ----------
def oracle_text(ref_f: str, fl: Flat, hyp_f: str, ops, zh_fold: bool) -> tuple[str, int]:
    """표시마다 후보 가운데 맞선 기준 구간(앞뒤 1자 포함)과의 편집 거리가 가장 작은 것을 고른다 — 첫 후보 정렬 기준의 탐욕법.
    첫 후보보다 나을 때만 바꾸므로 표시 하나하나는 좋아지지만 전체 거리가 꼭 단조롭지는 않다. 바꾼 글과 바꾼 수를 돌려준다."""
    repl = []
    for mk in fl.marks:
        s, e = mk["start"], mk["end"]; lo, hi = _ref_span(ops, max(0, s - 1), min(len(hyp_f), e + 1), len(ref_f)); rseg = ref_f[lo:hi]
        cost = lambda c: _lev(rseg, hyp_f[max(0, s - 1):s] + fold_text(c, zh_fold) + hyp_f[e:e + 1])
        base = cost(mk["cands"][0]); best = min(mk["cands"][1:], key=cost, default=None)
        if best is not None and cost(best) < base:
            repl.append((s, e, fold_text(best, zh_fold)))
    out = hyp_f
    for s, e, c in sorted(repl, reverse=True):
        out = out[:s] + c + out[e:]
    return out, len(repl)


def _by_page(fl: Flat, ops) -> dict[int, dict]:
    """쪽마다 (맞선 기준 글자 수, 틀린 수) — 쪽은 가설 글자의 쪽(끼울 자리는 그 다음 글자의 쪽)."""
    out: dict[int, dict] = {}
    for op, i, j in ops:
        d = out.setdefault(fl.page[min(j, len(fl.page) - 1)] if fl.page else 0, {"ref_chars": 0, "edits": 0}); d["ref_chars"] += op != "I"; d["edits"] += op != "="
    return out


def _diffs(ref_raw: str, fl: Flat, ops, near: set[int], wrong: dict[str, set[int]], seen: dict[str, int]) -> list[dict]:
    """병합(첫 후보) 정렬의 차이를 잇닿은 묶음으로. 단계 표지는 그 기준 자리가 글자층·1차·2차에서도 틀렸는지.
    id 는 차례가 아니라 내용(쪽·기준·가설·앞뒤 기준 글)에서 만든다 — 다시 읽기·3차·--pages 로 차이가 바뀌어도 판정(labels.json)이 엉뚱한 차이에 붙지 않게.
    같은 내용이 또 나오면 뒤에 -2, -3 …(seen 은 여러 단위에 걸친 셈)."""
    out: list[dict] = []; run: list = []

    def flush():
        if not run:
            return
        i_lo, i_hi = min(i for _, i, _ in run), max(i + (op != "I") for op, i, _ in run)
        j_lo, j_hi = min(j for _, _, j in run), max(j + (op != "D") for op, _, j in run)
        pos = set(range(i_lo, i_hi)) or {i_lo}; r, h = ref_raw[i_lo:i_hi], fl.text[j_lo:j_hi]
        pg = fl.page[min(j_lo, len(fl.page) - 1)] if fl.page else 0; pre, post = ref_raw[max(0, i_lo - CTX):i_lo], ref_raw[i_hi:i_hi + CTX]
        key = f"p{pg}-" + hashlib.sha1(json.dumps([pg, r, h, pre, post], ensure_ascii=False).encode("utf-8")).hexdigest()[:10]; k = seen[key] = seen.get(key, 0) + 1
        out.append({"id": key if k == 1 else f"{key}-{k}", "page": pg, "ref": r, "hyp": h, "pre": pre, "post": post, "kind": "바꿈" if r and h else ("빠짐" if r else "덧남"),
                    "chars": len(run), "marked": any(j in near for _, _, j in run), **{s: bool(pos & w) for s, w in wrong.items()}})
        run.clear()
    for op in ops:
        run.append(op) if op[0] != "=" else flush()
    flush()
    return out


def eval_unit(unit: dict, zh_fold: bool, seen: dict | None = None) -> dict:
    """단위 하나의 모든 지표. 위치 지표는 접은 글의 정렬로, 원글 CER 은 접기 전 글로 따로 센다. seen 은 차이 id 의 같은 내용 셈(_diffs, 단위들에 걸쳐 하나)."""
    from .adjudicate import LOWRES_NOTE
    ref_raw = unit["ref"].text; ref_f = fold_text(ref_raw, zh_fold); N = len(ref_f); al: dict[str, tuple] = {}; ast = {"alignments": 0, "inexact": 0, "approx_gaps": 0}
    res: dict = {"name": unit["name"], "pages": unit["pages"], "ref_chars": N, "stages": {}, "long_segments": unit["long_segments"], "trim_notes": unit["trim_notes"],
                 "adj_unlinked": unit["adj_unlinked"], "pass2_partial": unit["pass2_partial"], "align": ast}
    for s in ("layer", "pass1", "pass2", "merged_default", "final"):
        fl = unit[s]; hyp_f = fold_text(fl.text, zh_fold); ops = align(ref_f, hyp_f, ast); d = _dist(ops); al[s] = (fl, hyp_f, ops)
        draw = d if (hyp_f == fl.text and ref_f == ref_raw) else _dist(align(ref_raw, fl.text, ast))
        res["stages"][s] = {"hyp_chars": len(hyp_f), "edits": d, "cer": d / N if N else None, "edits_raw": draw, "cer_raw": draw / N if N else None}
    fl, hyp_f, ops = al["merged_default"]
    orc, n_rep = oracle_text(ref_f, fl, hyp_f, ops, zh_fold); d = _dist(align(ref_f, orc, ast))
    res["stages"]["merged_oracle"] = {"hyp_chars": len(orc), "edits": d, "cer": d / N if N else None, "edits_raw": None, "cer_raw": None, "replaced": n_rep}
    near: set[int] = set()
    for mk in fl.marks:
        near.update(range(mk["start"] - 1, mk["end"] + 1))
    outside = sum(1 for op, _, j in ops if op != "=" and j not in near); warranted = sum(1 for mk in fl.marks if any(op != "=" for op, _, _ in _in_span(ops, mk["start"], mk["end"])))
    res.update(marks=len(fl.marks), marks_warranted=warranted, mark_precision=warranted / len(fl.marks) if fl.marks else None, outside_errors=outside, outside_rate=outside / N if N else None)
    # 3차: 표시에 이어진 기록만(구획 밖으로 잘린 자리는 세지 않음). 저해상이라 묻지 않은 자리는 '물음'에서 뺀다
    adj = unit["adjudications"]; A: dict = {"asked": 0, "decided": 0, "correct": 0, "by": {}}
    for mk in fl.marks:
        a = adj[mk["entry"]] if mk["entry"] is not None else None
        if a is None or (a.get("evidence") or "").startswith(LOWRES_NOTE):
            continue
        A["asked"] += 1
        if a.get("choice"):
            ok = _choice_ok(ops, mk["start"], mk["end"], fold_text(WS_RE.sub("", a["choice"]), zh_fold), ref_f)
            A["decided"] += 1; A["correct"] += ok
            for key in ("model", "loc"):
                if a.get(key):
                    g = A["by"].setdefault(key, {}).setdefault(str(a[key]), {"decided": 0, "correct": 0}); g["decided"] += 1; g["correct"] += ok
    A["precision"] = A["correct"] / A["decided"] if A["decided"] else None; A["decision_rate"] = A["decided"] / A["asked"] if A["asked"] else None
    res["adjudication"] = A
    # 1차·2차 공통 오류·상관비 — 2차 조각이 실제로 읽은 기준 자리(0 ≤ i < N, 끝에 덧난 글자의 끼울 자리 N 은 기준 글자가 아님)에서만
    fl2, _, ops2 = al["pass2"]; w1, w2 = {i for i in _wrong_ref(al["pass1"][2]) if i < N}, {i for i in _wrong_ref(ops2) if i < N}
    cov = {i for op, i, j in ops2 if op != "I" and fl2.cov and fl2.cov[min(j, len(fl2.cov) - 1)]}
    both = w1 & w2 & cov; p1, p2 = len(w1 & cov), len(w2 & cov)
    res.update(pass2_coverage=len(cov) / N if N else None, common_errors=len(both), error_corr_ratio=_corr(len(both), p1, p2, len(cov)),
               corr_counts={"both": len(both), "p1": p1, "p2": p2, "cov": len(cov)})
    hw = "".join(c.key for c in hwpx_cells(unit["hw_paras"], 0)); oc = "".join(c.key for c in ocr_cells(unit["final_paras"]))
    res["collate_space"] = {"ref_chars": len(hw), "edits": _dist(align(hw, oc, ast))}       # 책갈피 교정이 보는 공간(부호 제외·간체 접기)
    res["by_page"] = {s: _by_page(al[s][0], al[s][2]) for s in ("merged_default", "final")}
    if unit["dpi"]:
        res["dpi"] = {str(n): str(v) for n, v in unit["dpi"].items()}
    res["diffs"] = _diffs(ref_raw, fl, ops, near, {s: _wrong_ref(al[s][2]) for s in ("layer", "pass1", "pass2")}, {} if seen is None else seen)
    return res


def sample_diffs(diffs: list[dict], k: int, seed: int = 0) -> tuple[list[dict], str]:
    """층화 표본: 층 = 종류 × 표시 안/밖 × 글자층도 틀림. 층마다 비례 배분(최소 1건), 고정 씨앗. 전체가 k 이하면 전부."""
    rule = f"층화 추출(종류×표시 안/밖×글자층 일치 여부, 층마다 비례 배분·최소 1건, 씨앗 {seed}) — 약 {k}건"
    if len(diffs) <= k:
        return list(diffs), rule
    strata: dict[tuple, list] = {}
    for d in diffs:
        strata.setdefault(_stratum(d), []).append(d)
    rng = random.Random(seed); picked: list[dict] = []; order = {id(d): n for n, d in enumerate(diffs)}
    for key in sorted(strata):
        picked += rng.sample(strata[key], min(len(strata[key]), max(1, round(k * len(strata[key]) / len(diffs)))))
    return sorted(picked, key=lambda d: order[id(d)]), rule                    # 글 차례(id 는 내용 해시라 차례가 없다)


def apply_labels(diffs: list[dict], labels: dict[str, str], N: int) -> dict:
    """사람 판정으로 머리 숫자를 보정한다: 층마다 판정된 표본 가운데 OCR 오류가 아닌 것(HWPX 틀림·인쇄 오자·이체자)의 글자 비율만큼 그 층의 오류를 뺀다.
    이번 차이에 없는 id 의 판정(차이가 바뀜)은 쓰지 않고 unmatched 로 센다."""
    share: dict[tuple, list[int]] = {}
    for d in diffs:
        if labels.get(d["id"]) in LABELS:
            t = share.setdefault(_stratum(d), [0, 0]); t[0] += d["chars"]; t[1] += d["chars"] * (labels[d["id"]] != LABELS[0])
    err = out = 0.0
    for d in diffs:
        t = share.get(_stratum(d)); keep = d["chars"] * (1 - t[1] / t[0]) if t and t[0] else d["chars"]
        err += keep; out += 0 if d["marked"] else keep
    ids = {d["id"] for d in diffs}
    return {"labelled": sum(1 for d in diffs if labels.get(d["id"]) in LABELS), "non_ocr_labelled": sum(1 for d in diffs if labels.get(d["id"]) in LABELS[1:]),
            "unmatched": sum(1 for k in labels if k not in ids), "cer_default_corrected": err / N if N else None, "outside_rate_corrected": out / N if N else None}


# ---------- 모으기·쓰기 ----------
def _overall(units: list[dict]) -> dict:
    N = sum(u["ref_chars"] for u in units); o: dict = {"ref_chars": N, "stages": {}}
    for s in STAGES:
        e = sum(u["stages"][s]["edits"] for u in units); er = [u["stages"][s]["edits_raw"] for u in units]
        o["stages"][s] = {"edits": e, "cer": e / N if N else None, "edits_raw": None if None in er else sum(er), "cer_raw": None if None in er or not N else sum(er) / N}
    for k in ("marks", "marks_warranted", "outside_errors", "common_errors", "long_segments", "adj_unlinked", "pass2_partial"):
        o[k] = sum(u[k] for u in units)
    o["mark_precision"] = o["marks_warranted"] / o["marks"] if o["marks"] else None; o["outside_rate"] = o["outside_errors"] / N if N else None
    A = {k: sum(u["adjudication"][k] for u in units) for k in ("asked", "decided", "correct")}
    A["precision"] = A["correct"] / A["decided"] if A["decided"] else None; A["decision_rate"] = A["decided"] / A["asked"] if A["asked"] else None
    o["adjudication"] = A; o["collate_space"] = {k: sum(u["collate_space"][k] for u in units) for k in ("ref_chars", "edits")}
    c = o["corr_counts"] = {k: sum(u["corr_counts"][k] for u in units) for k in ("both", "p1", "p2", "cov")}; o["error_corr_ratio"] = _corr(c["both"], c["p1"], c["p2"], c["cov"])  # 센 수를 모아서
    o["align"] = {k: sum(u["align"][k] for u in units) for k in ("alignments", "inexact", "approx_gaps")}
    dpi: dict[str, dict] = {}
    for u in units:
        for pg, v in (u.get("dpi") or {}).items():
            for s in ("merged_default", "final"):
                d = u["by_page"][s].get(int(pg)) or {"ref_chars": 0, "edits": 0}; g = dpi.setdefault(v, {}).setdefault(s, {"ref_chars": 0, "edits": 0})
                g["ref_chars"] += d["ref_chars"]; g["edits"] += d["edits"]
    if dpi:
        o["by_dpi"] = dpi
    return o


def write_md(path: Path, S: dict) -> None:
    o = S["overall"]; A = o["adjudication"]; ratio = "—" if o["error_corr_ratio"] is None else f"{o['error_corr_ratio']:.2f}"
    L = [f"# 평가 요약 — {S['hwpx']} ↔ {S['pdf']}", "",
         f"{S['at']} · 읽기 모드 {S['mode']} · 접기: {'부호 + 간체·이체자(compare_key)' if S['zh_fold'] else '부호만'} · 단위 {len(S['units'])}개 · 기준 {o['ref_chars']:,}자 · 쪽 {S['pages'][0]}~{S['pages'][-1]}",
         "", "## 단계별 CER(전체, 기준 글자 수 가중)", "", "| 단계 | 편집 수 | CER(접음) | CER(원글) |", "|---|---|---|---|"]
    L += [f"| {STAGE_KO[s]} | {o['stages'][s]['edits']} | {_pct(o['stages'][s]['cer'])} | {_pct(o['stages'][s]['cer_raw'])} |" for s in STAGES]
    L += ["", "## 표시·3차·상관", "", f"- 표시 {o['marks']}곳 · 표시 정밀도 {_pct(o['mark_precision'], 1)}(기준이 첫 후보와 다른 표시) · 표시 밖 오류율 {_pct(o['outside_rate'])}",
          f"- 3차: 물음 {A['asked']} · 확정 {A['decided']}(결정률 {_pct(A['decision_rate'], 1)}) · 확정 정밀도 {_pct(A['precision'], 1)}"
          + (f" · 표시에 잇지 못한 기록 {o['adj_unlinked']}건(그만큼 병합(첫 후보) 글·물음 수가 어림)" if o["adj_unlinked"] else ""),
          f"- 1차·2차 공통 오류 {o['common_errors']}자 · 오류 상관비 P(2차 틀림|1차 틀림)/P(2차 틀림) {ratio}(단위들의 센 수를 모아서) · 긴 구간 {o['long_segments']}"
          + (f" · 2차 조각 일부만 읽힌 영역 {o['pass2_partial']}곳은 1차 글로 셈" if o["pass2_partial"] else ""),
          f"- 대조 공간(부호 제외·간체 접기, 3차 뒤): 기준 {o['collate_space']['ref_chars']}자 · 차이 {o['collate_space']['edits']}자"]
    if o.get("by_dpi"):
        L += ["- dpi 별 CER(3차 뒤): " + " · ".join(f"{k}: {_pct(v['final']['edits'] / v['final']['ref_chars'] if v['final']['ref_chars'] else None)}" for k, v in o["by_dpi"].items())]
    if S.get("labels_applied"):
        la = S["labels_applied"]; L += [f"- 사람 판정 {la['labelled']}건(OCR 오류 아님 {la['non_ocr_labelled']}건) 반영 → 병합(첫 후보) CER {_pct(la['cer_default_corrected'])} · 표시 밖 오류율 {_pct(la['outside_rate_corrected'])}"
                                         + (f" · 이번 차이와 맞지 않는 판정 {la['unmatched']}건은 적용 안 함" if la.get("unmatched") else "")]
    for s in STAGES:
        L += ["", f"## {STAGE_KO[s]} — 단위별", "", "| 단위 | 쪽 | 기준 글자 | 편집 수 | CER(접음) | CER(원글) |", "|---|---|---|---|---|---|"]
        L += [f"| {u['name']} | {u['pages'][0]}~{u['pages'][-1]} | {u['ref_chars']} | {u['stages'][s]['edits']} | {_pct(u['stages'][s]['cer'])} | {_pct(u['stages'][s]['cer_raw'])} |" for u in S["units"]]
    untrimmed = ", ".join(f"{u['name']}/{s}" for u in S["units"] for s in u["trim_notes"])
    L += ["", "## 표본·판정", "", f"차이 전체 {S['diff_total']}건 가운데 {S['sample']['n']}건을 diffs.html 에 띄웠다({S['sample']['rule']}). "
          "판정은 labels.json({\"<id>\": \"판독 틀림|HWPX 틀림|인쇄 오자|이체자\"})에 적고 다시 돌리면 머리 숫자가 보정된다. id 는 차이의 내용(쪽·기준·가설·앞뒤 기준 글)에서 만들므로 다시 읽어 달라진 차이에는 붙지 않는다.",
          "", "## 유의할 점", "", "- 기준 HWPX 에도 오탈이 있다 — 차이가 모두 판독 오류는 아니며, 표본 판정으로만 그 몫을 가늠한다. 이체자·간체 접기(compare_key)는 사전에 든 글자만 접으므로"
          " 사전 밖 이체자는 오류로 센다(세로쓰기 모드는 부호만 접는다). 표본은 전체가 아니라 층화 추출이고, 구획 자르기(trim_to_section)가 제목을 못 찾은 단계는 쪽 전체를 넣어 CER 이 부풀 수 있다"
          + (f"(해당: {untrimmed})" if untrimmed else "") + "."
          + (f" 정렬 {o['align']['inexact']}개는 칸 수 상한을 넘어 닻 정렬(상한)로 셌다(difflib 로 어림한 구간 {o['align']['approx_gaps']}개) — 그 단위의 편집 수는 실제보다 클 수 있다." if o["align"]["inexact"] else ""),
          "", "## 방법", "", "- " + S["method"].replace("\n", "\n- ")]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def write_html(path: Path, S: dict, sample: list[dict], labels: dict[str, str], keep: dict[str, str] | None = None) -> None:
    """차이 표본 표. keep = 표본에 없지만 이번 차이와 맞는 판정 — 내려받는 labels.json 에 그대로 함께 담는다(표본이 바뀌어도 판정이 사라지지 않게)."""
    esc = html.escape; rows = []
    for d in sample:
        flags = " ".join(f"{k}{'○' if d[s] else '×'}" for k, s in (("층", "layer"), ("1차", "pass1"), ("2차", "pass2")))
        sel = f"<select data-id='{d['id']}'><option value=''>—</option>" + "".join(f"<option value='{l}'{' selected' if labels.get(d['id']) == l else ''}>{l}</option>" for l in LABELS) + "</select>"
        rows.append(f"<tr><td class=id>{d['id']}</td><td>{d['page']}</td><td class=ctx>{esc(d['pre'])}<b>{esc(d['ref']) or '∅'}</b>{esc(d['post'])}</td>"
                    f"<td class=ctx><b>{esc(d['hyp']) or '∅'}</b></td><td>{d['kind']}</td><td>{'표시 안' if d['marked'] else '표시 밖'}</td><td>{flags}</td><td>{sel}</td></tr>")
    css = ("body{font-family:'Noto Serif KR',serif;margin:0;background:#f4efe4;color:#231c18}header{padding:12px 20px;background:#3b1b14;color:#f1e7cf}h1{font-size:18px;margin:0}"
           "main{padding:16px 20px}table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}td,th{border:1px solid #d9d2c3;padding:4px 6px;vertical-align:top}"
           "td.ctx{font-size:16px;letter-spacing:.03em}td.ctx b{color:#b3261e}td.id{font-family:monospace;font-size:11px}button{margin:8px 0;padding:4px 10px}")
    js = ("const K=" + json.dumps(keep or {}, ensure_ascii=False).replace("</", "<\\/") + ";"
          "function dl(){const o=Object.assign({},K);document.querySelectorAll('select[data-id]').forEach(s=>{if(s.value)o[s.dataset.id]=s.value});"
          "const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(o,null,1)],{type:'application/json'}));a.download='labels.json';a.click()}")
    o = S["overall"]
    doc = (f"<!doctype html><html lang=ko><head><meta charset=utf-8><title>평가 차이 표본</title><style>{css}</style></head><body><header><h1>평가 — 차이 표본(사람 판정용)</h1>"
           f"<small>{esc(S['hwpx'])} ↔ {esc(S['pdf'])} · {S['at']} · 기준 {o['ref_chars']:,}자 · 병합(첫 후보) CER {_pct(o['stages']['merged_default']['cer'])}<br>"
           f"<b>표본이지 전체가 아님</b>: 차이 전체 {S['diff_total']}건(틀린 글자 {o['stages']['merged_default']['edits']}자) 가운데 {len(sample)}건 — {esc(S['sample']['rule'])}</small></header><main>"
           "<p>기준은 HWPX(굵은 글자가 차이 자리, ∅ 는 없음), 가설은 병합 판독(첫 후보). 단계 표지 ○ = 그 단계도 같은 자리에서 틀림. 판정을 고른 뒤 단추로 labels.json 을 받아 "
           "eval/ 에 두고 <code>eval --labels</code> 로 다시 돌리면 머리 숫자가 보정된다. id 는 차이 내용에서 만들어 판독이 바뀐 차이에는 붙지 않는다"
           + (f"(이번에 맞지 않은 판정 {S['labels_applied']['unmatched']}건은 적용하지 않았고 내려받기에서 빠진다)" if (S.get("labels_applied") or {}).get("unmatched") else "")
           + (f". 표본 밖 차이의 판정 {len(keep)}건은 내려받기에 그대로 담긴다" if keep else "") + ".</p><button onclick='dl()'>labels.json 내려받기</button>"
           f"<table><tr><th>id</th><th>쪽</th><th>기준(HWPX)</th><th>가설</th><th>종류</th><th>표시</th><th>층·1차·2차</th><th>판정</th></tr>{''.join(rows) or '<tr><td colspan=8>차이 없음</td></tr>'}</table>"
           f"<script>{js}</script></main></body></html>")
    path.write_text(doc, encoding="utf-8")


# ---------- 명령줄 ----------
def add_arguments(p) -> None:
    """eval 하위 명령의 선택지(pdf·--work·--pages·--dpi 는 common 이 이미 넣음)."""
    p.add_argument("--hwpx", required=True, help="기준으로 삼을, 사람이 입력한 원본 HWPX(교정본 <이름>_교정.hwpx 는 안 됨)")
    p.add_argument("--out", help="결과 폴더(기본 <work>/eval)")
    p.add_argument("--mode", choices=["auto", "vertical", "horizontal_zh"], default="auto", help="읽기 모드(auto=결과 폴더에 적힌 값·글자층 판정). horizontal_zh 면 간체·이체자 차이를 접는다")
    p.add_argument("--sample", type=int, default=200, help="diffs.html 에 띄울 차이 표본 수(층화 추출, 기본 200)")
    p.add_argument("--labels", help="사람 판정 파일(labels.json, {id: 판정}). 기본 <out>/labels.json 이 있으면 적용")
    p.add_argument("--heading-styles", help="책갈피처럼 쓸 HWPX 스타일 이름(쉼표로 여러 개). collate 와 같음")
    p.add_argument("--anchor-source", choices=["auto", "hwpx", "pdf"], default="auto", help="구획 기준(collate 와 같음)")


def plan_units(args, doc, work, book, have: set[int]) -> tuple[list[dict], list[str]]:
    """구획 기준이 있으면 쪽 기록이 다 있는 구획마다 한 단위, 기준이 없으면 요청 쪽 전체 ↔ HWPX 전체 한 단위. 건너뛴 구획 이유도 돌려준다."""
    from .collate.pdf_outline import page_range
    units: list[dict] = []; skipped: list[str] = []; anchors: list = []; pdf_anchors: list = []; matches: list = []
    if doc is not None:
        styles = [s for s in (getattr(args, "heading_styles", "") or "").split(",") if s.strip()] or None
        anchors, pdf_anchors, matches = index_both(doc, book, getattr(args, "anchor_source", "auto") or "auto", styles)
    for i, (a, m) in enumerate(zip(anchors, matches)):
        sec = book.section(anchors, i, flat=True); hw = book.section_texts(sec)
        if sec.n_paras <= 0 or not any(t.strip() for t in hw[1:]):
            continue                                                 # 제목뿐인 구획
        if not m.pdf:
            skipped.append(f"{a.name}(PDF 짝 없음)"); continue
        lo, hi = page_range(pdf_anchors, pdf_anchors.index(m.pdf), len(doc)); pages = list(range(lo, hi + 1))
        if not all(n in have for n in pages):
            skipped.append(f"{a.name}({lo}~{hi}쪽 기록 없음·범위 밖)"); continue
        units.append(build_unit(work, doc, a.name, pages, hw, sec.start, a.name, sec.next_anchor.name if sec.next_anchor else None))
    if not anchors and have:
        units.append(build_unit(work, doc, "전체", sorted(have), book.texts(), 0))
    return units, skipped


CORRECTED_NAME = re.compile(r"_교정(?:\.bak|\.tmp|_이전_\d+)*$")     # 교정본과 collate 가 만드는 그 백업·임시·옮겨 둔 사본(hwpx_edit.save_copy·run.set_aside)의 이름 줄기


def collate_traces(book) -> str:
    """책갈피 교정(collate --apply)이 고친 문서의 흔적 — 이름을 바꾼 교정본도 기준에서 거르려고(hwpx_edit 가 제 표시를 알아보는 셈과 같게):
    교정 범례 메모 · 교정 표지 달린 글자 모양(취소선 색 = 등급 글자 색, Editor._tag)을 쓰는 글 · 이 프로그램이 단 교정 메모(표지를 달기 전 판).
    일반 OCR 문서의 등급 색(표지 없음)·사람이 칠한 색·사람이 단 메모는 치지 않는다. 흔적이 없으면 ""."""
    from .grades import COLLATE_LEGEND, GRADE_STYLE
    from .hwpx_memo import _header
    from .collate.hwpx_edit import HP, HH, BLUE, RED, MEMO_HEADS, legend_head, _tag_of
    ours = {BLUE.upper(), RED.upper()} | {g["color"].upper() for g in GRADE_STYLE.values()}
    try:
        tagged = {el.get("id") for el in _header(book.doc).element.iter(f"{HH}charPr") if (c := (el.get("textColor") or "").upper()) in ours and _tag_of(el) == c} - {None, ""}
    except Exception:
        tagged = set()
    head = legend_head(COLLATE_LEGEND)
    def author(t):
        fb = next((a for a in t.iterancestors() if a.tag == f"{HP}fieldBegin"), None)
        sp = fb.find(f"{HP}parameters/{HP}stringParam[@name='Author']") if fb is not None else None
        return (sp.text or "") if sp is not None else ""
    for p in book.paragraphs:
        for el in p.element.iter(f"{HP}run", f"{HP}t"):
            if el.tag == f"{HP}run":
                if el.get("charPrIDRef") in tagged:
                    return "교정 표지 달린 글자"
            elif (x := el.text or "").startswith(head):
                return "교정 범례 메모"
            elif x.startswith(MEMO_HEADS) and author(el) == "YeonE_OCR":
                return "이 프로그램의 교정 메모"
    return ""


def cmd_eval(args) -> int:
    """명령줄 진입점(읽기 전용 — 결과 폴더의 기록·state.json 을 옮기거나 고치지 않고 하위 폴더도 만들지 않는다. 쓰는 것은 결과 폴더 eval/ 뿐).
    기준 HWPX·기록이 없거나 평가할 단위가 없으면 2, 끝나면 0."""
    from contextlib import ExitStack
    with ExitStack() as resources:
        return _cmd_eval(args, resources)


def _cmd_eval(args, resources) -> int:
    from .state import default_work_dir
    from .pdfio import open_pdf, parse_pages
    from .collate.sections import HwpxBook
    hw = getattr(args, "hwpx", None) or ""
    if CORRECTED_NAME.search(Path(hw).stem):
        log("오류: 교정본(<이름>_교정.hwpx 와 그 .bak·.tmp·_이전_N 사본)은 OCR 이 이미 고친 파일이라 기준으로 쓰면 평가가 순환합니다 — 원본 HWPX 를 주십시오."); return 2
    if not hw or not Path(hw).exists():
        log("오류: --hwpx 로 사람이 입력한 원본 HWPX 를 주십시오."); return 2
    work = Records(getattr(args, "work", None) or default_work_dir(args.pdf))
    try:
        doc = open_pdf(args.pdf)
        resources.callback(doc.close)
    except Exception as e:
        doc = None
        if not getattr(args, "pages", None):
            log(f"오류: PDF 를 열지 못했습니다({str(e)[:60]}). --pages 를 주면 PDF 없이 기록만으로 평가합니다."); return 2
        log(f"주의: PDF 를 열지 못해({str(e)[:60]}) 구획 없이 기록만으로 평가합니다.")
    have_all = work.pages()
    if not have_all:
        log(f"오류: 판독 기록이 없습니다 — {work.dir}/readings/ 가 비어 있습니다."); return 2
    have = {n for n in parse_pages(getattr(args, "pages", None), len(doc) if doc is not None else max(have_all)) if n in have_all and work.load_record(n) is not None}
    mode = getattr(args, "mode", None); st = work.load_state()
    if mode not in ("vertical", "horizontal_zh"):
        from .cli import detect_mode
        mode = st["mode"] if st.get("mode") in ("vertical", "horizontal_zh") else (detect_mode(doc)[0] if doc is not None else "vertical")
    zh_fold = mode == "horizontal_zh"; book = HwpxBook(hw); t0 = time.time()
    resources.callback(book.doc.close)
    if why := collate_traces(book):                                  # 이름을 바꾼 교정본(--out 으로 저장한 것 포함)
        log(f"오류: 교정본의 흔적({why})이 있는 HWPX 는 OCR 이 이미 고친 파일이라 기준으로 쓰면 평가가 순환합니다 — 원본 HWPX 를 주십시오: {Path(hw).name}"); return 2
    log(f"평가: 기준 {Path(hw).name}(문단 {len(book.paragraphs):,}개) · 기록 {len(have)}쪽 · 모드 {mode}({'간체·이체자 접음' if zh_fold else '부호만 접음'}) · 모델 호출 없음")
    from .cli import progress
    units, skipped = plan_units(args, doc, work, book, have); results = []; seen: dict[str, int] = {}
    for k, u in enumerate(units, 1):
        progress("eval", k - 1, len(units))
        if not u["ref"].text:
            skipped.append(f"{u['name']}(HWPX 글 없음)"); continue
        r = eval_unit(u, zh_fold, seen); results.append(r)
        log(f"  {u['name'][:24]} {u['pages'][0]}~{u['pages'][-1]}쪽: 기준 {r['ref_chars']}자 · CER 병합 {_pct(r['stages']['merged_default']['cer'])} → 3차 뒤 {_pct(r['stages']['final']['cer'])} · 표시 {r['marks']}")
    if not results:
        log("오류: 평가할 단위가 없습니다 — " + ("; ".join(skipped[:6]) if skipped else "요청한 쪽에 판독 기록이 없습니다.")); return 2
    if sum(u["ref_chars"] for u in results) > 1.5 * sum(u["stages"]["final"]["hyp_chars"] for u in results):
        log("주의: 기준 HWPX 글이 판독 글보다 1.5배 넘게 길어 빠짐이 과대평가됩니다 — 구획 짝이 맞는지, --pages 가 HWPX 범위와 맞는지 확인하십시오.")
    out_dir = Path(getattr(args, "out", None) or work.dir / "eval"); out_dir.mkdir(parents=True, exist_ok=True)
    diffs = [d for u in results for d in u["diffs"]]; sample, rule = sample_diffs(diffs, max(1, int(getattr(args, "sample", 200) or 200)))
    lab_path = Path(getattr(args, "labels", None) or out_dir / "labels.json"); labels: dict[str, str] = {}
    if lab_path.exists():
        try:
            labels = {str(k): str(v) for k, v in json.loads(lab_path.read_text(encoding="utf-8")).items()}
        except Exception as e:
            log(f"주의: 판정 파일을 읽지 못해 적용하지 않습니다({str(e)[:60]}): {lab_path}")
    S = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "pdf": Path(args.pdf).name, "hwpx": Path(hw).name, "work": str(work.dir), "mode": mode, "zh_fold": zh_fold,
         "pages": sorted(have), "skipped": skipped, "method": METHOD, "overall": _overall(results), "units": [{k: v for k, v in u.items() if k != "diffs"} for u in results],
         "diff_total": len(diffs), "sample": {"rule": rule, "n": len(sample), "ids": [d["id"] for d in sample]}}
    if labels:
        S["labels_applied"] = la = apply_labels(diffs, labels, S["overall"]["ref_chars"]); S["labels_file"] = str(lab_path)
        if la["unmatched"]:
            log(f"주의: 판정 {la['unmatched']}건은 이번 차이와 맞지 않아(다시 읽기 등으로 차이가 달라짐) 적용하지 않았습니다: {lab_path}")
    ids = {d["id"] for d in diffs}; shown = {d["id"] for d in sample}; keep = {k: v for k, v in labels.items() if k in ids and k not in shown and v in LABELS}
    (out_dir / "summary.json").write_text(json.dumps(S, ensure_ascii=False, indent=1), encoding="utf-8")
    write_md(out_dir / "summary.md", S); write_html(out_dir / "diffs.html", S, sample, labels, keep); o = S["overall"]; progress("eval", len(units), len(units))
    if o["align"]["inexact"]:
        log(f"주의: 정렬 {o['align']['inexact']}개는 칸 수 상한({CELLS:,})을 넘어 어림했습니다(편집 수가 실제보다 클 수 있음) — summary.json 의 align 참고.")
    log(f"평가 끝({time.time() - t0:.0f}초): 단위 {len(results)}개 · 기준 {o['ref_chars']:,}자 · CER " + " · ".join(f"{STAGE_KO[s]} {_pct(o['stages'][s]['cer'])}" for s in STAGES)
        + f" · 표시 밖 오류율 {_pct(o['outside_rate'])} · 표시 정밀도 {_pct(o['mark_precision'], 1)} · 3차 정밀도 {_pct(o['adjudication']['precision'], 1)}"
        + (f" · 건너뜀 {len(skipped)}" if skipped else "") + f"\n  결과: {out_dir / 'summary.md'} · diffs.html(표본 {len(sample)}/{len(diffs)}건)")
    return 0
