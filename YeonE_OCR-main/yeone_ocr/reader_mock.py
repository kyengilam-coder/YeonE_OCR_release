"""모델 없이 돌리는 판독기. 기존 PDF 텍스트 레이어를 그대로 옮기고 큰 연결 성분으로 도판을 짐작한다.

결과는 '원본 대조 전' 상태의 1차 자료일 뿐이며, 처리 사슬(잘라내기·조립·보고)을 시험하거나
API 없이 뼈대를 만들어 두는 용도다. 글자 정확도는 기존 OCR 그대로다.
"""
from __future__ import annotations
import re
from PIL import Image
from .schema import PageReading, Region, Paragraph, Column, RegionReread, frac_bbox
from .pdfio import TextLayer
from .layout import detect_figures, overlap_ratio

LABEL_RE = re.compile(r"[-–—ー一]\s*(\d{1,4})\s*[-–—ー一]")


class MockReader:
    name = "mock"
    model = "textlayer+cv"
    usage = {"calls": 0}

    def __init__(self, textlayers: dict[int, TextLayer]):
        self.textlayers = textlayers

    def read_page(self, img: Image.Image, file_name: str, pdf_page: int, total: int, extra: str = "") -> PageReading:
        tl = self.textlayers.get(pdf_page) or TextLayer(page_width=1, page_height=1)
        W, H = (tl.page_width or 1), (tl.page_height or 1)
        figs = detect_figures(img)
        regions: list[Region] = []
        for i, f in enumerate(figs, 1):
            regions.append(Region(id=f"f{i}", kind="도식", bbox=frac_bbox(f), reading_direction="해당없음",
                                  note="자동 감지한 도판 후보(종류 미확인). 원형나경도·계통도 등 종류는 사람이 확인"))
        # 텍스트 레이어의 세로 열 중 도판 안에 든 것(그림 안 글자의 오인식)은 뺀다.
        cols = []
        for c in tl.vertical_columns:
            fr = (max(0, (c["x"] - 5) / W), c["y0"] / H, min(1, (c["x"] + 5) / W), c["y1"] / H)
            if any(overlap_ratio(fr, f) > 0.5 for f in figs):
                continue
            cols.append(c)
        if cols:
            x0 = min(c["x"] for c in cols) - 6; x1 = max(c["x"] for c in cols) + 6
            y0 = min(c["y0"] for c in cols); y1 = max(c["y1"] for c in cols)
            para = Paragraph(columns=[Column(col_from_right=i + 1, text=c["text"]) for i, c in enumerate(cols)])
            regions.append(Region(id="b1", kind="본문", bbox=frac_bbox((x0 / W, y0 / H, x1 / W, y1 / H)),
                                  reading_direction="세로(우→좌)", paragraphs=[para],
                                  note="기존 PDF 텍스트 레이어를 열 좌표로 모아 옮긴 것. 원본 대조 전"))
        label = tl.page_label
        m = LABEL_RE.search(label or "")
        return PageReading(pdf_page=pdf_page, print_page=(m.group(1) if m else ""), page_label=label or "",
                           regions=regions, page_note="mock 판독기: 기존 OCR 레이어 + 도판 자동 감지. 글자 정확도는 기존 OCR 그대로이며 원본 대조가 필요함")

    def reread_region(self, crop: Image.Image, kind: str, region_id: str, pdf_page: int) -> RegionReread:
        return RegionReread()   # 2차 판독 없음
