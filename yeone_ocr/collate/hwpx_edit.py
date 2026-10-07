"""HWPX 를 서식 그대로 둔 채 구획 안의 글자만 고친다.

원칙
- 문단 안의 <hp:t> 글만 손댄다. 각주·메모·필드·줄 배치 같은 다른 요소는 그대로 둔다(한컴이 줄 배치를 다시 계산한다).
- 글자 번호는 python-hwpx 의 paragraph.text 와 같은 셈법이다: hp:t 의 글 + 안쪽 요소(hp:tab 은 "\\t", 그 밖은 자기 글) + 그 뒤 글(tail),
  run 바로 아래의 hp:tab / ctrl(id=tab) 은 "\\t" 한 글자.
- 고친 글자는 원래 글자 모양을 바탕으로 색·형광펜만 바꾼 글자 모양(charPr)을 새로 만들어 입힌다.
    자동 교정(A·B2·누락 삽입)  파란 글자 + 연두 형광펜      BLUE / GREEN
    확인 요청(B1·C 표시)       빨간 글자 + 노란 형광펜      RED / YELLOW
- 메모: 한컴 메모 필드(MEMO)를 고친 글자에 붙여 원래 글자·PDF 쪽·근거를 적는다. 메모를 끄면 글 뒤에 〔원: ○〕 로 적는다.
- 원본 파일은 덮어쓰지 않고 사본으로 저장한다.
"""
from __future__ import annotations
import random, time
from dataclasses import dataclass
from lxml import etree
from .collate import Item

HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
HH = "{http://www.hancom.co.kr/hwpml/2011/head}"
BLUE, GREEN = "#1F4FBF", "#D8F5C8"
RED, YELLOW = "#C00000", "#FFFF00"
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
    def __init__(self, book, *, author: str = "YeonE_OCR", memo: bool = True):
        self.book = book
        self.doc = book.doc
        self.author = author
        self.use_memo = memo
        self._pr_cache: dict[tuple[str, str, str], str] = {}
        self._memo_no = self._count_memos()
        self._short = ""
        self.log: list[Applied] = []
        self._ours: set[str] = self._marked_char_prs()      # 이전 실행이 만든 색 글자 모양 id — 같은 자리를 또 표시하지 않기 위해

    def _marked_char_prs(self) -> set[str]:
        out = set()
        try:
            for el in self.doc.headers[0].element.iter(f"{HH}charPr"):
                if (el.get("textColor") or "").upper() in (BLUE.upper(), RED.upper()):
                    out.add(el.get("id", ""))
        except Exception:
            pass
        return out

    # ----- 글자 모양 -----
    def _style_for(self, base_pr: str | None, color: str, shade: str) -> str:
        key = (base_pr or "", color, shade)
        if key not in self._pr_cache:
            try:
                self._pr_cache[key] = str(self.doc.styles.ensure_run(color=color, highlight=shade, base_char_pr_id=base_pr))
            except Exception:
                self._pr_cache[key] = str(self.doc.ensure_run_style(color=color, highlight=shade))
        return self._pr_cache[key]

    # ----- 메모 -----
    def _count_memos(self) -> int:
        n = 0
        for p in self.book.paragraphs:
            for el in p.element.iter(f"{HP}fieldBegin"):
                if el.get("type") == "MEMO":
                    n += 1
        return n

    def _memo_shape_id(self) -> str:
        try:
            for el in self.doc.headers[0].element.iter(f"{HH}memoPr"):
                return el.get("id", "1")
        except Exception:
            pass
        try:
            root = self.doc.headers[0].element
            ref = root.find(f".//{HH}refList")
            if ref is not None:
                props = etree.SubElement(ref, f"{HH}memoProperties", {"itemCnt": "1"})
                etree.SubElement(props, f"{HH}memoPr", {"id": "1", "width": "15591", "lineWidth": "1", "lineType": "SOLID", "lineColor": "#B6D7AE",
                                                          "fillColor": "#F0FFE9", "activeColor": "#CFF1C7", "memoType": "NOMAL"})
        except Exception:
            pass
        return "1"

    def _memo_field(self, text: str) -> tuple[etree._Element, etree._Element]:
        """메모 필드의 시작(ctrl/fieldBegin, 메모 글 포함)과 끝(ctrl/fieldEnd). 업로드 문서의 한컴 표기를 그대로 따른다."""
        self._memo_no += 1
        fid, begin_id, shape = _rid(), _rid(), self._memo_shape_id()
        b = etree.Element(f"{HP}ctrl")
        fb = etree.SubElement(b, f"{HP}fieldBegin", {"id": begin_id, "type": "MEMO", "name": "", "editable": "1", "dirty": "1", "zorder": "0",
                                                     "fieldid": fid, "metaTag": ""})
        params = etree.SubElement(fb, f"{HP}parameters", {"cnt": "7", "name": ""})
        etree.SubElement(params, f"{HP}integerParam", {"name": "Prop"}).text = "0"
        etree.SubElement(params, f"{HP}stringParam", {"name": "Command"}).text = f"MEMO/{shape}/{self._memo_no}/{fid}/{_rid()}/{self.author}/\\;;"
        etree.SubElement(params, f"{HP}stringParam", {"name": "ID"}).text = f"memo{self._memo_no}"
        etree.SubElement(params, f"{HP}integerParam", {"name": "Number"}).text = str(self._memo_no)
        etree.SubElement(params, f"{HP}stringParam", {"name": "Author"}).text = self.author
        etree.SubElement(params, f"{HP}stringParam", {"name": "MemoShapeIDRef"}).text = shape
        etree.SubElement(params, f"{HP}stringParam", {"name": "CreateDateTime"}).text = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        sub = etree.SubElement(fb, f"{HP}subList", {"id": "", "textDirection": "HORIZONTAL", "lineWrap": "BREAK", "vertAlign": "TOP", "linkListIDRef": "0",
                                                    "linkListNextIDRef": "0", "textWidth": "0", "textHeight": "0", "hasTextRef": "0", "hasNumRef": "0"})
        p = etree.SubElement(sub, f"{HP}p", {"id": _rid(), "paraPrIDRef": "0", "styleIDRef": "0", "pageBreak": "0", "columnBreak": "0", "merged": "0"})
        r = etree.SubElement(p, f"{HP}run", {"charPrIDRef": "0"})
        etree.SubElement(r, f"{HP}t").text = text
        e = etree.Element(f"{HP}ctrl")
        etree.SubElement(e, f"{HP}fieldEnd", {"beginIDRef": begin_id, "fieldid": fid})
        return b, e

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
        run.addnext(nr)
        return nr

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
                try:
                    self._apply_one(pel, it); self.log.append(Applied(it, True))
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

    def _apply_one(self, pel: etree._Element, it: Item) -> None:
        auto = it.auto and it.action in ("replace", "insert", "delete")
        color, shade = (BLUE, GREEN) if auto else (RED, YELLOW)
        memo = self._memo_text(it)
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
        pr = self._style_for(base_pr, color, shade)
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
        first = True
        for s in _slots(pel):
            if s.start >= start and s.end <= end and s.end > s.start:
                s.run.set("charPrIDRef", pr)
                if first and memo and self.use_memo:
                    b, e = self._memo_field(memo)
                    s.t.addprevious(b); s.t.addnext(e)
                first = False

    def _short_note(self, it: Item) -> str:
        if it.kind == "A":
            return {"replace": f"원: {it.hwpx}", "insert": "삽입", "delete": f"삭제: {it.hwpx}"}[it.action]
        if it.kind == "B2":
            return f"원: {it.hwpx or '-'} · 후보 {'/'.join(it.candidates)}"
        if it.kind == "B1":
            return f"확인: 판독 {it.ocr or '-'}"
        if it.kind == "C" and it.action == "insert":
            return "삽입·확인"
        return "확인"

    def _memo_text(self, it: Item) -> str:
        where = f"PDF {it.pdf_page}쪽" if it.pdf_page else "PDF"
        if it.kind == "A":
            what = {"replace": f"원래 「{it.hwpx}」 → 「{it.new}」", "insert": f"빠진 글자 「{it.new}」 넣음", "delete": f"「{it.hwpx}」 지움(원본에 없음)"}[it.action]
            return f"[자동 교정] {what} · {where} 판독 「{it.ocr or '-'}」 · 1차·2차 판독 일치"
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
        for sec in getattr(self.doc, "sections", []) or []:
            if hasattr(sec, "mark_dirty"):
                sec.mark_dirty()
        self.doc.save_to_path(out_path)
        return out_path
