"""PDF 책갈피(outline) 읽기와 HWPX 구획 짝짓기, 쪽 범위."""
from __future__ import annotations
import difflib
from dataclasses import dataclass
from .sections import Anchor, normalize_name
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
    asyl = [rd.syllables(a.name) for a in anchors]
    chosen: dict[int, tuple[float, int, str]] = {}
    taken: set[int] = set()

    def free(j):
        return j not in taken

    # ① 정확 일치
    for i, a in enumerate(anchors):
        if not asyl[i]:
            continue
        for j in range(len(pdf_anchors)):
            if free(j) and psyl[j] and rd.seq_equal(asyl[i], psyl[j]):
                chosen[i] = (1.0, j, "exact"); taken.add(j); break
    # ② 품음
    for i, a in enumerate(anchors):
        if i in chosen or len(asyl[i]) < 2:
            continue
        cands = []
        for j in range(len(pdf_anchors)):
            pj = psyl[j]
            if not free(j) or len(pj) < 2:
                continue
            if (len(pj) >= len(asyl[i]) and rd.seq_find(pj, asyl[i]) >= 0) or (len(asyl[i]) > len(pj) and rd.seq_find(asyl[i], pj) >= 0):
                cands.append((0.9 - min(0.2, abs(len(pj) - len(asyl[i])) * 0.01), j))
        if cands:
            r, j = max(cands); chosen[i] = (round(r, 3), j, "contains"); taken.add(j)
    # ③ 비슷한 글자열
    for i, a in enumerate(anchors):
        if i in chosen or not asyl[i]:
            continue
        cands = []
        for j in range(len(pdf_anchors)):
            if not free(j) or not psyl[j]:
                continue
            ka = _fuzzy_key(a.name, phangul[j]); kp = _fuzzy_key(pdf_anchors[j].title, any(rd.is_hangul(ch) for ch in asyl[i]))
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
_PAGE_KEYS: dict[int, list[str]] = {}


def page_keys(doc) -> list[str]:
    """쪽마다 글자층 텍스트의 비교 키(간체·부호 제거). 문서마다 한 번만 만든다. 글자층이 없는 쪽은 빈 문자열."""
    k = id(doc)
    if k not in _PAGE_KEYS:
        keys = []
        for i in range(len(doc)):
            try:
                keys.append(match_key(doc[i].get_text("text") or ""))
            except Exception:
                keys.append("")
        _PAGE_KEYS[k] = keys
    return _PAGE_KEYS[k]


def find_title_pages(doc, title: str, next_title: str | None = None, start_page: int = 1, max_span: int = 6) -> tuple[int, int] | None:
    """PDF 글자층에서 title 이 처음 나오는 쪽부터 next_title 이 나오는 쪽까지를 돌려준다(둘 다 1부터, 끝 포함).
    next_title 을 못 찾으면 시작 쪽부터 max_span 쪽까지. title 자체를 못 찾으면 None."""
    keys = page_keys(doc)
    kt = match_key(title)
    if not kt or len(kt) < 2:
        return None
    p0 = None
    for i in range(max(0, start_page - 1), len(keys)):
        if kt in keys[i]:
            p0 = i + 1; break
    if p0 is None:
        return None
    p1 = min(len(keys), p0 + max_span - 1)
    kn = match_key(next_title) if next_title else ""
    if kn and len(kn) >= 2:
        for i in range(p0 - 1, min(len(keys), p0 - 1 + 60)):
            # 다음 제목이 같은 쪽에 있어도 그 쪽은 포함(쪽 안 자르기는 판독 글로 한다)
            if kn in keys[i] and (i + 1 > p0 or keys[i].find(kn) > keys[i].find(kt)):
                p1 = i + 1; break
    return p0, p1
