"""HWPX 를 서식 그대로 둔 채 구획 안의 글자만 고친다.

원칙
- 문단 안의 <hp:t> 글만 손댄다. 각주·메모·필드·줄 배치 같은 다른 요소는 그대로 둔다(한컴이 줄 배치를 다시 계산한다).
- 글자 번호는 python-hwpx 의 paragraph.text 와 같은 셈법이다: hp:t 의 글 + 안쪽 요소(hp:tab 은 "\\t", 그 밖은 자기 글) + 그 뒤 글(tail),
  run 바로 아래의 hp:tab / ctrl(id=tab) 은 "\\t" 한 글자.
- 고친 글자는 원래 글자 모양을 바탕으로 색·형광펜·밑줄만 바꾼 글자 모양(charPr)을 새로 만들어 입힌다(일반 OCR 과 같은 확인 등급).
    초록(점선 밑줄)  자동 교정 — 판독(1차·2차 일치)대로, 또는 3차 자형 판별로 승격한 것(A)
    파랑(파선 밑줄)  자동 교정 — 간체 한 글자가 여러 번체로 갈려 사전으로 고른 것(B2), 글자층·반복 근거로 승격한 것
    빨강(굵게+노랑) 확인 요청 — 글자는 HWPX 그대로(B1·C 표시, 긴 구간 삽입 제안 — 어느 정책에서도 자동으로 넣지 않음, 반영하지 않은 제안)
  교정이 만든 글자 모양에는 보이지 않는 표지(취소선 '없음'의 색 = 등급 색)를 단다 — 일반 OCR 로 만든 문서의 같은 색 글자와 가르려고.
- 메모(memo_mode): notice(기본) = 색으로 전할 수 없는 고지 사항에만 — 지운 글자, 폭 없는 표시, 구조 차이(C), 반영하지 않은 제안,
  한글 풀이 자리 — 와 문서 앞의 범례 하나. all = 모든 항목에 근거(0.6.3 까지의 방식). inline = 메모 없이 고지만 글 뒤(폭 있는 표시는 그 범위 뒤) 〔…〕
  — 이미 있는 〔…〕 안이면 그 〕 뒤에(괄호를 겹치지 않게), 제목(구획 기준) 문단에는 넣지 않고 색만(구획 이름이 바뀌지 않게, 지운 글자·폭 없는
  표시는 보고에만). 반영하지 않은 제안은 〔제안: …〕·〔삽입 제안: …〕 로 적는다(고친 것처럼 〔원: …〕 으로 적지 않게).
  원래 글자·PDF 쪽·근거 전체는 늘 보고(collate/*.html·json)에 남는다.
- 원본 파일은 덮어쓰지 않고 사본으로 저장한다.
"""
from __future__ import annotations
import os, random, shutil, time
from copy import deepcopy
from dataclasses import dataclass
from lxml import etree
from .collate import Item

HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
HH = "{http://www.hancom.co.kr/hwpml/2011/head}"
BLUE, GREEN = "#1F4FBF", "#D8F5C8"            # 0.6.3 까지의 자동 교정 색(다시 돌릴 때 알아보려고 남김)
RED, YELLOW = "#C00000", "#FFFF00"
# 표지를 달기 전 판의 교정 메모 머리(_memo_text) — 일반 OCR 의 '[확인 필요 — …]' 와 겹치지 않게 닫는 괄호(와 빈칸)까지
MEMO_HEADS = ("[자동 교정]", "[자동 교정·확인]", "[자동 삽입·확인]", "[확인 필요] ", "[확인] ")
LEGACY_NOTES = ("〔원: ", "〔삭제: ", "〔확인: 판독 ", "〔삽입·확인〕")   # 표지 이전 판의 글 속 표기 가운데 사람이 손으로 칠 법하지 않은 것(맨 〔확인〕·〔삽입〕 은 뺀다)


def log(msg: str) -> None:
    from ..cli import log as _log
    _log(msg)


def legend_head(legend: str) -> str:
    """범례의 머리 '[…]'. 교정 범례 '[연어OCR 표시 범례 — 책갈피 교정]' 과 일반 OCR 범례 '[연어OCR 표시 범례]' 를 가른다."""
    return legend[:legend.find("]") + 1]


def _tag_of(el) -> str:
    """글자 모양의 교정 표지(취소선 색). 표지가 글자 색과 같으면 교정이 만든 글자 모양이다."""
    s = el.find(f"{HH}strikeout")
    return (s.get("color") or "").upper() if s is not None else ""


