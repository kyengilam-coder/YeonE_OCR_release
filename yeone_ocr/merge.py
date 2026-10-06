"""1차(쪽 전체) 판독과 2차(영역 확대) 독립 판독의 비교·병합, 기존 텍스트 레이어와의 대조."""
from __future__ import annotations
import difflib, re
from .schema import PageReading, RegionReread, Region, Uncertain, TEXT_KINDS, region_columns, region_text

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


def merge_passes(p1: PageReading, rereads: dict[str, RegionReread]) -> tuple[PageReading, dict]:
    """2차 판독이 있는 문자 영역마다 열 단위로 비교해 다른 곳을 표시한다. 통계도 돌려준다."""
    merged = p1.model_copy(deep=True)
    stats = {"regions_compared": 0, "columns_compared": 0, "columns_differ": 0, "marks": 0, "column_count_mismatch": []}
    for r in merged.regions:
        if r.kind not in TEXT_KINDS or r.id not in rereads:
            continue
        rr = rereads[r.id]
        if not rr.columns:
            continue
        stats["regions_compared"] += 1
        second = {c.col_from_right: c.text for c in rr.columns}
        cols = region_columns(r)
        if len(cols) != len(rr.columns):
            stats["column_count_mismatch"].append((r.id, len(cols), len(rr.columns)))
            merged.uncertain.append(Uncertain(region_id=r.id, col_from_right=0, position="",
                context=f"열 개수 불일치: 1차 {len(cols)}열, 2차 {len(rr.columns)}열 → 열 경계·순서를 원본에서 확인"))
        for c in cols:
            if c.col_from_right not in second:
                continue
            stats["columns_compared"] += 1
            if _norm(c.text) == _norm(second[c.col_from_right]):
                continue
            stats["columns_differ"] += 1
            text, marks = diff_mark(c.text, second[c.col_from_right])
            c.text = text
            for i, ctx in marks:
                stats["marks"] += 1
                merged.uncertain.append(Uncertain(region_id=r.id, col_from_right=c.col_from_right,
                                                  position=_pos(i, len(_norm(c.text))), context=ctx))
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
