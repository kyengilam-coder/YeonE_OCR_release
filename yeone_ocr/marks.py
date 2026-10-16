"""판독 표시 〔…〕 를 한곳에서: 정규식, 표시 갈래, 1차 글자로 접기, 글자 모양 나누기. 모듈마다 따로 두던 사본(blocks·adjudicate·grades·merge·textlayer·collate·…)을 모았다.
뜻이 실제로 다른 곳은 인자로 가른다: keep_other(모르는 표시의 안 글을 남김 — 짜임 규칙·제목 견주기), work_notes(작업 표시의 / 를 a/b 로 보지 않음 — 글자층 대조),
inner_pos(표시에서 나온 글자의 원글 자리). 안 글의 앞뒤 빈칸은 늘 무시하고 〔疑: X〕·〔疑：X〕(전각 쌍점)는 같은 뜻이다(0.7.4 까지는 등급·글자층 대조·
책갈피 교정은 반각 쌍점만 받고 빈칸을 남겨 3차 판별의 글자 번호와 어긋났다)."""
from __future__ import annotations
import re

MARK_RE = re.compile(r"〔[^〔〕]*〕")                                 # 표시 하나(괄호째)
SPLIT_RE = re.compile(r"(〔[^〔〕]*〕)")                              # re.split 용(표시도 조각으로 남김)
MARK_AB = re.compile(r"〔([^〔〕/]{1,2})/([^〔〕]{1,2})〕")           # 3차가 묻는 한두 글자 갈림 〔a/b〕
UNKNOWN = "판독불명"
NOTE_PREFIXES = ("〔→", "〔소주〕", "〔작업 메모〕", "〔판독 메모〕", "〔도판 미삽입", "〔이어짐〕", "〔고지: ")   # 작업 표시(회색) — 〔고지/구자〕 같은 두 글자 후보 표시와 겹치지 않게 쌍점까지
WORK_PREFIXES = ("→", "소주", "작업", "판독 메모", "도판", "이어짐", "원본")          # 글자층 대조에서 / 가 있어도 a/b 가 아닌 작업 표시의 안 글 머리


def mark_kind(inner: str, work_notes: bool = False) -> str:
    """doubt | ab | unknown | other — inner 는 괄호를 뗀 안 글, 앞뒤 빈칸은 무시. 疑: 와 疑：(전각) 모두 doubt.
    work_notes 면 WORK_PREFIXES 로 시작하는 안 글은 / 가 있어도 ab 가 아니다(〔→PDF 2쪽/인쇄 3쪽〕)."""
    s = inner.strip()
    if s.startswith(("疑:", "疑：")):
        return "doubt"
    if "/" in s and not (work_notes and s.startswith(WORK_PREFIXES)):
        return "ab"
    return "unknown" if s == UNKNOWN else "other"


def mark_reading(inner: str, *, keep_other: bool = False, work_notes: bool = False) -> str:
    """표시 안 글의 1차 글자: doubt → 疑 뒤 글, ab → / 앞, unknown → 빈 글, other → keep_other 면 안 글(앞뒤 빈칸 뗌) 그대로, 아니면 빈 글."""
    s = inner.strip(); k = mark_kind(s, work_notes)
    if k == "doubt":
        return s[2:].strip()
    if k == "ab":
        return s.split("/")[0]
    return s if k == "other" and keep_other else ""


def fold(text: str | None, *, keep_other: bool = False, work_notes: bool = False) -> str:
    """표시를 1차 글자로 접은 글(mark_reading). 빈 글·None 은 빈 글."""
    return MARK_RE.sub(lambda m: mark_reading(m.group(0)[1:-1], keep_other=keep_other, work_notes=work_notes), text or "")


def fold_map(text: str | None, *, keep_other: bool = False, work_notes: bool = False, inner_pos: bool = True) -> tuple[str, list[int | None]]:
    """fold 와 같은 글, 접은 글의 글자마다 원글 자리(pos[k]). 표시에서 나온 글자는 inner_pos 면 그 표시의 시작 자리, 아니면 None.
    끝에 len(원글)을 하나 더 붙인다(pos[len(접은 글)] — 접은 글에 맞춘 정규식의 끝 자리를 원글로 되돌릴 때)."""
    text = text or ""
    out: list[str] = []; pos: list[int | None] = []; i = 0
    for m in MARK_RE.finditer(text):
        out.extend(text[i:m.start()]); pos.extend(range(i, m.start()))
        rep = mark_reading(m.group(0)[1:-1], keep_other=keep_other, work_notes=work_notes)
        out.extend(rep); pos.extend([m.start() if inner_pos else None] * len(rep))
        i = m.end()
    out.extend(text[i:]); pos.extend(range(i, len(text)))
    pos.append(len(text))
    return "".join(out), pos


def split_marks(text: str) -> list[tuple[str, str]]:
    """글을 (조각, 종류) 로 나눈다. 종류: body | mark(의심 자리) | note(작업 표시)."""
    out = []
    for seg in SPLIT_RE.split(text):
        if not seg:
            continue
        if seg.startswith("〔") and seg.endswith("〕"):
            out.append((seg, "note" if seg.startswith(NOTE_PREFIXES) else "mark"))
        else:
            out.append((seg, "body"))
    return out
