"""쪽별 판독 기록을 문서 블록(제목·문단·그림·표·확인 필요 목록)의 한 줄기로 엮는다. HWPX/DOCX 조립이 공통으로 쓴다."""
from __future__ import annotations
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from .marks import fold
from .schema import PageReading, TEXT_KINDS, TITLE_KINDS, IMAGE_KINDS, SKIP_KINDS, paragraph_text, bbox_frac


def clip_ranges(rs, lo: int, hi: int, *, points: str = "strict") -> list:
    """[lo, hi) 에 걸친 (시작, 끝, 종류) 만 lo 기준으로 잘라 옮긴다. 넓이 없는 자리(시작=끝)는 points="strict" 면 lo<자리<hi(짜임 가르기),
    "inclusive" 면 lo<=자리<=hi(굵기 틈). 거꾸로 된 범위(시작>끝)는 버린다."""
    if points not in ("strict", "inclusive"):
        raise ValueError(f"points 는 strict·inclusive 중 하나: {points!r}")
    out = []
    for a, e, k in rs:
        if a == e:
            if (lo < a < hi) if points == "strict" else (lo <= a <= hi):
                out.append((a - lo, a - lo, k))
        elif a < e and a < hi and e > lo:
            out.append((max(a, lo) - lo, min(e, hi) - lo, k))
    return out


def shift_ranges(rs, k: int) -> list:
    """(시작, 끝, 종류) 들을 k 만큼 민다."""
    return [(a + k, e + k, z) for a, e, z in rs]


def anchor_map(anchors, n: int) -> dict[int, list[str]]:
    """[(자리, 책갈피 이름)] → {자리(0~n 으로 자름): [이름…](차례대로)} — 작성기가 책갈피를 넣을 자리."""
    out: dict[int, list[str]] = {}
    for o, label in anchors:
        out.setdefault(min(max(0, o), n), []).append(label)
    return out


