"""짜임 규칙(preset): 내용별 문서에서 책마다 다른 규칙 — 약재명(조문 제목) 떼기·주석 문단·문단 끝 출처·굵은/보통 글씨의 쓰임·쓰임 → 양식 스타일 이름 —
을 코드 상수가 아니라 JSON 데이터로 둔다. 내장 규칙은 data/preset_<id>.json(regions = 영역 종류만(기본), kaibao = 本草 조문), 다른 책은 사용자 JSON 파일.
기본 규칙 basic 은 원본의 제목·본문·소주를 일반적인 한글 스타일 이름에 연결한다.
파일에는 정규식이 아니라 낱말 목록·글자 모음·수만 적고 여기서 정규식으로 짠다(괄호 하나 틀려 짜임이 몰래 바뀌지 않게). 모르는 칸·틀린 값은 칸 이름을 밝힌 오류.
kaibao 가 짜는 정규식은 0.7.x structure.py 의 상수와 같다(tests/test_presets 의 말뭉치 비교). 표준 라이브러리만 쓴다 — 창 프로그램이 처음부터 불러온다."""
from __future__ import annotations
import json, re
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

ROLES = ("chapter", "section", "subsection", "entry", "body", "bonjing", "bielu", "commentary", "small", "caption", "note")   # 블록의 쓰임(structure 가 붙임)
EMPHASIS_ROLES = ("body", "bonjing", "bielu", "commentary")           # 굵은/보통 글씨 조문에 붙일 수 있는 쓰임
BUILTIN = ("basic", "regions", "kaibao")
ALIASES = {"none": ("regions", None), "auto": ("kaibao", None), "bencao": ("kaibao", "always")}   # 예전 --structure 값 → (내장, entry.when 덮어쓰기)
DEFAULT_PRESET = "basic"
DATA = Path(__file__).with_name("data")                               # data/preset_<id>.json
_HAN = "㐀-䶿一-鿿豈-﫿\U00020000-\U0003134f"   # 한자(확장 A·통합·호환·확장 B~) — 이스케이프로 적는다(호환 한자 豈 가 NFC 로 바뀌면 한글까지 한자로 센다)
_ALIAS_LABELS = {"none": "영역 종류만 (예전 설정 none)", "auto": "本草 조문 (예전 설정 auto)", "bencao": "本草 조문 · 약재명 늘 떼기(예전 설정 bencao)"}
_KEYS = {"": ("schema", "id", "label", "description", "entry", "commentary", "source", "emphasis", "styles"),
         "entry": ("when", "min_hits", "min_ratio", "max_name", "openers", "heading_openers", "name_end_not", "name_start_not"),
         "commentary": ("open", "close", "max_label", "markers"), "source": ("after", "tags"), "emphasis": ("bold", "plain"),
         "styles": ("paragraph", "character")}


class PresetError(ValueError):
    """짜임 규칙을 쓸 수 없음 — 글은 사용자에게 그대로 보인다(어느 파일·어느 칸인지)."""


@dataclass(frozen=True)
class Entry:
    """약재명(조문 제목) 떼기. when: auto = 그런 문단이 min_hits 이상이고 문단의 min_ratio 이상일 때만 / always = 늘."""
    when: str
    min_hits: int
    min_ratio: float
    max_name: int
    openers: tuple[str, ...]
    heading_openers: tuple[str, ...]
    name_end_not: str
    name_start_not: str


@dataclass(frozen=True)
class Preset:
    ref: str                       # 고른 이름 그대로(내장 id · 예전 이름 · 파일 경로) — 설정·명령줄에 적는 값
    id: str
    label: str
    description: str
    entry: Entry | None
    entry_re: re.Pattern | None    # 문단 첫머리 「이름 + 빈칸 + 조문 첫 낱말」, group(1) = 이름
    entry_start_re: re.Pattern | None   # 조문 첫 낱말(판독이 빈칸을 빠뜨려 쪽 이미지의 틈으로 가를 때)
    heading_next_re: re.Pattern | None  # 제목으로 읽힌 이름 다음 문단의 첫머리(앞에 되읽은 이름이 붙어도), group(1) = 되읽은 이름
    name_re: re.Pattern | None     # 이름 꼴(한자 1~max_name 자) — fullmatch
    comment_re: re.Pattern | None  # 주석 문단 첫머리 ［…云］
    source_re: re.Pattern | None   # 문단 끝 출처, group(1) = 출처 글
    emphasis: tuple[str, str] | None    # (굵은 글씨 쓰임, 보통 글씨 쓰임)
    para_styles: dict              # {쓰임: (양식 문단 스타일 이름, …)} 앞의 것이 양식에 있으면 그것
    char_styles: dict              # {글자 범위 종류: (양식 글자 스타일 이름, …)}


