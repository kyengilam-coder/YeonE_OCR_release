"""Editable literal Hanja output rules; macro code is never executed."""
from __future__ import annotations

import json
from pathlib import Path
import unicodedata

MODES = {"auto": "자동 치환", "review": "문맥 확인", "leaf": "문맥 자동(叶→葉)", "yu": "문맥 자동(于→於·이름 보존)"}
MODES['xian']='문맥 자동(咸→鹹·맛 표현)'
MODES['remainder']='문맥 판별(余/餘)'
MODES['atractylodes']='문맥 판별(術/朮)'
MODES.update(yun='문맥 판별(云/雲)', song='문맥 판별(松/鬆)', gan='문맥 판별(干/乾/幹)', zhu='문맥 판별(着/著)', ji='문맥 판별(极/極)', sheng='문맥 판별(胜/勝)')
CONTEXT_PAIRS = {'leaf':('叶','葉'),'yu':('于','於'),'xian':('咸','鹹'),
                 'yun':('云','雲'),'song':('松','鬆'),'gan':('干','乾'),'zhu':('着','著'),'ji':('极','極'),'sheng':('胜','勝'),'remainder':('余','餘'),
                 'atractylodes':('術','朮')}
RULE_GROUPS = ('전체', '문맥 판별 규칙', '자동 치환 규칙')
CONTEXT_HELP={
 'atractylodes':'術/朮: 백출·창출과 두 약재의 총칭에는 朮을 쓰고, 醫術·方術처럼 기술·방법을 뜻하면 術을 씁니다.',
 'remainder':'余/餘: 남음·나머지·수량의 초과는 餘, 나(1인칭)·성씨·인명은 余로 문맥에 따라 판별합니다. 미확정이면 원문을 보존합니다.',
 'yun':'云/雲: 인용·말하다와 구름·고유명사를 전체 문맥에서 판별합니다.',
 'song':'松/鬆: 根中虛松→根中虛鬆, 其體极松→其體極鬆. 松柏·松脂는 보존합니다.',
 'gan':'干/乾/幹: 咽干→咽乾, 成根荄枝干→成根荄枝幹. 干涉·干戈는 보존합니다.',
 'zhu':'着/著: 着作→著作, 着衣·附着은 보존합니다. 애매하면 전체 문맥에서 판별합니다.',
 'ji':'极/極: 此藥質极黏膩→此藥質極黏膩. 정도가 매우 심하다는 뜻을 판별합니다.',
 'sheng':'胜/勝: 거의 대부분 勝이지만 인접 글자로 자동 변환하지 않고 전체 문맥에서 판별합니다. 제어·극복·감당은 勝, 胜 자체의 뜻이면 胜을 유지하며 미확정이면 원문을 보존합니다.',
 'yu':'于/於: 고전 일반 문장은 於, 이름은 보존합니다. 1900년 이후·백화문 책별 설정은 于를 유지합니다.',
 'xian':'咸/鹹: 맛·짠 성미는 鹹, 이름·지명과 모두/다의 뜻은 보존합니다.',
 'leaf':'叶/葉: 잎을 뜻하는 편집형 간체는 葉, 叶韻 등의 표기는 보존합니다.',
 'review':'뜻이 달라질 수 있는 후보입니다. 앞뒤 전체 문맥에서 판별하며, 판별이 없거나 미확정이면 원문을 보존합니다.'}
DEFAULT_PHRASE_FORMS = {"本草征要": "本草徵要", "炮制": "炮製", "干姜": "乾薑", "干薑": "乾薑", "髒": "臟", "血之余": "血之餘", "有余之火": "有餘之火"}


def rule_group(rule):
    """Every semantic/review rule belongs to the same separately managed group."""
    return '자동 치환 규칙' if rule['mode']=='auto' else '문맥 판별 규칙'


