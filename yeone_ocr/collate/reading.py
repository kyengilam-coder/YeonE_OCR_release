"""한자 → 한글 독음. 구획 이름을 발음으로 찾을 수 있게 한다(예: '대계' → 大薊, '인삼' → 人參·人蔘).

- 독음은 hanja 패키지(없으면 변환 없음)로 얻는다. 낱말 첫머리의 두음법칙은 패키지가 처리한다(羅→나, 龍→용).
- 찾기는 글자(음절) 단위로 맞춘다. 여러 음을 가진 글자(參 참/삼, 樂 락/악/요, 說 설/열 …)는 어느 음이든 되고,
  낱말 가운데 글자도 두음법칙을 적용한 꼴(릉/능, 량/양)로 찾을 수 있다.
"""
from __future__ import annotations
import functools, re, unicodedata

# 본초·역학 문헌에서 흔한 여러 음 글자(기본 독음 외의 음). 표시에는 첫 음을 쓰고, 찾기에는 모두 허용한다.
MULTI = {
    "參": ["삼", "참"], "數": ["수", "삭", "촉"], "樂": ["락", "악", "요"], "說": ["설", "열", "세"], "車": ["차", "거"], "金": ["금", "김"],
    "復": ["복", "부"], "省": ["성", "생"], "切": ["절", "체"], "行": ["행", "항"], "降": ["강", "항"], "惡": ["악", "오"], "更": ["경", "갱"],
    "讀": ["독", "두"], "洞": ["동", "통"], "殺": ["살", "쇄"], "塞": ["색", "새"], "索": ["색", "삭"], "識": ["식", "지"], "什": ["십", "집"],
    "拾": ["십", "습"], "若": ["약", "야"], "易": ["역", "이"], "刺": ["자", "척"], "辰": ["진", "신"], "則": ["칙", "즉"], "宅": ["택", "댁"],
    "便": ["편", "변"], "暴": ["폭", "포"], "北": ["북", "배"], "不": ["불", "부"], "否": ["부", "비"], "度": ["도", "탁"], "狀": ["상", "장"],
    "宿": ["숙", "수"], "食": ["식", "사"], "見": ["견", "현"], "率": ["률", "솔"], "著": ["저", "착"], "茶": ["다", "차"], "糖": ["당", "탕"],
    "丹": ["단", "란"], "合": ["합", "홉"], "龜": ["귀", "구", "균"], "萆": ["비", "폐"], "薤": ["해", "회"], "蒺": ["질"], "藜": ["려", "리"],
    "柴": ["시", "채"], "荷": ["하"], "沈": ["침", "심"], "葉": ["엽", "섭"], "廓": ["곽", "확"], "滑": ["활", "골"], "乾": ["건", "간"],
    "朮": ["출", "술"], "苦": ["고"], "辛": ["신"], "蔘": ["삼"],
}
DISPLAY_FIX = {"參": "蔘"}     # 표시용 독음에서 參 은 삼(人參)으로 읽는다 — 蔘 으로 바꿔 넣으면 패키지가 '삼'으로 읽는다

_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"


def available() -> bool:
    try:
        import hanja  # noqa: F401
        return True
    except Exception:
        return False


def is_hanja(ch: str) -> bool:
    return "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿" or "豈" <= ch <= "﫿"


def is_hangul(ch: str) -> bool:
    return "가" <= ch <= "힣"


def reading(text: str) -> str:
    """표시용 독음. 한자가 아닌 글자는 그대로 둔다. 패키지가 없으면 원문을 돌려준다(캐시하지 않음)."""
    if not available():
        return text
    return _reading_cached(text)


@functools.lru_cache(maxsize=4096)
def _reading_cached(text: str) -> str:
    import hanja
    fixed = "".join(DISPLAY_FIX.get(ch, ch) for ch in unicodedata.normalize("NFC", text))
    try:
        return hanja.translate(fixed, "substitution")
    except Exception:
        return text


def _decompose(syl: str) -> tuple[int, int, int] | None:
    if not is_hangul(syl):
        return None
    code = ord(syl) - 0xAC00
    return code // 588, (code % 588) // 28, code % 28


def _compose(cho: int, jung: int, jong: int) -> str:
    return chr(0xAC00 + cho * 588 + jung * 28 + jong)


