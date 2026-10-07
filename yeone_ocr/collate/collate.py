"""HWPX 구획 글과 PDF 판독 글을 맞춰 보고 차이를 분류한다.

비교 공간: 양쪽을 compare_key(NFC → 이체자 접기 → 간체)로 바꾸고, 부호·공백은 빼고(부호 차이는 D) 글자만 정렬한다.
정렬: difflib.SequenceMatcher 로 구획 전체를 한 줄로 이어 글자 단위 정렬. 문단 경계는 HWPX 쪽 글자 지도로 되찾는다.

분류
  A  확실한 오탈: 판독이 확실(〔〕 없음)하고 간→번이 1:1 이며 HWPX 와 다름 → 자동 교정
  B2 1:多 글자: 판독은 확실하나 간체 한 글자가 여러 번체로 갈라짐 → 어구 사전으로 고른 번체로 자동 교정, 후보를 메모에
  B1 판독 불확실: 그 자리의 판독에 〔疑〕·〔a/b〕 표시가 있음 → 글자는 두고 표시만
  C  구조 차이: 긴 구간(LONG 이상)이 한쪽에만 있음 → 누락은 삽입(표시), 추가·순서는 표시만
  D  표기 차이: 부호·공백·이체자만 다름 → 기본 무시
"""
from __future__ import annotations
import difflib, re, unicodedata
from dataclasses import dataclass, field
from . import zh

LONG = 20                      # 이 길이 이상이 한쪽에만 있으면 글자 오탈이 아니라 구조 차이(C)로 본다
MAX_DELETE_AUTO = 2            # HWPX 에만 있는 글자를 자동으로 지우는 최대 길이(그 이상은 C 로 표시만)
MARK_RE = re.compile(r"〔[^〔〕]*〕")
PUNCT_RE = re.compile(r"[\s　·ㆍ・•\.。,，、:：;；!！?？()（）\[\]【】『』「」《》〈〉“”\"'‘’—─―－\-_/／\\|*＊~～…‥]+")


@dataclass
class Char:
    ch: str                    # 원래 글자
    key: str                   # 비교용 글자(간체)
    para: int                  # HWPX: 문단 번호 / OCR: pdf 쪽
    pos: int                   # HWPX: 문단 안 글자 위치 / OCR: 글자 위치
    uncertain: bool = False    # OCR: 〔〕 표시 안이거나 바로 옆


@dataclass
class Item:
    kind: str                  # A | B1 | B2 | C | D
    action: str                # replace | insert | delete | mark
    para: int                  # HWPX 문단 번호(삽입이면 그 앞 문단)
    start: int                 # 문단 안 시작 위치
    end: int                   # 문단 안 끝 위치(미포함)
    hwpx: str                  # HWPX 쪽 글
    ocr: str                   # 판독 쪽 글(인쇄된 글자 그대로, 간체)
    new: str                   # 반영할 글(번체). mark 면 빈 글
    candidates: list[str] = field(default_factory=list)   # B2 다른 후보
    pdf_page: int = 0
    note: str = ""
    auto: bool = False         # 정책에 따라 실제로 반영할지


def strip_marks(text: str) -> tuple[str, list[tuple[int, int]]]:
    """판독 글의 〔〕 표시를 지운다. 〔a/b〕 → a(1차 판독), 〔疑: x〕 → x, 그 밖(〔판독불명〕·작업 표시) → 빈 글.
    돌려주는 구간은 지운 자리 앞뒤 1자까지 '불확실'로 볼 범위."""
    pieces: list[str] = []; spans: list[tuple[int, int]] = []; last = 0; n = 0
    for m in MARK_RE.finditer(text):
        inner = m.group(0)[1:-1]
        if inner.startswith("疑:"):
            core = inner[2:].strip()
        elif "/" in inner and not inner.startswith(("→", "소주", "작업", "판독 메모", "도판", "이어짐", "원본")):
            core = inner.split("/")[0]
        else:
            core = ""
        pieces.append(text[last:m.start()]); n += m.start() - last
        pieces.append(core); spans.append((n - 1, n + len(core) + 1)); n += len(core)
        last = m.end()
    pieces.append(text[last:])
    return "".join(pieces), spans


