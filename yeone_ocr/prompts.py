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


# 혼동하기 쉬운 글자 카드. 지침 7·8·14장의 목록을 "자형 기술 + 문맥 단서 + 출력 규칙"으로 바꿔 지시문 끝에 둔다(모델이 끝부분을 더 잘 따른다).
# 묶음은 중의역대약론선 실측 장부(HWPX·글자층·1차·2차 대조)에서 실제로 섞인 것들이다.
CONFUSION_CARD = """
## 혼동하기 쉬운 글자 — 자형을 먼저 보고, 문맥은 나중에

아래 묶음은 자형이 비슷해 판독기가 자주 섞는 글자다. 이 묶음에 든 글자는 획을 확인한 뒤에 적는다.
자형 근거가 보이지 않거나 자형과 문맥이 어긋나면 〔자형 글자/문맥 글자〕처럼 두 후보를 적고 unc 에 남긴다. 문맥만으로 고쳐 적지 않는다.
- 入/人: 오른쪽 삐침(捺)이 위로 솟고 왼쪽 획이 그 아래에 붙으면 入, 왼쪽 삐침이 위에서 시작하면 人. 「入肝·入肺·能入」처럼 장부 앞은 入이 흔하지만 인쇄가 人이면 人으로 적고 〔人/入〕로 표시한다.
- 曰/日: 납작하고 가운데 획이 오른쪽 세로획에 닿지 않으면 曰(「…曰：」). 日華子·一日·每日의 日.
- 于/干/千: 于는 세로획 끝이 왼쪽 갈고리(亅), 干은 곧게 내려오고, 千은 첫 획이 삐침이다.
- 己/已/巳: 왼쪽 위가 열림/반쯤 닫힘/닫힘. 戊/戌/戍: 안이 빔/가로획/점. 未/末: 위 획이 짧으면 未. 土/士: 위 획이 길면 士.
- 白/自, 大/太/犬, 問/間, 几/凡, 午/牛, 刀/力, 卯/卵, 元/无, 火/灭, 今/令, 予/子, 失/尖, 山/由, 府/腐, 信/倍, 届/属, 镇/慎, 宜/宣, 囊/裹, 胀/脓, 恶/悉, 摄/慑, 源/原, 常/尝, 僻/澼, 浥/挹.
- 부수가 같고 성부만 다른 글자(疒: 瘕 疲 痼 痕 瘦 癰 痛 / 艹: 芩 苓 芪 菖 葛 茱 萸 / 木: 棱 枝 楂 枸 拘 / 月: 脘 院 腠): 성부를 획 단위로 확인한다. 약재명(三棱·黃芩·枸杞·石菖蒲·吳茱萸·黃芪)은 통째로 확인한다.
- 이체자·상용자로 바꾸지 않는다: 인쇄가 苢·濬·澼·浥·嘗이면 苡·浚·僻·挹·常으로 바꾸지 않는다(지침 21).
- 한글이 섞인 문헌에서 한문 문장 속 '의·이·고·가'는 的·二·古·加를 다시 본다. 인쇄된 글자대로 적는다.
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
        "- 특히 干 과 于 를 혼동하지 않는다. 于 는 둘째 가로획 아래 세로획이 왼쪽으로 꺾인 갈고리(亅)이고, 전치사(於의 간체: …에·…에서·…보다)로 "
        "동사·형용사 뒤에 온다(生于, 至于, 由于, 属于, 甚于, 走于, 从于). 干 은 세로획이 곧게 내려오며 '마르다·방패·관계하다·줄기·몇'의 뜻이다"
        "(干姜, 能干, 若干, 干涉, 干支). 자형과 문맥이 어긋나면 그 자리를 다시 보고, 확정하기 어려우면 〔干/于〕 처럼 두 후보를 적는다. "
        "日/曰, 己/已/巳, 土/士, 未/末, 白/自, 問/間, 大/太/犬 도 같은 식으로 다시 확인한다.\n"
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
    return load_guide().rstrip() + "\n\n" + OUTPUT_RULES + mode_note() + CONFUSION_CARD


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


# 3차 글자 판별 — 묶음별 자형 단서(간체·번체 섞어 찾는다)
GLYPH_HINTS = {
    frozenset("入人"): "오른쪽 삐침(捺)이 위로 솟고 왼쪽 획이 그 아래에 붙으면 入, 왼쪽 삐침이 위에서 시작하면 人",
    frozenset("曰日"): "납작하고 가운데 가로획이 오른쪽 세로획에 닿지 않으면 曰, 세로로 길고 가운데 획이 양쪽에 닿으면 日",
    frozenset("于干"): "둘째 가로획 아래 세로획이 왼쪽 갈고리(亅)로 꺾이면 于, 곧게 내려오면 干",
    frozenset("己已巳"): "왼쪽 위가 열려 있으면 己, 반쯤 닫혀 있으면 已, 닫혀 있으면 巳",
    frozenset("戊戌戍"): "안이 비어 있으면 戊, 짧은 가로획이 있으면 戌, 점이 있으면 戍",
    frozenset("未末"): "위 가로획이 아래보다 짧으면 未, 길면 末",
    frozenset("土士"): "위 가로획이 아래보다 짧으면 土, 길면 士",
    frozenset("白自"): "안에 가로획이 하나면 白, 둘이면 自",
    frozenset("大太犬"): "점이 없으면 大, 아래 가운데 점이면 太, 오른쪽 위 점이면 犬",
    frozenset("宜宣"): "宀 아래가 且 면 宜, 亘(위아래 가로획 사이에 日)이면 宣",
    frozenset("苡苢"): "艹 아래가 以 면 苡, 㠯(네모에 가까운 꼴)이면 苢",
}


def glyph_hint(candidates: list[str]) -> str:
    """후보 묶음에 맞는 자형 단서(없으면 빈 글)."""
    for k, v in GLYPH_HINTS.items():
        if all(any(c in k for c in cand) for cand in candidates if cand):
            return v
    return ""


# 3차 글자 판별 전용 시스템 지시(0.6.5 눈가림). 1·2차의 지침 전문·출력 규칙·혼동 카드는 넣지 않는다:
# 혼동 카드의 문맥 단서("장부 앞은 入이 흔하다")나 가로 모드의 于 용법 목록은 자형 질문에서 문맥으로 고르게 만든다.
GLYPH_SYSTEM = """# 3차 글자 판별 — 확대 조각의 자형만 본다

