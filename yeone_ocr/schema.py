"""판독 결과의 구조. 모델(Gemini)의 구조화 출력 스키마이자 작업 파일(JSON)의 형식이다.

좌표(bbox)는 Gemini 관례대로 [ymin, xmin, ymax, xmax] 0~1000 정규화 값이다.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass
from typing import Annotated, Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator

# 영역 유형. 지침 3장의 A~G형을 영역 단위로 풀어 쓴 것.
KINDS = ["제목", "본문", "소주", "설명문", "그림제목", "표제목",
         "원형나경도", "산도", "계통도", "도식", "삽화", "단순표", "복잡한표",
         "쪽번호", "머리말", "기타"]
RegionKind = Literal["제목", "본문", "소주", "설명문", "그림제목", "표제목",
                     "원형나경도", "산도", "계통도", "도식", "삽화", "단순표", "복잡한표", "쪽번호", "머리말", "기타"]
Coordinate = Annotated[int, Field(ge=0, le=1000)]
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
    id: str = Field(min_length=1)
    kind: RegionKind = Field(alias="k")
    bbox: list[Coordinate] = Field(default_factory=lambda: [0, 0, 1000, 1000], min_length=4, max_length=4)
    reading_direction: str = Field("", alias="dir")   # "세로(우→좌)" | "가로" | "해당없음"
    title_text: str = Field("", alias="title")        # 제목·그림제목·표제목의 글자
    title_level: int = Field(0, alias="lv")           # 1=章, 2=節·層, 3=그 아래. 모르면 0
    paragraphs: list[Paragraph] = Field(default_factory=list, alias="paras")   # 본문·설명문·소주
    table_rows: list[TableRow] = Field(default_factory=list, alias="rows")     # 단순표
    note: str = ""                                    # 판독 메모(손글씨 표시, 훼손 등). 원문이 아님

    @field_validator("id")
    @classmethod
    def nonempty_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("영역 id 는 빈 글일 수 없음")
        return value

    @field_validator("bbox")
    @classmethod
    def valid_bbox(cls, value: list[int]) -> list[int]:
        y0, x0, y1, x1 = value
        if y0 == y1 or x0 == x1:
            raise ValueError("영역 bbox 의 너비·높이는 0보다 커야 함")
        # bbox_frac 가 지원하던 역순 끝점은 유지하되 저장할 때 정렬한다.
        return [min(y0, y1), min(x0, x1), max(y0, y1), max(x0, x1)]


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


def duplicate_region_ids(page: PageReading) -> set[str]:
    return {rid for rid, n in Counter(r.id for r in page.regions).items() if n > 1}


class PageResponse(PageReading):
    """새 모델 응답은 영역 id 가 유일해야 한다. PageReading 은 과거 중복 id 기록의 검수·본문 보존을 허용한다."""

    @field_validator("regions")
    @classmethod
    def unique_region_ids(cls, regions: list[Region]) -> list[Region]:
        duplicate = [rid for rid, n in Counter(r.id for r in regions).items() if n > 1]
        if duplicate:
            raise ValueError("영역 id 겹침: " + ", ".join(duplicate))
        return regions


class RegionReread(_M):
    """확대 조각으로 한 영역만 다시 독립 판독한 결과."""
    columns: list[Column] = Field(default_factory=list, alias="cols")
    uncertain: list[Uncertain] = Field(default_factory=list, alias="unc")


class RegionRereadItem(_M):
    region_id: str = Field(alias="r")
    columns: list[Column] = Field(default_factory=list, alias="cols")
    uncertain: list[Uncertain] = Field(default_factory=list, alias="unc")


class GlyphChoice(_M):
    """3차 글자 판별의 답. choice 는 후보 가운데 하나(자형 근거가 보일 때만), 아니면 빈 글.
    필드 차례가 곧 모델이 쓰는 차례다(0.6.5): 조각에 보이는 글자(w) → 본 자형(e) → 고른 글자(c). 보고 적은 뒤에 고르게 한다."""
    window: str = Field("", alias="w")         # 조각에 보이는 글자 전부(조각이 엉뚱한 자리면 코드가 앞뒤 글자와 맞춰 가려낸다)
    evidence: str = Field("", alias="e")       # 본 자형 한 줄(갈고리·점·획 길이 등)
    choice: str = Field("", alias="c")


class GlyphChoiceItem(_M):
    """묶음 3차 판별의 자리 하나. s 는 호출자가 붙인 자리 번호(【s】 표시와 같음)."""
    site_id: str = Field("", alias="s")
    window: str = Field("", alias="w")
    evidence: str = Field("", alias="e")
    choice: str = Field("", alias="c")


@dataclass(frozen=True)
class GlyphAsk:
    """3차 글자 판별 질문 하나 — 모델에게 글로 주는 것만 담는다(눈가림). 앞뒤 글자는 글로 넣지 않는다(조각 그림에는 이웃 글자가 보인다):
    판독기가 받지 못하니 프롬프트에 새어 들어갈 수 없다. 앞뒤 글자는 답을 받은 뒤 코드 검사(window_ok)에만 쓴다.
    - candidates: 묻는 차례대로의 후보(고정 씨앗으로 섞은 것). 빈 튜플이면 후보 없이 읽게 하는 눈가림 확인 질문.
    - pos: 조각 안 읽기 방향 순번(1부터, 대상 첫 글자). 0 이면 모름. k: 대상 글자 수. count: 조각에 든 글자 수(0=모름).
    - measured: 자리를 실측했는가(글자층·먹 셈 일치). 아니면 순번은 어림이다. tick: 조각 여백에 눈금을 그렸는가."""
    candidates: tuple[str, ...] = ()
    pos: int = 0
    k: int = 1                     # 대상 글자 수. 0 이면 후보마다 글자 수가 달라 1~2자로만 가리킨다
    count: int = 0
    measured: bool = False
    tick: bool = False


class MultiGlyphChoice(_M):
    """글자 조각 여러 장을 한 호출로 판별한 결과."""
    items: list[GlyphChoiceItem] = Field(default_factory=list)


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
