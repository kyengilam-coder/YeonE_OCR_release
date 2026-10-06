"""python-docx 로 DOCX 도 함께 만들 수 있다(선택)."""
from __future__ import annotations
from pathlib import Path
from .blocks import Block
from .hwpx_builder import figure_width_mm


def build_docx(blocks: list[Block], out_path: str, min_mm: float = 70, large_min_mm: float = 120) -> dict:
    import docx
    from docx.shared import Mm
    D = docx.Document()
    stats = {"paragraphs": 0, "headings": 0, "images": 0, "tables": 0}
    for b in blocks:
        if b.type == "meta":
            p = D.add_paragraph(b.text); p.runs[0].italic = True; stats["paragraphs"] += 1
        elif b.type == "heading":
            D.add_heading(b.text, level=min(3, max(1, b.level))); stats["headings"] += 1
        elif b.type in ("para", "note"):
            D.add_paragraph(b.text); stats["paragraphs"] += 1
        elif b.type == "image":
            w = figure_width_mm(b, min_mm, large_min_mm)
            if w * b.ratio > 230: w = 230 / b.ratio
            D.add_picture(b.path, width=Mm(w)); stats["images"] += 1
        elif b.type == "table":
            t = D.add_table(rows=len(b.rows), cols=len(b.rows[0])); t.style = "Table Grid"
            for i, row in enumerate(b.rows):
                for j, cell in enumerate(row):
                    t.cell(i, j).text = cell
            stats["tables"] += 1
        elif b.type == "uncertain":
            D.add_heading("[판독 확인 필요]", level=3)
            for it in (b.items or ["없음"]):
                D.add_paragraph(it)
        elif b.type == "report":
            D.add_heading("〔작업 기록〕", level=1)
            for line in b.text.splitlines():
                D.add_paragraph(line)
    D.save(out_path)
    return stats
