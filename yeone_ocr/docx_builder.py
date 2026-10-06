"""python-docx 로 DOCX 도 함께 만들 수 있다(선택). HWPX 와 같은 꾸밈: 쪽 나누기, 의심 자리 붉은 글자+노란 형광펜, 작업 표시 회색."""
from __future__ import annotations
from pathlib import Path
from .blocks import Block
from .hwpx_builder import figure_width_mm, split_marks, FONT_BODY, FONT_HEAD


def _font(run, name: str, size_pt: float, bold=False, color: str | None = None, highlight=False):
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_COLOR_INDEX
    from docx.oxml.ns import qn
    run.font.name = name; run.font.size = Pt(size_pt); run.font.bold = bold
    rpr = run._element.get_or_add_rPr(); rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {}); rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), name)
    if color:
        run.font.color.rgb = RGBColor.from_string(color.lstrip("#"))
    if highlight:
        run.font.highlight_color = WD_COLOR_INDEX.YELLOW


def build_docx(blocks: list[Block], out_path: str, min_mm: float = 70, large_min_mm: float = 120,
               highlight: bool = True, page_breaks: bool = True) -> dict:
    import docx
    from docx.shared import Mm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    D = docx.Document()
    for s in D.sections:
        s.left_margin = s.right_margin = Mm(25); s.top_margin = Mm(22); s.bottom_margin = Mm(20)
    stats = {"paragraphs": 0, "headings": 0, "images": 0, "tables": 0, "marks": 0, "page_breaks": 0}

    def para(text, size, *, bold=False, color=None, marks=False, font=FONT_BODY):
        p = D.add_paragraph(); stats["paragraphs"] += 1
        if marks:
            for seg, kind in split_marks(text):
                r = p.add_run(seg)
                if kind == "mark":
                    _font(r, font, size, bold=True, color="#C00000" if highlight else None, highlight=highlight); stats["marks"] += 1
                elif kind == "note":
                    _font(r, font, 9, color="#666666")
                else:
                    _font(r, font, size, bold=bold, color=color)
        else:
            _font(p.add_run(text), font, size, bold=bold, color=color)
        return p

    first_meta = True
    for b in blocks:
        if b.type == "meta":
            if page_breaks and not first_meta:
                D.add_page_break(); stats["page_breaks"] += 1
            first_meta = False
            para(b.text, 8, color="#8A8A8A", font=FONT_HEAD)
        elif b.type == "heading":
            size = 16 if b.level <= 1 else (14 if b.level == 2 else 12)
            para(b.text, size, bold=True, font=FONT_HEAD); stats["headings"] += 1
        elif b.type == "para":
            para(b.text, 9.5 if b.kind == "소주" else 11, marks=True)
        elif b.type == "note":
            para(b.text, 9, color="#666666", marks=True)
        elif b.type == "image":
            w = figure_width_mm(b, min_mm, large_min_mm)
            if w * b.ratio > 230: w = 230 / b.ratio
            D.add_picture(b.path, width=Mm(w)); D.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER; stats["images"] += 1
        elif b.type == "table":
            t = D.add_table(rows=len(b.rows), cols=len(b.rows[0])); t.style = "Table Grid"
            for i, row in enumerate(b.rows):
                for j, cell in enumerate(row):
                    t.cell(i, j).text = cell
            stats["tables"] += 1
        elif b.type == "uncertain":
            para("[판독 확인 필요]", 9.5, bold=True, color="#666666", font=FONT_HEAD)
            for it in (b.items or ["없음"]):
                para(("· " + it) if b.items else it, 9, color="#666666", marks=True)
        elif b.type == "report":
            para("〔작업 기록〕", 14, bold=True, font=FONT_HEAD)
            for line in b.text.splitlines():
                para(line, 9, color="#666666")
    D.save(out_path)
    return stats
