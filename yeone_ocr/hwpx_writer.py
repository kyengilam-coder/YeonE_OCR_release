"""HWPX 작성기 하나: 블록 → 문단·런·책갈피·메모·그림·표 → 저장 → 다시 열어 검사. 모양은 '모양 원천(look)' 이 정한다.

- DefaultLook(기본 모양, hwpx_builder.build_hwpx): 새 문서, 함초롬 글자 모양, 원본 쪽마다 기록 줄·쪽 나누기, 제목 문단 모양(가운데·앞뒤 간격).
- TemplateLook(사용자 양식, hwpx_template.build_hwpx_template): 양식의 쪽 설정은 두고 본문을 비운 뒤 쓰임(role)마다 양식 스타일, 한문 문단은
  줄 나눔을 글자 단위로 바꾼 문단 모양 사본, 양식 글자 모양 위에 등급 색 덧입히기, 쪽 번호·첫 문단 합치기.
문단 하나를 쓰는 셈(고지 글 속 넣기, 등급·표시 조각, 책갈피 자리, 쓰임별 글자 스타일 범위, 메모, 등급 수 세기)과 그림·표·저장·검사는 여기 한곳에 있다.
두 모양의 차이는 look 의 Para 와 몇 가지 깃발(count_pieces·styled·skip_if_empty·heading)로만 둔다 — 예전 두 작성기의 출력 그대로(tests/test_hwpx_golden)."""
from __future__ import annotations
import copy, re, zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import Path
from .blocks import Block, anchor_map
from .grades import styled_runs, GRADE_STYLE, STYLE_ALIAS
from .marks import split_marks
from .presets import Preset, resolve_preset

HH = "{http://www.hancom.co.kr/hwpml/2011/head}"
HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
HC = "{http://www.hancom.co.kr/hwpml/2011/core}"
OPF = "{http://www.idpf.org/2007/opf/}"
HWPUNIT_PER_MM = 7200 / 25.4
BODY_WIDTH_MM = 160.0   # 기본 모양: A4, 좌우 여백 25mm
MEMO_AUTHOR = "YeonE_OCR"
FONT_BODY, FONT_HEAD = "함초롬바탕", "함초롬돋움"
HEADING_FORMAT = {"h1": dict(alignment="CENTER", spacing_before_pt=14, spacing_after_pt=7),
                  "h2": dict(alignment="LEFT", spacing_before_pt=10, spacing_after_pt=4),
                  "h3": dict(alignment="LEFT", spacing_before_pt=6, spacing_after_pt=2)}
COLOR_MARK, SHADE_MARK, COLOR_NOTE, COLOR_META = "#C00000", "#FFFF00", "#666666", "#8A8A8A"
LARGE_FIGURES = ("원형나경도", "복잡한표", "계통도", "도식")    # 큰 도판(large_min_mm 이상)
GRADES = ("green", "blue", "red_ctx")                            # 등급 수를 세는 모양
# 쓰임 → 양식 스타일 이름은 짜임 규칙(presets, data/preset_*.json 의 styles)에 있다 — 규칙에 없는 쓰임은 regions 의 이름, 그것도 없으면 바탕글.
HEADING_ROLES = {"chapter", "section", "subsection", "entry", "caption"}
BASIC_FORMATS = {
    "chapter": (16, True, "CENTER", 14, 7),
    "section": (14, True, "LEFT", 10, 4),
    "subsection": (12, True, "LEFT", 6, 2),
    "body": (11, False, "JUSTIFY", 0, 0),
    "commentary": (10.5, False, "JUSTIFY", 0, 0),
    "small": (9.5, False, "JUSTIFY", 0, 0),
    "caption": (9.5, False, "CENTER", 3, 3),
    "note": (9, False, "LEFT", 0, 0),
}
_HAN = re.compile("[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003134f]")   # 한자(확장 A·통합·호환·확장 B~) — 이스케이프로 적는다(호환 한자 豈 가 NFC 로 바뀌면 한글까지 한자로 센다)


