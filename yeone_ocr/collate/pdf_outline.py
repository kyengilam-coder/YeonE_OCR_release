"""PDF 책갈피(outline) 읽기와 HWPX 구획 짝짓기, 쪽 범위."""
from __future__ import annotations
import difflib
from dataclasses import dataclass
from .sections import Anchor, Section, normalize_name
from . import zh

def _is_cjk_or_hangul(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x2FA1F   # 한자
            or 0xAC00 <= o <= 0xD7A3 or 0x3040 <= o <= 0x30FF)                                                   # 한글·가나


def _only_cjk(s: str) -> str:
    return "".join(ch for ch in normalize_name(s) if _is_cjk_or_hangul(ch))


def match_key(s: str) -> str:
    """글자층 검색용 이름: 정규화 → 한자·한글만 남김(쪽수·번호·병음·부호 제거) → 이체자 접기 → 간체."""
    return zh.compare_key(_only_cjk(s))


@dataclass
class PdfAnchor:
    title: str
    level: int
    page: int                # PDF 쪽(1부터)
    y: float | None = None   # 쪽 안 세로 위치(pt). 없으면 None

    @property
    def key(self) -> str:
        return match_key(self.title)


def pdf_bookmarks(doc) -> list[PdfAnchor]:
    """PyMuPDF 문서의 outline → PdfAnchor 목록(문서 순서)."""
    out = []
    try:
        toc = doc.get_toc(simple=False)
    except Exception:
        toc = []
    for item in toc:
        lvl, title, page = item[0], item[1], item[2]
        y = None
        if len(item) > 3 and isinstance(item[3], dict):
            to = item[3].get("to")
            if to is not None:
                try: y = float(to.y)
                except Exception: y = None
        if page and page > 0:
            out.append(PdfAnchor(str(title or "").strip(), int(lvl), int(page), y))
    return out


def page_range(pdf_anchors: list[PdfAnchor], idx: int, n_pages: int) -> tuple[int, int]:
    """책갈피 idx 의 쪽 범위(양 끝 포함). 다음 책갈피(급 무관)가 시작하는 쪽까지 — 그 쪽 안의 시작·끝은 제목 위치로 잘라 낸다."""
    a = pdf_anchors[idx]
    end = n_pages
    for b in pdf_anchors[idx + 1:]:
        if b.page >= a.page:
            end = b.page; break
    return a.page, max(a.page, min(end, n_pages))


HOW_LABELS = {"exact": "정확", "contains": "품음", "fuzzy": "비슷", "pdf": "PDF→본문", "none": "없음"}   # 짝 종류 → 화면 표시


@dataclass
class Match:
    anchor: Anchor
    pdf: PdfAnchor | None
    score: float             # 1.0 정확, 0.85 이상 비슷, 0 없음
    how: str                 # exact | fuzzy | none


def section_page_range(section: Section, matches: list[Match], n_pages: int) -> tuple[int, int] | None:
    """HWPX에서 선택한 범위와 같은 PDF 범위. 장·절 선택은 하위 제목을 모두 포함한다."""
    by_para = {m.anchor.para: m.pdf for m in matches if getattr(m, "anchor", None) is not None and m.pdf}
    first = by_para.get(section.start)
    if first is None:
        return None
    if section.next_anchor is None:
        return first.page, n_pages
    following = by_para.get(section.next_anchor.para)
    if following is None or following.page < first.page:
        return None
    return first.page, min(n_pages, following.page)


def _fuzzy_key(s: str, other_has_hangul: bool) -> str:
    """비슷함 비교용 글자열. 두 이름이 모두 한자면 간체 글자열(음이 같은 다른 글자는 다르게), 한쪽이 한글이면 독음."""
    from . import reading as rd
    core = _only_cjk(s)
    if other_has_hangul or any(rd.is_hangul(ch) for ch in core):
        return _only_cjk(rd.reading(core))
    return zh.compare_key(core)


