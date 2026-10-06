"""1차(쪽 전체) 판독과 2차(영역 확대) 독립 판독의 비교·병합, 기존 텍스트 레이어와의 대조."""
from __future__ import annotations
import difflib, re
from .schema import PageReading, RegionReread, Region, Column, Uncertain, TEXT_KINDS, region_columns, region_text

WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return WS.sub(" ", s).strip()


def _pos(i: int, n: int) -> str:
    if n <= 0: return "위"
    r = i / n
    return "위" if r < 0.34 else ("중간" if r < 0.67 else "아래")


MARK_RE = re.compile(r"〔[^〔〕]*〕")
_PUA = 0xE000


def _tokenize(s: str, table: dict[str, str]) -> str:
    """〔…〕 표시 하나를 사용자 영역 글자 하나로 바꿔 diff 가 표시를 쪼개지 못하게 한다."""
    def rep(m):
        ch = chr(_PUA + len(table)); table[ch] = m.group(0); return ch
    return MARK_RE.sub(rep, s)


def _detok(s: str, table: dict[str, str]) -> str:
    for ch, mk in table.items():
        s = s.replace(ch, mk)
    return s


def diff_mark(a: str, b: str) -> tuple[str, list[tuple[int, str]]]:
    """두 독립 판독이 다른 자리에 〔a/b〕 표시를 넣은 글과, (위치, 문맥) 목록을 돌려준다.
    공백만 다른 곳은 무시한다. 한쪽에만 있는 글자는 〔疑: x〕 로 적는다. 어느 쪽이든 이미 불확실 표시를 한 자리는 그 표시를 살린다."""
    table: dict[str, str] = {}
    a, b = _tokenize(_norm(a), table), _tokenize(_norm(b), table)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out, marks = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        sa, sb = a[i1:i2], b[j1:j2]
        if op == "equal":
            out.append(sa); continue
        if sa.strip() == sb.strip():          # 공백만 다름
            out.append(sa); continue
        ma, mb = any(c in table for c in sa), any(c in table for c in sb)
        if ma or mb:                          # 이미 표시된 자리: 표시를 살린다(1차 우선)
            seg = sa if ma else sb
        elif sa.strip() and sb.strip():
            seg = f"〔{_detok(sa.strip(), table)}/{_detok(sb.strip(), table)}〕"
        else:
            seg = f"〔疑: {_detok((sa or sb).strip(), table)}〕"
        out.append(seg)
        ctx = _detok(a[max(0, i1 - 3):i1] + seg + a[i2:i2 + 3], table)
        marks.append((i1, "「" + ctx + "」"))
    return _detok("".join(out), table), marks


def columns_in_reading_order(r: Region) -> list[Column]:
    """문단 순서 → 문단 안 열 번호 순서. (모델이 문단마다 열 번호를 1부터 다시 세는 경우가 있어 번호로 전역 정렬하면 안 된다)"""
    out: list[Column] = []
    for p in r.paragraphs:
        out.extend(sorted(p.columns, key=lambda c: c.col_from_right))
    return out


