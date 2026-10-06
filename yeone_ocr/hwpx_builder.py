"""python-hwpx 로 실제 HWPX(OWPML zip) 파일을 만든다. 확장자만 바꾼 파일이 아니다.

가로쓰기 정리본: 제목은 계층대로, 본문은 함초롬바탕, 소주는 작게, 쪽 기록 줄은 작은 회색.
원본 쪽마다 쪽 나누기. 〔疑: ○〕〔○/△〕〔판독불명〕 같은 의심 자리는 붉은 굵은 글자 + 노란 형광펜(음영),
〔→PDF 2쪽〕〔소주〕 같은 작업 표시는 회색 작은 글씨로 구분한다(지침 22: 작업 메모는 원문과 분명히 구분).
"""
from __future__ import annotations
import re
from pathlib import Path
from .blocks import Block

BODY_WIDTH_MM = 160.0   # A4, 좌우 여백 25mm
MARK_RE = re.compile(r"(〔[^〔〕]*〕)")
NOTE_PREFIXES = ("〔→", "〔소주〕", "〔작업 메모〕", "〔판독 메모〕", "〔도판 미삽입", "〔이어짐〕")

FONT_BODY, FONT_HEAD = "함초롬바탕", "함초롬돋움"
HEADING_FORMAT = {"h1": dict(alignment="CENTER", spacing_before_pt=14, spacing_after_pt=7),
                  "h2": dict(alignment="LEFT", spacing_before_pt=10, spacing_after_pt=4),
                  "h3": dict(alignment="LEFT", spacing_before_pt=6, spacing_after_pt=2)}
COLOR_MARK, SHADE_MARK, COLOR_NOTE, COLOR_META = "#C00000", "#FFFF00", "#666666", "#8A8A8A"


def split_marks(text: str) -> list[tuple[str, str]]:
    """글을 (조각, 종류) 로 나눈다. 종류: body | mark(의심 자리) | note(작업 표시)."""
    out = []
    for seg in MARK_RE.split(text):
        if not seg:
            continue
        if seg.startswith("〔") and seg.endswith("〕"):
            out.append((seg, "note" if seg.startswith(NOTE_PREFIXES) else "mark"))
        else:
            out.append((seg, "body"))
    return out


def figure_width_mm(b: Block, min_mm: float, large_min_mm: float) -> float:
    w = BODY_WIDTH_MM * max(0.0, min(1.0, b.frac_w)) * 1.15     # 판면이 쪽 폭보다 좁으므로 조금 키운다
    floor = large_min_mm if b.kind in ("원형나경도", "복잡한표", "계통도", "도식") else min_mm
    return max(floor, min(BODY_WIDTH_MM, w))


