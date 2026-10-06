"""python-hwpx 로 실제 HWPX(OWPML zip) 파일을 만든다. 확장자만 바꾼 파일이 아니다."""
from __future__ import annotations
from pathlib import Path
from .blocks import Block

BODY_WIDTH_MM = 150.0   # 본문 폭 가정(A4, 좌우 여백 30mm)


def figure_width_mm(b: Block, min_mm: float, large_min_mm: float) -> float:
    w = BODY_WIDTH_MM * max(0.0, min(1.0, b.frac_w)) * 1.15     # 판면이 쪽 폭보다 좁으므로 조금 키운다
    floor = large_min_mm if b.kind in ("원형나경도", "복잡한표", "계통도", "도식") else min_mm
    return max(floor, min(BODY_WIDTH_MM, w))


def build_hwpx(blocks: list[Block], out_path: str, min_mm: float = 70, large_min_mm: float = 120) -> dict:
    from hwpx import HwpxDocument
    doc = HwpxDocument.new()
    stats = {"paragraphs": 0, "headings": 0, "images": 0, "tables": 0, "uncertain_items": 0}

    def heading(text, level):
        try:
            doc.add_heading(text, level=level)
        except TypeError:
            doc.add_paragraph(text)
        stats["headings"] += 1

    for b in blocks:
        if b.type == "meta":
            doc.add_paragraph(b.text); stats["paragraphs"] += 1
        elif b.type == "heading":
            heading(b.text, b.level)
        elif b.type in ("para", "note"):
            doc.add_paragraph(b.text); stats["paragraphs"] += 1
        elif b.type == "image":
            data = Path(b.path).read_bytes()
            w = figure_width_mm(b, min_mm, large_min_mm)
            h = w * b.ratio
            if h > 230:                              # 쪽 높이를 넘지 않게
                h = 230; w = h / b.ratio
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
            heading("[판독 확인 필요]", 3)
            if b.items:
                for it in b.items:
                    doc.add_paragraph(it); stats["uncertain_items"] += 1
            else:
                doc.add_paragraph("없음")
            stats["paragraphs"] += 1
        elif b.type == "report":
            heading("〔작업 기록〕", 1)
            for line in b.text.splitlines():
                doc.add_paragraph(line)
    doc.save_to_path(out_path)
    # 다시 열어 확인
    re = HwpxDocument.open(out_path)
    try:
        n_img = len(list(re.media.images))
    except Exception:
        n_img = len(list(re.list_images()))
    stats["images_in_file"] = n_img
    try:
        rep = re.validate(); stats["validate_issues"] = len(getattr(rep, "issues", ()) or ())
    except Exception as e:
        stats["validate_issues"] = f"검사 불가: {e}"
    return stats
