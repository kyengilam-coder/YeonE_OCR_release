"""간체·번체 변환과 글자 분류. OpenCC(opencc-python-reimplemented)를 쓰고, 없으면 변환 없이 동작한다.

비교는 간체 공간에서 한다(번체→간체는 거의 1:1 로 확정적). 교정 글자를 만들 때만 간체→번체가 필요하고,
이때 한 글자가 여러 번체로 갈라지는 글자(发→發/髮, 术→術/朮 …)는 어구 사전으로 고르되 '1:多' 로 표시해 사람이 확인하게 한다.
"""
from __future__ import annotations
import functools, os, unicodedata

# 한국 문헌에서 흔한 이체자 → 비교용 대표 글자(비교 전에 양쪽 모두 적용). 표시는 D(표기 차이)로만 남긴다.
VARIANTS = {
    "蔘": "參", "裡": "裏", "為": "爲", "説": "說", "衆": "眾", "麪": "麵", "畵": "畫", "羣": "群", "鷄": "雞",
    "敎": "教", "眞": "真", "靑": "青", "淸": "清", "卽": "即", "槪": "概", "飮": "飲", "册": "冊", "甁": "瓶", "硏": "研",
    "祕": "秘", "絶": "絕", "囘": "回", "鉤": "鈎", "僞": "偽", "虛": "虚", "溫": "温", "況": "况", "歿": "殁", "凉": "涼",
    "冲": "沖", "刄": "刃", "勳": "勛", "恒": "恆", "緖": "緒", "隣": "鄰", "吳": "吴", "決": "决", "淚": "泪", "臺": "台",
    "幷": "並", "竝": "並", "査": "查", "産": "產", "尙": "尚", "旣": "既", "兪": "俞", "兎": "兔", "隷": "隸", "黃": "黄", "黑": "黑",
    # 0.6.0 — 중의역대약론선 대조 장부에서 "확실한 오탈(A)"로 잘못 잡힌 이체자(한국식 표기 ↔ 인쇄 글자). 양쪽에 똑같이 접으므로
    # 沈(성씨)·着(붙다)처럼 두 뜻이 있는 글자도 비교에서는 같은 글자로 본다(표기 차이 D 로만 남는다).
    "澁": "澀", "沈": "沉", "汚": "污", "昻": "昂", "濬": "浚", "擧": "舉", "脇": "脅", "袪": "祛", "塡": "填", "巓": "巔",
    "愼": "慎", "蔲": "蔻", "苢": "苡", "着": "著",
}


_CC_CACHE: dict[str, object] = {}


def _cc(kind: str):
    """OpenCC 변환기. 패키지가 없으면 None — 실패는 캐시하지 않아 설치 직후부터 쓰인다."""
    if kind in _CC_CACHE:
        return _CC_CACHE[kind]
    try:
        import opencc
        c = opencc.OpenCC(kind)
    except Exception:
        return None
    _CC_CACHE[kind] = c
    return c


def available() -> bool:
    return _cc("t2s") is not None


def to_simplified(s: str) -> str:
    c = _cc("t2s")
    return c.convert(s) if c else s


def to_traditional(s: str) -> str:
    """어구 사전을 쓰는 변환(白术→白朮, 其后→其後). 1:多 글자는 문맥에 따라 고른다."""
    c = _cc("s2t")
    return c.convert(s) if c else s


@functools.lru_cache(maxsize=None)
def ambiguous_map() -> dict[str, list[str]]:
    """간체 한 글자가 여러 번체로 갈라지는 글자 → 후보 목록. OpenCC 사전(STCharacters)에서 읽는다."""
    try:
        import opencc
        path = os.path.join(os.path.dirname(opencc.__file__), "dictionary", "STCharacters.txt")
        out = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                k, _, v = line.rstrip("\n").partition("\t")
                vs = v.split()
                if len(vs) > 1:
                    out[k] = vs
        return out
    except Exception:
        return {}


def is_ambiguous(ch: str) -> bool:
    return ch in ambiguous_map()


def candidates(ch: str) -> list[str]:
    """간체 글자 하나의 번체 후보들(1:1 이면 한 개)."""
    m = ambiguous_map()
    if ch in m:
        return list(m[ch])
    t = to_traditional(ch)
    return [t] if t else [ch]


def fold_variants(s: str) -> str:
    return "".join(VARIANTS.get(ch, ch) for ch in s)


def compare_key(s: str) -> str:
    """비교용 글자: NFC → 이체자 접기 → 간체. 양쪽(HWPX·OCR)에 똑같이 적용한다."""
    return to_simplified(fold_variants(unicodedata.normalize("NFC", s)))


def is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿" or "豈" <= ch <= "﫿"