def _after_bracket(text: str, pos: int) -> int | None:
    """글자 번호 pos 가 이미 있는 〔…〕 안이면 그 〔…〕 를 닫는 〕 바로 뒤의 글자 번호(글 속 표기를 괄호 안에 겹쳐 넣지 않게). 밖이면 None."""
    d = text[:pos].count("〔") - text[:pos].count("〕")
    if d <= 0:
        return None
    for j in range(pos, len(text)):
        d += (text[j] == "〔") - (text[j] == "〕")
        if d == 0:
            return j + 1
    return None                                         # 닫히지 않은 〔 — 감싼 〔…〕 가 없다


def _canon(el) -> tuple:
    """글자 모양 비교용(id 를 뺀 속성·자식)."""
    return (el.tag, sorted((k, v) for k, v in el.attrib.items() if k != "id"), [_canon(c) for c in el])


def item_grade(it: Item) -> str:
    """항목의 확인 등급(grades.collate_grade): green | blue | red."""
    from ..grades import collate_grade
    return collate_grade(it) or "red"


def is_notice(it: Item) -> bool:
    """색만으로는 전할 수 없는 고지 사항인지(notice 메모 대상, grades.needs_memo)."""
    from ..grades import needs_memo
    return needs_memo(it, "notice")


# python-hwpx 와 같은 셈법: hp:t 안의 이 요소들은 글자 하나로 센다
INLINE_ATOMS = {"tab": "\t", "fwSpace": "　", "nbSpace": " ", "hyphen": "-", "lineBreak": "\n"}


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


@dataclass
class Slot:
    """문단 글의 한 조각: hp:t 의 text, 또는 hp:t 안 요소의 tail. (run, t, holder, attr, start, end)"""
    run: etree._Element
    t: etree._Element
    holder: etree._Element      # 글을 쥔 요소(t 자체면 attr="text", 안쪽 요소면 attr="tail")
    attr: str
    start: int
    end: int

    @property
    def text(self) -> str:
        return getattr(self.holder, self.attr) or ""

    @text.setter
    def text(self, v: str) -> None:
        setattr(self.holder, self.attr, v)


class _AlreadyMarked(Exception):
    """이전 실행에서 이미 색·메모를 붙인 자리 — 다시 붙이지 않는다."""


@dataclass
class Applied:
    item: Item
    ok: bool
    note: str = ""
    memo: bool = False          # 메모나 글 속 〔…〕 표기를 실제로 달았는지(보고의 메모 수)
    hidden: bool = False        # inline 제목 문단이라 표기 없이 문서에서 안 보이는 표시(지운 글자·폭 없는 표시 — 보고에만)


def _rid() -> str:
    return str(random.randint(10 ** 9, 2 ** 31 - 1))


def paragraph_text(pel: etree._Element) -> str:
    """python-hwpx paragraph.text 와 같은 글."""
    parts = []
    for run in pel:
        if _local(run.tag) != "run":
            continue
        for child in run:
            name = _local(child.tag)
            if name == "t":
                parts.append(child.text or "")
                for sub in child:
                    sn = _local(sub.tag)
                    parts.append(INLINE_ATOMS.get(sn, "" if sn in ("markpenBegin", "markpenEnd", "insertBegin", "insertEnd", "deleteBegin", "deleteEnd") else "".join(sub.itertext())))
                    parts.append(sub.tail or "")
            elif name == "tab" or (name == "ctrl" and (child.get("id") or "").lower() == "tab"):
                parts.append("\t")
    return "".join(parts)


def _slots(pel: etree._Element) -> list[Slot]:
    """편집 가능한 글 조각들(hp:t 의 text 와 안쪽 요소 tail)과 그 글자 번호. 탭 같은 원자 요소는 번호만 차지하고 조각이 아니다."""
    out: list[Slot] = []; n = 0
    for run in pel:
        if _local(run.tag) != "run":
            continue
        for child in run:
            name = _local(child.tag)
            if name == "t":
                s = child.text or ""; out.append(Slot(run, child, child, "text", n, n + len(s))); n += len(s)
                for sub in child:
                    sn = _local(sub.tag)
                    n += len(INLINE_ATOMS.get(sn, "" if sn in ("markpenBegin", "markpenEnd", "insertBegin", "insertEnd", "deleteBegin", "deleteEnd") else "".join(sub.itertext())))
                    s = sub.tail or ""; out.append(Slot(run, child, sub, "tail", n, n + len(s))); n += len(s)
            elif name == "tab" or (name == "ctrl" and (child.get("id") or "").lower() == "tab"):
                n += 1
    return out


