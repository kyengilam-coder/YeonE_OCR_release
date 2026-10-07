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


def match_key(s: str) -> str:
    """짝짓기용 이름: 정규화 → 한자·한글만 남김(쪽수·번호·병음·부호 제거) → 이체자 접기 → 간체."""
    return zh.compare_key("".join(ch for ch in normalize_name(s) if _is_cjk_or_hangul(ch)))


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


@dataclass
class Match:
    anchor: Anchor
    pdf: PdfAnchor | None
    score: float             # 1.0 정확, 0.85 이상 비슷, 0 없음
    how: str                 # exact | fuzzy | none


def match_anchors(anchors: list[Anchor], pdf_anchors: list[PdfAnchor], min_ratio: float = 0.82) -> list[Match]:
    """HWPX 기준마다 PDF 책갈피 하나를 고른다. 이름을 정규화(부호·숫자·로마자 제거, 이체자 접기, 간체화)해
    정확 일치 → 한쪽이 다른 쪽을 품음(冬瓜 ⊂ 第三节冬瓜附冬瓜子) → 비슷한 이름 순.
    같은 PDF 책갈피를 두 HWPX 기준이 가져가지 않도록 하되, 문서 순서가 어긋나면 점수를 깎는다."""
    pkeys = [p.key for p in pdf_anchors]
    taken: set[int] = set()
    out: list[Match] = []
    last_pdf = -1
    for a in anchors:
        k = match_key(a.name)
        best = None
        if k:
            for j, pk in enumerate(pkeys):
                if j in taken or pk != k:
                    continue
                best = (1.0, j, "exact"); break
            if best is None and len(k) >= 2:
                cands = []
                for j, pk in enumerate(pkeys):
                    if j in taken or not pk or len(pk) < 2:
                        continue
                    if k in pk or pk in k:
                        r = 0.9 - (0.05 if j < last_pdf else 0) - min(0.2, abs(len(pk) - len(k)) * 0.01)
                        cands.append((r, j))
                if cands:
                    r, j = max(cands); best = (round(r, 3), j, "contains")
            if best is None:
                cands = []
                for j, pk in enumerate(pkeys):
                    if j in taken or not pk:
                        continue
                    r = difflib.SequenceMatcher(None, k, pk, autojunk=False).ratio()
                    if r >= min_ratio:
                        if j < last_pdf:
                            r -= 0.05          # 순서가 뒤로 가는 짝은 조금 불리하게
                        cands.append((r, j))
                if cands:
                    r, j = max(cands); best = (round(r, 3), j, "fuzzy")
        if best:
            score, j, how = best
            taken.add(j); last_pdf = max(last_pdf, j)
            out.append(Match(a, pdf_anchors[j], score, how))
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