@dataclass
class Para:
    """쓸 문단 하나(look 이 블록에서 만든다). key: 기본 모양은 글자 모양 열쇠(body·small·meta·note·h1·h2·h3·chk_h·chk), 양식은 쓰임(role)."""
    text: str
    key: str
    base: str | int                  # 글자 모양 id(조각이 없을 때·책갈피 기본)
    para_pr: str | int               # 문단 모양 id
    style: str | int                 # 스타일 id
    styled: bool = True              # True = 늘 등급·표시 조각으로 나눈다. False = 범위·메모·책갈피가 있을 때만(기본 모양의 기록 줄·제목·작업 기록)
    spans: list = field(default_factory=list)
    notes: list = field(default_factory=list)     # (자리, 메모 글) — memos=False 면 글 끝에 〔고지: …〕
    anchors: list = field(default_factory=list)   # (자리, 책갈피 이름)
    cstyles: list | None = None      # 쓰임별 글자 범위(양식만). None 이면 글자 스타일로 가르지 않는다
    cs_fill: str | None = None       # 글자 스타일 바탕(양식의 소주 문단은 전부 small — cstyles 가 덮는다)
    page_break: bool = False
    heading: bool = False            # 제목 수(stats["headings"])에 센다
    skip_if_empty: bool = False      # 글도 책갈피도 없으면 문단을 만들지 않는다(양식의 빈 제목 — 런 없는 <hp:p/> 를 막는다)
    fmt: dict | None = None          # 기본 모양 제목의 문단 꾸밈(set_paragraph_format)


def figure_size(b: Block, body_w: float, h_cap: float, min_mm: float, large_min_mm: float) -> tuple[float, float]:
    """도판 크기(mm): 쪽 폭 비율 × 1.15(판면이 쪽 폭보다 좁으므로 조금 키움)를 본문 폭 안으로, 큰 도판(나경도·복잡한 표·계통도·도식)은 large_min_mm,
    그 밖은 min_mm 이상(본문 폭을 넘지 않게), 높이가 h_cap 을 넘으면 비율대로 줄인다."""
    floor = large_min_mm if b.kind in LARGE_FIGURES else min_mm
    w = max(min(floor, body_w), min(body_w, body_w * max(0.0, min(1.0, b.frac_w)) * 1.15))
    h = w * b.ratio
    if h > h_cap:
        h = h_cap; w = h / b.ratio
    return w, h


def figure_width_mm(b: Block, min_mm: float, large_min_mm: float) -> float:
    """기본 모양(A4 본문 160mm)의 도판 폭 — DOCX 도 같이 쓴다."""
    return figure_size(b, BODY_WIDTH_MM, 1e9, min_mm, large_min_mm)[0]


def template_styles(doc) -> dict[str, dict]:
    """양식의 스타일: {이름: {"id", "type", "para", "char"}}."""
    out = {}
    for st in doc.oxml.headers[0].element.iter(f"{HH}style"):
        out[st.get("name", "")] = {"id": st.get("id"), "type": st.get("type"), "para": st.get("paraPrIDRef"), "char": st.get("charPrIDRef")}
    return out


def _ensure_basic_styles(doc, preset: Preset, *, template: bool = False) -> dict[str, dict]:
    """기본 규칙의 여덟 문단 스타일을 만든다. 양식에 같은 이름이 있으면 그 모양을 그대로 쓴다.
    예전 이름만 있으면 그 스타일의 모양을 잇고, 없으면 번호 없는 기본 문단 모양에서 만든다."""
    header = doc.oxml.headers[0]
    existing = template_styles(doc)
    created = []
    for role, (size, bold, alignment, before, after) in BASIC_FORMATS.items():
        names = preset.para_styles.get(role, ())
        if not names or names[0] in existing:
            continue
        inherited = next((existing[n] for n in names[1:] if n in existing and existing[n]["type"] == "PARA"), None) if template else None
        if inherited:
            ppr, cpr = inherited["para"], inherited["char"]
        else:
            cpr = doc.oxml.ensure_run_style(font=FONT_HEAD if bold else FONT_BODY, size=size, bold=bold,
                                           color=COLOR_NOTE if role == "note" else "#000000")
            ppr = header.ensure_paragraph_format(base_para_pr_id=0, alignment=alignment, heading={"type": "NONE"},
                                                margins={"prev": int(before * 100), "next": int(after * 100)})
        style_id = header.ensure_style(names[0], style_type="PARA", para_pr_id_ref=ppr, char_pr_id_ref=cpr, next_style_id_ref=0)
        existing[names[0]] = {"id": style_id, "type": "PARA", "para": str(ppr), "char": str(cpr)}
        created.append(names[0])
    body = next((existing[n] for n in preset.para_styles.get("body", ()) if n in existing and existing[n]["type"] == "PARA"), None)
    if body:
        for name in created:
            header.ensure_style(name, next_style_id_ref=body["id"])
    return template_styles(doc)