@dataclass
class Block:
    type: str                      # meta | heading | para | note | image | table | uncertain | report
    text: str = ""
    level: int = 0
    kind: str = ""
    path: str = ""                 # image
    frac_w: float = 0.0            # image: 원본 쪽 폭 대비 비율
    ratio: float = 1.0             # image: 높이/폭
    rows: list[list[str]] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    page: int = 0
    spans: list[tuple[int, int, str]] = field(default_factory=list)   # 글자 모양 덮어쓰기: (시작, 끝, 등급) — grades.GRADE_STYLE
    memos: list[tuple[int, str]] = field(default_factory=list)        # (글자 위치, 메모 글) — 고지 사항만
    role: str = ""                                                    # 내용별 문서의 쓰임(structure.ROLES) — 양식 스타일을 고르는 데 쓴다
    cstyles: list[tuple[int, int, str]] = field(default_factory=list) # 쓰임별 글자 범위: (시작, 끝, bold·source·bonjing·small) — structure 가 붙인다
    anchors: list[tuple[int, str]] = field(default_factory=list)       # 내용별 문서: 원본 쪽이 시작되는 글자 위치와 책갈피 이름

    # ---- 글자 자리를 가진 네 목록(spans·memos·cstyles·anchors)을 글과 함께 옮기는 셈은 여기 한곳에서 ----
    def cut(self, cut: int, rest: int | None = None) -> tuple["Block", "Block"]:
        """[0, cut) 와 [rest, 끝)(rest 기본 cut) 둘로 — 사이 글자(빈칸)는 버린다. spans·cstyles 는 clip_ranges(strict), memos·anchors 는 cut 앞이면 앞,
        cut 이후면 뒤 블록의 max(0, 자리 − rest). 다른 칸은 dataclasses.replace 로 둘 다 그대로."""
        rest = cut if rest is None else rest; n = len(self.text)
        head = replace(self, text=self.text[:cut], spans=clip_ranges(self.spans, 0, cut), cstyles=clip_ranges(self.cstyles, 0, cut),
                       memos=[(o, m) for o, m in self.memos if o < cut], anchors=[(o, a) for o, a in self.anchors if o < cut])
        tail = replace(self, text=self.text[rest:], spans=clip_ranges(self.spans, rest, n), cstyles=clip_ranges(self.cstyles, rest, n),
                       memos=[(max(0, o - rest), m) for o, m in self.memos if o >= cut], anchors=[(max(0, o - rest), a) for o, a in self.anchors if o >= cut])
        return head, tail

    def extend(self, text: str, *, spans=(), memos=(), cstyles=(), anchors=()) -> int:
        """끝에 text 를 잇고 text 기준의 범위·자리를 이은 자리만큼 밀어 붙인다(목록은 제자리에서 늘린다). 돌려주는 값: 이은 자리."""
        base = len(self.text); self.text += text
        self.spans.extend(shift_ranges(spans, base)); self.cstyles.extend(shift_ranges(cstyles, base))
        self.memos.extend((o + base, m) for o, m in memos); self.anchors.extend((o + base, a) for o, a in anchors)
        return base

    @classmethod
    def joined(cls, a: "Block", b: "Block") -> "Block":
        """a 의 글을 b 앞에 — 종류·쓰임 등은 b 의 것."""
        out = replace(b, text=a.text, spans=list(a.spans), memos=list(a.memos), cstyles=list(a.cstyles), anchors=list(a.anchors))
        out.extend(b.text, spans=b.spans, memos=b.memos, cstyles=b.cstyles, anchors=b.anchors)
        return out

    def remap(self, text: str, origins: list[int]) -> None:
        """글이 바뀐 뒤(문맥·부호 정리) 글을 text 로 바꾸고 범위·메모·책갈피를 새 글 자리로(origins[새 자리] = 옛 자리).
        범위는 같은 옛 글자에서 나온 새 글자를 모두 덮고, 메모는 그 글자의 첫 새 자리(없으면 글 안으로 자름), 책갈피는 그 글자(없으면 뒤 글자, 끝이면 끝)."""
        self.text = text
        mapped: dict[int, list[int]] = {}
        for new, source in enumerate(origins):
            mapped.setdefault(source, []).append(new)

        def cover(rs) -> list:
            out = []
            for begin, end, kind in rs:
                covered = [new for source in range(begin, end) for new in mapped.get(source, [])]
                if covered:
                    out.append((min(covered), max(covered) + 1, kind))
            return out
        self.spans = cover(self.spans)
        self.memos = [(mapped[o][0] if o in mapped else min(len(text) - 1, max(0, o)), m) for o, m in self.memos]
        self.cstyles = cover(self.cstyles)
        self.anchors = [(min((new for source, news in mapped.items() if source >= o for new in news), default=len(text)), label) for o, label in self.anchors]

    def anchor_map(self, n: int | None = None) -> dict[int, list[str]]:
        """{자리(0~n 으로 자름, n 기본 len(text)): [책갈피 이름…]}."""
        return anchor_map(self.anchors, len(self.text) if n is None else n)


LAYOUTS = ("page", "flow")   # page = 원본 쪽마다 기록 줄·쪽 나누기(검수용), flow = 내용별(제목·문단으로 잇고 원본 쪽은 책갈피로)


_VOLUME = re.compile(r"卷\s*第?\s*([一二三四五六七八九十百千〇零\d]+)")
# 제목에서 뜻을 가르는 토막: 권 번호, 품급(上品·中品·下品·之上·之下), 부 이름(木部·草部 — 部 앞 한 글자). 하나라도 다르면 다른 장이다.
_GRADE = re.compile(r"卷\s*第?\s*[一二三四五六七八九十百千〇零\d]+|第\s*[一二三四五六七八九十百千〇零\d]+|[上中下]品|之[上中下]|[上中下]$|[㐀-䶿一-鿿]部")   # 장을 가르는 표지: 권·第N·품급·之上下·끝의 上中下(…并治上/中/下)·部
RUNNING_TOP = 0.15        # 쪽 위 이 비율 안에서 시작하는 제목만 머리말 후보
RUNNING_SIMILAR = 0.6     # 앞서 남긴 쪽머리 제목과 이만큼 비슷하면 머리말(잘못 읽은 변형 포함)


