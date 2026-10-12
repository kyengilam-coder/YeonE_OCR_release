"""쪽별 판독 기록을 문서 블록(제목·문단·그림·표·확인 필요 목록)의 한 줄기로 엮는다. HWPX/DOCX 조립이 공통으로 쓴다."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from .schema import PageReading, Region, TEXT_KINDS, TITLE_KINDS, IMAGE_KINDS, SKIP_KINDS, paragraph_text


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


def meta_line(rec: dict, whole_offset: int | None) -> str:
    r = rec["merged"]
    parts = [Path(rec["file"]).name]
    if whole_offset is not None:
        parts.append(f"전체 PDF {rec['pdf_page'] + whole_offset}쪽")
    parts.append(f"PDF {rec['pdf_page']}쪽")
    parts.append(f"인쇄 {r['print_page']}쪽" if r.get("print_page") else "인쇄 쪽수 확인 안 됨")
    return "[" + " / ".join(parts) + "]"


MARK_RE = re.compile(r"〔[^〔〕]*〕")
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
                    memo_mode: str = "notice", legend: bool = True, stats: dict | None = None) -> list[Block]:
    """grades=True 면 글자마다 확인 등급(초록·파랑·빨강 안 문맥 밑줄)과 고지 메모를 블록에 단다(grades.page_grades).
    context 는 {쪽: 문맥 추정 기록}(context.analyze). 글 자체는 바꾸지 않는다."""
    if check_list not in CHECK_LIST_MODES:
        raise ValueError(f"check_list 는 {CHECK_LIST_MODES} 중 하나: {check_list!r}")
    from .grades import page_grades, LEGEND, classify_uncertain, page_body, decided_keys, page_ranges
    blocks: list[Block] = []
    legend_block: Block | None = None
    no_pass2_pages: list[int] = []
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
        for reg in pr.regions:
            if reg.kind in SKIP_KINDS:
                continue
            if reg.kind in TITLE_KINDS:
                lvl = reg.title_level if reg.title_level in (1, 2, 3) else (2 if reg.kind == "제목" else 3)
                blocks.append(Block(type="heading", text=reg.title_text.strip(), level=lvl, kind=reg.kind, page=n))
            elif reg.kind in TEXT_KINDS:
                for i, p in enumerate(reg.paragraphs):
                    raw = paragraph_text(p)
                    t = raw.strip()
                    if not t:
                        continue
                    prefix = "〔소주〕 " if reg.kind == "소주" else ""
                    sp, me = grade_of(reg.id, i, raw, t, len(prefix))
                    t = prefix + t
                    if join_pages and i == 0 and not first_text_done and p.continues_from_previous:
                        prev = next((b for b in reversed(blocks) if b.type == "para"), None)
                        if prev is not None:
                            boundary = f"〔→PDF {n}쪽" + (f"/인쇄 {pr.print_page}쪽" if pr.print_page else "") + "〕"
                            base = len(prev.text) + len(boundary)
                            prev.text = prev.text + boundary + t
                            prev.spans.extend((a + base, b + base, k) for a, b, k in sp)
                            prev.memos.extend((o + base, m) for o, m in me)
                            first_text_done = True
                            continue
                    blocks.append(Block(type="para", text=t, kind=reg.kind, page=n, spans=sp, memos=me))
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
                        blocks.append(Block(type="para", text=t, kind="기타", page=n))
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
    if legend_block is not None:                    # 범례: 이 파일 첫 쪽 기록 줄의 첫 글자에 메모 하나
        extra = f" · 2차 판독 없이 1차만 들어간 쪽: {page_ranges(no_pass2_pages)}" if no_pass2_pages else ""
        legend_block.memos.insert(0, (0, LEGEND + extra))
    return blocks