def validate_hanja_rules(value) -> list[dict]:
    if isinstance(value, dict):
        value = value.get("rules")
    if not isinstance(value, list) or len(value) > 2048:
        raise ValueError("한자 규칙은 2,048개 이하의 목록이어야 합니다.")
    result, sources, identifiers = [], set(), set()
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict) or set(item) - {"id", "find", "replace", "mode"}:
            raise ValueError(f"한자 규칙 {index}: find·replace·mode만 지정하십시오.")
        find, replace, mode = item.get("find"), item.get("replace"), item.get("mode", "review")
        if (not isinstance(find, str) or not find or len(find) > 80
                or not isinstance(replace, str) or len(replace) > 80
                or any(unicodedata.category(c) in {"Cc", "Cs"} for c in find + replace)):
            raise ValueError(f"한자 규칙 {index}: 찾을 글자는 필수이며 각 문자열은 80자 이하여야 합니다.")
        if not any(unicodedata.category(c)[0] in "LN" for c in find + replace):
            raise ValueError(f"한자 규칙 {index}: 부호만 바꾸는 규칙은 중문부호 설정에서 편집하십시오.")
        if mode not in MODES or (mode in CONTEXT_PAIRS and (find,replace)!=CONTEXT_PAIRS[mode]):
            raise ValueError(f"한자 규칙 {index}: 적용 방법이 올바르지 않습니다. 문맥 자동은 해당 글자쌍 전용입니다.")
        if find in sources:
            raise ValueError(f"한자 규칙 {index}: 같은 찾을 글자가 이미 있습니다. 기존 줄을 수정하십시오: {find}")
        identifier = item.get("id", 1_000_000 + index)
        if type(identifier) is not int or not 1 <= identifier <= 2_147_483_647 or identifier in identifiers:
            raise ValueError(f"한자 규칙 {index}: 규칙 번호가 중복되었거나 올바르지 않습니다.")
        sources.add(find); identifiers.add(identifier)
        result.append(dict(id=identifier, find=find, replace=replace, mode=mode))
    return result


def default_hanja_rules() -> list[dict]:
    from .korean_forms import KOREAN_FORMS
    from .collate.zh import ambiguous_map
    from .normalization import rule_data, _glyph_rule, _punctuation_rule
    ambiguous = ambiguous_map()
    rows = [dict(id=100_000 + ord(a), find=a, replace=b,
                 mode="review" if a in ambiguous else "auto") for a, b in KOREAN_FORMS.items()]
    seen = {r["find"] for r in rows}
    for rule in rule_data()["rules"]:
        if _punctuation_rule(rule) or rule["find"] == rule["replace"] or rule["find"] in seen:
            continue
        rows.append(dict(id=rule["id"], find=rule["find"], replace=rule["replace"],
                         mode="auto" if _glyph_rule(rule) else "review"))
        seen.add(rule["find"])
    for i,(mode,(source,target)) in enumerate(CONTEXT_PAIRS.items()):
        old=next((r for r in rows if r['find']==source),None)
        if old is not None:old.update(replace=target,mode=mode)
        else:rows.append(dict(id=900_100 if mode=='atractylodes' else 900_000+i,find=source,replace=target,mode=mode))
    for i,(source,target) in enumerate(DEFAULT_PHRASE_FORMS.items()):
        old=next((r for r in rows if r['find']==source),None)
        if old is not None:old.update(replace=target,mode='auto')
        else:rows.append(dict(id=900_010+i,find=source,replace=target,mode='auto'))
    return validate_hanja_rules(rows)


def load_hanja_rules(path: str | Path) -> list[dict]:
    path = Path(path)
    try:
        if path.stat().st_size > 2_000_000:
            raise ValueError("한자 규칙 파일이 너무 큽니다.")
        return validate_hanja_rules(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as exc:
        raise ValueError(f"한자 변환 규칙을 읽을 수 없습니다: {path.name} ({exc})") from exc


def save_hanja_rules(path: str | Path, value) -> Path:
    from .state import write_atomic
    path = Path(path).expanduser().resolve()
    rules = validate_hanja_rules(value)
    if path.suffix.lower() != ".json":
        raise ValueError("한자 변환 규칙은 JSON 파일로 저장하십시오. 원본 MSR은 변경하지 않습니다.")
    if path.exists():
        # A save dialog must never turn an OCR project/config into rule data.
        load_hanja_rules(path)
    write_atomic(path, json.dumps({"schema_version": 1, "rules": rules}, ensure_ascii=False, indent=2) + "\n")
    return path


def import_msr_hanja_rules(path: str | Path) -> list[dict]:
    from .normalization import extract_msr_rules, _glyph_rule, _punctuation_rule
    path = Path(path)
    if path.stat().st_size > 2_000_000:
        raise ValueError("MSR 파일이 너무 큽니다.")
    parsed = extract_msr_rules(path.read_bytes(), path.name)
    rows = default_hanja_rules()
    positions = {r["find"]: i for i, r in enumerate(rows)}
    for rule in parsed["rules"]:
        if _punctuation_rule(rule) or rule["find"] == rule["replace"]:
            continue
        row = dict(find=rule["find"], replace=rule["replace"], mode="auto" if _glyph_rule(rule) else "review")
        if row["find"] in positions:
            at = positions[row["find"]]; row["id"] = rows[at]["id"]
            prior=rows[at]['mode']
            if prior in CONTEXT_PAIRS and (row['find'],row['replace'])==CONTEXT_PAIRS[prior]:row['mode']=prior
            rows[at] = row
        else:
            row["id"] = max(r["id"] for r in rows) + 1; positions[row["find"]] = len(rows); rows.append(row)
    return validate_hanja_rules(rows)