너는 옛 한자·한글 문헌의 인쇄 글자를 획 단위로 가리는 판독자다. 받는 이미지는 본문의 한 줄(세로쓰기면 한 열)에서 글자 몇 개만 크게 자른 조각이고, 그 가운데 한 글자를 순번으로 가리켜 묻는다.

## 원칙
- 판정의 기준은 조각에 실제로 보이는 획과 부수다. 앞뒤 문맥·흔한 낱말·전문용어로 고르지 않는다. 문장이 자연스럽다는 것은 근거가 아니다(지침 2).
- 이미지에서 그 자형을 확인할 수 없으면 확정하지 않는다(지침 7). 그때는 빈 답("")을 낸다. 빈 답은 실패가 아니라 원본 확인으로 넘기는 정상 결과다(지침 19).
- 인쇄된 꼴 그대로 답한다. 이체자·약자·간체를 상용자·번체로, 어려운 글자를 쉬운 글자로 바꾸지 않는다(지침 21).
- 확대는 보는 것을 돕는 수단일 뿐이다. 뭉개지거나 번진 획을 상상으로 채우지 않는다(지침 24).
- 가리킨 글자만 판정한다. 이웃 글자의 획을 섞지 않고, 조각 가장자리에 조금 걸친 이웃 글자 조각은 글자로 세지 않는다.
- 조각 바깥 흰 여백에 빨간 눈금이 있으면 그 눈금 옆 글자가 묻는 글자다. 눈금은 표시일 뿐 글자의 획이 아니다.

## 답하는 차례
1) w: 조각에 보이는 글자를 읽기 방향 차례로 모두 적는다(부호 포함, 가장자리에 반쯤 잘린 글자는 빼도 된다).
2) e: 묻는 글자에서 본 자형을 한 줄로 적는다(갈고리·점·삐침의 시작점·가로획 길이·닫힘 여부 등).
3) c: 판정한 글자. 후보가 주어지면 후보 가운데 하나만 적는다. 가릴 수 없거나 후보 모두 아니면 "".