def build_hwpx(blocks: list[Block], out_path: str, min_mm: float = 70, large_min_mm: float = 120,
               highlight: bool = True, page_breaks: bool = True) -> dict:
    from hwpx import HwpxDocument
    doc = HwpxDocument.new()
    try:
        doc.set_page_setup(paper_size="A4", margin_left_mm=25, margin_right_mm=25, margin_top_mm=22, margin_bottom_mm=20)
    except Exception:
        pass
    S = {
        "body": doc.ensure_run_style(font=FONT_BODY, size=11),
        "small": doc.ensure_run_style(font=FONT_BODY, size=9.5),
        "mark": doc.ensure_run_style(font=FONT_BODY, size=11, bold=True, color=COLOR_MARK, highlight=SHADE_MARK) if highlight
                else doc.ensure_run_style(font=FONT_BODY, size=11),
        "note": doc.ensure_run_style(font=FONT_BODY, size=9, color=COLOR_NOTE),
        "meta": doc.ensure_run_style(font=FONT_HEAD, size=8, color=COLOR_META),
        "h1": doc.ensure_run_style(font=FONT_HEAD, size=16, bold=True),
        "h2": doc.ensure_run_style(font=FONT_HEAD, size=14, bold=True),
        "h3": doc.ensure_run_style(font=FONT_HEAD, size=12, bold=True),
        "chk_h": doc.ensure_run_style(font=FONT_HEAD, size=9.5, bold=True, color=COLOR_NOTE),
        "chk": doc.ensure_run_style(font=FONT_BODY, size=9, color=COLOR_NOTE),
    }
    stats = {"paragraphs": 0, "headings": 0, "images": 0, "tables": 0, "uncertain_items": 0, "marks": 0, "page_breaks": 0}

    # 모든 문단은 '바탕글'(문단 모양 0, 스타일 0)을 명시한다. python-hwpx 는 앞 문단의 모양을 물려주므로,
    # 제목(개요 스타일) 다음 문단들이 개요 모양을 받아 한컴이 "가. 나. 다." 자동 번호를 붙이던 문제를 막는다.
    def para(text: str, style: str, *, page_break: bool = False, marks: bool = False):
        attrs = {"pageBreak": "1"} if page_break else {}
        p = doc.add_paragraph("", para_pr_id_ref=0, style_id_ref=0, char_pr_id_ref=S[style], include_run=False, inherit_style=False, **attrs)
        if marks:
            for seg, kind in split_marks(text):
                st = S[style] if kind == "body" else (S["mark"] if kind == "mark" else S["note"])
                if kind == "mark": stats["marks"] += 1
                p.add_run(seg, char_pr_id_ref=st)
        else:
            p.add_run(text, char_pr_id_ref=S[style])
        if page_break: stats["page_breaks"] += 1
        stats["paragraphs"] += 1
        return p

    first_meta = True
    for b in blocks:
        if b.type == "meta":
            para(b.text, "meta", page_break=(page_breaks and not first_meta))
            first_meta = False
        elif b.type == "heading":
            # 개요 스타일(add_heading)을 쓰지 않는다: 한컴이 개요 번호("1." "가.")를 덧붙여 원본에 없는 글자가 생긴다.
            # 제목은 바탕글 문단에 굵은 큰 글자로 두고, 자리 잡기(가운데·앞뒤 간격)만 문단 모양으로 준다.
            lvl = "h1" if b.level <= 1 else ("h2" if b.level == 2 else "h3")
            para(b.text, lvl)
            try:
                doc.set_paragraph_format(paragraph_index=len(doc.paragraphs) - 1, **HEADING_FORMAT[lvl])
            except Exception:
                pass
            stats["headings"] += 1
        elif b.type == "para":
            para(b.text, "small" if b.kind == "소주" else "body", marks=True)
        elif b.type == "note":
            para(b.text, "note", marks=True)
        elif b.type == "image":
            data = Path(b.path).read_bytes()
            w = figure_width_mm(b, min_mm, large_min_mm)
            h = w * b.ratio
            if h > 230:                              # 쪽 높이를 넘지 않게
                h = 230; w = h / b.ratio
            try:
                doc.add_picture(data, Path(b.path).suffix.lstrip(".").lower() or "png", width_mm=round(w, 1), height_mm=round(h, 1), align="CENTER")
            except Exception:
                doc.add_picture(data, Path(b.path).suffix.lstrip(".").lower() or "png", width_mm=round(w, 1), height_mm=round(h, 1))
            stats["images"] += 1
        elif b.type == "table":
            rows, cols = len(b.rows), len(b.rows[0])
            t = doc.add_table(rows, cols)
            for i, row in enumerate(b.rows):
                for j, cell in enumerate(row):
                    t.cell(i, j).text = cell
            stats["tables"] += 1
        elif b.type == "uncertain":
            para("[판독 확인 필요]", "chk_h")
            if b.items:
                for it in b.items:
                    para("· " + it, "chk", marks=True); stats["uncertain_items"] += 1
            else:
                para("없음", "chk")
        elif b.type == "report":
            para("〔작업 기록〕", "h2")
            for line in b.text.splitlines():
                para(line, "chk")
    doc.save_to_path(out_path)
    # 다시 열어 확인
    re_ = HwpxDocument.open(out_path)
    try:
        imgs = re_.media.images
        n_img = len(list(imgs() if callable(imgs) else imgs))
    except Exception:
        n_img = len(list(re_.list_images()))
    stats["images_in_file"] = n_img
    try:
        rep = re_.validate(); stats["validate_issues"] = len(getattr(rep, "issues", ()) or ())
    except Exception as e:
        stats["validate_issues"] = f"검사 불가: {e}"
    return stats
