"""python-hwpx 로 실제 HWPX(OWPML zip) 파일을 만든다. 확장자만 바꾼 파일이 아니다.

가로쓰기 정리본: 제목은 계층대로, 본문은 함초롬바탕, 소주는 작게, 쪽 기록 줄은 작은 회색.
원본 쪽마다 쪽 나누기. 글자는 확인 등급(grades.GRADE_STYLE)으로 칠한다: 〔疑: ○〕〔○/△〕〔판독불명〕 같은 의심 자리는 빨강
(굵게 + 노란 형광펜), 3차 자형 판별로 정한 글자는 초록, 문맥이 가리키는 후보는 파랑. 메모(한컴 메모)는 고지 사항에만 글자 하나에 단다.
〔→PDF 2쪽〕〔소주〕 같은 작업 표시는 회색 작은 글씨로 구분한다(지침 22: 작업 메모는 원문과 분명히 구분).
"""
from __future__ import annotations
import re
from pathlib import Path
from .blocks import Block
from .grades import split_marks, styled_runs, GRADE_STYLE, STYLE_ALIAS, NOTE_PREFIXES   # split_marks·NOTE_PREFIXES 는 예전 이름으로도 쓰이게 다시 내보냄

BODY_WIDTH_MM = 160.0   # A4, 좌우 여백 25mm
MARK_RE = re.compile(r"(〔[^〔〕]*〕)")
HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
MEMO_AUTHOR = "YeonE_OCR"

FONT_BODY, FONT_HEAD = "함초롬바탕", "함초롬돋움"
HEADING_FORMAT = {"h1": dict(alignment="CENTER", spacing_before_pt=14, spacing_after_pt=7),
                  "h2": dict(alignment="LEFT", spacing_before_pt=10, spacing_after_pt=4),
                  "h3": dict(alignment="LEFT", spacing_before_pt=6, spacing_after_pt=2)}
COLOR_MARK, SHADE_MARK, COLOR_NOTE, COLOR_META = "#C00000", "#FFFF00", "#666666", "#8A8A8A"


def figure_width_mm(b: Block, min_mm: float, large_min_mm: float) -> float:
    w = BODY_WIDTH_MM * max(0.0, min(1.0, b.frac_w)) * 1.15     # 판면이 쪽 폭보다 좁으므로 조금 키운다
    floor = large_min_mm if b.kind in ("원형나경도", "복잡한표", "계통도", "도식") else min_mm
    return max(floor, min(BODY_WIDTH_MM, w))


