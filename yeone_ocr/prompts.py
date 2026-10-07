"""모델에 주는 지시문. 사용자 지침(ocr/지침.md) 전문을 시스템 지시로 삼고, 출력 형식 규칙을 덧붙인다."""
from __future__ import annotations
from pathlib import Path
from .schema import KINDS

_HERE = Path(__file__).resolve().parent
GUIDE_CANDIDATES = [_HERE / "지침.md", _HERE.parent / "지침.md"]   # 패키지 안(설치형) → 저장소 ocr/ 폴더 순


def load_guide() -> str:
    for p in GUIDE_CANDIDATES:
        if p.exists():
            return p.read_text(encoding="utf-8")
    return "(지침 파일 yeone_ocr/지침.md 이 없습니다. 원본 이미지의 자형과 배열을 최종 기준으로 삼으십시오.)"


OUTPUT_RULES = f"""
## 출력 형식 규칙 (프로그램이 읽는 JSON — 키 이름은 짧은 별칭)

- 반드시 주어진 JSON 스키마대로만 답한다. 설명문·머리말·코드블록을 붙이지 않는다.
- 쪽: pdf_page, print(인쇄 쪽수 숫자만, 모르면 ""), header(반복 머리말 원문), label(쪽번호 표기 원문, 예 "- 30 -"), regions, unc, note(쪽 메모, 원문 아님).
- 영역(regions 의 원소): id, k(종류), bbox, dir(읽기 방향), title, lv, paras, rows, note.
  · bbox 는 [ymin, xmin, ymax, xmax] 0~1000 정규화 좌표(이미지 왼쪽 위가 0,0).
  · k 는 다음 중 하나: {", ".join(KINDS)}.
  · 제목/그림제목/표제목 → title 에 글자, lv 에 1=章, 2=節·層, 3=그 아래.
  · 본문/설명문/소주 → paras 에 넣는다. 문단 하나는 {{cols:[{{n, t}}…], cont}} 이고, 열(cols 의 원소) 하나는 세로 한 줄이다.
    t 는 그 열을 위→아래로 읽은 글자, n 은 영역 전체에서 오른쪽부터 센 열 번호(1부터)다. 문단이 바뀌어도 1 로 되돌리지 말고 이어서 센다.
    열과 열 사이에 글자를 보태거나 빼지 않는다. 한 문단이 여러 열에 걸치면 그 열들을 한 문단의 cols 로 묶는다.
    들여쓰기·빈 공간 등 원본 근거가 있을 때만 문단을 나눈다. 一層·二層·三層, ①②③, 가·나·다 처럼 번호·머리글자가 붙은 짧은 항목 목록은 항목마다 문단을 나눈다.
    쪽 첫 문단이 앞 쪽에서 이어지는 문장이면 cont = true.
  · 원형나경도/산도/계통도/도식/삽화/복잡한표 → bbox 만 정확히 적고 글자는 적지 않는다(그림 안 글자는 전사하지 않는다).
    그림 밖의 제목·번호·설명문은 별도 영역(그림제목·설명문)으로 둔다.
  · 단순표 → rows 에 행 순서대로 {{cells:[…]}} 를 넣는다. 빈칸은 "" 로 둔다. 구조가 불확실하면 복잡한표로 분류한다.
  · 쪽번호/머리말 → 영역(title 에 원문)으로 적고, 쪽의 label / header 에도 원문 그대로 적는다.
- 원문에 띄어쓰기(글자 사이 빈칸)가 보이면 공백 한 칸으로 적는다. 보이지 않으면 붙여 쓴다.
- 한글은 한글로, 한자는 한자로. 번역·현대어 변환·맞춤법 교정 금지.
- 판독할 수 없는 글자는 글자 자리에 그대로 표시를 넣는다: 〔판독불명〕 / 〔疑: 龍〕 / 〔龍/龐〕 / 〔원본 결락〕.
  그리고 unc 에 {{r(영역 id), n(열 번호), pos(위·중간·아래), ctx(앞뒤 글자 포함)}} 를 적는다.
- 괘상 기호: 원본의 획 수대로 적는다. 육획(重卦)이면 ䷀~䷿(U+4DC0~4DFF) 한 글자, 삼획이면 ☰~☷ 한 글자.
  삼획 기호 두 개를 이어 육획을 대신하지 않는다. 괘 이름 옆 기호는 그 이름의 괘여야 한다(困卦 → ䷮). 확인할 수 없으면 〔판독불명〕.
- 손글씨 밑줄·동그라미 등 인쇄가 아닌 표시는 글자로 옮기지 말고 영역의 note 에 "손글씨 표시 있음" 처럼 적는다.
- regions 는 읽기 순서대로 나열한다 (제목 → 오른쪽 본문 → … → 그림제목 → 그림 → 설명문 순서가 원본 배열과 맞을 때).
"""


