"""출력 문서 전용 정자·부호 정리. 판독 기록은 그대로 두고 Block 사본만 바꾼다.

순서: OpenCC s2t → 사용자가 제공한 MSR의 AllReplace 순서 → 云/雲 문맥 판별.
MSR은 실행하지 않고 정적 문자열/플래그만 추출한다. 모든 변경과 보류를 보고한다.
표시 괄호 〔…〕, 작업 기록, 메모의 원문 증거는 정리 대상이 아니다.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
import hashlib
import json
import re
from pathlib import Path
from typing import Callable
import unicodedata

from .blocks import Block

RULE_FILE = Path(__file__).with_name("data") / "normalization_260929.json"
RULE_VERSION = "260929"
MARK_RE = re.compile(r"〔[^〔〕]*〕")
# 匹配大小寫는 원본 매크로가 0으로 지정: O뿐 아니라 o도 대상이다.
_SAFE_FLAGS = {
    "MatchCase": 0, "AllWordForms": 0, "SeveralWords": 0, "UseWildCards": 0,
    "WholeWordOnly": 0, "AutoSpell": 1, "Direction": 'FindDir("AllDoc")',
    "IgnoreFindString": 0, "IgnoreReplaceString": 0, "ReplaceMode": 1,
    "IgnoreMessage": 1, "HanjaFromHangul": 0, "FindJaso": 0,
    "FindRegExp": 0, "FindStyle": "", "ReplaceStyle": "", "FindType": 1,
}


def extract_msr_rules(data: bytes, source_name: str) -> dict:
    """Hancom 스크립트 매크로에서 치환 데이터만 읽는다. JS/HAction은 절대 실행하지 않는다.

    제공된 2023년식 파일의 리터럴 문법만 허용한다. 알 수 없는 동작/정규식/서식은
    오류로 알리므로 '비슷한' 파이썬 정규식으로 바꾸어 원문을 훼손하지 않는다.
    연속 Execute(매크로에 실제로 2곳 있음)도 생략하지 않는다.
    """
    start = data.find("function ".encode("utf-16le"))
    if start < 0:
        raise ValueError("MSR에서 UTF-16LE 스크립트를 찾지 못했습니다.")
    source = data[start:].decode("utf-16le").rstrip("\x00").replace("\r", "")
    token = re.compile(
        r'HAction\.GetDefault\("AllReplace",\s*HParameterSet\.HFindReplace\.HSet\);'
        r'|with\s*\(HParameterSet\.HFindReplace\)\s*\{(?P<body>[^{}]*)\}'
        r'|HAction\.Execute\("AllReplace",\s*HParameterSet\.HFindReplace\.HSet\);', re.S)
    assign = re.compile(r'\s*(\w+)\s*=\s*("(?:\\.|[^"\\])*"|[^;]+);')
    current = None
    block = 0
    rules = []
    remainder = token.sub("", source)
    # 함수 이름/선언과 괄호 외 실행문이 남으면 거부한다.
    remainder = re.sub(r"\A\s*function\s+[^\s()]+\s*\(\s*\)\s*\{", "", remainder)
    if remainder.strip() != "}":
        raise ValueError("지원하지 않는 MSR 동작이 있습니다. 매크로는 실행하지 않았습니다.")
    for match in token.finditer(source):
        body = match.group("body")
        if body is not None:
            values = {}
            pos = 0
            for item in assign.finditer(body):
                if body[pos:item.start()].strip():
                    raise ValueError("지원하지 않는 MSR 매개 변수 표현식입니다.")
                name, value = item.groups()
                if value.startswith('"'):
                    value = json.loads(value)  # 자료 문자열, eval을 쓰지 않는다.
                elif value.isdigit():
                    value = int(value)
                values[name] = value
                pos = item.end()
            if body[pos:].strip():
                raise ValueError("MSR 매개 변수 블록을 완전히 읽지 못했습니다.")
            flags = {k: v for k, v in values.items() if k not in ("FindString", "ReplaceString")}
            if flags != _SAFE_FLAGS:
                raise ValueError(f"지원하지 않는 MSR 검색/서식 플래그: {flags}")
            if not isinstance(values.get("FindString"), str) or not values["FindString"]:
                raise ValueError("MSR의 찾을 문자열이 비어 있습니다.")
            if not isinstance(values.get("ReplaceString"), str):
                raise ValueError("MSR의 바꿀 문자열이 없습니다.")
            block += 1
            current = dict(source_block=block, find=values["FindString"], replace=values["ReplaceString"])
        elif ".GetDefault" in match.group():
            current = None
        else:
            if current is None:
                raise ValueError("MSR 치환 동작 앞에 검색 설정이 없습니다.")
            rules.append(dict(id=len(rules) + 1, **current))
    return {
        "source_name": source_name, "sha256": hashlib.sha256(data).hexdigest(),
        "encoding": "utf-16le", "script_offset": start, "parameter_blocks": block,
        "flags": _SAFE_FLAGS, "rules": rules,
    }


@lru_cache(maxsize=1)
def rule_data() -> dict:
    data = json.loads(RULE_FILE.read_text(encoding="utf-8"))
    if data.get("flags") != _SAFE_FLAGS or not data.get("rules"):
        raise ValueError("동봉된 MSR 치환 데이터가 손상되었거나 지원되지 않습니다.")
    return data


def _opencc_converter() -> Callable[[str], str]:
    from .collate.zh import _cc
    converter = _cc("s2t")
    if converter is None:
        raise RuntimeError("간체→번체 자동 정리에 OpenCC가 필요합니다. 패키지 설치·점검을 실행하십시오.")
    return converter.convert


@dataclass
class TextResult:
    text: str
    # 출력 글자마다 입력 글자 번호. 부호 뒤에 생긴 공백은 그 부호와 같은 번호.
    origins: list[int]
    changes: list[dict] = field(default_factory=list)
    review: list[dict] = field(default_factory=list)
    proposals: list[dict] = field(default_factory=list)
    source_text: str = ""
    location: dict = field(default_factory=dict)


def _change(text: str, origins: list[int], begin: int, end: int, after: str,
            stage: str, **detail) -> dict:
    return dict(stage=stage, start=origins[begin] if begin < len(origins) else len(origins),
                end=(origins[end - 1] + 1) if end > begin else begin,
                before=text[begin:end], after=after,
                context=text[max(0, begin - 16):min(len(text), end + 16)], **detail)


def _replace_spans(result: TextResult, replacements: list[tuple[int, int, str, dict]], stage: str) -> None:
    """같은 입력 글에서 찾은 겹치지 않는 변경을 적용하며 원문 자리도 같이 옮긴다."""
    text, origins = result.text, result.origins
    parts, mapped = [], []
    pos = 0
    for begin, end, after, detail in replacements:
        parts.append(text[pos:begin]); mapped.extend(origins[pos:begin])
        before = text[begin:end]
        if before != after:
            result.changes.append(_change(text, origins, begin, end, after, stage, **detail))
        parts.append(after)
        width = end - begin
        mapped.extend(origins[begin + min(i, width - 1)] for i in range(len(after)))
        pos = end
    parts.append(text[pos:]); mapped.extend(origins[pos:])
    result.text, result.origins = "".join(parts), mapped


def _is_hangul(ch: str) -> bool:
    return bool(ch and ("가" <= ch <= "힣" or "ᄀ" <= ch <= "ᇿ" or "ㄱ" <= ch <= "ㆎ"))


def _guard_reason(text: str, begin: int, end: int, find: str) -> str:
    left = text[begin - 1] if begin else ""
    right = text[end] if end < len(text) else ""
    if find == "O" and any(ch and (ch.isascii() and (ch.isalnum() or ch == "_")) for ch in (left, right)):
        return "영문 단어·식별자 안의 O/o를 온점으로 바꾸지 않음"
    if find in ("고", "ᄉ", "ᄎ") and any(_is_hangul(ch) for ch in (left, right)):
        return "한국어 단어·어미 안의 한글을 한자로 바꾸지 않음"
    return ""


# 의미가 비교적 분명한 고전 인용 구문과 구름 어휘를 작은 규칙으로 한정한다.
# 이름·지명은 자연/기상 의미가 아니어도 雲을 쓰므로 별도로 보존한다.
_CLOUD_PHRASES = (
    "白雲", "黑雲", "烏雲", "浮雲", "青雲", "靑雲", "碧雲", "彩雲", "彤雲", "陰雲", "濃雲",
    "煙雲", "風雲", "雲雨", "雲霧", "雲霞", "雲氣", "雲煙", "雲海", "雲層", "雲朵", "雲天",
    "雲散", "雲集", "雲湧", "雲涌", "雲起", "雲生", "雲飛", "雲遊", "雲水", "雲端", "雲中",
    "雲外", "雲間", "雲開", "雲密", "雲覆", "雲蒸", "雲母", "雲南", "雲貴", "趙雲", "紀昀",
    "密雲", "行雲", "騰雲", "乘雲", "望雲", "凌雲", "卷雲", "積雲", "層雲", "捲雲",
)
_SPEECH_PHRASES = ("云云", "人云亦云", "不知所云", "詩云", "書云", "經云", "傳云", "論云", "註云",
                   "注云", "疏云", "故云", "又云", "或云", "所云", "古人云", "古語云", "俗云",
                   "諺云", "醫云", "師云", "曰云", "云爾", "云者", "云何")
_CITE_END = re.compile(r"(?:『[^『』]{1,48}』|《[^《》]{1,48}》|[「“\"][^」”\"]{1,48}[」”\"]|"
                       r"(?:孔子|孟子|老子|莊子|仲景|景岳|景嶽|東垣|丹溪|朱子|程子|先儒|內經|素問|靈樞|本草|難經))$")


def _phrase_covers(text: str, offset: int, phrases: tuple[str, ...], canonical: str) -> list[str]:
    folded = text.replace("云", canonical).replace("雲", canonical)
    hits = []
    for phrase in phrases:
        pattern = phrase.replace("云", canonical).replace("雲", canonical)
        begin = max(0, offset - len(pattern) + 1)
        end = min(len(text), offset + len(pattern))
        for match in re.finditer(re.escape(pattern), folded[begin:end]):
            if begin + match.start() <= offset < begin + match.end():
                hits.append(phrase)
    return hits


def _yun_choice(text: str, offset: int) -> tuple[str | None, str]:
    weather = _phrase_covers(text, offset, _CLOUD_PHRASES, "雲")
    speech = _phrase_covers(text, offset, _SPEECH_PHRASES, "云")
    before = text[:offset].rstrip()
    if _CITE_END.search(before):
        speech.append("인명·문헌명 뒤 인용 서술")
    if weather and speech:
        return None, "구름/인용 문맥이 겹침: " + ", ".join(weather + speech)
    if weather:
        return "雲", "구름·기상·고유명 어휘: " + ", ".join(weather)
    if speech:
        return "云", "말하다·인용 구문: " + ", ".join(speech)
    return None, "云/雲을 확정할 문맥 근거가 부족하여 원래 글자를 보존함"


def _punctuation_rule(rule: dict) -> bool:
    return all(unicodedata.category(ch)[0] in "PS" or ch.isspace()
               for ch in rule["find"] + rule["replace"])


def _semantic_proposals(result: TextResult, original: str, start: int, converted: str, rules: list[dict], ambiguous: dict):
    """MSR 한자 치환은 답이 아닌 후보. 겹치는 단자/어구 규칙은 넓은 문맥 한 건으로 묶는다."""
    pending = []
    baseline = list(result.text)
    for pos, source in enumerate(result.origins):
        old = original[source - start]
        if old in ambiguous and old not in "云雲" and result.text[pos] == old:
            choices = list(dict.fromkeys([old] + ambiguous[old]))
            # OpenCC 후보에 적용 가능한 매크로 이체자도 같은 문맥 판정에서 고르게 한다.
            for rule in rules:
                if rule["find"] in choices and rule["replace"] not in choices:
                    choices.append(rule["replace"])
            pending.append(dict(begin=pos, end=pos + 1, candidates=choices, stage="traditional_context", rule_ids=[],
                                simplified=old if old not in ambiguous[old] else ""))
            if len(converted) == len(original):
                baseline[pos] = converted[source - start]
    baseline = "".join(baseline)
    for rule in rules:
        find, replace = rule["find"], rule["replace"]
        if find == replace or _punctuation_rule(rule):
            continue
        seen = set()
        for source_text in (result.text, baseline):
            for match in re.finditer(re.escape(find), source_text, re.IGNORECASE):
                begin, end = match.span()
                if (begin, end) in seen:
                    continue
                seen.add((begin, end))
                reason = _guard_reason(result.text, begin, end, find)
                if reason:
                    result.review.append(_change(result.text, result.origins, begin, end, replace,
                                                 "msr_guard", rule_id=rule["id"], reason=reason))
                else:
                    choices = list(dict.fromkeys([result.text[begin:end], source_text[begin:end], replace]))
                    pending.append(dict(begin=begin, end=end, candidates=choices, stage="msr_context", rule_ids=[rule["id"]]))
    # 겹치는 단자·어구를 한 질문으로 만든다. 각 후보의 나머지 글자는 현재 원문 그대로다.
    groups = []
    for item in sorted(pending, key=lambda p: (p["begin"], p["end"])):
        if groups and item["begin"] < max(p["end"] for p in groups[-1]):
            groups[-1].append(item)
        else:
            groups.append([item])
    for group in groups:
        begin, end = min(p["begin"] for p in group), max(p["end"] for p in group)
        source = result.text[begin:end]
        choices, ids = [source], []
        for item in group:
            for candidate in item["candidates"]:
                choice = source[:item["begin"] - begin] + candidate + source[item["end"] - begin:]
                if choice not in choices:
                    choices.append(choice)
            ids.extend(item["rule_ids"])
        # 한 어구에 둘 이상의 중의적 글자가 있으면 각 글자를 동시에 바꾼 조합도 포함한다.
        # 중첩된 어구/단자 규칙은 한 경로에서 겹쳐 적용하지 않는다.
        variants = {""}
        paths = {begin: variants}
        for position in range(begin, end):
            for prefix in tuple(paths.get(position, ())):
                paths.setdefault(position + 1, set()).add(prefix + result.text[position])
                for item in group:
                    if item["begin"] == position:
                        destination = paths.setdefault(item["end"], set())
                        destination.update(prefix + c for c in item["candidates"])
        for choice in sorted(paths.get(end, ())):
            if choice not in choices:
                choices.append(choice)
        simplified = [(p["begin"] - begin, p["simplified"]) for p in group if p.get("simplified")]
        # 发/术 등 간체 전용 글자는 '그대로'를 성공한 번체 판정으로 인정하지 않는다.
        # 모델은 번체 후보를 고르거나 응답을 비워 보류한다. 干/后/台 같은 번체도 되는 글자는 허용.
        choices = [c for c in choices if not any(i < len(c) and c[i] == old for i, old in simplified)]
        if choices and (len(choices) > 1 or choices[0] != source):
            result.proposals.append(dict(begin=begin, end=end, source=source, candidates=choices,
                                         stage="msr_context" if ids else "traditional_context", rule_ids=sorted(set(ids))))
        elif not choices:
            result.review.append(_change(result.text, result.origins, begin, end, source, "context_unresolved",
                                         reason="겹치는 간체 후보를 확정하지 못하여 원문을 보존함", rule_ids=sorted(set(ids))))


def _normal_plain(text: str, start: int, converter: Callable[[str], str], rules: list[dict]) -> TextResult:
    from .collate.zh import ambiguous_map
    ambiguous = ambiguous_map()
    result = TextResult(text, list(range(start, start + len(text))))
    converted = converter(text)
    replacements = []
    # 1:多 글자와 云/雲은 원래 글자를 보존하고 아래에서 문맥 후보로 보낸다.
    if len(text) == len(converted):
        after = "".join(old if old in "云雲" or old in ambiguous else new for old, new in zip(text, converted))
        # 반복 글자도 같은 위치끼리 대응한다. SequenceMatcher는 雲云 같은 경우 글자를
        # 삽입/삭제로 정렬할 수 있어, 길이가 같은 정자 변환에는 쓰지 않는다.
        runs = re.finditer(r"1+", "".join("1" if a != b else "0" for a, b in zip(text, after)))
        replacements = [(m.start(), m.end(), after[m.start():m.end()], {}) for m in runs]
        opcodes = []
    else:
        opcodes = SequenceMatcher(None, text, converted, autojunk=False).get_opcodes()
    for tag, a, b, c, d in opcodes:
        if tag == "equal":
            continue
        after = converted[c:d]
        if b - a == d - c:
            after = "".join(old if old in "云雲" or old in ambiguous else new for old, new in zip(text[a:b], after))
        if a == b:
            # 중국어 정자 변환기는 보통 글을 삽입하지 않는다. 미지원 결과를 통째로 버리기보다 명시적으로 거부.
            raise ValueError("정자 변환기가 원문에 없는 삽입을 반환했습니다.")
        replacements.append((a, b, after, {}))
    _replace_spans(result, replacements, "opencc_s2t")
    for rule in rules:
        find, replace = rule["find"], rule["replace"]
        if find == replace or not _punctuation_rule(rule):
            continue
        matches = []
        for match in re.finditer(re.escape(find), result.text, re.IGNORECASE):
            matches.append((match.start(), match.end(), replace, {"rule_id": rule["id"]}))
        if matches:
            _replace_spans(result, matches, "msr")
    for match in re.finditer("[云雲]", result.text):
        choice, reason = _yun_choice(result.text, match.start())
        # 「陶雲中暑…」의 雲中은 인용 경계를 가로지를 수도 있다. 어구 목록은
        # 참고 근거로만 주며, 모든 云/雲은 전체 앞뒤 문맥을 보는 모델이 결정한다.
        result.proposals.append(dict(begin=match.start(), end=match.end(), source=match.group(),
                                     candidates=["云", "雲"], stage="yun_context", rule_ids=[],
                                     reason="어구 참고(확정 아님): " + reason))
    _semantic_proposals(result, text, start, converted, rules, ambiguous)
    result.proposals.sort(key=lambda p: p["begin"])
    return result


def _prepare_text(text: str, converter: Callable[[str], str]) -> TextResult:
    """본문만 정리하고 〔의심 후보/작업 표시〕는 원문 그대로 보존한다."""
    rules = rule_data()["rules"]
    result = TextResult("", [], source_text=text)
    pos = 0
    # 닫지 않은 의심 괄호도 끝까지 보존한다.
    protected = list(MARK_RE.finditer(text))
    last_close = max((m.end() for m in protected), default=0)
    unclosed = text.find("〔", last_close)
    segments = [(m.start(), m.end()) for m in protected]
    if unclosed >= 0:
        segments.append((unclosed, len(text)))
    for begin, end in segments + [(len(text), len(text))]:
        normal = _normal_plain(text[pos:begin], pos, converter, rules)
        offset = len(result.text)
        result.proposals.extend(dict(p, begin=p["begin"] + offset, end=p["end"] + offset) for p in normal.proposals)
        result.text += normal.text + text[begin:end]
        result.origins.extend(normal.origins + list(range(begin, end)))
        result.changes.extend(normal.changes); result.review.extend(normal.review)
        pos = end
    return result


def _resolve(results: list[TextResult], resolver: Callable | None, report: dict) -> None:
    """문맥 후보를 32건씩 보내고, 요청한 ID와 정확히 같은 후보 문자열만 반영한다."""
    entries, requests = [], []
    for result in results:
        for proposal in result.proposals:
            request_id = str(len(requests) + 1)
            begin, end = proposal["begin"], proposal["end"]
            original_begin = result.origins[begin]
            original_end = result.origins[end - 1] + 1
            requests.append(dict(id=request_id, context=result.text[max(0, begin - 600):end + 600],
                                 source=proposal["source"], candidates=proposal["candidates"],
                                 source_is_valid_traditional=proposal["source"] in proposal["candidates"],
                                 target_offset=min(begin, 600), reason=proposal.get("reason", proposal["stage"]),
                                 original_context=result.source_text[max(0, original_begin - 600):original_end + 600],
                                 original_source=result.source_text[original_begin:original_end],
                                 original_target_offset=min(original_begin, 600)))
            entries.append((result, proposal, request_id))
    decisions = {}
    if resolver is not None:
        for begin in range(0, len(requests), 32):
            batch = requests[begin:begin + 32]
            try:
                response = resolver(batch)
                if not isinstance(response, dict):
                    raise ValueError("문맥 판별 응답은 ID:선택 문자열 사전이어야 합니다.")
                for item in batch:
                    choice = response.get(item["id"])
                    if isinstance(choice, str) and choice in item["candidates"]:
                        decisions[item["id"]] = choice
            except Exception as exc:
                # API의 예외 문자열에는 URL/키가 담길 수 있으므로 종류만 보고한다.
                report.setdefault("resolver_errors", []).append(dict(type=type(exc).__name__,
                    count=len(batch), message="문맥 판별을 완료하지 못하여 원문을 보존했습니다."))
    replacements = {}
    for result, proposal, request_id in entries:
        begin, end = proposal["begin"], proposal["end"]
        choice = decisions.get(request_id)
        if choice is None:
            stage = "yun_ambiguous" if proposal["stage"] == "yun_context" else "context_unresolved"
            result.review.append(_change(result.text, result.origins, begin, end, proposal["source"], stage,
                candidates=proposal["candidates"], rule_ids=proposal["rule_ids"],
                reason="모델 문맥 판별이 없거나 확정되지 않아 원래 글자를 보존함"))
        elif choice != proposal["source"]:
            replacements.setdefault(id(result), []).append((begin, end, choice,
                {"rule_ids": proposal["rule_ids"], "reason": "모델이 앞뒤 문맥에서 선택", "choice_id": request_id}))
        report.setdefault("decisions", []).append(dict(**result.location, id=request_id, source=proposal["source"],
            candidates=proposal["candidates"], choice=choice, stage=proposal["stage"], rule_ids=proposal["rule_ids"],
            context=result.text[max(0, begin - 40):end + 40],
            original_context=result.source_text[max(0, result.origins[begin] - 40):result.origins[end - 1] + 41]))
    for result in results:
        if id(result) in replacements:
            _replace_spans(result, sorted(replacements[id(result)]), "model_context")


def normalize_text(text: str, *, converter: Callable[[str], str] | None = None,
                   resolver: Callable | None = None) -> TextResult:
    result = _prepare_text(text, converter or _opencc_converter())
    _resolve([result], resolver, {})
    return result


def normalize_blocks(blocks: list[Block], *, converter: Callable[[str], str] | None = None,
                     resolver: Callable | None = None) -> tuple[list[Block], dict]:
    """문서용 사본과 JSON으로 저장할 감사 기록을 돌려준다. 원본 Block/판독 기록은 변경하지 않는다.

    원래의 문자 위치를 추적하므로 MSR가 부호 뒤에 공백을 넣어도 색·밑줄·메모가 따라간다.
    여러 서식 run으로 갈라지기 전 문단 단위로 처리하여 어구 변환과 문맥 판별이 끊기지 않는다.
    """
    converter = converter or _opencc_converter()
    output = deepcopy(blocks)
    data = rule_data()
    report = {"schema_version": 1, "rule_version": RULE_VERSION,
              "msr_source": data["source_name"], "msr_sha256": data["sha256"],
              "rule_count": len(data["rules"]), "changes": [], "review": [], "blocks": [], "decisions": []}
    prepared = []
    for index, block in enumerate(output):
        where = dict(block=index, page=block.page, type=block.type)
        if block.type in ("para", "heading"):
            prepared.append((block, None, _prepare_text(block.text, converter), block.text, where))
        elif block.type == "table":
            for ri, row in enumerate(block.rows):
                for ci, cell in enumerate(row):
                    prepared.append((block, (ri, ci), _prepare_text(cell, converter), cell, dict(**where, row=ri, column=ci)))
    for _, _, result, _, location in prepared:
        result.location = location
    _resolve([item[2] for item in prepared], resolver, report)

    def record(result: TextResult, original: str, location: dict):
        report["changes"].extend(dict(**location, **entry) for entry in result.changes)
        report["review"].extend(dict(**location, **entry) for entry in result.review)
        if result.text != original or result.review:
            report["blocks"].append(dict(**location, before=original, after=result.text))

    for block, cell, result, old, where in prepared:
        if cell is None:
            block.text = result.text
            # 같은 원문 글자에서 나온 출력 범위를 모두 포함시킨다.
            mapped = {}
            for new, source in enumerate(result.origins):
                mapped.setdefault(source, []).append(new)
            spans = []
            for begin, end, kind in block.spans:
                covered = [new for source in range(begin, end) for new in mapped.get(source, [])]
                if covered:
                    spans.append((min(covered), max(covered) + 1, kind))
            block.spans = spans
            block.memos = [(mapped[offset][0] if offset in mapped else
                            min(len(block.text) - 1, max(0, offset)), message)
                           for offset, message in block.memos]
            record(result, old, where)
        else:
            block.rows[cell[0]][cell[1]] = result.text
            record(result, old, where)
    report["summary"] = {
        "changed_blocks": sum(b["before"] != b["after"] for b in report["blocks"]),
        "changes": len(report["changes"]), "review": len(report["review"]),
        "yun_ambiguous": sum(i["stage"] == "yun_ambiguous" for i in report["review"]),
        "guarded_rules": sum(i["stage"] == "msr_guard" for i in report["review"]),
        "context_unresolved": sum(i["stage"] == "context_unresolved" for i in report["review"]),
    }
    report["complete"] = not any(i["stage"] != "msr_guard" for i in report["review"])
    return output, report
