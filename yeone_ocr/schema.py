"""판독 결과의 구조. 모델(Gemini)의 구조화 출력 스키마이자 작업 파일(JSON)의 형식이다.

좌표(bbox)는 Gemini 관례대로 [ymin, xmin, ymax, xmax] 0~1000 정규화 값이다.
"""
from __future__ import annotations
from pydantic import BaseModel, Field, ConfigDict

# 영역 유형. 지침 3장의 A~G형을 영역 단위로 풀어 쓴 것.
KINDS = ["제목", "본문", "소주", "설명문", "그림제목", "표제목",
         "원형나경도", "산도", "계통도", "도식", "삽화", "단순표", "복잡한표",
         "쪽번호", "머리말", "기타"]
TEXT_KINDS = {"본문", "설명문", "소주"}          # 편집 가능한 문자로 입력
TITLE_KINDS = {"제목", "그림제목", "표제목"}      # 제목 계층 유지
IMAGE_KINDS = {"원형나경도", "산도", "계통도", "도식", "삽화", "복잡한표"}  # 원본 이미지로 보존
SKIP_KINDS = {"쪽번호", "머리말"}                 # 본문에서 분리, 페이지 기록에만 남김

MARK_UNKNOWN = "〔판독불명〕"
MARK_MISSING = "〔원본 결락〕"


class _M(BaseModel):
    """모델에 보내는 JSON 키는 짧은 별칭(alias)을 쓴다. 출력 토큰이 줄어 빠르고 싸다. 코드와 저장 파일은 긴 이름을 쓴다."""
    model_config = ConfigDict(populate_by_name=True)


class Column(_M):
    """세로 열 하나. 위→아래로 읽은 글자. 오른쪽에서 몇 번째 열인지 기록."""
    col_from_right: int = Field(0, alias="n")
    text: str = Field("", alias="t")


class Paragraph(_M):
    """실제 문단. 인쇄상의 열 바꿈은 columns 안에서 이어진다."""
    columns: list[Column] = Field(default_factory=list, alias="cols")
    continues_from_previous: bool = Field(False, alias="cont")   # 앞 쪽(또는 앞 영역)의 문장이 이어지는 문단인가


class TableRow(_M):
    cells: list[str] = Field(default_factory=list)


class Region(_M):
    id: str
    kind: str = Field(alias="k")
    bbox: list[int] = Field(default_factory=lambda: [0, 0, 1000, 1000])  # [ymin,xmin,ymax,xmax] 0~1000
    reading_direction: str = Field("", alias="dir")   # "세로(우→좌)" | "가로" | "해당없음"
    title_text: str = Field("", alias="title")        # 제목·그림제목·표제목의 글자
    title_level: int = Field(0, alias="lv")           # 1=章, 2=節·層, 3=그 아래. 모르면 0
    paragraphs: list[Paragraph] = Field(default_factory=list, alias="paras")   # 본문·설명문·소주
    table_rows: list[TableRow] = Field(default_factory=list, alias="rows")     # 단순표
    note: str = ""                                    # 판독 메모(손글씨 표시, 훼손 등). 원문이 아님


class Uncertain(_M):
    region_id: str = Field("", alias="r")
    col_from_right: int = Field(0, alias="n")
    position: str = Field("", alias="pos")      # 위 | 중간 | 아래
    context: str = Field("", alias="ctx")       # 「○○〔疑: 龍〕○○」 처럼 앞뒤 글자 포함


class PageReading(_M):
    pdf_page: int
    print_page: str = Field("", alias="print")        # 인쇄 쪽수. 확인 못 하면 ""
    running_header: str = Field("", alias="header")   # 반복 머리말(책 제목·장 제목). 없으면 ""
    page_label: str = Field("", alias="label")        # 쪽번호 표기 원문. 예 "- 30 -"
    regions: list[Region] = Field(default_factory=list)
    uncertain: list[Uncertain] = Field(default_factory=list, alias="unc")
    page_note: str = Field("", alias="note")          # 쪽 전체에 대한 메모(원문 아님)


class RegionReread(_M):
    """확대 조각으로 한 영역만 다시 독립 판독한 결과."""
    columns: list[Column] = Field(default_factory=list, alias="cols")
    uncertain: list[Uncertain] = Field(default_factory=list, alias="unc")


class RegionRereadItem(_M):
    region_id: str = Field(alias="r")
    columns: list[Column] = Field(default_factory=list, alias="cols")
    uncertain: list[Uncertain] = Field(default_factory=list, alias="unc")


class MultiRegionReread(_M):
    """한 쪽의 문자 영역 조각 여러 장을 한 호출로 다시 판독한 결과."""
    regions: list[RegionRereadItem] = Field(default_factory=list)


# ---- 도우미 ----
def bbox_frac(bbox: list[int]) -> tuple[float, float, float, float]:
    """[ymin,xmin,ymax,xmax] 0~1000 → (x0,y0,x1,y1) 0~1 비율."""
    y0, x0, y1, x1 = [max(0, min(1000, int(v))) for v in bbox]
    return (min(x0, x1) / 1000, min(y0, y1) / 1000, max(x0, x1) / 1000, max(y0, y1) / 1000)


def frac_bbox(fr: tuple[float, float, float, float]) -> list[int]:
    x0, y0, x1, y1 = fr
    return [round(y0 * 1000), round(x0 * 1000), round(y1 * 1000), round(x1 * 1000)]


def paragraph_text(p: Paragraph) -> str:
    """열 바꿈을 이어 붙인 문단 글. 열 끝과 다음 열 처음 사이에는 아무것도 넣지 않는다(지침 18)."""
    return "".join(c.text for c in sorted(p.columns, key=lambda c: c.col_from_right))


def region_text(r: Region) -> str:
    if r.kind in TITLE_KINDS:
        return r.title_text
    return "\n".join(paragraph_text(p) for p in r.paragraphs)


def region_columns(r: Region) -> list[Column]:
    cols: list[Column] = []
    for p in r.paragraphs:
        cols.extend(p.columns)
    return sorted(cols, key=lambda c: c.col_from_right)
