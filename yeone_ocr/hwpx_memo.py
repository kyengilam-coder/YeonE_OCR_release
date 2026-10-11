"""한컴 메모(MEMO 필드)를 글자 조각 하나에 건다. 일반 OCR 문서 조립(hwpx_builder)과 책갈피 교정(collate/hwpx_edit)이 같이 쓴다.

메모 XML 은 실제 한컴 문서의 표기를 그대로 따른다: 메모 글은 fieldBegin 안의 subList 에 들고, 범위는 그 글자 조각(hp:t) 앞뒤의
fieldBegin·fieldEnd 사이다. 메모 모양(memoPr)이 머리(header)에 없으면 하나 만든다.
"""
from __future__ import annotations
import random, time
from lxml import etree

HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
HH = "{http://www.hancom.co.kr/hwpml/2011/head}"


def rid() -> str:
    return str(random.randint(10 ** 9, 2 ** 31 - 1))


def count_memos(paragraph_elements) -> int:
    """문서에 이미 있는 메모 필드 수(새 메모 번호를 이어 매기려고)."""
    n = 0
    for el in paragraph_elements:
        for f in el.iter(f"{HP}fieldBegin"):
            if f.get("type") == "MEMO":
                n += 1
    return n


def _header(doc):
    parts = getattr(doc, "parts", None)
    hs = getattr(parts, "headers", None) if parts is not None else None
    return (hs if hs is not None else doc.headers)[0]


def memo_shape_id(doc) -> str:
    """머리의 메모 모양 id. 없으면 연두 메모 모양(id 1)을 만든다."""
    try:
        for el in _header(doc).element.iter(f"{HH}memoPr"):
            return el.get("id", "1")
    except Exception:
        pass
    try:
        root = _header(doc).element
        ref = root.find(f".//{HH}refList")
        if ref is not None:
            props = etree.SubElement(ref, f"{HH}memoProperties", {"itemCnt": "1"})
            etree.SubElement(props, f"{HH}memoPr", {"id": "1", "width": "15591", "lineWidth": "1", "lineType": "SOLID", "lineColor": "#B6D7AE",
                                                      "fillColor": "#F0FFE9", "activeColor": "#CFF1C7", "memoType": "NOMAL"})
    except Exception:
        pass
    return "1"


def memo_field(text: str, number: int, author: str, shape: str) -> tuple[etree._Element, etree._Element]:
    """메모 필드의 시작(ctrl/fieldBegin, 메모 글 포함)과 끝(ctrl/fieldEnd)."""
    fid, begin_id = rid(), rid()
    b = etree.Element(f"{HP}ctrl")
    fb = etree.SubElement(b, f"{HP}fieldBegin", {"id": begin_id, "type": "MEMO", "name": "", "editable": "1", "dirty": "1", "zorder": "0",
                                                 "fieldid": fid, "metaTag": ""})
    params = etree.SubElement(fb, f"{HP}parameters", {"cnt": "7", "name": ""})
    etree.SubElement(params, f"{HP}integerParam", {"name": "Prop"}).text = "0"
    etree.SubElement(params, f"{HP}stringParam", {"name": "Command"}).text = f"MEMO/{shape}/{number}/{fid}/{rid()}/{author}/\\;;"
    etree.SubElement(params, f"{HP}stringParam", {"name": "ID"}).text = f"memo{number}"
    etree.SubElement(params, f"{HP}integerParam", {"name": "Number"}).text = str(number)
    etree.SubElement(params, f"{HP}stringParam", {"name": "Author"}).text = author
    etree.SubElement(params, f"{HP}stringParam", {"name": "MemoShapeIDRef"}).text = shape
    etree.SubElement(params, f"{HP}stringParam", {"name": "CreateDateTime"}).text = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sub = etree.SubElement(fb, f"{HP}subList", {"id": "", "textDirection": "HORIZONTAL", "lineWrap": "BREAK", "vertAlign": "TOP", "linkListIDRef": "0",
                                                "linkListNextIDRef": "0", "textWidth": "0", "textHeight": "0", "hasTextRef": "0", "hasNumRef": "0"})
    p = etree.SubElement(sub, f"{HP}p", {"id": rid(), "paraPrIDRef": "0", "styleIDRef": "0", "pageBreak": "0", "columnBreak": "0", "merged": "0"})
    r = etree.SubElement(p, f"{HP}run", {"charPrIDRef": "0"})
    etree.SubElement(r, f"{HP}t").text = text
    e = etree.Element(f"{HP}ctrl")
    etree.SubElement(e, f"{HP}fieldEnd", {"beginIDRef": begin_id, "fieldid": fid})
    return b, e


class MemoWriter:
    """한 문서에 메모를 차례로 단다(번호를 이어 매김)."""

    def __init__(self, doc, author: str = "YeonE_OCR", start: int = 0):
        self.doc, self.author, self.n = doc, author, start
        self._shape: str | None = None

    def wrap_t(self, t_el: etree._Element, text: str) -> None:
        """글 노드(hp:t) 하나를 메모 범위로 감싼다."""
        if self._shape is None:
            self._shape = memo_shape_id(self.doc)
        self.n += 1
        b, e = memo_field(text, self.n, self.author, self._shape)
        t_el.addprevious(b); t_el.addnext(e)

    def wrap_run(self, run_el: etree._Element | None, text: str) -> bool:
        if run_el is None:
            return False
        t = run_el.find(f"{HP}t")
        if t is None:
            t = etree.SubElement(run_el, f"{HP}t")
        self.wrap_t(t, text)
        return True