def match_anchors(anchors: list[Anchor], pdf_anchors: list[PdfAnchor], min_ratio: float = 0.82) -> list[Match]:
    """HWPX 기준마다 PDF 책갈피 하나를 고른다. 이름에서 한자·한글만 남겨(쪽수·번호·병음·부호 제거) 음절 단위로 맞춘다.
    한자↔한자는 간체·이체자를 접어, 한자↔한글은 독음(여러 음·두음법칙 포함)으로 비교하므로 PDF 책갈피가
    「동과」「백과(은행)」처럼 한글이어도 冬瓜·白果(銀杏) 와 맞는다.
    세 단계로 돈다: ① 모든 기준의 정확 일치 → ② 남은 것끼리 품음(짧은 쪽이 긴 쪽의 연속 구간) → ③ 남은 것끼리 비슷한 글자열.
    정확 일치를 먼저 다 끝내므로 약한 짝(蓮子·石蓮子 ⊃ 자석)이 정확한 짝(磁石=자석)의 책갈피를 가로채지 않는다."""
    from . import reading as rd
    psyl = [rd.syllables(p.title) for p in pdf_anchors]
    phangul = [any(rd.is_hangul(ch) for ch in sy) for sy in psyl]
    variants = [[rd.syllables(n) for n in a.names] for a in anchors]
    chosen: dict[int, tuple[float, int, str]] = {}
    taken: set[int] = set()

    def free(j):
        return j not in taken

    # ① 정확 일치
    for i, a in enumerate(anchors):
        if not any(variants[i]):
            continue
        for j in range(len(pdf_anchors)):
            if free(j) and psyl[j] and any(rd.seq_equal(sy, psyl[j]) for sy in variants[i] if sy):
                chosen[i] = (1.0, j, "exact"); taken.add(j); break
    # ② 품음
    for i, a in enumerate(anchors):
        if i in chosen or not any(len(sy) >= 2 for sy in variants[i]):
            continue
        cands = []
        for j in range(len(pdf_anchors)):
            pj = psyl[j]
            if not free(j) or len(pj) < 2:
                continue
            for sy in variants[i]:
                if len(sy) >= 2 and ((len(pj) >= len(sy) and rd.seq_find(pj, sy) >= 0) or (len(sy) > len(pj) and rd.seq_find(sy, pj) >= 0)):
                    cands.append((0.9 - min(0.2, abs(len(pj) - len(sy)) * 0.01), j))
        if cands:
            r, j = max(cands); chosen[i] = (round(r, 3), j, "contains"); taken.add(j)
    # ③ 비슷한 글자열
    for i, a in enumerate(anchors):
        if i in chosen or not any(variants[i]):
            continue
        cands = []
        for j in range(len(pdf_anchors)):
            if not free(j) or not psyl[j]:
                continue
            for name, sy in zip(a.names, variants[i]):
                ka = _fuzzy_key(name, phangul[j])
                kp = _fuzzy_key(pdf_anchors[j].title, any(rd.is_hangul(ch) for ch in sy))
                if not ka or not kp:
                    continue
                r = difflib.SequenceMatcher(None, ka, kp, autojunk=False).ratio()
                if r >= min_ratio:
                    cands.append((r, j))
        if cands:
            r, j = max(cands); chosen[i] = (round(r, 3), j, "fuzzy"); taken.add(j)
    out: list[Match] = []
    for i, a in enumerate(anchors):
        if i in chosen:
            score, j, how = chosen[i]; out.append(Match(a, pdf_anchors[j], score, how))
        else:
            out.append(Match(a, None, 0.0, "none"))
    return out


# ---------- PDF 글자층에서 제목 찾기(책갈피에 없을 때) ----------
def fallback_page_bounds(matches: list[Match], idx: int, n_pages: int) -> tuple[int, int]:
    """이웃의 확정된 책갈피 안에서만 찾는다. 역전된 짝은 빈 범위로 실패한다."""
    before = next((m.pdf.page for m in reversed(matches[:idx]) if m.pdf), 1)
    after = next((m.pdf.page for m in matches[idx + 1:] if m.pdf), n_pages)
    return max(1, before), min(n_pages, after)


def _heading_positions(text: str, title: str) -> list[int]:
    """독립된 제목 행만 인정한다. '泻\\n白\\n散' 같은 한 글자씩의 제목도 받는다.

    본문 언급·목차의 제목+쪽수는 제목으로 삼지 않는다. 줄 전체가 제목이어야 하므로
    서술 중 사백산을 처음 언급한 36쪽으로 이동하지 않는다.
    """
    key = match_key(title)
    if len(key) < 2:
        return []
    lines = (text or "").splitlines()
    out = []
    for i, line in enumerate(lines):
        if any(c.isdigit() for c in line):
            continue
        if match_key(line) == key:
            out.append(i)
            continue
        # PDF 글자층에서 자간이 큰 제목이 여러 행으로 분리되는 경우.
        parts = []
        for piece in lines[i:i + len(key)]:
            part = match_key(piece)
            if len(part) != 1 or any(c.isdigit() for c in piece):
                break
            parts.append(part)
        if "".join(parts) == key:
            out.append(i)
    return out


def find_title_pages(doc, title: str, next_title: str | None = None, start_page: int = 1,
                     max_span: int = 6, end_page: int | None = None) -> tuple[int, int] | None:
    """범위 안의 독립된 제목을 검증한다. 제목이 여럿이면 추측하지 않고 None.

    다음 제목이 주어졌는데 검증할 수 없을 때도 자동 범위를 만들지 않는다.
    마지막 구획처럼 다음 제목이 없으면 max_span 쪽까지만 제안한다.
    """
    lo, hi = max(1, start_page), min(len(doc), end_page if end_page is not None else len(doc))
    if lo > hi or len(match_key(title)) < 2:
        return None
    found: list[tuple[int, int]] = []
    texts = {}
    for page in range(lo, hi + 1):
        try:
            text = doc[page - 1].get_text("text") or ""
        except Exception:
            continue
        texts[page] = text
        found.extend((page, pos) for pos in _heading_positions(text, title))
        if len(found) > 1:
            return None
    if len(found) != 1:
        return None
    first, start_pos = found[0]
    if next_title:
        for page in range(first, min(hi, first + 59) + 1):
            if any(page > first or pos > start_pos for pos in _heading_positions(texts.get(page, ""), next_title)):
                return first, page
        return None
    return first, min(hi, first + max(1, max_span) - 1)