def _cls(chars: str) -> str:
    """글자 모음 → 정규식 [] 안에 넣을 글(\\ ] ^ - [ 만 이스케이프)."""
    return "".join("\\" + c if c in "\\]^-[" else c for c in chars)


def _alt(words) -> str:
    return "|".join(re.escape(w) for w in words)


def _compile(data, where: str, ref: str, stem: str) -> Preset:
    """검사(칸 이름·꼴·범위) → 정규식. 틀리면 PresetError(where + 무엇이 왜)."""
    def bad(msg: str):
        return PresetError(where + msg)

    def obj(v, path: str) -> dict | None:
        if v is None:
            return None
        if not isinstance(v, dict):
            raise bad(f"{path or '맨 위'} 는 {{…}}" + (" 또는 null" if path else "") + " 이어야 합니다")
        for k in v:
            if k not in _KEYS[path]:
                raise bad(f"모르는 칸 '{path + '.' if path else ''}{k}' — 쓸 수 있는 칸: {', '.join(_KEYS[path])}")
        return v

    def text(d: dict, k: str, path: str, default: str) -> str:
        v = d.get(k, default)
        if not isinstance(v, str):
            raise bad(f"{path}{k} 는 글자열이어야 합니다")
        return v

    def words(d: dict, k: str, path: str, msg: str, default=None) -> tuple[str, ...]:
        v = d.get(k, default)
        if not isinstance(v, (list, tuple)) or not v or not all(isinstance(w, str) and w for w in v):
            raise bad(msg)
        return tuple(v)

    def num(d: dict, k: str, default, lo, hi, msg: str, integer: bool = True):
        v = d.get(k, default)
        if isinstance(v, bool) or not isinstance(v, int if integer else (int, float)) or not lo <= v <= hi:
            raise bad(msg)
        return v

    if not isinstance(data, dict):
        raise bad("맨 위가 {…} 가 아닙니다")
    obj(data, "")
    if type(data.get("schema")) is not int or data["schema"] != 1:
        raise bad(f"schema 는 1 이어야 합니다(지금 {data.get('schema')!r})")
    pid, label, desc = text(data, "id", "", stem), text(data, "label", "", stem), text(data, "description", "", "")
    entry = entry_re = entry_start_re = heading_next_re = name_re = comment_re = source_re = emphasis = None
    if (e := obj(data.get("entry"), "entry")) is not None:
        when = e.get("when", "auto")
        if when not in ("auto", "always"):
            raise bad('entry.when 은 "auto" 또는 "always" 입니다')
        openers = words(e, "openers", "entry.", "entry.openers 는 비지 않은 글자열 목록이어야 합니다")
        entry = Entry(when=when, min_hits=num(e, "min_hits", 3, 1, 10 ** 9, "entry.min_hits 는 1 이상 정수"),
                      min_ratio=num(e, "min_ratio", 0.01, 0, 1, "entry.min_ratio 는 0~1 사이 수", integer=False),
                      max_name=num(e, "max_name", 6, 1, 12, "entry.max_name 은 1~12 정수"), openers=openers,
                      heading_openers=words(e, "heading_openers", "entry.", "entry.heading_openers 는 비지 않은 글자열 목록이어야 합니다", openers),
                      name_end_not=text(e, "name_end_not", "entry.", ""), name_start_not=text(e, "name_start_not", "entry.", ""))
        n = entry.max_name
        entry_re = re.compile(rf"^([{_HAN}]{{1,{n}}})[ \t\n   　]+(?=(?:{_alt(openers)})[{_HAN}])")   # 사이 빈칸: 보통·전각·em·줄바꿈
        entry_start_re = re.compile(rf"(?:{_alt(openers)})[{_HAN}]")
        heading_next_re = re.compile(rf"([{_HAN}]{{0,{n}}})[ 　,，、]*(?:{_alt(entry.heading_openers)})[{_HAN}]")
        name_re = re.compile(rf"[{_HAN}]{{1,{n}}}")
    if (c := obj(data.get("commentary"), "commentary")) is not None:
        op, cl = c.get("open"), c.get("close")
        if not (isinstance(op, str) and op and isinstance(cl, str) and cl):
            raise bad("commentary.open·close 는 글자열(괄호 글자들)이어야 합니다")
        ml = num(c, "max_label", 16, 0, 40, "commentary.max_label 은 0~40 정수")
        markers = words(c, "markers", "commentary.", "commentary.markers 는 비지 않은 글자열 목록이어야 합니다")
        comment_re = re.compile(rf"^[{_cls(op)}]\s*[^{_cls(cl)}\s/:：]{{0,{ml}}}?(?:{_alt(markers)})\s*[{_cls(cl)}]")   # /·: 가 든 〔a/b〕·〔疑: X〕 는 판독 표시
    if (s := obj(data.get("source"), "source")) is not None:
        after = text(s, "after", "source.", "。，、；：")
        tags = words(s, "tags", "source.", "source.tags 는 비지 않은 글자열 목록이어야 합니다")
        source_re = re.compile(rf"(?:(?<=[{_cls(after)}\s　])|^)({_alt(sorted(tags, key=len, reverse=True))})[ 　]*$")   # 긴 것부터(唐本附 를 唐… 보다 먼저)
    if (m := obj(data.get("emphasis"), "emphasis")) is not None:
        for k in ("bold", "plain"):
            if m.get(k) not in EMPHASIS_ROLES:
                raise bad(f"emphasis.{k} {m.get(k)!r} 는 쓸 수 없는 쓰임입니다 — 가능: {', '.join(EMPHASIS_ROLES)}")
        if m["bold"] == m["plain"]:
            raise bad("emphasis 의 bold 와 plain 이 같습니다")
        if entry is None:
            raise bad("emphasis 는 entry(약재명 떼기)가 있을 때만 씁니다 — 굵기는 조문 문단에만 적용됩니다. entry 를 넣거나 emphasis 를 null 로 두십시오")
        emphasis = (m["bold"], m["plain"])
    styles = obj(data.get("styles"), "styles") or {}
    kinds = ("small", "source") + ((emphasis[0],) if emphasis and emphasis[0] not in ("small", "source") else ())
    maps = {}
    for part, allowed, what in (("paragraph", ROLES, "쓰임"), ("character", kinds, "글자 범위 종류")):
        got = styles.get(part) or {}
        if not isinstance(got, dict):
            raise bad(f"styles.{part} 는 {{쓰임: [스타일 이름, …]}} 이어야 합니다")
        for k, v in got.items():
            if k not in allowed:
                raise bad(f"styles.{part} 의 {k!r} 는 {what}{'이' if what == '쓰임' else '가'} 아닙니다 — 가능: {', '.join(allowed)}")
            words(got, k, "", f"styles.{part}.{k} 는 스타일 이름 목록(비지 않은 글자열)이어야 합니다")
        maps[part] = {k: tuple(v) for k, v in got.items()}
    return Preset(ref=ref, id=pid, label=label, description=desc, entry=entry, entry_re=entry_re, entry_start_re=entry_start_re,
                  heading_next_re=heading_next_re, name_re=name_re, comment_re=comment_re, source_re=source_re, emphasis=emphasis,
                  para_styles=maps["paragraph"], char_styles=maps["character"])


