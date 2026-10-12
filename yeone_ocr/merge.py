"""1차(쪽 전체) 판독과 2차(영역 확대) 독립 판독의 비교·병합, 기존 텍스트 레이어와의 대조."""
from __future__ import annotations
import difflib, re, unicodedata
from .schema import PageReading, RegionReread, Region, Column, Uncertain, TEXT_KINDS, region_columns, region_text, duplicate_region_ids

WS = re.compile(r"\s+")
INVISIBLE = re.compile(r"[\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufe00-\ufe0f\ufeff]")   # 폭 없는 글자·이체자 선택자 등


# 자형이 다른 호환 한자(JIS 등 출처: 塚 U+FA10·諸 U+FA22·者 U+FA5B…와 보충 구역). NFC 로 접으면 이체자가 표준 글자로 바뀐다(지침 21 위반)
VARIANT_COMPAT = re.compile(r"([﨎-﫿\U0002f800-\U0002fa1f])")


def _is_variant_compat(o: int) -> bool:
    return 0xFA0E <= o <= 0xFAFF or 0x2F800 <= o <= 0x2FA1F


def _is_ideo(o: int) -> bool:
    return 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x3134F


def nfc_text(s: str) -> str:
    """저장용 NFC. 한국어 발음 중복 호환 한자(U+F900~FA0D, 자형이 같다: 樂 U+F914 → U+6A02)는 표준 글자로 접고,
    자형이 다른 호환 한자(U+FA0E~FAFF)는 그대로 둔다 — 비교할 때만 _fold 가 표준 글자로 본다. 호환 한자 사이의 글은 통째로 NFC(자모 결합은 그대로 된다)."""
    return "".join(p if i % 2 else unicodedata.normalize("NFC", p) for i, p in enumerate(VARIANT_COMPAT.split(s)))


def _norm(s: str) -> str:
    """공백을 하나로, 호환 한자(樂 U+F914 등)는 표준 글자(nfc_text)로. 같은 글자가 부호값만 달라 다른 글자로 비교되는 일을 막는다."""
    return WS.sub(" ", INVISIBLE.sub("", nfc_text(s))).strip()


FOLD_ZH = False        # 가로쓰기 한문(간체) 모드에서 켠다: 비교할 때만 간체·이체자를 접는다(저장되는 글은 1차 판독 그대로)
_PUNCT_FOLD = {ord(a): b for a, b in {"，": ",", "。": ".", "：": ":", "；": ";", "！": "!", "？": "?", "（": "(", "）": ")", "、": ",",
                                        "～": "~", "－": "-", "．": ".", "“": '"', "”": '"', "‘": "'", "’": "'", "ㆍ": "·", "・": "·", "•": "·",
                                        "［": "[", "］": "]", "｛": "{", "｝": "}", "〈": "<", "〉": ">", "《": "<", "》": ">", "「": "'", "」": "'", "『": '"', "』": '"'}.items()}


def _fold(s: str) -> str:
    """비교 전용 접기 — 글자 수를 바꾸지 않는다. 전각 부호는 반각으로(，↔, 같은 모델 표기 차이는 판독 차이가 아니다),
    저장 글에 남겨 둔 이체자 호환 한자(塚 U+FA10)는 표준 글자(U+585A)로, FOLD_ZH 면 한자를 간체·이체자 대표 글자로. 〔〕 표시 토큰(짝 없는 대리 부호값, _tokenize)은 그대로 둔다."""
    out = []
    for ch in s:
        o = ord(ch)
        if _TOK <= o < _TOK + _TOK_N:          # 표시 토큰. 판독 글의 사용자 영역 글자(U+E000~F8FF)는 토큰이 아니라 글자로 접는다(접어도 그대로다)
            out.append(ch); continue
        ch = unicodedata.normalize("NFC", ch) if _is_variant_compat(o) else ch.translate(_PUNCT_FOLD)
        if FOLD_ZH and _is_ideo(o):
            k = _ZH_KEY.get(ch)
            if k is None:
                from .collate import zh
                k = _ZH_KEY[ch] = zh.compare_key(ch)
            if len(k) == 1:
                ch = k
        out.append(ch)
    return "".join(out)