def _break_word_para(header, base_id: str, cache: dict) -> str:
    """base_id 문단 모양을 복사해 줄 나눔을 '글자 단위'로 바꾼 문단 모양 id(한자 문단에 씀). 이미 둘 다 글자 단위면 그대로.
    라틴 쪽도 바꾼다: 판독 글은 쉼표·마침표를 반각(,.)으로 적는 일이 많아 「就州者大,徑二四寸,長五六寸.」 가 라틴 단어 하나로 묶이고,
    어절 단위(KEEP_WORD)면 그 덩어리가 통째로 다음 줄로 넘어가 앞 줄이 양쪽 맞춤으로 벌어진다."""
    if base_id in cache:
        return cache[base_id]
    root = header.element
    props = root.find(f".//{HH}paraProperties")
    base = next((p for p in props.iter(f"{HH}paraPr") if p.get("id") == str(base_id)), None) if props is not None else None
    brk = base.find(f".//{HH}breakSetting") if base is not None else None
    if brk is None or (brk.get("breakNonLatinWord") == "BREAK_WORD" and brk.get("breakLatinWord", "BREAK_WORD") == "BREAK_WORD"):
        cache[base_id] = str(base_id)
        return cache[base_id]
    new = copy.deepcopy(base)
    new_id = str(max(int(p.get("id", "0")) for p in props.iter(f"{HH}paraPr")) + 1)
    new.set("id", new_id)
    for b in new.iter(f"{HH}breakSetting"):
        b.set("breakNonLatinWord", "BREAK_WORD"); b.set("breakLatinWord", "BREAK_WORD")
    props.append(new)
    props.set("itemCnt", str(sum(1 for _ in props.iter(f"{HH}paraPr"))))
    header.mark_dirty()
    cache[base_id] = new_id
    return new_id


def _body_size_mm(doc) -> tuple[float, float]:
    """양식 쪽의 본문 너비·높이(mm)."""
    try:
        sec = doc.sections[0].element
        pp = sec.find(f".//{HP}pagePr"); mg = pp.find(f"{HP}margin")
        w = int(pp.get("width")) - int(mg.get("left")) - int(mg.get("right")) - int(mg.get("gutter", "0"))
        h = int(pp.get("height")) - int(mg.get("top")) - int(mg.get("bottom")) - int(mg.get("header", "0")) - int(mg.get("footer", "0"))
        return w / HWPUNIT_PER_MM, h / HWPUNIT_PER_MM
    except Exception:
        return 160.0, 230.0