def _cells(text: str, para: int, *, strip: bool = False) -> list[Char]:
    """글 → 비교 글자 목록. 부호·공백은 뺀다. 위치(pos)는 원래 글의 글자 번호 그대로라 편집에 바로 쓴다."""
    s, unc_spans = (strip_marks(text) if strip else (text, []))
    out: list[Char] = []
    for i, ch in enumerate(s):
        if PUNCT_RE.match(ch) or unicodedata.category(ch)[0] in "ZCPS":
            continue
        key = zh.compare_key(ch)
        if not key:
            continue
        out.append(Char(ch, key[0], para, i, any(a <= i < b for a, b in unc_spans)))
    return out


def hwpx_cells(paras: list[str], first_para: int) -> list[Char]:
    out = []
    for k, t in enumerate(paras):
        out.extend(_cells(t, first_para + k))
    return out


def ocr_cells(pages: list[tuple[int, str]]) -> list[Char]:
    """[(pdf 쪽, 판독 글)] → 글자 목록. 판독 글은 build 와 같은 문단 글(〔〕 표시 포함)."""
    out = []
    for page, t in pages:
        out.extend(_cells(t, page, strip=True))
    return out


def _s2t_context(ocr: list[Char], i0: int, i1: int) -> str:
    """OCR 글자 i0:i1 을 번체로. 앞뒤 4자를 붙여 어구 사전이 문맥을 보게 한 뒤 가운데만 잘라 낸다."""
    lo, hi = max(0, i0 - 4), min(len(ocr), i1 + 4)
    ctx = "".join(c.ch for c in ocr[lo:hi])
    t = zh.to_traditional(ctx)
    if len(t) == len(ctx):
        return t[i0 - lo:i1 - lo]
    return zh.to_traditional("".join(c.ch for c in ocr[i0:i1]))