def build_hwpx(blocks: list[Block], out_path: str, min_mm: float = 70, large_min_mm: float = 120,
               highlight: bool = True, page_breaks: bool = True, memos: bool = True) -> dict:
    """highlight=False 면 등급 색·형광펜·밑줄 없이 글자만(〔〕 표기는 그대로). memos=False 면 고지를 메모 대신 회색 〔고지: …〕 로 글 속에."""
    from hwpx import HwpxDocument
    doc = HwpxDocument.new()
    from .hwpx_memo import MemoWriter
    mw = MemoWriter(doc, MEMO_AUTHOR)
    styles = getattr(doc, "styles", None)
    _rs = styles.ensure_run if styles is not None and hasattr(styles, "ensure_run") else doc.ensure_run_style   # 6.x 의 새 이름 우선
    try:
        doc.set_page_setup(paper_size="A4", margin_left_mm=25, margin_right_mm=25, margin_top_mm=22, margin_bottom_mm=20)
    except Exception:
        pass
    S = {
        "body": _rs(font=FONT_BODY, size=11),
        "small": _rs(font=FONT_BODY, size=9.5),
        "mark": _rs(font=FONT_BODY, size=11, bold=True, color=COLOR_MARK, highlight=SHADE_MARK) if highlight
                else _rs(font=FONT_BODY, size=11),
        "note": _rs(font=FONT_BODY, size=9, color=COLOR_NOTE),
        "meta": _rs(font=FONT_HEAD, size=8, color=COLOR_META),
        "h1": _rs(font=FONT_HEAD, size=16, bold=True),
        "h2": _rs(font=FONT_HEAD, size=14, bold=True),
        "h3": _rs(font=FONT_HEAD, size=12, bold=True),
        "chk_h": _rs(font=FONT_HEAD, size=9.5, bold=True, color=COLOR_NOTE),
        "chk": _rs(font=FONT_BODY, size=9, color=COLOR_NOTE),
    }
    stats = {"paragraphs": 0, "headings": 0, "images": 0, "tables": 0, "uncertain_items": 0, "marks": 0, "page_breaks": 0,
             "green": 0, "blue": 0, "red_ctx": 0, "memos": 0}
    SIZE = {"body": 11, "small": 9.5, "chk": 9, "note": 9, "meta": 8}
    _grade_cache: dict[tuple[str, str], str] = {}

    def grade_style(base: str, grade: str) -> str:
        """본문·소주 크기에 맞춘 등급 글자 모양(같은 것은 한 번만 만든다)."""
        key = (base, grade)
        if key not in _grade_cache:
            g = GRADE_STYLE[grade]
            kw = dict(font=FONT_BODY, size=SIZE.get(base, 11), bold=g["bold"], color=g["color"])
            if g["highlight"]:
                kw["highlight"] = g["highlight"]
            if g["underline"]:
                kw.update(underline=True, underline_shape=g["underline"], underline_color=g["color"])
            try:
                _grade_cache[key] = _rs(**kw)
            except (TypeError, ValueError):                       # 밑줄 모양을 모르는 옛 python-hwpx
                kw.pop("underline_shape", None); kw.pop("underline_color", None)
                _grade_cache[key] = _rs(**kw)
        return _grade_cache[key]

    # 모든 문단은 '바탕글'(문단 모양 0, 스타일 0)을 명시한다. python-hwpx 는 앞 문단의 모양을 물려주므로,
    # 제목(개요 스타일) 다음 문단들이 개요 모양을 받아 한컴이 "가. 나. 다." 자동 번호를 붙이던 문제를 막는다.
    def para(text: str, style: str, *, page_break: bool = False, marks: bool = False, spans=(), notes=()):
        attrs = {"pageBreak": "1"} if page_break else {}
        p = doc.add_paragraph("", para_pr_id_ref=0, style_id_ref=0, char_pr_id_ref=S[style], include_run=False, inherit_style=False, **attrs)
        if not memos and notes:                                    # 메모를 쓰지 않으면 고지를 글 끝에 회색으로
            text = text + "".join(f"〔고지: {m}〕" for _, m in notes); notes = ()
        if marks or spans or notes:
            stats["marks"] += sum(1 for _, k in split_marks(text) if k == "mark")
            for seg, kind, msgs in styled_runs(text, spans if highlight else (), notes):
                kind = STYLE_ALIAS.get(kind, kind)
                if kind == "body":
                    st = S[style]
                elif kind == "note":
                    st = S["note"]
                elif not highlight:
                    st = S[style]
                else:
                    st = grade_style(style, kind)
                if kind in ("green", "blue", "red_ctx") and seg.strip():
                    stats[kind] += 1
                r = p.add_run(seg, char_pr_id_ref=st)
                for m in msgs:
                    if mw.wrap_run(getattr(r, "element", None), m): stats["memos"] += 1
        else:
            p.add_run(text, char_pr_id_ref=S[style])
        if page_break: stats["page_breaks"] += 1
        stats["paragraphs"] += 1
        return p

    first_meta = True
    for b in blocks:
        if b.type == "meta":
            para(b.text, "meta", page_break=(page_breaks and not first_meta), notes=b.memos)
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
            para(b.text, "small" if b.kind == "소주" else "body", marks=True, spans=b.spans, notes=b.memos)
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
            para(b.text or "[판독 확인 필요]", "chk_h")
            for it in b.items:
                para("· " + it, "chk", marks=True); stats["uncertain_items"] += 1
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