class DefaultLook:
    """기본 모양: 가로쓰기 정리본(제목은 계층대로, 본문 함초롬바탕, 소주 작게, 쪽 기록 줄 작은 회색, 원본 쪽마다 쪽 나누기).
    모든 문단은 '바탕글'(문단 모양 0, 스타일 0)을 명시한다. python-hwpx 는 앞 문단의 모양을 물려주므로, 제목(개요 스타일) 다음 문단들이
    개요 모양을 받아 한컴이 "가. 나. 다." 자동 번호를 붙이던 문제를 막는다."""
    count_pieces = False             # 등급 수를 조각(seg)마다 — 예전 기본 모양 그대로
    SIZE = {"body": 11, "small": 9.5, "chk": 9, "note": 9, "meta": 8}

    def __init__(self, page_breaks: bool = True, author: str = MEMO_AUTHOR):
        self.page_breaks, self.author = page_breaks, author
        self.extra_stats: dict = {}

    def open(self, highlight: bool):
        from hwpx import HwpxDocument
        doc = HwpxDocument.new()
        styles = getattr(doc, "styles", None)
        self._rs = styles.ensure_run if styles is not None and hasattr(styles, "ensure_run") else doc.ensure_run_style   # 6.x 의 새 이름 우선
        try:
            doc.set_page_setup(paper_size="A4", margin_left_mm=25, margin_right_mm=25, margin_top_mm=22, margin_bottom_mm=20)
        except Exception:
            pass
        rs = self._rs
        self.S = {                   # 만드는 차례도 예전 그대로(글자 모양 id 차례)
            "body": rs(font=FONT_BODY, size=11),
            "small": rs(font=FONT_BODY, size=9.5),
            "mark": rs(font=FONT_BODY, size=11, bold=True, color=COLOR_MARK, highlight=SHADE_MARK) if highlight else rs(font=FONT_BODY, size=11),
            "note": rs(font=FONT_BODY, size=9, color=COLOR_NOTE),
            "meta": rs(font=FONT_HEAD, size=8, color=COLOR_META),
            "h1": rs(font=FONT_HEAD, size=16, bold=True),
            "h2": rs(font=FONT_HEAD, size=14, bold=True),
            "h3": rs(font=FONT_HEAD, size=12, bold=True),
            "chk_h": rs(font=FONT_HEAD, size=9.5, bold=True, color=COLOR_NOTE),
            "chk": rs(font=FONT_BODY, size=9, color=COLOR_NOTE),
        }
        self.highlight = highlight; self._grade: dict = {}; self._first_meta = True
        return doc

    def _para(self, text: str, key: str, **kw) -> Para:
        return Para(text=text, key=key, base=self.S[key], para_pr=0, style=0, **kw)

    def paras(self, b: Block):
        if b.type == "meta":
            yield self._para(b.text, "meta", styled=False, notes=b.memos, page_break=self.page_breaks and not self._first_meta)
            self._first_meta = False
        elif b.type == "heading":
            # 개요 스타일(add_heading)을 쓰지 않는다: 한컴이 개요 번호("1." "가.")를 덧붙여 원본에 없는 글자가 생긴다.
            # 제목은 바탕글 문단에 굵은 큰 글자로 두고, 자리 잡기(가운데·앞뒤 간격)만 문단 모양으로 준다. 제목의 등급 범위는 칠하지 않는다(예전 그대로).
            lvl = "h1" if b.level <= 1 else ("h2" if b.level == 2 else "h3")
            yield self._para(b.text, lvl, styled=False, notes=b.memos, anchors=b.anchors, heading=True, fmt=HEADING_FORMAT[lvl])
        elif b.type == "para":
            yield self._para(b.text, "small" if b.kind == "소주" else "body", spans=b.spans, notes=b.memos, anchors=b.anchors)
        elif b.type == "note":
            yield self._para(b.text, "note", anchors=b.anchors)            # 작업 메모 문단에는 메모를 달지 않는다(예전 그대로)
        elif b.type == "uncertain":
            yield self._para(b.text or "[판독 확인 필요]", "chk_h", styled=False)
            for it in b.items:
                yield self._para("· " + it, "chk")
        elif b.type == "report":
            yield self._para("〔작업 기록〕", "h2", styled=False)
            for line in b.text.splitlines():
                yield self._para(line, "chk", styled=False)

    def char(self, para: Para, kind: str, cs_kind) -> str:
        kind = STYLE_ALIAS.get(kind, kind)
        if kind == "body":
            return self.S[para.key]
        if kind == "note":
            return self.S["note"]
        if not self.highlight:
            return self.S[para.key]
        key = (para.key, kind)
        if key not in self._grade:                                     # 본문·소주 크기에 맞춘 등급 글자 모양(같은 것은 한 번만 만든다)
            g = GRADE_STYLE[kind]
            kw = dict(font=FONT_BODY, size=self.SIZE.get(para.key, 11), bold=g["bold"], color=g["color"])
            if g["highlight"]:
                kw["highlight"] = g["highlight"]
            if g["underline"]:
                kw.update(underline=True, underline_shape=g["underline"], underline_color=g["color"])
            try:
                self._grade[key] = self._rs(**kw)
            except (TypeError, ValueError):                           # 밑줄 모양을 모르는 옛 python-hwpx
                kw.pop("underline_shape", None); kw.pop("underline_color", None)
                self._grade[key] = self._rs(**kw)
        return self._grade[key]

    def after_para(self, doc, para: Para) -> None:
        if para.fmt:
            try:
                doc.set_paragraph_format(paragraph_index=len(doc.paragraphs) - 1, **para.fmt)
            except Exception:
                pass

    def picture(self, doc, b: Block, min_mm: float, large_min_mm: float) -> dict:
        w, h = figure_size(b, BODY_WIDTH_MM, 230.0, min_mm, large_min_mm)   # 쪽 높이를 넘지 않게
        return dict(width_mm=round(w, 1), height_mm=round(h, 1))

    def finish(self, doc) -> None:
        pass