def _title_key(text: str) -> str:
    """머리말 견주기용: 표시는 1차 글자로, 빈칸·부호·쪽수·점선은 뺀다."""
    return re.sub(r"[\s　·・.,，。:：;；…‥\-—_()\[\]（）『』「」《》〈〉\d]", "", fold(text, keep_other=True))


def is_running_head(text: str, kept: list[str]) -> bool:
    """쪽 맨 위 제목이 쪽마다 찍힌 머리말(書眉)을 제목으로 읽은 것인지: 앞서 남긴 쪽머리 제목과 비슷하면 그렇다.
    권 번호·품급·부 이름(_GRADE)이 하나라도 다르면 새 장이다(木部上品 → 木部中品 은 비슷해 보여도 다른 장). 처음 나온 장 제목은 남는다."""
    from difflib import SequenceMatcher
    key = _title_key(text)
    if len(key) < 4:
        return False
    marks = _GRADE.findall(key)
    for old in kept:
        if _GRADE.findall(old) != marks:
            continue
        if SequenceMatcher(None, key, old).ratio() >= RUNNING_SIMILAR:
            return True
    return False


def anchor_label(rec: dict, whole_offset: int | None) -> str:
    """내용별 문서의 원본 쪽 책갈피 이름: 'PDF 6쪽 · 인쇄 3쪽'(전체 PDF 쪽수를 알면 그것도)."""
    r = rec["merged"]
    parts = [f"PDF {rec['pdf_page']}쪽"]
    if whole_offset is not None:
        parts.insert(0, f"전체 PDF {rec['pdf_page'] + whole_offset}쪽")
    if r.get("print_page"):
        parts.append(f"인쇄 {r['print_page']}쪽")
    return " · ".join(parts)


def meta_line(rec: dict, whole_offset: int | None) -> str:
    r = rec["merged"]
    parts = [Path(rec["file"]).name]
    if whole_offset is not None:
        parts.append(f"전체 PDF {rec['pdf_page'] + whole_offset}쪽")
    parts.append(f"PDF {rec['pdf_page']}쪽")
    parts.append(f"인쇄 {r['print_page']}쪽" if r.get("print_page") else "인쇄 쪽수 확인 안 됨")
    return "[" + " / ".join(parts) + "]"


CHECK_LIST_MODES = ("none", "extra", "all")   # 쪽 끝 확인 목록: 없음(기본) | 본문에 표시 못 한 것만 | 전부. 전체 목록은 검수 화면에
CHECK_HEADER = {"extra": "[확인 필요 — 본문에 표시하지 못한 항목]", "all": "[판독 확인 필요]"}


def page_text_of(pr: PageReading) -> str:
    """쪽의 본문·제목·표 글을 한 줄로. 확인 항목이 본문에 이미 표시돼 있는지 볼 때 쓴다."""
    parts = []
    for reg in pr.regions:
        if reg.kind in SKIP_KINDS:
            continue
        parts.append(reg.title_text)
        parts.extend(paragraph_text(p) for p in reg.paragraphs)
        parts.extend(c for r in reg.table_rows for c in r.cells)
    return "".join(parts)