_ZH_KEY: dict[str, str] = {}      # 글자별 간체·이체자 대표 글자 캐시(열 짝짓기 _sim 이 열마다 접으므로)


def _pos(i: int, n: int) -> str:
    if n <= 0: return "위"
    r = i / n
    return "위" if r < 0.34 else ("중간" if r < 0.67 else "아래")


MARK_RE = re.compile(r"〔[^〔〕]*〕")
_TOK, _TOK_N = 0xD800, 0x800      # 표시 토큰: 짝 없는 대리 부호값 U+D800~DFFF. 글자가 아니고 UTF-8 로 쓸 수도 없어 판독 글에 나오지 않는다
_TOK_RE = re.compile("[\ud800-\udfff]")


def _tokenize(s: str, table: dict[str, str]) -> str:
    """〔…〕 표시 하나를 토큰 글자 하나로 바꿔 diff 가 표시를 쪼개지 못하게 한다. 글이 같은 표시는 같은 글자(양쪽에 같은 표시가 있으면 같은 자리로 맞춰진다).
    토큰은 짝 없는 대리 부호값이라 표를 함께 쓰는 어느 글의 글자와도 겹치지 않는다 — 0.6.7 까지는 U+E000~ 이라 옛 글꼴·OCR 이 사용자 영역에 넣은 한자가
    표시로 바뀌었다. 글에 섞인 짝 없는 대리 부호값(깨진 글, 저장도 못 한다)은 U+FFFD 로 바꾼다. 글자 수는 그대로다(위치 1:1). 이미 토큰으로 바꾼 글은 다시 넣지 않는다."""
    rev = {mk: ch for ch, mk in table.items()}
    def rep(m):
        ch = rev.get(m.group(0))
        if ch is None:
            if len(table) >= _TOK_N:
                raise ValueError(f"한 영역의 〔〕 표시가 {_TOK_N}가지를 넘어 비교할 수 없음")
            ch = chr(_TOK + len(table)); table[ch] = m.group(0); rev[m.group(0)] = ch
        return ch
    return MARK_RE.sub(rep, _TOK_RE.sub("\ufffd", s))


def _detok(s: str, table: dict[str, str]) -> str:
    """토큰을 표시로 되돌린다. 한 번에 바꾸므로 되돌린 표시 안 글자가 다시 바뀌지 않고, 이미 되돌린 글이 섞여 다시 들어와도 그대로다(토큰은 판독 글에 없다)."""
    return _TOK_RE.sub(lambda m: table.get(m.group(0), m.group(0)), s) if table else s


def _mark_content(mk: str) -> str:
    """표시의 알맹이(비교용): 〔疑: x〕→x, 〔a/b〕→a(1차), 그 밖의 표시(〔판독불명〕·작업 표시)는 빈 글."""
    return WS.sub("", strip_marks_for_compare(mk))


def _expand(s: str, table: dict[str, str]) -> tuple[str, list[int | None]]:
    """토큰을 알맹이 글자로 펼친 글과, 글자마다 주인 토큰의 s 안 위치(맨글자면 None). 알맹이 없는 표시는 토큰 글자 그대로 두어 어느 글자와도 맞지 않게 한다."""
    out: list[str] = []; own: list[int | None] = []
    for i, ch in enumerate(s):
        if ch in table:
            for c in _mark_content(table[ch]) or ch:
                out.append(c); own.append(i)
        else:
            out.append(ch); own.append(None)
    return "".join(out), own


def _runs(idx, text: str, own: list) -> list[list]:
    """주인이 같은 이웃 글자 묶음 [[주인, 글], …]."""
    rs: list[list] = []
    for i in idx:
        if rs and rs[-1][0] == own[i]:
            rs[-1][1] += text[i]
        else:
            rs.append([own[i], text[i]])
    return rs