class BasicLook(DefaultLook):
    """양식 없이도 한글에서 다시 적용할 수 있는 대·중·소제목과 문단-1~5 스타일을 쓴다."""
    SIZE = {**DefaultLook.SIZE, **{role: fmt[0] for role, fmt in BASIC_FORMATS.items()}}

    def __init__(self, preset: Preset, page_breaks: bool = True, author: str = MEMO_AUTHOR):
        super().__init__(page_breaks=page_breaks, author=author)
        self.preset = replace(preset, para_styles={**resolve_preset("basic").para_styles, **preset.para_styles})

    def open(self, highlight: bool):
        doc = super().open(highlight)
        styles = _ensure_basic_styles(doc, self.preset)
        self.role_styles = {}
        used = {}
        for role, names in self.preset.para_styles.items():
            name = next((n for n in names if n in styles and styles[n]["type"] == "PARA"), None)
            if name is not None:
                st = styles[name]
                self.role_styles[role] = st
                self.S[role] = st["char"]
                used[role] = name
        self.extra_stats = {"styles": used}
        return doc

    def paras(self, b: Block):
        if b.type == "heading":
            role = b.role or ("caption" if b.kind in ("그림제목", "표제목") else ("chapter", "section", "subsection")[min(3, max(1, b.level)) - 1])
        elif b.type == "para":
            role = b.role or ("small" if b.kind == "소주" else "body")
        else:
            role = "note"
        role = role if role in self.role_styles else "body"
        st = self.role_styles[role]
        for para in super().paras(b):
            yield replace(para, key=role, base=st["char"], para_pr=st["para"], style=st["id"], fmt=None,
                          cstyles=list(b.cstyles) if b.type == "para" else None)

    def char(self, para: Para, kind: str, cs_kind) -> str:
        if cs_kind == "small":
            para = replace(para, key="small", base=self.S["small"])
        return super().char(para, kind, cs_kind)