class Editor:
    def __init__(self, book, *, author: str = "YeonE_OCR", memo: bool | str = True, heads=None):
        """memo: "notice" | "all" | "inline". 예전 호출과 맞게 True 는 "all", False 는 "inline".
        heads: 이번 실행의 구획 기준 문단 번호들(PDF 책갈피 제목·--heading-styles 로 찾은 것 포함). 없으면 book.anchors() 기본값."""
        self.book = book
        self.doc = book.doc
        self.author = author
        self.memo_mode = memo if memo in ("notice", "all", "inline") else ("all" if memo else "inline")   # 파이썬에서 부를 때 기본은 all(0.6.3 과 같게)
        self.use_memo = self.memo_mode != "inline"
        self._pr_cache: dict[tuple[str, str, str], str] = {}
        self._memo_no = self._count_memos()
        self._short = ""
        self._noted = False                                 # 지금 항목에 메모·글 속 표기를 실제로 달았는지
        self._hidden = False                                # 지금 항목이 제목 문단이라 표기 없이 안 보이게 됐는지(지운 글자·폭 없는 표시)
        self.log: list[Applied] = []
        self._legacy = False                                # 표지 이전의 교정본(일반 OCR 문서) — 표지를 달지 않고 예전처럼
        self._ours: set[str] = self._marked_char_prs()      # 이전 실행이 만든 색 글자 모양 id — 같은 자리를 또 표시하지 않기 위해
        self._heads: set[int] = (set(heads) if heads is not None else self._head_paras()) if not self.use_memo else set()

    def _marked_char_prs(self) -> set[str]:
        """이전 교정이 만든 색 글자 모양 id. 표지(_tag_of = 글자 색)가 있으면 교정이 만든 것이다. 표지 없는 등급 색은 표지를 달기 전의 교정본
        것으로 보되, 일반 OCR 로 만든 문서(일반 OCR 범례가 있음)에서는 일반 OCR 이 칠한 색(빨강 〔〕·초록 3차 글자 …)이므로 뺀다 —
        그 글자 위의 교정 표시가 '이미 표시된 자리'로 건너뛰어지지 않게. (python-hwpx 는 내용이 같은 글자 모양을 다시 쓰므로 id 로는 못 가른다.)
        단 표지 달린 글자 모양이 없는데 이전 교정의 흔적(교정 메모·글 속 표기, _collate_traces)이 있으면 표지를 달기 전 판의 교정본이므로
        예전처럼 모두 넣고, 이 문서에는 표지를 달지 않는다(다음 구획·다음 실행에서도 같은 셈이 되게). 교정 범례만으로는 치지 않는다 —
        그 판은 일반 OCR 범례 메모가 있는 문서·inline 에는 교정 범례를 달지 않았고, 범례는 표지 달린 교정본에서 표지만 사라져도 남는다."""
        from ..grades import GRADE_STYLE
        ours = {BLUE.upper(), RED.upper()} | {g["color"].upper() for g in GRADE_STYLE.values()}
        tagged, plain = set(), set()
        try:
            for el in self._header().element.iter(f"{HH}charPr"):
                c = (el.get("textColor") or "").upper()
                if c in ours:
                    (tagged if _tag_of(el) == c else plain).add(el.get("id", ""))
        except Exception:
            pass
        if plain and self._ocr_made():
            if tagged or not self._collate_traces(plain):
                return tagged
            self._legacy = True
            if not getattr(self.book, "_legacy_warned", False):      # 책마다 한 번(전체 교정은 구획마다 Editor 를 새로 만든다)
                self.book._legacy_warned = True
                log("주의: 일반 OCR 문서에 이전 교정의 메모·글 속 표기는 있는데 표지 달린 교정 글자 모양이 없음 — 표지를 달기 전 판의 교정본으로 보고 "
                    "등급 색 글자를 모두 이미 표시된 자리로 셈(일반 OCR 이 칠한 빨강·초록 위에는 새 교정 표시를 하지 않음)")
        return tagged | plain

    def _collate_traces(self, graded: set[str] = frozenset()) -> bool:
        """이전 교정이 남긴 흔적이 있는지(사람이 단 메모·손으로 친 〔…〕 는 치지 않는다):
        - 메모 글(subList 안)이 교정 메모 머리(MEMO_HEADS)로 시작하고 그 메모의 작성자가 이 프로그램(self.author)이거나,
        - 그 판의 글 속 표기(〔원: …〕·〔확인: 판독 …〕 …, LEGACY_NOTES — 메모를 끈 교정본)가 등급 색 글자 모양(graded, 표지 없는 id)
          조각 안에 있다(그 판은 표기를 고친 글자와 같은 색 조각에 넣었다)."""
        def author(t):
            fb = next((a for a in t.iterancestors() if a.tag == f"{HP}fieldBegin"), None)
            sp = fb.find(f"{HP}parameters/{HP}stringParam[@name='Author']") if fb is not None else None
            return (sp.text or "") if sp is not None else ""
        try:
            for p in self.book.paragraphs:
                for t in p.element.iter(f"{HP}t"):
                    x = t.text or ""
                    if not x:
                        continue
                    if any(a.tag == f"{HP}subList" for a in t.iterancestors()):
                        if x.startswith(MEMO_HEADS) and author(t) == self.author:
                            return True
                    elif graded and any(q in x for q in LEGACY_NOTES):
                        r = t.getparent()
                        if r is not None and r.get("charPrIDRef") in graded:
                            return True
        except Exception:
            return False
        return False

    def _has_collate_legend(self) -> bool:
        """교정 범례 메모('[연어OCR 표시 범례 — 책갈피 교정] …', 메모 글 subList 안)가 이미 있는지."""
        from ..grades import COLLATE_LEGEND
        head = legend_head(COLLATE_LEGEND)
        try:
            return any((t.text or "").startswith(head) and any(a.tag == f"{HP}subList" for a in t.iterancestors())
                       for p in self.book.paragraphs for t in p.element.iter(f"{HP}t"))
        except Exception:
            return False

    def _head_paras(self) -> set[int]:
        """제목(구획 기준) 문단 번호: 책갈피·제목 스타일 문단(HwpxBook.anchors 기본값 — heads 를 주지 않았을 때만). inline 표기를 넣으면
        구획 이름이 바뀌므로 색만 칠한다."""
        try:
            return {a.para for a in self.book.anchors()}
        except Exception:
            return set()

    def _ocr_made(self) -> bool:
        """일반 OCR 로 만든 문서인지: 일반 OCR 범례(메모, 메모를 끈 문서면 글 속 〔고지: …〕)가 있다."""
        from ..grades import LEGEND
        head = legend_head(LEGEND)
        try:
            return any(head in (t.text or "") for p in self.book.paragraphs for t in p.element.iter(f"{HP}t"))
        except Exception:
            return False

    # ----- 글자 모양 -----
    def _header(self):
        from ..hwpx_memo import _header
        return _header(self.doc)

    def _char_pr(self, pid: str | None):
        try:
            return None if pid is None else self._header().element.find(f".//{HH}charPr[@id='{pid}']")
        except Exception:
            return None

    def _tag(self, pid: str, color: str) -> str:
        """교정 표지를 단 글자 모양: 취소선 색 = 등급 색(취소선 모양은 '없음' 그대로라 보이지 않는다). 같은 것이 있으면 그것, 없으면 pid 를 본떠 만든다.
        취소선 요소가 없는 글자 모양(다른 프로그램이 만든 HWPX)에는 모양 '없음' 취소선을 제자리(밑줄 뒤)에 만들어 단다. 못 달면 알린다."""
        if self._legacy:
            return pid                                  # 표지 이전 교정본: 예전처럼 표지 없이(_marked_char_prs)
        color = color.upper()
        def set_tag(x):
            s = x.find(f"{HH}strikeout")
            if s is None:
                s = x.makeelement(f"{HH}strikeout", {"shape": "NONE"})
                after = [i for i, c in enumerate(x) if _local(c.tag) in ("outline", "shadow", "emboss", "engrave", "supscript", "subscript")]
                x.insert(after[0] if after else len(x), s)
            s.set("color", color)
        try:
            el = self._char_pr(pid)
            if el is None:
                raise ValueError("글자 모양이 머리에 없음")
            if _tag_of(el) == color:
                return pid
            want = deepcopy(el); set_tag(want); want = _canon(want)
            return str(self._header().ensure_char_property(predicate=lambda x: _canon(x) == want, modifier=set_tag, base_char_pr_id=pid).get("id") or pid)
        except Exception as e:
            log(f"주의: 교정 표지를 달지 못함(글자 모양 {pid}: {type(e).__name__}: {e}) — 다시 돌리면 이 표시를 이미 표시된 자리로 알아보지 못할 수 있음")
            return pid

    def _style_for(self, base_pr: str | None, grade: str) -> str:
        """원래 글자 모양에 등급 색·형광펜·굵게·밑줄만 바꾼 글자 모양(교정 표지 포함). 이미 등급 색인 자리(이전 교정의 빨강, 일반 OCR 의
        빨강·초록 …)를 다시 칠할 때는 형광펜(초록은 없음)·굵게·밑줄도 명시한다 — 빨강 위를 초록·파랑으로 고칠 때 노랑·굵게를, 초록 위를
        빨강으로 표시할 때 점선 밑줄을 물려받지 않게. 등급 색이 아닌 원래 형광펜·굵은·밑줄 글자는 등급이 바꾸지 않는 한 그대로 둔다."""
        from ..grades import GRADE_STYLE
        g = GRADE_STYLE[grade]
        key = (base_pr or "", grade)
        if key not in self._pr_cache:
            kw = dict(color=g["color"])
            base = self._char_pr(base_pr)
            graded = base is not None and (base.get("textColor") or "").upper() in {s["color"].upper() for s in GRADE_STYLE.values()}
            if g["highlight"]:
                kw["highlight"] = g["highlight"]
            elif graded:
                kw["highlight"] = "none"
            if g["bold"] or graded:
                kw["bold"] = bool(g["bold"])
            if g["underline"]:
                kw.update(underline=True, underline_shape=g["underline"], underline_color=g["color"])
            elif graded:
                kw["underline"] = False
            try:
                pid = str(self.doc.styles.ensure_run(base_char_pr_id=base_pr, **kw))
            except Exception:
                kw.pop("underline_shape", None); kw.pop("underline_color", None)
                try:
                    pid = str(self.doc.styles.ensure_run(base_char_pr_id=base_pr, **kw))
                except Exception:
                    pid = str(self.doc.ensure_run_style(**kw))
            self._pr_cache[key] = self._tag(pid, g["color"])
        return self._pr_cache[key]

    # ----- 메모 -----
    def _count_memos(self) -> int:
        from ..hwpx_memo import count_memos
        return count_memos(p.element for p in self.book.paragraphs)

    def _memo_shape_id(self) -> str:
        from ..hwpx_memo import memo_shape_id
        return memo_shape_id(self.doc)

    def _memo_field(self, text: str) -> tuple[etree._Element, etree._Element]:
        """메모 필드의 시작(ctrl/fieldBegin, 메모 글 포함)과 끝(ctrl/fieldEnd). 업로드 문서의 한컴 표기를 그대로 따른다(hwpx_memo)."""
        from ..hwpx_memo import memo_field
        self._memo_no += 1; self._noted = True
        return memo_field(text, self._memo_no, self.author, self._memo_shape_id())

    # ----- 조각 나누기 -----
    @staticmethod
    def _locate(slots: list[Slot], pos: int) -> Slot:
        """pos 가 든 조각. 조각 경계(끝)면 그 조각(뒤에 붙일 수 있게). 아무 조각도 없으면 마지막."""
        for s in slots:
            if s.start <= pos < s.end:
                return s
        for s in reversed(slots):
            if pos == s.end:
                return s
        return slots[-1]

    def _split_after(self, pel: etree._Element, slot: Slot, pos: int) -> etree._Element:
        """조각 slot 을 pos(문단 글자 번호)에서 잘라, pos 뒤쪽을 새 run(같은 글자 모양)으로 떼어 낸다. 새 run 을 돌려준다.
        pos 가 조각 끝이면 그 조각 뒤의 요소들만 새 run 으로 간다(글은 안 자름)."""
        run, t = slot.run, slot.t
        at = pos - slot.start
        new_run = etree.Element(f"{HP}run", dict(run.attrib))
        new_t = etree.SubElement(new_t_parent := new_run, f"{HP}t")
        txt = slot.text
        slot.text = txt[:at]
        new_t.text = txt[at:]
        if slot.attr == "text":
            # t 의 안쪽 요소 전부가 새 t 로
            for sub in list(t):
                t.remove(sub); new_t.append(sub)
        else:
            # holder(안쪽 요소) 뒤의 형제 요소들이 새 t 로
            kids = list(t); i = kids.index(slot.holder)
            for sub in kids[i + 1:]:
                t.remove(sub); new_t.append(sub)
        # run 안에서 t 뒤의 요소(ctrl 등)도 새 run 으로
        rk = list(run); j = rk.index(t)
        for k in rk[j + 1:]:
            run.remove(k); new_run.append(k)
        if not (new_t.text or len(new_t)):
            new_run.remove(new_t)
        run.addnext(new_run)
        return new_run

    def _new_run_after(self, pel: etree._Element, run: etree._Element, char_pr: str, text: str, memo: str | None) -> etree._Element:
        nr = etree.Element(f"{HP}run", {"charPrIDRef": char_pr})
        if memo and self.use_memo:
            b, e = self._memo_field(memo)
            nr.append(b)
            etree.SubElement(nr, f"{HP}t").text = text
            nr.append(e)
        else:
            etree.SubElement(nr, f"{HP}t").text = text + (f"〔{self._short}〕" if memo and self._short else "")
            self._noted = self._noted or bool(memo and self._short)
        run.addnext(nr)
        return nr

    def _note_run(self, run: etree._Element, pr: str) -> None:
        """inline: run 뒤에 같은 모양으로 〔짧은 표기〕 run 하나."""
        nr = etree.Element(f"{HP}run", {"charPrIDRef": pr})
        etree.SubElement(nr, f"{HP}t").text = f"〔{self._short}〕"
        run.addnext(nr); self._noted = True

    def _delete_range(self, pel: etree._Element, start: int, end: int) -> None:
        for s in _slots(pel):
            if s.end <= start or s.start >= end:
                continue
            a, b = max(start, s.start) - s.start, min(end, s.end) - s.start
            txt = s.text; s.text = txt[:a] + txt[b:]

    def _split_at(self, pel: etree._Element, pos: int) -> etree._Element:
        """글자 번호 pos 앞에서 run 을 갈라 pos 바로 앞 run 을 돌려준다(여기 뒤에 새 run 을 끼운다)."""
        slots = _slots(pel)
        if not slots:
            raise ValueError("문단에 글이 없음")
        s = self._locate(slots, pos)
        if pos <= s.start:
            # 조각 앞 경계: 앞 조각이 있으면 그 run 뒤에, 없으면 이 run 앞에 빈 run 을 둔다
            prev = [x for x in slots if x.end <= pos]
            if prev:
                return prev[-1].run if prev[-1].run is not s.run else self._split_after(pel, prev[-1], pos).getprevious()
            empty = etree.Element(f"{HP}run", dict(s.run.attrib)); s.run.addprevious(empty); return empty
        if pos >= s.end:
            # 조각 끝 경계 — 이 run 에서 t 뒤 요소들을 떼어 내고 run 을 돌려준다
            self._split_after(pel, s, s.end)
            return s.run
        self._split_after(pel, s, pos)
        return s.run

    # ----- 편집 -----
    def apply(self, items: list[Item]) -> list[Applied]:
        """항목들을 문단별로 뒤에서부터 적용한다(앞쪽 위치가 흔들리지 않게)."""
        by_para: dict[int, list[Item]] = {}
        for it in items:
            by_para.setdefault(it.para, []).append(it)
        for para, its in by_para.items():
            pobj = self.book.paragraphs[para]; pel = pobj.element
            for it in sorted(its, key=lambda x: (x.start, x.end), reverse=True):
                self._noted = self._hidden = False
                try:
                    self._apply_one(pel, it); self.log.append(Applied(it, True, memo=self._noted, hidden=self._hidden))
                except _AlreadyMarked as e:
                    self.log.append(Applied(it, True, str(e)))
                except Exception as e:
                    self.log.append(Applied(it, False, f"{type(e).__name__}: {e}"))
            # python-hwpx 는 '바뀐' 섹션만 다시 쓴다. XML 을 직접 고쳤으니 바뀌었다고 알려 준다.
            sec = getattr(pobj, "section", None)
            if sec is not None and hasattr(sec, "mark_dirty"):
                sec.mark_dirty()
        if hasattr(self.book, "_texts"):
            self.book._texts = None                     # 글 캐시 비움(고친 뒤 다시 읽게)
        return self.log

    def add_legend_memo(self, text: str | None = None) -> bool:
        """표시 범례 메모 하나를 문서 첫 글 문단의 맨 앞(폭 없는 빈 조각)에 단다. 문단을 넣거나 글을 바꾸지 않는다
        (이어 교정할 때 구획 문단 번호가 같아야 하므로). 이미 교정 범례 메모가 있으면 달지 않는다. 단 메모가 있으면 True.
        일반 OCR 로 만든 문서의 범례('[연어OCR 표시 범례] …')는 색의 뜻이 달라(파랑 = 확인 권장 / 자동 교정) 교정 범례로 치지 않는다."""
        from ..grades import COLLATE_LEGEND
        if self._has_collate_legend():
            return False
        try:
            for pobj in self.book.paragraphs:
                slots = _slots(pobj.element)
                if not slots:
                    continue
                # 첫 조각(구역 정의 secPr 이 든 조각일 수 있음) 안에서 글 노드 바로 앞에 [메모 시작][빈 글][메모 끝] — 조각 순서·글은 그대로
                t0 = slots[0].t
                b, e = self._memo_field(text or COLLATE_LEGEND)
                empty = etree.Element(f"{HP}t"); empty.text = ""
                t0.addprevious(b); b.addnext(empty); empty.addnext(e)
                sec = getattr(pobj, "section", None)
                if sec is not None and hasattr(sec, "mark_dirty"):
                    sec.mark_dirty()
                if hasattr(self.book, "_texts"):
                    self.book._texts = None
                return True
        except Exception:
            return False
        return False

    def _apply_one(self, pel: etree._Element, it: Item) -> None:
        auto = it.auto and it.action in ("replace", "insert", "delete")
        grade = item_grade(it)
        from ..grades import needs_memo
        memo = self._memo_text(it) if needs_memo(it, self.memo_mode) else None
        self._short = self._short_note(it)              # 메모를 끌 때 글 뒤에 붙일 짧은 표시
        slots = _slots(pel)
        if not slots:
            raise ValueError("문단에 글 노드가 없음")
        if not auto:
            if it.end > it.start:
                over = [s for s in slots if s.start < it.end and s.end > it.start and s.end > s.start]
                if over and all(s.run.get("charPrIDRef") in self._ours for s in over):
                    raise _AlreadyMarked("이미 표시된 자리")
            elif any(s.run.get("charPrIDRef") in self._ours and s.start <= it.start <= s.end for s in slots):
                raise _AlreadyMarked("이미 표시된 자리")        # 폭 없는 표시(빈 run + 메모)가 그 자리에 이미 있음
        base_pr = self._locate(slots, it.start).run.get("charPrIDRef")
        pr = self._style_for(base_pr, grade)
        if memo and not self.use_memo and it.para in self._heads:
            memo = None                                 # inline: 제목(구획 기준) 문단에는 글 속 표기를 넣지 않고 색만(구획 이름이 바뀌지 않게)
            self._hidden = (auto and it.action == "delete") or (not auto and it.end == it.start)   # 색 칠할 글자도 없는 표시 — 보고에만(run 기록에 셈)
        elif memo and not self.use_memo and (at := _after_bracket(paragraph_text(pel), it.end)) is not None:
            # 이미 있는 〔…〕 안: 표기는 그 〕 뒤에(괄호를 겹치지 않게). 표시 자리보다 뒤라 먼저 넣어도 앞 위치가 흔들리지 않는다
            self._note_run(self._split_at(pel, at), pr); memo = None
        if auto:
            if it.action in ("replace", "delete") and it.end > it.start:
                self._delete_range(pel, it.start, it.end)
            run = self._split_at(pel, it.start)
            self._new_run_after(pel, run, pr, it.new if it.action != "delete" else "", memo)
            return
        # 표시만: 글자는 두고 모양을 바꾸고 메모를 단다
        if it.end > it.start:
            self._recolor_range(pel, it.start, it.end, pr, memo)
        else:
            run = self._split_at(pel, it.start)
            self._new_run_after(pel, run, pr, "", memo)

    def _recolor_range(self, pel: etree._Element, start: int, end: int, pr: str, memo: str | None) -> None:
        self._split_at(pel, end)
        self._split_at(pel, start)
        first = True; last = None
        for s in _slots(pel):
            if s.start >= start and s.end <= end and s.end > s.start:
                s.run.set("charPrIDRef", pr)
                if first and memo and self.use_memo:
                    b, e = self._memo_field(memo)
                    s.t.addprevious(b); s.t.addnext(e)
                first = False; last = s.run
        if memo and not self.use_memo and self._short and last is not None:
            self._note_run(last, pr)                                        # inline: 범위 뒤에 같은 모양으로 〔짧은 표기〕

    def _short_note(self, it: Item) -> str:
        if it.kind == "A":
            if not it.auto:                                                 # 반영하지 않은 제안 — 제안 글자를 글 속에 남긴다
                return {"replace": f"제안: {it.new}", "insert": f"삽입 제안: {it.new}", "delete": f"삭제 제안: {it.hwpx}"}[it.action]
            return {"replace": f"원: {it.hwpx}", "insert": "삽입", "delete": f"삭제: {it.hwpx}"}[it.action]
        if it.kind == "B2":
            if not it.auto:                                                 # 반영하지 않은 제안 — 고친 것처럼 '원:' 으로 적지 않는다
                return f"{'삽입 제안' if it.action == 'insert' else '제안'}: {it.new} · 후보 {'/'.join(it.candidates)}"
            return f"원: {it.hwpx or '-'} · 후보 {'/'.join(it.candidates)}"
        if it.kind == "B1":                                                 # 판독 글자가 없으면 HWPX 에만 있는 한글 풀이 자리
            return f"확인: 판독 {it.ocr}" if it.ocr else ("확인: 한글 풀이" if it.notice else "확인")
        if it.kind == "C" and it.action == "insert":
            return "삽입·확인" if it.auto else f"삽입 제안: 빠진 구간 {len(it.new)}자"     # 긴 구간 글은 보고에
        return "확인"

    def _memo_text(self, it: Item) -> str:
        where = f"PDF {it.pdf_page}쪽" if it.pdf_page else "PDF"
        if not it.auto and it.action in ("replace", "insert", "delete"):     # 반영하지 않은 제안(교정 정책·긴 구간) — 고쳤다고 적지 않는다
            what = {"replace": f"「{it.hwpx}」 → 「{it.new}」", "insert": f"빠진 {'구간' if it.kind == 'C' else '글자'} 「{it.new[:60]}」 넣기",
                    "delete": f"「{it.hwpx}」 지우기(원본에 없음)"}[it.action]
            why = f"긴 구간({len(it.ocr)}자)은 어느 정책에서도 자동으로 넣지 않음 — 원본 확인 뒤 사람이 넣음" if it.kind == "C" else "교정 정책상 반영하지 않음"
            return (f"[제안] {what} · {where} 판독 「{(it.ocr or '-')[:60]}」" + (f" · 후보 {'/'.join(it.candidates)}" if it.candidates else "")
                    + f" · {why}, 글자는 그대로 둠")
        if it.kind == "A":
            what = {"replace": f"원래 「{it.hwpx}」 → 「{it.new}」", "insert": f"빠진 글자 「{it.new}」 넣음", "delete": f"「{it.hwpx}」 지움(원본에 없음)"}[it.action]
            return f"[자동 교정] {what} · {where} 판독 「{it.ocr or '-'}」 · 판독에 불확실 표시 없음(2차를 했으면 1차·2차 일치)"
        if it.kind == "B2":
            return f"[자동 교정·확인] 원래 「{it.hwpx}」 → 「{it.new}」 · 간체 「{it.ocr}」 가 여러 번체로 갈라짐: {'/'.join(it.candidates)} · {where}"
        if it.kind == "B1":
            return f"[확인 필요] 판독이 불확실한 자리 · {where} 판독 「{it.ocr or '-'}」 · HWPX 「{it.hwpx or '-'}」 · 글자는 그대로 둠"
        if it.kind == "C":
            if it.action == "insert":
                return f"[자동 삽입·확인] HWPX 에 없던 구간을 {where} 판독으로 넣음 · 원문(간체) 「{it.ocr[:60]}」"
            return f"[확인 필요] {it.note} · {where} · 판독 「{(it.ocr or '-')[:60]}」"
        return f"[확인] {it.note} · {where}"

    # ----- 저장 -----
    def save_copy(self, out_path: str) -> str:
        """같은 폴더의 임시 파일(<이름>.tmp.hwpx)에 다 쓴 뒤 바꿔 넣는다 — 쓰다 멈춰도 쌓아 온 교정본이 깨지지 않게.
        있던 교정본은 <이름>.bak.hwpx 로 한 세대만 남긴다(이전 .bak 은 덮어씀). 백업은 옮기지 않고 복사 — 바꿔 넣기가 실패해도
        (한컴에 열린 교정본 등) 교정본이 사라지는 틈이 없고, 어디서 실패하든 임시 파일은 지운다."""
        for sec in getattr(self.doc, "sections", []) or []:
            if hasattr(sec, "mark_dirty"):
                sec.mark_dirty()
        out = str(out_path); root, ext = os.path.splitext(out); ext = ext or ".hwpx"
        tmp = f"{root}.tmp{ext}"                        # 확장자는 그대로(.hwp 로 저장하면 python-hwpx 가 확장자로 가른다)
        try:
            self.doc.save_to_path(tmp)
            if os.path.exists(out):
                shutil.copy2(out, f"{root}.bak{ext}")
            os.replace(tmp, out)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return out_path