def assemble_blocks(records: list[dict], join_pages: bool = True, whole_offset: int | None = None,
                    check_list: str = "none", grades: bool = True, context: dict | None = None,
                    memo_mode: str = "notice", legend: bool = True, stats: dict | None = None,
                    layout: str = "page", bold: dict | None = None) -> list[Block]:
    """grades=True 면 글자마다 확인 등급(초록·파랑·빨강 안 문맥 밑줄)과 고지 메모를 블록에 단다(grades.page_grades).
    context 는 {쪽: 문맥 추정 기록}(context.analyze). 글 자체는 바꾸지 않는다.
    layout="flow"(내용별): 쪽마다의 기록 줄을 넣지 않고, 그 쪽이 시작되는 글자 자리에 책갈피 이름(anchors)과 쪽 고지 메모를 단다.
    앞 영역·앞 쪽에서 이어지는 문단(cont)은 쪽 안에서도 잇고 잇는 자리에 〔→PDF n쪽〕 표시 대신 책갈피를 둔다. 소주는 〔소주〕 머리 대신 쓰임으로.
    bold 는 {(쪽, 영역 id, 문단 번호): [(시작, 끝, 종류)]}(structure.measure_bold) — 굵은 글씨 범위("bold")·약재명 뒤 틈("gap")을 cstyles 로 옮긴다."""
    if check_list not in CHECK_LIST_MODES:
        raise ValueError(f"check_list 는 {CHECK_LIST_MODES} 중 하나: {check_list!r}")
    if layout not in LAYOUTS:
        raise ValueError(f"layout 은 {LAYOUTS} 중 하나: {layout!r}")
    flow = layout == "flow"
    from .grades import page_grades, LEGEND, classify_uncertain, page_body, decided_keys, page_ranges
    blocks: list[Block] = []
    legend_block: Block | None = None
    no_pass2_pages: list[int] = []
    pending: tuple[str, list[str]] | None = None    # 내용별: 아직 글자 자리를 못 찾은 이 쪽의 책갈피(이름, 메모들)
    top_titles: list[str] = []                      # 내용별: 쪽 맨 위에서 남긴 제목(최근 셋) — 쪽마다 찍힌 머리말을 가린다
    vol_titles: list[str] = []                      # 권 이름(卷)이 든 제목 가운데 남긴 것(최근 셋) — 장 안의 소제목에 밀려나지 않게 따로
    prev_header = ""                                # 앞 쪽의 머리말 칸(판독이 적은 것) — 이 쪽 제목이 그것과 같으면 되풀이된 머리말
    if stats is not None and flow:
        stats.setdefault("running_heads", 0); stats.setdefault("running_head_texts", [])
    pending_list: list[tuple[str, list[str]]] = []  # 글 없는 쪽(그림만 등)이 이어질 때 앞 쪽들의 것

    def place(b: Block, offset: int) -> None:
        """기다리던 쪽 책갈피·메모를 블록 b 의 offset 글자 자리에 단다(내용별)."""
        nonlocal pending
        for label, msgs in pending_list + ([pending] if pending else []):
            b.anchors.append((offset, label))
            for m in msgs:
                b.memos.append((min(offset, max(0, len(b.text) - 1)), m))
        pending_list.clear(); pending = None

    def bold_of(n: int, reg_id: str, pi: int, raw: str, t: str, shift: int) -> list:
        """문단 글(raw) 기준 굵기 범위를 잘라 낸 글(t) 기준으로(앞 공백 lead 를 빼고 shift 를 더함). 틈(넓이 없는 자리)은 글 끝 자리까지 받는다."""
        lead = len(raw) - len(raw.lstrip())
        return shift_ranges(clip_ranges((bold or {}).get((n, reg_id, pi), []), lead, lead + len(t), points="inclusive"), shift)
    for rec in records:
        pr = PageReading.model_validate(rec["merged"])
        n = pr.pdf_page
        pg = page_grades(rec, (context or {}).get(n), memo_mode) if grades else None
        meta = Block(type="meta", text=meta_line(rec, whole_offset), page=n)
        if grades and legend and legend_block is None:
            legend_block = meta
        if pg is not None:
            if pg.page_notices:
                meta.memos.append((max(0, len(meta.text) - 1), " / ".join(pg.page_notices)))   # 쪽 고지는 쪽 기록 줄 끝 글자에 하나로
            if pg.no_pass2:
                no_pass2_pages.append(n)
            if stats is not None:
                for k, v in pg.stats.items():
                    stats[k] = stats.get(k, 0) + v
        if flow:                                    # 내용별: 기록 줄 대신 이 쪽의 첫 글자 자리에 책갈피(와 쪽 고지 메모)를
            if pending is not None:
                pending_list.append(pending)
            pending = (anchor_label(rec, whole_offset), [m for _, m in meta.memos])
        else:
            blocks.append(meta)

        def grade_of(reg_id: str, pi: int, raw: str, t: str, shift: int):
            """문단 글(raw) 기준 등급·메모를 잘라 낸 글(t) 기준으로 옮긴다(앞 공백 lead 를 빼고 shift 를 더함)."""
            if pg is None:
                return [], []
            lead = len(raw) - len(raw.lstrip())
            sp = [(a - lead + shift, b - lead + shift, k) for a, b, k in pg.spans.get((reg_id, pi), []) if lead <= a and b <= lead + len(t)]
            me = [(o - lead + shift, m) for o, m in pg.memos.get((reg_id, pi), []) if lead <= o < lead + len(t)]
            return sp, me
        if rec.get("reader") == "mock":
            blocks.append(Block(type="note", text="〔작업 메모〕 아래 본문은 기존 PDF 텍스트 레이어를 옮긴 것으로 원본 대조 전 상태입니다.", page=n))
        first_text_done = False
        tops = [bbox_frac(r.bbox)[1] for r in pr.regions if r.kind not in SKIP_KINDS and (r.kind in TITLE_KINDS or r.kind in TEXT_KINDS)]
        page_top = min(tops) if tops else 0.0       # 이 쪽에서 가장 위 영역(판독 영역 순서가 위→아래가 아닐 수 있어 좌표로)
        for reg in pr.regions:
            if reg.kind in SKIP_KINDS:
                continue
            if reg.kind in TITLE_KINDS:
                lvl = reg.title_level if reg.title_level in (1, 2, 3) else (2 if reg.kind == "제목" else 3)
                hb = Block(type="heading", text=reg.title_text.strip(), level=lvl, kind=reg.kind, page=n)
                if flow and not hb.text:
                    continue                                    # 글 없는 제목 영역은 문단을 만들지 않는다(책갈피는 다음 글로)
                if flow and reg.kind == "제목":
                    y0 = bbox_frac(reg.bbox)[1]
                    key = _title_key(hb.text); vol = "卷" in key
                    at_top = y0 < RUNNING_TOP and y0 <= page_top + 0.02          # 쪽에서 가장 위 영역
                    same_as_header = bool(prev_header) and is_running_head(hb.text, [prev_header])   # 앞 쪽 머리말과 같음(이 쪽 머리말과 같은 것은 새 장일 수 있다)
                    if same_as_header or ((at_top or vol) and is_running_head(hb.text, vol_titles + top_titles)):
                        if stats is not None:                   # 쪽마다 찍힌 머리말을 판독이 제목으로 읽은 것 — 내용별에서는 글을 끊지 않게 뺀다
                            stats["running_heads"] = stats.get("running_heads", 0) + 1
                            stats.setdefault("running_head_texts", []).append((n, hb.text))
                        continue
                    if vol:
                        vol_titles[:] = (vol_titles + [key])[-3:]
                    elif at_top:
                        top_titles[:] = (top_titles + [key])[-3:]
                if flow:
                    place(hb, 0)
                blocks.append(hb)
            elif reg.kind in TEXT_KINDS:
                for i, p in enumerate(reg.paragraphs):
                    raw = paragraph_text(p)
                    t = raw.strip()
                    if not t:
                        continue
                    prefix = "〔소주〕 " if reg.kind == "소주" and not flow else ""
                    sp, me = grade_of(reg.id, i, raw, t, len(prefix))
                    bd = bold_of(n, reg.id, i, raw, t, len(prefix))
                    t = prefix + t
                    joinable = (i == 0 and not first_text_done) if not flow else True    # 내용별은 쪽 안의 영역 사이도 잇는다
                    if join_pages and joinable and p.continues_from_previous:
                        if flow:                                # 바로 앞 글 블록(쪽 끝 확인 목록은 건너뜀)에, 소주는 소주끼리·본문은 본문끼리만 잇는다
                            last = next((b for b in reversed(blocks) if b.type != "uncertain"), None)
                            prev = last if last is not None and last.type == "para" and (last.kind == "소주") == (reg.kind == "소주") else None
                        else:
                            prev = next((b for b in reversed(blocks) if b.type == "para"), None)
                        if prev is not None:
                            prev.extend("" if flow else f"〔→PDF {n}쪽" + (f"/인쇄 {pr.print_page}쪽" if pr.print_page else "") + "〕")
                            base = prev.extend(t, spans=sp, memos=me, cstyles=bd)
                            if flow:
                                place(prev, base)
                            first_text_done = True
                            continue
                    pb = Block(type="para", text=t, kind=reg.kind, page=n, spans=sp, memos=me, cstyles=bd)
                    if flow:
                        place(pb, 0)
                    blocks.append(pb)
                    first_text_done = True
            elif reg.kind in IMAGE_KINDS:
                info = (rec.get("crops") or {}).get(reg.id)
                if info and info.get("path"):
                    blocks.append(Block(type="image", path=info["path"], kind=reg.kind, page=n,
                                        frac_w=info.get("frac_w", 0.5), ratio=info.get("ratio", 1.0)))
                else:
                    blocks.append(Block(type="note", text=f"〔도판 미삽입: {reg.kind} {reg.id} — 잘라낸 이미지가 없음〕", page=n))
                if reg.note.strip():
                    blocks.append(Block(type="note", text=f"〔판독 메모〕 {reg.note.strip()}", page=n))
            elif reg.kind == "단순표":
                rows = [list(r.cells) for r in reg.table_rows if r.cells]
                if rows:
                    w = max(len(r) for r in rows)
                    rows = [r + [""] * (w - len(r)) for r in rows]
                    blocks.append(Block(type="table", rows=rows, page=n))
            elif reg.kind == "기타":
                for p in reg.paragraphs:
                    t = paragraph_text(p).strip()
                    if t:
                        ob = Block(type="para", text=t, kind="기타", page=n)
                        if flow:
                            place(ob, 0)
                        blocks.append(ob)
            else:
                raise ValueError(f"지원하지 않는 영역 종류 {reg.kind!r} ({reg.id}) — 본문 누락을 막기 위해 문서 조립을 중단함")
        items = []
        body, dec = (page_body(pr), decided_keys(rec)) if check_list == "extra" else ("", set())
        for u in pr.uncertain:
            if check_list == "extra" and classify_uncertain(u, body, dec) != "notice":
                continue                           # 본문에 〔a/b〕 로 보이거나 3차가 정한 자리 — 목록에 되풀이하지 않음
            where = []
            if u.region_id: where.append(u.region_id)
            if u.col_from_right: where.append(f"오른쪽에서 {u.col_from_right}번째 열")
            if u.position: where.append(f"{u.position} 구간")
            items.append((", ".join(where) + ": " if where else "") + u.context)
        for iss in rec.get("verify") or []:
            if iss["level"] == "error":
                items.append(f"구조 검사: {iss['rule']} ({iss['where']}) — {iss['msg']}")
        if check_list != "none" and items:
            blocks.append(Block(type="uncertain", items=items, page=n, text=CHECK_HEADER[check_list]))
        if pr.running_header.strip():
            prev_header = _title_key(pr.running_header)    # 다음 쪽의 되풀이 머리말 판정에
    if flow and (pending or pending_list):         # 끝 쪽들에 글이 없으면 마지막 글 끝에
        last = next((b for b in reversed(blocks) if b.type in ("para", "heading") and b.text), None)
        if last is not None:
            place(last, len(last.text))
    if legend_block is not None:                    # 범례: 이 파일 첫 쪽 기록 줄(내용별은 첫 글)의 첫 글자에 메모 하나
        extra = f" · 2차 판독 없이 1차만 들어간 쪽: {page_ranges(no_pass2_pages)}" if no_pass2_pages else ""
        target = legend_block if not flow else next((b for b in blocks if b.type in ("para", "heading") and b.text), None)
        if target is not None:
            target.memos.insert(0, (0, LEGEND + extra))
    return blocks