def _shown(s: str, table: dict[str, str]) -> str:
    """설명에 적을 상대 글. 알맹이 없는 표시(〔판독불명〕 등)는 이름만 — 설명 속 〔〕 가 본문 표시로 읽히지 않게."""
    if len(s) == 1 and s in table:
        return table[s][1:-1]
    return "「" + "".join(f"({table[c][1:-1]})" if c in table else c for c in s) + "」"


def _merge_marked(sa: str, sb: str, table: dict[str, str]) -> tuple[list[str], list[tuple[int, str, bool]]]:
    """〔…〕 표시 토큰이 든 차이 구간의 병합. 표시는 그대로 두고(1차 우선) 그 알맹이 자리를 상대 글과 맞춘 뒤, 표시 밖 글자의 차이만 〔a/b〕·〔疑: x〕 로 적는다.
    (0.6.7 까지는 구간 전체를 1차 글로 덮어 '能入〔疑: 肝〕' ↔ '能人肝' 의 入/人 차이가 사라졌다.)
    돌려주는 것: (토큰 상태의 조각 목록 — 글자·토큰·새 표시가 조각 하나, [(표시한 조각 번호, 덧붙일 설명, 새 표시인가)]).
    1차 표시의 알맹이와 2차 글이 다르면 표시는 두고 2차 글을 설명에 남긴다. 1차 맨글자 자리에 2차 표시가 오면 2차 표시를 살리고 밀려난 1차 글을 설명에 남긴다(기존 동작).
    1차 표시와 한 자리에 걸친 2차 표시는 버리고 그 알맹이를 2차 글자로 본다. 표시가 여러 맞춤 조각에 걸치면 그 사이 글도 표시의 몫이고, 설명은 표시마다 한 번(상대 글 전체)."""
    xa, oa = _expand(sa, table); xb, ob = _expand(sb, table)
    ops: list[tuple[list[int], list[int], bool]] = []          # (1차 위치들, 2차 위치들, 같음) — 같은 글자는 한 글자씩
    for op, p1, p2, q1, q2 in difflib.SequenceMatcher(None, _fold(xa), _fold(xb), autojunk=False).get_opcodes():
        if op == "equal":
            ops.extend(([p], [q], True) for p, q in zip(range(p1, p2), range(q1, q2)))
        else:
            ops.append((list(range(p1, p2)), list(range(q1, q2)), False))
    span_a: dict[int, list[int]] = {}; span_b: dict[int, list[int]] = {}       # 표시 → [첫 조각, 끝 조각]
    for k, (A, B, _) in enumerate(ops):
        for p in A:
            if oa[p] is not None: span_a.setdefault(oa[p], [k, k])[1] = k
        for q in B:
            if ob[q] is not None: span_b.setdefault(ob[q], [k, k])[1] = k
    drop = {o for o, (f, l) in span_b.items() if any(f <= l2 and f2 <= l for f2, l2 in span_a.values())}
    ob = [None if o in drop else o for o in ob]                 # 버린 2차 표시의 알맹이는 2차 맨글자, 알맹이 없는 표시는 토큰 글자 그대로 자리표
    inside: dict[int, tuple[str, int]] = {}                     # 표시가 걸친 조각 사이의 조각 → 그 표시
    for side, spans in (("a", span_a), ("b", {o: s for o, s in span_b.items() if o not in drop})):
        for o, (f, l) in spans.items():
            inside.update((k, (side, o)) for k in range(f + 1, l))
    out: list[str] = []; marked: list[tuple[int, str, bool]] = []
    where: dict[tuple[str, int], int] = {}; other: dict[tuple[str, int], list[str]] = {}

    def tok(side: str, i: int, part: str):                     # 표시는 처음 나올 때 한 번 넣고, 맞춰진 상대 글은 모은다
        if (side, i) not in where:
            where[(side, i)] = len(out); out.append((sa if side == "a" else sb)[i])
        other.setdefault((side, i), []).append(part)

    def plain(x: str, y: str):                       # x: 1차 글, y: 2차 글 — 표시 밖 글자끼리의 차이(y 에 버린 2차 표시의 자리표가 섞일 수 있다)
        y0 = "".join(c for c in y if c not in table)
        x, y0 = x.strip(), y0.strip()
        if x or y0:
            out.append(f"〔{x}/{y0}〕" if x and y0 else f"〔疑: {x or y0}〕")
            marked.append((len(out) - 1, f" ↔ 2차 {_shown(y.strip(), table)}" if x and not y0 and y.strip() else "", True))

    def split(rs: list[list], rest: str):            # 상대 글자를 묶음 길이만큼 자리 순서로 나눠 붙인다(마지막 묶음이 나머지를 다 가진다)
        for n, (owner, text) in enumerate(rs):
            take, rest = (rest, "") if n == len(rs) - 1 else (rest[:len(text)], rest[len(text):])
            yield owner, text, take

    for k, (A, B, eq) in enumerate(ops):
        if k in inside:                              # 표시 안쪽: 1차 표시면 2차 글, 2차 표시면 밀려난 1차 글
            side, o = inside[k]
            tok(side, o, "".join(xb[q] for q in B) if side == "a" else "".join(xa[p] for p in A))
        elif eq:
            p, q = A[0], B[0]
            if oa[p] is not None:                    # 1차 표시: 알맹이가 2차와 같다
                tok("a", oa[p], xb[q])
            elif ob[q] is not None:                  # 1차 맨글자 자리에 2차 표시: 2차 표시를 살린다
                tok("b", ob[q], xa[p])
            else:
                out.append(xa[p])
        elif any(oa[p] is not None for p in A):      # 1차 표시가 든 조각: 2차 글자(버린 표시 포함)를 1차 묶음마다 나눠 붙인다
            for owner, text, take in split(_runs(A, xa, oa), "".join(xb[q] for q in B)):
                if owner is None: plain(text, take)
                else: tok("a", owner, take)
        elif any(ob[q] is not None for q in B):      # 2차 표시만 든 조각: 2차 표시는 살리고 밀려난 1차 글은 설명에 남긴다
            for owner, text, take in split(_runs(B, xb, ob), "".join(xa[p] for p in A)):
                if owner is None: plain(take, text)
                else: tok("b", owner, take)
        else:
            plain("".join(xa[p] for p in A), "".join(xb[q] for q in B))
    for (side, i), ix in where.items():
        part = WS.sub("", "".join(other[(side, i)]))
        if side == "a":
            if not part or _fold(part) != _fold(_mark_content(table[sa[i]]) or sa[i]):
                marked.append((ix, f" ↔ 2차 {_shown(part, table)}" if part else " ↔ 2차 없음", False))
        else:                                        # 1차 꼴 그대로 남긴다(접어서만 같아도 적는다)
            marked.append((ix, f" ← 1차 「{part}」" if part and part != _mark_content(table[sb[i]]) else "" if part else " ← 1차 없음", True))
    marked.sort(key=lambda m: m[0])
    return out, marked


