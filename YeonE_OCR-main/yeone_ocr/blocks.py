"""쪽별 판독 기록을 문서 블록(제목·문단·그림·표·확인 필요 목록)의 한 줄기로 엮는다. HWPX/DOCX 조립이 공통으로 쓴다."""
from __future__ import annotations
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


def meta_line(rec: dict, whole_offset: int | None) -> str:
    r = rec["merged"]
    parts = [Path(rec["file"]).name]
    if whole_offset is not None:
        parts.append(f"전체 PDF {rec['pdf_page'] + whole_offset}쪽")
    parts.append(f"PDF {rec['pdf_page']}쪽")
    parts.append(f"인쇄 {r['print_page']}쪽" if r.get("print_page") else "인쇄 쪽수 확인 안 됨")
    return "[" + " / ".join(parts) + "]"


def assemble_blocks(records: list[dict], join_pages: bool = True, whole_offset: int | None = None) -> list[Block]:
    blocks: list[Block] = []
    for rec in records:
        pr = PageReading.model_validate(rec["merged"])
        n = pr.pdf_page
        blocks.append(Block(type="meta", text=meta_line(rec, whole_offset), page=n))
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
                    t = paragraph_text(p).strip()
                    if not t:
                        continue
                    if reg.kind == "소주":
                        t = "〔소주〕 " + t
                    if join_pages and i == 0 and not first_text_done and p.continues_from_previous:
                        prev = next((b for b in reversed(blocks) if b.type == "para"), None)
                        if prev is not None:
                            boundary = f"〔→PDF {n}쪽" + (f"/인쇄 {pr.print_page}쪽" if pr.print_page else "") + "〕"
                            prev.text = prev.text + boundary + t
                            first_text_done = True
                            continue
                    blocks.append(Block(type="para", text=t, kind=reg.kind, page=n))
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
        items = []
        for u in pr.uncertain:
            where = []
            if u.region_id: where.append(u.region_id)
            if u.col_from_right: where.append(f"오른쪽에서 {u.col_from_right}번째 열")
            if u.position: where.append(f"{u.position} 구간")
            items.append((", ".join(where) + ": " if where else "") + u.context)
        for iss in rec.get("verify") or []:
            if iss["level"] == "error":
                items.append(f"구조 검사: {iss['rule']} ({iss['where']}) — {iss['msg']}")
        blocks.append(Block(type="uncertain", items=items, page=n))
    return blocks