def collate(hw: list[Char], oc: list[Char], *, policy: str = "all") -> list[Item]:
    """정렬해 차이 항목을 만든다. policy: memo(아무것도 자동 반영 안 함) | sure(A만) | all(A+B2+누락 삽입)."""
    a = "".join(c.key for c in hw); b = "".join(c.key for c in oc)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    items: list[Item] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        hw_span, oc_span = hw[i1:i2], oc[j1:j2]
        hw_text = "".join(c.ch for c in hw_span); oc_text = "".join(c.ch for c in oc_span)
        uncertain = any(c.uncertain for c in oc_span) or (not oc_span and j1 < len(oc) and oc[min(j1, len(oc) - 1)].uncertain)
        page = oc_span[0].para if oc_span else (oc[min(j1, len(oc) - 1)].para if oc else 0)
        # 문단 경계를 넘는 HWPX 구간은 문단별로 쪼갠다(편집은 문단 안에서만)
        groups: list[list[Char]] = []
        for c in hw_span:
            if groups and groups[-1][-1].para == c.para:
                groups[-1].append(c)
            else:
                groups.append([c])
        if op == "insert" or not groups:
            # HWPX 에 없는 글이 판독에 있음 → 삽입 자리는 앞 글자 뒤
            if i1 > 0:
                anchor = hw[i1 - 1]; para, pos = anchor.para, anchor.pos + 1
            elif hw:
                para, pos = hw[0].para, hw[0].pos
            else:
                para, pos = 0, 0
            if uncertain:
                items.append(Item("B1", "mark", para, pos, pos, "", oc_text, "", pdf_page=page, note="판독이 불확실한 자리라 넣지 않음"))
            elif len(oc_span) >= LONG:
                items.append(Item("C", "insert", para, pos, pos, "", oc_text, _s2t_context(oc, j1, j2), pdf_page=page, note=f"HWPX 에 없는 긴 구간({len(oc_span)}자)"))
            else:
                new = _s2t_context(oc, j1, j2)
                amb = [c.ch for c in oc_span if zh.is_ambiguous(c.ch)]
                kind = "B2" if amb else "A"
                cands = sorted({x for ch in amb for x in zh.candidates(ch)}) if amb else []
                items.append(Item(kind, "insert", para, pos, pos, "", oc_text, new, cands, page, "빠진 글자"))
            continue
        for g in groups:
            para, start, end = g[0].para, g[0].pos, g[-1].pos + 1
            g_text = "".join(c.ch for c in g)
            if op == "delete" or not oc_span:
                if uncertain:
                    items.append(Item("B1", "mark", para, start, end, g_text, "", "", pdf_page=page, note="판독이 불확실한 자리"))
                elif len(hw_span) >= LONG:
                    items.append(Item("C", "mark", para, start, end, g_text, "", "", pdf_page=page, note=f"판독에 없는 긴 구간({len(hw_span)}자) — PDF 범위·누락 확인"))
                elif len(hw_span) <= MAX_DELETE_AUTO:
                    items.append(Item("A", "delete", para, start, end, g_text, "", "", pdf_page=page, note="판독에 없는 글자"))
                else:
                    items.append(Item("C", "mark", para, start, end, g_text, "", "", pdf_page=page, note="판독에 없는 글자(여러 자)"))
                continue
            # replace: 이 문단 조각에 대응하는 판독 글자 — 길이 비례로 나눈다
            share = len(g) / max(1, len(hw_span))
            k1 = j1 + round((g[0].pos - hw_span[0].pos if g is groups[0] else sum(len(x) for x in groups[:groups.index(g)])) / max(1, len(hw_span)) * (j2 - j1))
            k2 = j2 if g is groups[-1] else min(j2, k1 + max(1, round(share * (j2 - j1))))
            seg = oc[k1:k2]
            seg_text = "".join(c.ch for c in seg)
            if uncertain or any(c.uncertain for c in seg):
                items.append(Item("B1", "mark", para, start, end, g_text, seg_text, "", pdf_page=page, note="판독이 불확실한 자리"))
            elif len(g) >= LONG and len(seg) >= LONG:
                items.append(Item("C", "mark", para, start, end, g_text, seg_text, "", pdf_page=page, note="긴 구간이 서로 다름 — 문단 순서·범위 확인"))
            else:
                new = _s2t_context(oc, k1, k2) if seg else ""
                amb = [c.ch for c in seg if zh.is_ambiguous(c.ch)]
                if not seg:
                    items.append(Item("A", "delete", para, start, end, g_text, "", "", pdf_page=page, note="판독에 없는 글자"))
                elif amb:
                    cands = sorted({x for ch in amb for x in zh.candidates(ch)})
                    items.append(Item("B2", "replace", para, start, end, g_text, seg_text, new, cands, page, "간체 한 글자가 여러 번체로 갈라짐"))
                else:
                    items.append(Item("A", "replace", para, start, end, g_text, seg_text, new, [], page, "판독과 다름"))
    # 정책에 따라 자동 반영 여부
    for it in items:
        if it.kind == "A" and it.action in ("replace", "insert", "delete"):
            it.auto = policy in ("sure", "all")
        elif it.kind == "B2" and it.action in ("replace", "insert"):
            it.auto = policy == "all"
        elif it.kind == "C" and it.action == "insert":
            it.auto = policy == "all"
        else:
            it.auto = False
        if it.action in ("replace", "insert") and it.new == it.hwpx:
            it.kind, it.action, it.auto, it.note = "D", "none", False, "표기만 다름(이체자·부호)"
    return [it for it in items if it.kind != "D"]


def summary(items: list[Item]) -> dict:
    out = {"A": 0, "B1": 0, "B2": 0, "C": 0, "auto": 0, "mark": 0}
    for it in items:
        out[it.kind] = out.get(it.kind, 0) + 1
        if it.auto: out["auto"] += 1
        else: out["mark"] += 1
    return out