def _context(pre: str, pieces: list[str], ix: int, post: str) -> str:
    """표시한 조각 앞뒤 세 단위(글자·토큰·표시)씩의 문맥."""
    return "".join((list(pre) + pieces[:ix])[-3:]) + pieces[ix] + "".join((pieces[ix + 1:] + list(post))[:3])


def diff_mark(a: str, b: str) -> tuple[str, list[tuple[int, str]]]:
    """두 독립 판독이 다른 자리에 〔a/b〕 표시를 넣은 글과, (위치, 문맥) 목록을 돌려준다.
    공백만 다른 곳은 무시한다. 한쪽에만 있는 글자는 〔疑: x〕 로 적는다. 어느 쪽이든 이미 불확실 표시를 한 자리는 그 표시를 살리고 그 곁의 글자 차이는 따로 표시한다."""
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
        if any(c in table for c in sa) or any(c in table for c in sb):      # 이미 표시된 자리: 표시는 살리고 표시 밖 글자만 비교
            pieces, marked = _merge_marked(sa, sb, table)
            out.append("".join(pieces))
            marks.extend((i1, _detok("「" + _context(a[max(0, i1 - 3):i1], pieces, ix, a[i2:i2 + 3]) + "」" + note, table)) for ix, note, _ in marked)
            continue
        if sa.strip() and sb.strip():
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
    a, b = _fold(WS.sub("", a)), _fold(WS.sub("", b))      # 부호·이체자 호환 한자 표기 차이는 닮음에 영향을 주지 않는다
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
LONG_SEG_NO_P2 = "2차 확대 판독이 이 구간을 읽지 못해 1차 판독만으로 들어감"   # 긴 구간 확인 항목 글 머리: 표시 없이 1차만 본문에 들어감
LONG_SEG_DIFFER = "1차·2차 판독이 크게 달라 1차를 따름"                          # (context.settled_segments 가 이 두 머리로 바탕에서 뺀다)
NOTATION_DIFF = "1차·2차 표기 차이(간체·번체·이체)"   # 접어서 비교하면 같은데 글자가 다른 자리(後↔后·塚 U+FA10↔U+585A): 글은 1차 그대로, 확인 항목 한 건으로만 알린다


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