def _sim(a: str, b: str) -> float:
    a, b = WS.sub("", a), WS.sub("", b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def align_columns(c1: list[Column], c2: list[Column], min_sim: float = 0.55) -> list[tuple[int | None, int | None]]:
    """두 판독의 열을 글 유사도로 짝짓는다(순서 보존, 빈칸 허용). 번호가 어긋나거나 열이 빠져도 같은 열끼리 비교하게 한다."""
    n, m = len(c1), len(c2)
    S = [[_sim(c1[i].text, c2[j].text) for j in range(m)] for i in range(n)]
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    bt = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best, move = dp[i - 1][j], "up"
            if dp[i][j - 1] > best:
                best, move = dp[i][j - 1], "left"
            gain = S[i - 1][j - 1] if S[i - 1][j - 1] >= min_sim else -0.01   # 닮지 않은 열은 짝짓지 않는다
            if dp[i - 1][j - 1] + gain > best:
                best, move = dp[i - 1][j - 1] + gain, "diag"
            dp[i][j], bt[i][j] = best, move
    pairs: list[tuple[int | None, int | None]] = []
    i, j = n, m
    while i > 0 and j > 0:
        mv = bt[i][j]
        if mv == "diag":
            pairs.append((i - 1, j - 1) if S[i - 1][j - 1] >= min_sim else (i - 1, None)); i -= 1; j -= 1
            if pairs[-1][1] is None:
                pairs.append((None, j))          # 짝 못 지은 2차 열도 남긴다
        elif mv == "up":
            pairs.append((i - 1, None)); i -= 1
        else:
            pairs.append((None, j - 1)); j -= 1
    while i > 0:
        pairs.append((i - 1, None)); i -= 1
    while j > 0:
        pairs.append((None, j - 1)); j -= 1
    return list(reversed(pairs))


def merge_passes(p1: PageReading, rereads: dict[str, RegionReread]) -> tuple[PageReading, dict]:
    """2차 판독이 있는 문자 영역마다 열을 글 유사도로 맞춘 뒤 비교해 다른 곳을 표시한다. 열 번호는 영역 전체 기준으로 다시 매긴다."""
    merged = p1.model_copy(deep=True)
    stats = {"regions_compared": 0, "columns_compared": 0, "columns_differ": 0, "marks": 0,
             "columns_only_pass1": 0, "columns_only_pass2": 0, "column_count_mismatch": []}
    for r in merged.regions:
        if r.kind not in TEXT_KINDS:
            continue
        cols = columns_in_reading_order(r)
        for k, c in enumerate(cols, 1):           # 영역 전체에서 오른쪽부터 1, 2, 3… 으로 통일
            c.col_from_right = k
        rr = rereads.get(r.id)
        if not rr or not rr.columns:
            continue
        stats["regions_compared"] += 1
        cols2 = sorted(rr.columns, key=lambda c: c.col_from_right)
        if len(cols) != len(cols2):
            stats["column_count_mismatch"].append((r.id, len(cols), len(cols2)))
        for i, j in align_columns(cols, cols2):
            if i is not None and j is not None:
                stats["columns_compared"] += 1
                c = cols[i]
                if _norm(c.text) == _norm(cols2[j].text):
                    continue
                stats["columns_differ"] += 1
                text, marks = diff_mark(c.text, cols2[j].text)
                c.text = text
                for pos, ctx in marks:
                    stats["marks"] += 1
                    merged.uncertain.append(Uncertain(region_id=r.id, col_from_right=c.col_from_right,
                                                      position=_pos(pos, len(_norm(c.text))), context=ctx))
            elif i is not None:
                stats["columns_only_pass1"] += 1
                merged.uncertain.append(Uncertain(region_id=r.id, col_from_right=cols[i].col_from_right, position="",
                    context=f"2차 판독에 대응하는 열이 없음 — 열 경계·순서를 원본에서 확인: 「{_norm(cols[i].text)[:24]}…」"))
            else:
                stats["columns_only_pass2"] += 1
                merged.uncertain.append(Uncertain(region_id=r.id, col_from_right=0, position="",
                    context=f"2차 판독에만 있는 열(1차 누락 가능) — 원본 확인: 「{_norm(cols2[j].text)[:24]}…」"))
        for u in rr.uncertain:            # 2차가 따로 적은 불확실 항목도 보탠다
            u2 = u.model_copy(); u2.region_id = u2.region_id or r.id
            if all(not (x.region_id == u2.region_id and x.context == u2.context) for x in merged.uncertain):
                merged.uncertain.append(u2)
    return merged, stats


def compare_textlayer(reading: PageReading, layer_plain: str, max_items: int = 60) -> dict:
    """판독문과 기존 텍스트 레이어를 공백 없이 이어 붙여 대조한다. 다른 곳은 검수 참고용이며 자동 반영하지 않는다."""
    body = "".join(region_text(r) for r in reading.regions if r.kind in TEXT_KINDS).replace("\n", "")
    a = WS.sub("", body); b = WS.sub("", layer_plain)
    if not a and not b:
        return {"ratio": 1.0, "items": [], "reading_chars": 0, "layer_chars": 0}
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    items = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal": continue
        items.append({"op": op, "reading": a[i1:i2], "layer": b[j1:j2], "ctx": a[max(0, i1 - 4):i2 + 4]})
        if len(items) >= max_items: break
    return {"ratio": round(sm.ratio(), 4), "items": items, "reading_chars": len(a), "layer_chars": len(b)}
