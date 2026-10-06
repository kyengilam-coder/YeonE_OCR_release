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


LONG_SEGMENT = 12   # 이 길이 이상의 구간이 한쪽에만 있으면 본문 안에 표시하지 않고 확인 항목으로만 남긴다


def _strip_map(cols: list[Column]) -> tuple[str, list[tuple[int, int]], dict[str, str]]:
    """열들의 글을 공백 없이 이어 붙인 문자열과, 그 글자마다 (열 번호, 열 안 위치) 대응표. 〔…〕 표시는 한 글자 토큰."""
    table: dict[str, str] = {}
    out, pos = [], []
    for ci, c in enumerate(cols):
        t = _tokenize(_norm(c.text), table)
        c.text = _detok(t, table)              # 정규화된 글로 바꿔 둔다(공백 하나로)
        for k, ch in enumerate(t):
            if ch.isspace():
                continue
            out.append(ch); pos.append((ci, k))
    return "".join(out), pos, table


def merge_passes(p1: PageReading, rereads: dict[str, RegionReread]) -> tuple[PageReading, dict]:
    """2차 판독이 있는 문자 영역마다 글 전체(열 경계 무시, 공백 무시)를 비교해 다른 자리를 표시한다.
    열 경계가 달라도 글이 같으면 표시하지 않는다. 열 번호는 영역 전체 기준으로 다시 매긴다."""
    merged = p1.model_copy(deep=True)
    stats = {"regions_compared": 0, "columns_compared": 0, "columns_differ": 0, "marks": 0,
             "long_segments": 0, "column_count_mismatch": []}
    for r in merged.regions:
        if r.kind not in TEXT_KINDS:
            continue
        cols = columns_in_reading_order(r)
        for k, c in enumerate(cols, 1):           # 영역 전체에서 오른쪽부터 1, 2, 3… 으로 통일
            c.col_from_right = k
        rr = rereads.get(r.id)
        if not rr or not rr.columns or not cols:
            continue
        stats["regions_compared"] += 1
        cols2 = sorted(rr.columns, key=lambda c: c.col_from_right)
        if len(cols) != len(cols2):
            stats["column_count_mismatch"].append((r.id, len(cols), len(cols2)))
        pairs = align_columns(cols, cols2)
        stats["columns_compared"] += sum(1 for i, j in pairs if i is not None and j is not None)
        a, pos, table = _strip_map(cols)
        b = "".join(ch for ch in _tokenize("".join(_norm(c.text) for c in cols2), table) if not ch.isspace())
        if a == b:
            continue
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        ops = [op for op in sm.get_opcodes() if op[0] != "equal"]
        touched_cols: set[int] = set()
        # 뒤에서부터 고쳐야 앞쪽 위치가 흔들리지 않는다
        edits: list[tuple[int, int, int, str]] = []      # (col, start, end, 대체 글)  — 열 안 위치 기준
        for op, i1, i2, j1, j2 in ops:
            sa, sb = a[i1:i2], b[j1:j2]
            ma, mb = any(ch in table for ch in sa), any(ch in table for ch in sb)
            sa_t, sb_t = _detok(sa, table), _detok(sb, table)
            ci, k = pos[i1] if i1 < len(pos) else (pos[-1][0], len(cols[pos[-1][0]].text))
            ctx_col = cols[ci].text
            if (len(sa_t) >= LONG_SEGMENT and not sb_t) or (len(sb_t) >= LONG_SEGMENT and not sa_t) or (len(sa_t) >= LONG_SEGMENT and len(sb_t) >= LONG_SEGMENT):
                stats["long_segments"] += 1
                who = "1차에만" if not sb_t else ("2차에만" if not sa_t else "1차·2차가 크게 다름")
                shown = sa_t if sa_t else sb_t
                merged.uncertain.append(Uncertain(region_id=r.id, col_from_right=ci + 1, position=_pos(k, len(ctx_col)),
                    context=f"긴 구간({len(shown)}자)이 {who}: 「{shown[:40]}{'…' if len(shown) > 40 else ''}」" + (f" ↔ 「{sb_t[:40]}」" if sa_t and sb_t else "") + " — 열 누락·열 경계를 원본에서 확인"))
                continue
            if ma or mb:
                seg = sa_t if ma else sb_t
            elif sa_t and sb_t:
                seg = f"〔{sa_t}/{sb_t}〕"
            else:
                seg = f"〔疑: {sa_t or sb_t}〕"
            # a 구간이 걸친 열 범위: 첫 글자의 열에 표시를 넣고, 다음 열로 넘어간 글자는 지운다
            span = [pos[i] for i in range(i1, i2)]
            if span:
                first_ci, first_k = span[0]
                last_in_first = max(kk for cc, kk in span if cc == first_ci)
                edits.append((first_ci, first_k, last_in_first + 1, seg))
                for cc in sorted({cc for cc, _ in span if cc != first_ci}):
                    ks = [kk for c2, kk in span if c2 == cc]
                    edits.append((cc, min(ks), max(ks) + 1, ""))
            else:                                   # 삽입: i1 위치 앞에 넣는다
                edits.append((ci, k, k, seg))
            touched_cols.add(first_ci if span else ci)
            stats["marks"] += 1
            ctx = _detok(a[max(0, i1 - 3):i1], table) + seg + _detok(a[i2:i2 + 3], table)
            merged.uncertain.append(Uncertain(region_id=r.id, col_from_right=(span[0][0] if span else ci) + 1,
                                              position=_pos(k, len(ctx_col)), context="「" + ctx + "」"))
        # 공백 제거 위치 → 원래 열 글 위치로 되돌려 적용
        for ci in sorted({e[0] for e in edits}):
            cols[ci].text = _apply_edits(cols[ci].text, [(e[1], e[2], e[3]) for e in edits if e[0] == ci], table)
        stats["columns_differ"] += len(touched_cols)
        for u in rr.uncertain:            # 2차가 따로 적은 불확실 항목도 보탠다
            u2 = u.model_copy(); u2.region_id = u2.region_id or r.id
            if all(not (x.region_id == u2.region_id and x.context == u2.context) for x in merged.uncertain):
                merged.uncertain.append(u2)
    return merged, stats


def _apply_edits(text: str, edits: list[tuple[int, int, str]], table: dict[str, str]) -> str:
    """열 글(공백 포함)에 (시작, 끝, 대체) 편집을 적용한다. 위치는 토큰화된(표시 1글자) 문자열 기준이며 공백 글자도 센다."""
    tok = list(_tokenize(text, table))
    for st, en, seg in sorted(edits, key=lambda e: -e[0]):
        tok[st:en] = list(seg)
    return _detok("".join(tok), table)


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