def _notation_item(rid: str, a: str, b: str, idx: list[tuple[int, int]], pos: list[tuple[int, int]], cols: list[Column]) -> Uncertain:
    """접어서만 같은 자리(idx: (a 위치, b 위치))를 영역마다 확인 항목 한 건으로. 1차(저장되는 글)와 2차의 자형 표기가 다르다 — 글은 그대로, 표시 없음."""
    pairs = list(dict.fromkeys((a[i], b[j]) for i, j in idx))
    cp = lambda ch: f"{ch}(U+{ord(ch):04X})" if _is_variant_compat(ord(ch)) else ch      # 塚 U+FA10 은 화면에서 U+585A 와 안 갈린다
    ci, k = pos[idx[0][0]]
    return Uncertain(region_id=rid, col_from_right=ci + 1, position=_pos(k, len(cols[ci].text)),
                     context=f"{NOTATION_DIFF} {len(idx)}자: " + ", ".join(f"{cp(x)}→{cp(y)}" for x, y in pairs[:5]) + (" …" if len(pairs) > 5 else "") + " — 인쇄된 꼴 그대로인지 원본 확인")


def merge_passes(p1: PageReading, rereads: dict[str, RegionReread]) -> tuple[PageReading, dict]:
    """2차 판독이 있는 문자 영역마다 글 전체(열 경계 무시, 공백 무시)를 비교해 다른 자리를 표시한다.
    열 경계가 달라도 글이 같으면 표시하지 않는다. 열 번호는 영역 전체 기준으로 다시 매긴다."""
    merged = p1.model_copy(deep=True)
    stats = {"regions_compared": 0, "columns_compared": 0, "columns_differ": 0, "marks": 0,
             "long_segments": 0, "notation_diffs": 0, "column_count_mismatch": []}
    duplicate = duplicate_region_ids(merged)
    for rid in sorted(duplicate):
        merged.uncertain.append(Uncertain(region_id=rid, context="영역 id 겹침 — 2차 판독의 대상을 구분할 수 없어 병합하지 않음(원본 확인)"))

    def keep_uncertain(rid: str, rr: RegionReread | None) -> None:
        if rr is None:
            return
        for u in rr.uncertain:            # 본문이 같거나 비어 있어도 2차의 추가 확인 메모를 보존한다.
            u2 = u.model_copy(); u2.region_id = u2.region_id or rid
            if all(not (x.region_id == u2.region_id and x.context == u2.context) for x in merged.uncertain):
                merged.uncertain.append(u2)

    for r in merged.regions:
        if r.kind not in TEXT_KINDS or r.id in duplicate:
            continue
        cols = columns_in_reading_order(r)
        for k, c in enumerate(cols, 1):           # 영역 전체에서 오른쪽부터 1, 2, 3… 으로 통일
            c.col_from_right = k
        rr = rereads.get(r.id)
        if not rr or not rr.columns or not cols:
            keep_uncertain(r.id, rr)
            continue
        stats["regions_compared"] += 1
        cols2 = sorted(rr.columns, key=lambda c: c.col_from_right)
        if len(cols) != len(cols2):
            stats["column_count_mismatch"].append((r.id, len(cols), len(cols2)))
        pairs = align_columns(cols, cols2)
        stats["columns_compared"] += sum(1 for i, j in pairs if i is not None and j is not None)
        a, pos, table = _strip_map(cols)
        b = "".join(ch for ch in _tokenize("".join(_norm(c.text) for c in cols2), table) if not ch.isspace())
        fa, fb = _fold(a), _fold(b)                # 부호 표기·(간체 모드) 자형 차이는 접고 비교한다. 위치는 a·b 와 1:1
        sm = None if fa == fb else difflib.SequenceMatcher(None, fa, fb, autojunk=False)
        same = [(0, 0, len(a))] if sm is None else [(i1, j1, i2 - i1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op == "equal"]
        idx = [(i, j) for i0, j0, n in same for i, j in zip(range(i0, i0 + n), range(j0, j0 + n))
               if a[i] != b[j] and _is_ideo(ord(a[i])) and _is_ideo(ord(b[j]))]       # 접어서만 같은 한자(부호 차이는 뺀다) — 다른 자리의 갈림과 상관없이 영역마다 센다
        stats["notation_diffs"] += len(idx)
        notation = _notation_item(r.id, a, b, idx, pos, cols) if idx else None       # 열 글을 고치기 전 위치로
        if sm is None:
            if notation: merged.uncertain.append(notation)
            keep_uncertain(r.id, rr)
            continue
        ops = [op for op in sm.get_opcodes() if op[0] != "equal"]
        touched_cols: set[int] = set()
        # 뒤에서부터 고쳐야 앞쪽 위치가 흔들리지 않는다
        edits: list[tuple[int, int, int, str]] = []      # (col, start, end, 대체 글)  — 열 안 위치 기준
        for op, i1, i2, j1, j2 in ops:
            sa, sb = a[i1:i2], b[j1:j2]
            ma, mb = any(ch in table for ch in sa), any(ch in table for ch in sb)
            sa_t, sb_t = _detok(sa, table), _detok(sb, table)
            ci, k = pos[i1] if i1 < len(pos) else (pos[-1][0], len(cols[pos[-1][0]].text)) if pos else (0, 0)    # 1차 열이 모두 비었으면 첫 열 앞
            ctx_col = cols[ci].text
            if (len(sa_t) >= LONG_SEGMENT and not sb_t) or (len(sb_t) >= LONG_SEGMENT and not sa_t) or (len(sa_t) >= LONG_SEGMENT and len(sb_t) >= LONG_SEGMENT):
                stats["long_segments"] += 1
                who = (LONG_SEG_NO_P2 if not sb_t else ("1차 판독에 없고 2차에만 있어 본문에 넣지 않음" if not sa_t else LONG_SEG_DIFFER))
                shown = sa_t if sa_t else sb_t
                merged.uncertain.append(Uncertain(region_id=r.id, col_from_right=ci + 1, position=_pos(k, len(ctx_col)),
                    context=f"{who}({len(shown)}자): 「{shown[:40]}{'…' if len(shown) > 40 else ''}」" + (f" ↔ 2차 「{sb_t[:40]}」" if sa_t and sb_t else "") + " — 열이 빠졌거나 열 경계가 어긋났는지 원본 확인"))
                continue
            if ma or mb:                            # 표시가 든 구간: 표시는 그대로 두고 표시 밖 글자의 차이만 적는다(토큰 상태로 고친다)
                pieces, marked = _merge_marked(sa, sb, table)
                seg = "".join(pieces)
                if seg == sa and not marked:        # 2차가 1차 표시의 알맹이대로 읽었다: 달라진 것 없음
                    continue
            else:
                seg = f"〔{sa_t}/{sb_t}〕" if sa_t and sb_t else f"〔疑: {sa_t or sb_t}〕"
                pieces, marked = [seg], [(0, "", True)]
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
            for ix, note, new in marked:            # 표시마다 확인 항목 하나(표시 없이 설명만 남기는 것은 marks 에 세지 않는다)
                if new: stats["marks"] += 1
                ctx = _context(a[max(0, i1 - 3):i1], pieces, ix, a[i2:i2 + 3])
                merged.uncertain.append(Uncertain(region_id=r.id, col_from_right=(span[0][0] if span else ci) + 1,
                                                  position=_pos(k, len(ctx_col)), context=_detok("「" + ctx + "」" + note, table)))
        # 공백 제거 위치 → 원래 열 글 위치로 되돌려 적용
        for ci in sorted({e[0] for e in edits}):
            cols[ci].text = _apply_edits(cols[ci].text, [(e[1], e[2], e[3]) for e in edits if e[0] == ci], table)
        stats["columns_differ"] += len(touched_cols)
        if notation: merged.uncertain.append(notation)
        keep_uncertain(r.id, rr)
    return merged, stats


def _apply_edits(text: str, edits: list[tuple[int, int, str]], table: dict[str, str]) -> str:
    """열 글(공백 포함)에 (시작, 끝, 대체) 편집을 적용한다. 위치는 토큰화된(표시 1글자) 문자열 기준이며 공백 글자도 센다."""
    tok = list(_tokenize(text, table))
    for st, en, seg in sorted(edits, key=lambda e: -e[0]):
        tok[st:en] = list(seg)
    return _detok("".join(tok), table)


def strip_marks_for_compare(s: str) -> str:
    """〔a/b〕 → a(1차), 〔疑: x〕 → x, 그 밖의 표시(〔판독불명〕·작업 표시) → 빈 글. 글자층과 비교할 때 표시 글자가 차이로 잡히지 않게."""
    def rep(m):
        inner = m.group(0)[1:-1]
        if inner.startswith("疑:"):
            return inner[2:].strip()
        if "/" in inner and not inner.startswith(("→", "소주", "작업", "판독 메모", "도판", "이어짐", "원본")):
            return inner.split("/")[0]
        return ""
    return MARK_RE.sub(rep, s)


def compare_textlayer(reading: PageReading, layer_plain: str, max_items: int = 60) -> dict:
    """판독문과 기존 텍스트 레이어를 공백 없이 이어 붙여 대조한다. 다른 곳은 검수 참고용이며 자동 반영하지 않는다.
    전각·반각 부호(，↔,)와 (간체 모드) 자형 차이는 접고 비교한다 — 중의역대약론선 실측에서 글자층 차이의 60%가 부호 표기였다."""
    body = "".join(region_text(r) for r in reading.regions if r.kind in TEXT_KINDS).replace("\n", "")
    a = WS.sub("", strip_marks_for_compare(body)); b = WS.sub("", layer_plain or "")
    if not a and not b:
        return {"ratio": 1.0, "items": [], "reading_chars": 0, "layer_chars": 0}
    fa, fb = _fold(a), _fold(b)                     # 위치는 a·b 와 1:1 (접기는 글자 수를 바꾸지 않는다)
    sm = difflib.SequenceMatcher(None, fa, fb, autojunk=False)
    items = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal": continue
        items.append({"op": op, "reading": a[i1:i2], "layer": b[j1:j2], "ctx": a[max(0, i1 - 4):i2 + 4]})
        if len(items) >= max_items: break
    return {"ratio": round(sm.ratio(), 4), "items": items, "reading_chars": len(a), "layer_chars": len(b)}