def load_preset(path, *, ref: str | None = None) -> Preset:
    """규칙 JSON 파일 → 검사 → Preset. 오류 글은 '짜임 규칙 파일 <경로>: ' 로 시작한다."""
    path = Path(path)
    where = f"짜임 규칙 파일 {path}: "
    try:
        raw = path.read_text(encoding="utf-8-sig")                    # 메모장이 붙이는 BOM 도
    except UnicodeDecodeError:                                        # 한국어 Windows 편집기의 기본 저장(ANSI·CP949)
        raise PresetError(where + "UTF-8 로 저장된 파일이 아닙니다(ANSI·CP949 등) — 편집기에서 '다른 이름으로 저장 → 인코딩 UTF-8' 로 다시 저장하십시오") from None
    except OSError as e:
        raise PresetError(where + f"파일을 읽지 못했습니다({type(e).__name__}: {e})") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        why = next((ko for en, ko in _JSON_MSGS if e.msg.startswith(en)), e.msg)
        raise PresetError(where + f"JSON 꼴이 틀렸습니다 — {e.lineno}째 줄 {e.colno}째 글자: {why}") from None
    return _compile(data, where, str(path) if ref is None else ref, path.stem)


_JSON_MSGS = (("Expecting ',' delimiter", "쉼표(,)가 빠졌습니다 — 칸과 칸, 목록의 값과 값 사이에 쉼표를 넣으십시오"),
              ("Expecting property name enclosed in double quotes", '칸 이름은 큰따옴표 "…" 로 감싸야 합니다(마지막 칸 뒤의 쉼표도 이 오류)'),
              ("Expecting ':' delimiter", '칸 이름 뒤에 쌍점(:)이 빠졌습니다 — "칸": 값'),
              ("Expecting value", "값이 없거나 꼴이 틀렸습니다 — 글은 큰따옴표 \"…\", 목록은 [ ], 없음은 null(목록 마지막 값 뒤의 쉼표도 이 오류)"),
              ("Illegal trailing comma before end of array", "값이 없거나 꼴이 틀렸습니다 — 목록 마지막 값 뒤의 쉼표를 지우십시오"),
              ("Unterminated string", "큰따옴표로 연 글이 닫히지 않았습니다"),
              ("Invalid control character", "글 안에 줄바꿈·탭이 그대로 들어 있습니다 — 한 줄로 쓰십시오"),
              ("Extra data", "맨 위 {…} 뒤에 글이 더 있습니다"))   # json 의 영어 오류 → 고칠 길을 말하는 글(모르는 것은 영어 그대로)