class TemplateLook:
    """사용자 양식(HWPX)의 스타일로 내용별 문서를 채운다. 양식의 쪽 설정(용지·여백)은 그대로 두고 본문은 비운 뒤, 블록의 쓰임(structure.ROLES)마다
    양식 스타일을 골라 문단을 넣는다. 확인 등급 색·고지 메모는 양식 글자 모양 위에 덧입힌다(바탕은 양식 그대로, 색·굵기·형광펜만 바꾼 글자 모양을 따로).
    한문 문단은 줄 나눔을 '글자 단위'로 바꾼 문단 모양을 쓴다: 양식 스타일 대부분이 '어절 단위'(KEEP_WORD)라 띄어쓰기 없는 한문이 긴 덩어리째
    다음 줄로 넘어가며 줄 가운데 큰 빈칸이 생긴다. 스타일 연결은 그대로라 한글에서 스타일을 다시 적용하면 양식대로 돌아간다."""
    count_pieces = True              # 등급 수를 조각(cut)마다 — 예전 양식 작성기 그대로

    def __init__(self, template: str, preset: Preset | None = None, author: str = "YeonE_OCR"):
        """preset: 쓰임 → 스타일 이름을 줄 짜임 규칙. None 이면 kaibao(본초학 양식 이름 — 예전 PARA_STYLES·CHAR_STYLES 그대로).
        규칙에 없는 쓰임은 regions 의 이름으로(그것도 양식에 없으면 바탕글)."""
        preset = preset or resolve_preset("kaibao")
        base = resolve_preset("regions")
        self.preset = preset
        self.template, self.author = template, author
        self.para_styles = {**base.para_styles, **preset.para_styles}
        self.char_styles = {**base.char_styles, **preset.char_styles}

    def open(self, highlight: bool):
        from hwpx import HwpxDocument
        doc = HwpxDocument.open(self.template)
        sec0 = doc.sections[0]
        dropped = {"sections": 0, "headers": 0, "footers": 0, "controls": 0, "paragraphs": 0}   # 양식에서 뺀 것(cli 가 알린다)
        for extra in list(doc.sections[1:]):
            doc.remove_section(extra); dropped["sections"] += 1
        stories = lambda: [len(sec0.element.findall(f".//{HP}ctrl/{HP}{k}")) for k in ("header", "footer")]   # 한글이 읽는 머리말·꼬리말(hp:ctrl 안)
        before = stories()
        rep = sec0.clear_body(on_control_content="strip")             # 본문과 글·개체가 든 머리말·꼬리말을 비운다(쪽 설정은 그대로)
        for hide in list(sec0.element.iter(f"{HP}pageHiding")):      # 양식 첫 쪽(표지)의 머리말·쪽 번호 감추기는 빼고
            ctrl = hide.getparent(); ctrl.getparent().remove(ctrl)
        after = stories()
        dropped.update(headers=before[0] - after[0], footers=before[1] - after[1],
                       controls=getattr(rep, "stripped_controls", 0), paragraphs=getattr(rep, "removed_paragraphs", 0))
        # 양식에 쪽 번호(남은 머리말·꼬리말 안 pageNum/autoNum)가 이미 있으면 그대로 두고 우리 것은 넣지 않는다
        self.has_page_num = any(sec0.element.find(f".//{HP}ctrl/{HP}{k}//{HP}{n}") is not None for k in ("header", "footer") for n in ("pageNum", "autoNum"))
        # 비운 본문이 쓰던 양식 그림(BinData)은 꾸러미에 그대로 둔다: 구역이 둘 이상인 양식에서 remove_unused_images() 를 부르면 뒤에 넣는
        # 그림이 꾸러미 목록(content.hpf)에 오르지 않아 한글이 열지 못하는 파일이 된다. 한글이 저장할 때 알아서 지운다.
        self.doc, self.sec0, self.header = doc, sec0, doc.oxml.headers[0]
        self.styles = _ensure_basic_styles(doc, self.preset, template=True) if self.preset.id == "basic" else template_styles(doc)
        self.base = self.styles.get("바탕글") or {"id": "0", "type": "PARA", "para": "0", "char": "0"}
        self.extra_stats = {"styles": {}, "missing": [], "template_dropped": dropped, "page_number_from_template": self.has_page_num}
        self.body_w, self.body_h = _body_size_mm(doc)
        self.highlight = highlight; self._brk: dict = {}; self._run: dict = {}; self._pic_ppr: list = []
        return doc

    def para_style(self, role: str) -> dict:
        st_ = self.extra_stats
        for name in self.para_styles.get(role, ("바탕글",)):
            st = self.styles.get(name)
            if st and st["type"] == "PARA":
                st_["styles"][role] = name
                return st
        if role not in st_["missing"]:
            st_["missing"].append(role)
        st_["styles"][role] = "바탕글"
        return self.base

    def char_style(self, kind: str) -> str | None:
        for name in self.char_styles.get(kind, ()):
            st = self.styles.get(name)
            if st:
                return st["char"]
        return None

    def _para(self, b: Block, role: str, *, spans=(), cstyles=()) -> Para:
        st = self.para_style(role)
        han = len(_HAN.findall(b.text)) >= max(1, len(b.text.strip())) * 0.5
        ppr = _break_word_para(self.header, st["para"], self._brk) if han and role not in HEADING_ROLES else st["para"]
        return Para(text=b.text, key=role, base=st["char"], para_pr=ppr, style=st["id"], spans=list(spans), notes=list(b.memos), anchors=list(b.anchors),
                    cstyles=list(cstyles), cs_fill="small" if role == "small" else None, heading=role in HEADING_ROLES, skip_if_empty=role in HEADING_ROLES)

    def paras(self, b: Block):
        if b.type in ("heading", "para"):
            yield self._para(b, b.role or ("section" if b.type == "heading" else "body"), spans=b.spans, cstyles=b.cstyles)
        elif b.type in ("note", "meta"):
            yield self._para(b, "note")                                # 등급 범위·쓰임 범위 없이 메모·책갈피만
        elif b.type == "uncertain":
            yield self._para(Block(type="para", text=b.text or "[판독 확인 필요]"), "note")
            for it in b.items:
                yield self._para(Block(type="para", text="· " + it), "note")
        elif b.type == "report":
            for line in ["〔작업 기록〕"] + b.text.splitlines():
                yield self._para(Block(type="para", text=line), "note")

    def _derived(self, base_char: str, kind: str) -> str:
        """양식 글자 모양 base_char 에 등급·작업 표시 모양을 덧입힌 글자 모양 id(같은 것은 한 번만)."""
        if kind == "body" or (not self.highlight and kind != "note"):
            return base_char
        key = (base_char, kind)
        if key not in self._run:
            if kind == "note":
                kw = dict(color=COLOR_NOTE)
            else:
                g = GRADE_STYLE[STYLE_ALIAS.get(kind, kind)]
                kw = dict(color=g["color"])
                if g["bold"]:
                    kw["bold"] = True                                   # 굵기는 등급이 굵게일 때만 — 아니면 양식 글자 모양(본경 조문·제목)의 굵기를 그대로
                if g["highlight"]:
                    kw["highlight"] = g["highlight"]
                if g["underline"]:
                    kw.update(underline=True, underline_shape=g["underline"], underline_color=g["color"])
            # doc.oxml 쪽을 부른다: doc.ensure_run_style(옛 겉면)은 bold·italic 을 안 주면 False 로 넘겨 양식 글자 모양의 굵기를 지운다
            try:
                self._run[key] = self.doc.oxml.ensure_run_style(base_char_pr_id=base_char, **kw)
            except (TypeError, ValueError):
                kw.pop("underline_shape", None); kw.pop("underline_color", None)
                self._run[key] = self.doc.oxml.ensure_run_style(base_char_pr_id=base_char, **kw)
        return self._run[key]

    def char(self, para: Para, kind: str, cs_kind) -> str:
        if para.key == "note" and kind == "body":
            kind = "note"                                               # 작업 표시·확인 목록은 회색
        base_char = (self.char_style(cs_kind) if cs_kind else None) or para.base
        return self._derived(base_char, STYLE_ALIAS.get(kind, kind) if kind not in ("body", "note") else kind)

    def after_para(self, doc, para: Para) -> None:
        pass

    def picture(self, doc, b: Block, min_mm: float, large_min_mm: float) -> dict:
        w, h = figure_size(b, self.body_w, self.body_h * 0.9, min_mm, large_min_mm)
        if not self._pic_ppr:                                         # 그림 문단은 바탕글을 가운데 정렬한 문단 모양으로(앞 제목의 문단 모양을 물려받지 않게)
            try:
                self._pic_ppr.append(self.header.ensure_paragraph_format(base_para_pr_id=self.base["para"], alignment="CENTER"))
            except Exception:
                self._pic_ppr.append(self.base["para"])
        return dict(width_mm=round(w, 1), height_mm=round(h, 1), para_pr_id_ref=self._pic_ppr[0], style_id_ref=self.base["id"], char_pr_id_ref=self.base["char"])

    def finish(self, doc) -> None:
        sec0 = self.sec0; sec = sec0.element
        try:                                                          # 쪽 번호: 컨트롤을 구역 설정 문단으로 옮겨 맨 앞 빈 줄이 생기지 않게
            if not self.has_page_num:
                doc.set_page_number(position="BOTTOM_CENTER")
            paras = sec.findall(f"{HP}p")
            holder = next((q for q in paras[1:] if q.find(f".//{HP}pageNum") is not None and not "".join(t.text or "" for t in q.iter(f"{HP}t")).strip()), None)
            if holder is not None and paras:
                for run in holder.findall(f"{HP}run"):
                    paras[0].insert(len(paras[0].findall(f"{HP}run")), run)
                sec.remove(holder)
                sec0.mark_dirty()
        except Exception:
            pass
        try:                                                          # 양식의 첫 문단(쪽 설정·단·꼬리말 컨트롤만 든 빈 줄)을 첫 내용 문단에 합친다
            paras = sec.findall(f"{HP}p")
            runs0 = paras[0].findall(f"{HP}run") if paras else []
            if len(paras) > 1 and runs0 and all(ch.tag in (f"{HP}secPr", f"{HP}ctrl") for run in runs0 for ch in run):
                for i, run in enumerate(runs0):
                    paras[1].insert(i, run)                             # 구역 설정은 첫 문단 첫 런에 있어야 하므로 맨 앞에, 차례대로
                sec.remove(paras[0])
                sec0.mark_dirty()
        except Exception:
            pass