# 읽기 모드. vertical(기본): 세로쓰기 한자·한글 문헌(지침 그대로). horizontal_zh: 가로쓰기 한문(간체) 책 — 줄 순서 위→아래, 줄 안 왼쪽→오른쪽.
MODES = {
    "vertical": "",
    "horizontal_zh": (
        "\n## 읽기 모드: 가로쓰기 한문(간체)\n"
        "- 이 책은 가로쓰기 중국어(간체) 책이다. 지침의 '열(세로 줄)'은 이 책에서 '가로 한 줄'로 읽는다. "
        "cols 의 원소 하나가 가로 한 줄이고, n 은 영역 안에서 위에서부터 센 줄 번호(1부터), t 는 그 줄을 왼쪽→오른쪽으로 읽은 글자다.\n"
        "- 인쇄된 글자 그대로 간체로 적는다. 번체로 바꾸지 않는다. 문장부호(，。；：『』「」)도 인쇄된 대로 적는다.\n"
        "- 읽기 방향(dir)은 \"가로(좌→우)\" 로 적고, regions 는 위→아래 순서로 나열한다. 쪽번호·머리말은 지침대로 따로 둔다.\n"
    ),
}
MODE = "vertical"


def set_mode(mode: str | None) -> None:
    global MODE
    if mode and mode in MODES:
        MODE = mode


def mode_note() -> str:
    return MODES.get(MODE, "")


def system_prompt() -> str:
    return load_guide().rstrip() + "\n\n" + OUTPUT_RULES + mode_note()


def page_prompt(file_name: str, pdf_page: int, total: int, extra: str = "") -> str:
    return (f"첨부 이미지는 원본 파일 「{file_name}」의 PDF {pdf_page}쪽(전체 {total}쪽)이다.\n"
            "지침 35장의 점검 항목(영역 유형, 읽기 방향, 오른쪽 첫 열, 제목·본문·주석 경계, 그림 안 글자 혼입 위험, "
            "표 대응관계, 도판 밖 설명문 누락, 판독 불가 글자, 앞뒤 쪽 이어짐)을 먼저 살핀 뒤, "
            + ("이 쪽의 영역을 나누고 편집 가능한 문자 영역을 위 줄부터 아래로, 줄 안은 왼쪽→오른쪽으로 판독하여 JSON 으로만 답하라.\n" if MODE == "horizontal_zh"
               else "이 쪽의 영역을 나누고 편집 가능한 문자 영역을 오른쪽 열부터 위→아래로 판독하여 JSON 으로만 답하라.\n")
            + "긴 판단 과정은 출력하지 말고 결과만 낸다." + (("\n" + extra) if extra else ""))


def region_prompt(kind: str, region_id: str, pdf_page: int) -> str:
    return (f"첨부 이미지는 PDF {pdf_page}쪽의 「{kind}」 영역({region_id})만 확대한 것이다.\n"
            "이 영역만 독립적으로 다시 판독하라. 다른 영역이나 앞서 읽은 결과를 참고하지 말고 이 이미지의 자형만 본다.\n"
            + _cols_rule() + "확정할 수 없는 글자는 〔疑: ○〕〔○/△〕〔판독불명〕 으로 표시하고 unc 에 적는다. JSON 으로만 답한다.")


def regions_prompt(items: list[tuple[str, str]], pdf_page: int) -> str:
    """items = [(region_id, kind), ...] 순서대로 이미지가 붙는다."""
    listing = ", ".join(f"{rid}({kind})" for rid, kind in items)
    return (f"첨부한 이미지 {len(items)}장은 PDF {pdf_page}쪽의 문자 영역을 하나씩 확대한 조각이다. 순서대로 {listing} 이다. "
            "각 이미지 바로 앞에 【region_id】 표시가 있다.\n"
            "영역마다 독립적으로 다시 판독하라. 다른 영역이나 앞서 읽은 결과를 참고하지 말고 그 조각의 자형만 본다.\n"
            + _cols_rule() + "결과는 regions 에 같은 r(region_id) 로 넣고, "
            "확정할 수 없는 글자는 〔疑: ○〕〔○/△〕〔판독불명〕 으로 표시하고 unc 에 적는다. JSON 으로만 답한다.")


def _cols_rule() -> str:
    if MODE == "horizontal_zh":
        return "줄은 위에서부터 1, 2, 3… 으로 세어 n 에, 각 줄을 왼쪽→오른쪽으로 읽은 글자를 t 에 넣어 cols 로 돌려준다(간체 그대로). "
    return "열은 오른쪽부터 1, 2, 3… 으로 세어 n 에, 각 열을 위→아래로 읽은 글자를 t 에 넣어 cols 로 돌려준다. "