@lru_cache(maxsize=None)
def _builtin(pid: str) -> Preset:
    return load_preset(DATA / f"preset_{pid}.json", ref=pid)


def resolve_preset(ref: str | None) -> Preset:
    """None·"" → 기본(basic) · 내장 id · 예전 --structure 이름(auto=kaibao, bencao=kaibao 약재명 늘, none=regions) · 그 밖은 규칙 JSON 파일 경로(지금 폴더 기준)."""
    ref = (ref or "").strip() or DEFAULT_PRESET
    if ref in BUILTIN:
        return _builtin(ref)
    if ref in ALIASES:
        pid, when = ALIASES[ref]
        p = _builtin(pid)
        return replace(p, ref=ref, label=_ALIAS_LABELS[ref], entry=replace(p.entry, when=when) if when and p.entry else p.entry)
    path = Path(ref)
    if path.is_file():
        return load_preset(path, ref=ref)
    if path.suffix.lower() == ".json" or "/" in ref or "\\" in ref:
        raise PresetError(f"짜임 규칙 파일이 없습니다: {ref}")
    raise PresetError(f"짜임 규칙 {ref!r} 를 모릅니다 — 내장: basic(기본 규칙)·regions(영역 종류만)·kaibao(本草 조문), 또는 규칙 JSON 파일 경로")


def preset_label(ref: str | None) -> str:
    """화면·로그에 보일 이름(예외 없이): 내장은 그 label, 예전 이름은 '…(예전 설정 bencao)', 파일은 '파일: 내책.json'(못 읽으면 '… (읽지 못함)')."""
    ref = (ref or "").strip() or DEFAULT_PRESET
    if ref in BUILTIN or ref in ALIASES:
        try:
            return resolve_preset(ref).label
        except Exception:                                             # 꾸러미에서 내장 파일이 빠졌을 때도 창은 뜬다(쓰려 하면 preset_ok 가 알린다)
            return ref
    name = f"파일: {Path(ref).name}"
    try:
        resolve_preset(ref)
    except Exception:
        return name + " (읽지 못함)"
    return name


def template_suggests(template, ref: str | None) -> list[str]:
    """양식 HWPX 에 내장 本草 조문 규칙(kaibao)만 쓰는 문단 스타일(약재명·본경·별록)이 있는데 지금 규칙(ref)에 약재명 떼기가 없으면 그 스타일 이름들 — 창이 양식을 고를 때 알린다
    (0.7.x 설정이 덮여 '원본 쪽대로·양식 없음' 이 된 채 옮겨지면 내용별을 쓴 줄 몰라 regions 가 된다). 아니거나 읽지 못하면 []."""
    import zipfile
    import xml.etree.ElementTree as ET
    try:
        if resolve_preset(ref).entry is not None:
            return []
        k, r = _builtin("kaibao"), _builtin("regions")
        common = {n for v in r.para_styles.values() for n in v}
        want = {n for role in ("entry", "bonjing", "bielu") for n in k.para_styles.get(role, ()) if n not in common}
        with zipfile.ZipFile(template) as z:
            root = ET.fromstring(z.read("Contents/header.xml"))
        names = {e.get("name") for e in root.iter() if e.tag.endswith("}style")}
    except Exception:
        return []
    return sorted(want & names)


def export_builtin(pid: str, dest) -> Path:
    """내장 규칙 파일(data/preset_<pid>.json)을 dest 에 그대로 복사한다 — 새 책의 규칙을 그것을 고쳐 만들게(설치판·실행 파일 하나 판에서는 data 폴더를 찾기 어렵다)."""
    if pid not in BUILTIN:
        raise PresetError(f"내장 규칙이 아닙니다: {pid}")
    dest = Path(dest)
    dest.write_bytes((DATA / f"preset_{pid}.json").read_bytes())
    return dest


def preset_choices(current: str | None) -> list[tuple[str, str]]:
    """고를 수 있는 [(ref, 이름)]: 내장 + (지금 것이 내장이 아니면 그것 — 예전 이름·파일). 창이 끝에 '파일에서 고르기…' 를 붙인다."""
    out = [(i, preset_label(i)) for i in BUILTIN]
    cur = (current or "").strip()
    if cur and cur not in BUILTIN:
        out.append((cur, preset_label(cur)))
    return out