def write_hwpx(blocks: list[Block], out_path: str, look, *, min_mm: float = 70, large_min_mm: float = 120,
               highlight: bool = True, memos: bool = True) -> dict:
    """blocks 를 look 의 모양으로 out_path 에 쓴다. highlight=False 면 등급 색·형광펜·밑줄 없이 글자만(〔〕 표기는 그대로).
    memos=False 면 고지를 메모 대신 회색 〔고지: …〕 로 글 속에. 돌려주는 값: 문단·제목·그림·표·의심 표시·등급·메모·책갈피 수와
    다시 열어 센 그림(images_in_file)·python-hwpx 검사(validate_issues·validate_messages), 그리고 look 의 통계(양식: styles·missing·template_dropped …)."""
    from hwpx import HwpxDocument
    from .hwpx_memo import MemoWriter
    doc = look.open(highlight)
    mw = MemoWriter(doc, look.author)
    stats = {"paragraphs": 0, "headings": 0, "images": 0, "tables": 0, "uncertain_items": 0, "marks": 0, "page_breaks": 0,
             "green": 0, "blue": 0, "red_ctx": 0, "memos": 0, "bookmarks": 0}
    names: set = set()

    def bookmark(p, label: str, cpr) -> None:
        """원본 쪽 책갈피(내용별 문서). 같은 이름이 또 나오면 (2)·(3) 을 붙인다."""
        name = label; k = 2
        while name in names:
            name = f"{label} ({k})"; k += 1
        names.add(name); p.add_bookmark(name, char_pr_id_ref=cpr); stats["bookmarks"] += 1

    def write(para: Para) -> None:
        text, notes = para.text, list(para.notes)
        if not memos and notes:                                        # 메모를 쓰지 않으면 고지를 글 끝에 회색으로
            text = text + "".join(f"〔고지: {m}〕" for _, m in notes); notes = []
        amap = anchor_map(para.anchors, len(text))
        if para.skip_if_empty and not text and not amap:
            return
        p = doc.add_paragraph("", para_pr_id_ref=para.para_pr, style_id_ref=para.style, char_pr_id_ref=para.base, include_run=False, inherit_style=False,
                              **({"pageBreak": "1"} if para.page_break else {}))
        if para.styled or para.spans or notes or para.anchors:
            stats["marks"] += sum(1 for _, k in split_marks(text) if k == "mark")
            segs = styled_runs(text, para.spans if highlight else (), notes)
            if not segs and not amap:
                p.add_run("", char_pr_id_ref=para.base)                # 빈 줄도 런 하나는 있어야 한다
            cs = None
            if para.cstyles is not None:                               # 글자마다 글자 스타일(양식의 소주·출처·본경 조문)
                cs = [para.cs_fill] * len(text)
                for a, e, kind in para.cstyles:
                    for i in range(max(0, a), min(len(text), e)):
                        cs[i] = kind
            pos = 0
            for seg, kind, msgs in segs:
                end = pos + len(seg)
                if not look.count_pieces and kind in GRADES and seg.strip():
                    stats[kind] += 1
                cuts = sorted({pos, end} | {o for o in amap if pos < o < end}
                              | ({i for i in range(pos + 1, end) if cs[i] != cs[i - 1]} if cs is not None else set()))
                for a, e in zip(cuts, cuts[1:]):                       # 책갈피 자리·글자 스타일이 바뀌는 자리에서 조각을 가른다
                    cpr = look.char(para, kind, cs[a] if cs is not None and a < len(cs) else None)
                    for label in amap.pop(a, []):
                        bookmark(p, label, cpr)
                    r = p.add_run(text[a:e], char_pr_id_ref=cpr)
                    if look.count_pieces and kind in GRADES and text[a:e].strip():
                        stats[kind] += 1
                    for m in (msgs if a == pos else []):
                        if mw.wrap_run(getattr(r, "element", None), m):
                            stats["memos"] += 1
                pos = end
            for o in sorted(amap):                                     # 글 끝(다음 쪽 글이 없는 쪽 등)
                for label in amap[o]:
                    bookmark(p, label, para.base)
        else:
            p.add_run(text, char_pr_id_ref=para.base)
        if para.page_break:
            stats["page_breaks"] += 1
        stats["paragraphs"] += 1
        if para.heading:
            stats["headings"] += 1
        look.after_para(doc, para)

    for b in blocks:
        if b.type == "image":
            data = Path(b.path).read_bytes(); fmt = Path(b.path).suffix.lstrip(".").lower() or "png"
            kw = look.picture(doc, b, min_mm, large_min_mm)
            try:
                doc.add_picture(data, fmt, align="CENTER", **kw)
            except Exception:
                doc.add_picture(data, fmt, **kw)
            stats["images"] += 1
        elif b.type == "table":
            t = doc.add_table(len(b.rows), len(b.rows[0]))
            for i, row in enumerate(b.rows):
                for j, cell in enumerate(row):
                    t.cell(i, j).text = cell
            stats["tables"] += 1
        else:
            for para in look.paras(b):
                write(para)
            if b.type == "uncertain":
                stats["uncertain_items"] += len(b.items)
    look.finish(doc)
    try:
        doc.save_to_path(out_path)
    finally:
        doc.close()
    re_ = HwpxDocument.open(out_path)
    try:
        try:                                                          # 본문이 가리키는 그림이 꾸러미 목록(content.hpf)에 있고 파일도 있는 것만 센다
            with zipfile.ZipFile(out_path) as z:
                have = set(z.namelist())
                listed = {it.get("id"): (it.get("href") or "").lstrip("/") for it in ET.fromstring(z.read("Contents/content.hpf")).iter(f"{OPF}item")}
            refs = [im.get("binaryItemIDRef") for pic in re_.sections[0].element.iter(f"{HP}pic") for im in pic.findall(f"{HC}img")]
            stats["images_in_file"] = sum(1 for r in refs if listed.get(r) in have)
        except Exception:
            stats["images_in_file"] = len(re_.sections[0].element.findall(f".//{HP}pic"))
        try:
            issues = list(getattr(re_.validate(), "issues", ()) or ())
            stats["validate_issues"] = len(issues); stats["validate_messages"] = [str(i)[:200] for i in issues[:5]]
        except Exception as e:
            stats["validate_issues"] = f"검사 불가: {e}"; stats["validate_messages"] = []
    finally:
        re_.close()       # Windows 에서도 이어 만들기 때 같은 출력 파일을 다시 쓸 수 있게
    stats.update(look.extra_stats)
    return stats