## 혼동하기 쉬운 묶음의 자형 단서
"""


def glyph_system_prompt() -> str:
    """3차 판별 판독기의 시스템 지시(지침 2·7·19·21·24장 요지 + 묶음별 자형 단서). 짧아서 컨텍스트 캐시는 쓰지 않는다."""
    hints = "\n".join(f"- {'/'.join(sorted(k))}: {v}" for k, v in GLYPH_HINTS.items())
    return GLYPH_SYSTEM + hints + "\n"


def _pointer(ask, horizontal: bool) -> str:
    """질문 글자를 가리키는 말. 앞뒤 글자 대신 조각 안 순번(과 눈금)으로 가리킨다."""
    side = "왼쪽에서" if horizontal else "위에서"
    if not ask.pos:
        t = "조각 가운데 글자"
    elif ask.k == 0:
        t = f"조각 안 {side} {ask.pos}번째 글자부터 1~2자(후보마다 글자 수가 다르다)"
    elif ask.k > 1:
        t = f"조각 안 {side} {ask.pos}~{ask.pos + ask.k - 1}번째 {ask.k}글자(한 묶음으로 읽는다)"
    else:
        t = f"조각 안 {side} {ask.pos}번째 글자"
    if ask.count:
        t += f"(조각의 글자 {ask.count}자" + ("" if ask.measured else " 안팎") + ")"
    if ask.pos and not ask.measured:
        t += " — 순번은 어림이라 한 자쯤 어긋날 수 있다"
    if ask.tick:
        t += f" · {'위' if horizontal else '오른쪽'} 여백의 빨간 눈금 옆"
    return t


def _piece(horizontal: bool) -> str:
    return "가로쓰기 한 줄" if horizontal else "세로쓰기 한 열"


def _order(horizontal: bool) -> str:
    return "왼쪽→오른쪽" if horizontal else "위→아래"


def adjudicate_prompt(ask, horizontal: bool, pdf_page: int) -> str:
    """3차 판별 질문(눈가림): 앞뒤 글자를 주지 않고 조각 안 순번으로 가리킨다. 후보는 ask.candidates 차례(섞인 것) 그대로."""
    h = glyph_hint(list(ask.candidates))
    return (f"첨부 이미지는 PDF {pdf_page}쪽 본문의 {_piece(horizontal)}에서 글자 몇 개를 크게 자른 조각이다.\n"
            f"묻는 글자: {_pointer(ask, horizontal)}.\n"
            f"후보: 「{' / '.join(ask.candidates)}」. 자형만 보고 어느 것인지 가려라." + (f"\n자형 단서: {h}." if h else "") + "\n"
            f"w 에 조각의 글자를 {_order(horizontal)} 차례로 모두, e 에 묻는 글자에서 본 자형 한 줄, "
            "c 에 후보 가운데 그 글자 하나(가릴 수 없거나 후보 모두 아니면 \"\")를 적는다. JSON 으로만 답한다.")


def adjudicate_many_prompt(items: list, horizontal: bool, pdf_page: int) -> str:
    """묶음 3차 판별(눈가림). items = [(자리 번호, GlyphAsk), …] 순서대로 이미지가 붙고, 각 이미지 바로 앞에 【자리 번호】 표시가 있다."""
    lines = []
    for sid, ask in items:
        h = glyph_hint(list(ask.candidates))
        lines.append(f"- 【{sid}】 {_pointer(ask, horizontal)} · 후보 「{' / '.join(ask.candidates)}」" + (f" · 자형 단서: {h}" if h else ""))
    return (f"첨부한 이미지 {len(items)}장은 PDF {pdf_page}쪽 본문의 {_piece(horizontal)}에서 글자 몇 개씩 크게 자른 조각이다. "
            "각 이미지 바로 앞에 【자리 번호】 표시가 있다. 조각마다 묻는 글자와 후보:\n" + "\n".join(lines) + "\n"
            "조각마다 독립적으로, 자형만 보고 답하라. 다른 조각의 답을 참고하지 않는다.\n"
            f"items 에 조각마다 하나씩 넣는다: s 에 자리 번호를 그대로, w 에 그 조각의 글자를 {_order(horizontal)} 차례로 모두, "
            "e 에 묻는 글자에서 본 자형 한 줄, c 에 후보 가운데 그 글자 하나(가릴 수 없거나 후보 모두 아니면 \"\"). "
            "조각 수와 items 수가 같아야 한다. JSON 으로만 답한다.")


def identify_prompt(ask, horizontal: bool, pdf_page: int) -> str:
    """눈가림 확인 질문: 후보도 앞뒤 글자도 주지 않고 가리킨 글자를 그냥 읽게 한다(객관식 답과 맞춰 볼 독립 판독)."""
    return (f"첨부 이미지는 PDF {pdf_page}쪽 본문의 {_piece(horizontal)}에서 글자 몇 개를 크게 자른 조각이다.\n"
            f"묻는 글자: {_pointer(ask, horizontal)}.\n"
            "후보는 주지 않는다. 이 글자를 인쇄된 꼴 그대로 읽어라(이체자·간체를 바꾸지 않는다).\n"
            f"w 에 조각의 글자를 {_order(horizontal)} 차례로 모두, e 에 묻는 글자에서 본 자형 한 줄, "
            "c 에 그 글자(획이 뭉개져 읽을 수 없으면 \"\")를 적는다. JSON 으로만 답한다.")


def identify_many_prompt(items: list, horizontal: bool, pdf_page: int) -> str:
    """묶음 눈가림 확인. items = [(자리 번호, GlyphAsk), …] — 후보 없이 읽게 한다."""
    lines = [f"- 【{sid}】 {_pointer(ask, horizontal)}" for sid, ask in items]
    return (f"첨부한 이미지 {len(items)}장은 PDF {pdf_page}쪽 본문의 {_piece(horizontal)}에서 글자 몇 개씩 크게 자른 조각이다. "
            "각 이미지 바로 앞에 【자리 번호】 표시가 있다. 조각마다 묻는 글자:\n" + "\n".join(lines) + "\n"
            "후보는 주지 않는다. 조각마다 독립적으로, 그 글자를 인쇄된 꼴 그대로 읽어라(이체자·간체를 바꾸지 않는다).\n"
            f"items 에 조각마다 하나씩 넣는다: s 에 자리 번호를 그대로, w 에 그 조각의 글자를 {_order(horizontal)} 차례로 모두, "
            "e 에 묻는 글자에서 본 자형 한 줄, c 에 그 글자(읽을 수 없으면 \"\"). 조각 수와 items 수가 같아야 한다. JSON 으로만 답한다.")