def dueum_variants(syl: str) -> set[str]:
    """두음법칙 변형: 릉→능, 량→양, 녀→여, 리→이 … 와 그 역(능→릉, 양→량, 여→녀·려, 이→리·니)."""
    out = {syl}
    d = _decompose(syl)
    if not d:
        return out
    cho, jung, jong = d
    yv = {_JUNG.index(v) for v in "ㅑㅒㅕㅖㅛㅠㅣ"}       # ㅣ·y 계열 모음
    av = {_JUNG.index(v) for v in "ㅏㅐㅓㅔㅗㅜㅡㅚㅘㅙㅝㅞㅟㅢ"}
    r, n, o = _CHO.index("ㄹ"), _CHO.index("ㄴ"), _CHO.index("ㅇ")
    if cho == r:
        out.add(_compose(o if jung in yv else n, jung, jong))
        if jung in yv: out.add(_compose(n, jung, jong))
    elif cho == n:
        out.add(_compose(r, jung, jong))
        if jung in yv: out.add(_compose(o, jung, jong))
    elif cho == o:
        out.add(_compose(r, jung, jong))
        if jung in yv: out.add(_compose(n, jung, jong))
    return out


def char_readings(ch: str) -> tuple[str, ...]:
    if is_hanja(ch) and not available():
        return (ch,)
    return _char_readings_cached(ch)


@functools.lru_cache(maxsize=8192)
def _char_readings_cached(ch: str) -> tuple[str, ...]:
    """글자 하나의 가능한 음들(두음법칙 변형 포함). 한글은 자기 자신(+두음 변형), 그 밖은 자기 자신."""
    if is_hangul(ch):
        return tuple(sorted(dueum_variants(ch)))
    if not is_hanja(ch):
        return (ch,)
    alts: list[str] = []
    base = reading(ch)
    if base and base != ch and len(base) == 1:
        alts.append(base)
    for a in MULTI.get(ch, []):
        if a not in alts:
            alts.append(a)
    if not alts:
        return (ch,)
    out: set[str] = set()
    for a in alts:
        out |= dueum_variants(a)
    return tuple(sorted(out))


def _syllables(s: str) -> list[str]:
    """비교에 쓸 글자들: 한자·한글만(부호·공백·숫자·로마자는 뺀다)."""
    return [ch for ch in unicodedata.normalize("NFC", s) if is_hanja(ch) or is_hangul(ch)]


def matches(name: str, query: str) -> bool:
    """query(한글 독음, 또는 한자·한글 섞임)가 name 의 어느 연속 구간의 독음과 맞는가. 음절 단위로 비교한다."""
    q = _syllables(query)
    if not q:
        return False
    n = _syllables(name)
    k = len(q)
    if k > len(n):
        return False
    qsets = [set(char_readings(ch)) for ch in q]
    nsets = [set(char_readings(ch)) for ch in n]
    for i in range(len(n) - k + 1):
        if all(qsets[j] & nsets[i + j] for j in range(k)):
            return True
    return False


# ---------- 이름끼리 맞추기(한자 ↔ 한글 독음, 한자 ↔ 한자, 한글 ↔ 한글) ----------
def char_eq(a: str, b: str) -> bool:
    """글자 하나씩: 둘 다 한자면 간체·이체자를 접어 같은 글자인지, 한쪽이 한글이면 그 한글이 한자의 음(두음 변형 포함)인지,
    둘 다 한글이면 두음 변형끼리 겹치는지. (한자끼리는 음이 같아도 다른 글자면 다르다 — 大薊 ≠ 大計)"""
    if a == b:
        return True
    ha, hb = is_hanja(a), is_hanja(b)
    if ha and hb:
        from . import zh
        return zh.compare_key(a) == zh.compare_key(b)
    if ha or hb:
        if not available():
            return False
        hanja_ch, hangul_ch = (a, b) if ha else (b, a)
        return is_hangul(hangul_ch) and bool(dueum_variants(hangul_ch) & set(char_readings(hanja_ch)))
    return bool(dueum_variants(a) & dueum_variants(b))


def seq_equal(x: list[str], y: list[str]) -> bool:
    return len(x) == len(y) and all(char_eq(a, b) for a, b in zip(x, y))


def seq_find(hay: list[str], needle: list[str]) -> int:
    """needle 이 hay 의 연속 구간과 맞으면 그 시작 위치, 아니면 -1."""
    k = len(needle)
    if k == 0 or k > len(hay):
        return -1
    for i in range(len(hay) - k + 1):
        if all(char_eq(hay[i + j], needle[j]) for j in range(k)):
            return i
    return -1


def syllables(s: str) -> list[str]:
    return _syllables(s)
