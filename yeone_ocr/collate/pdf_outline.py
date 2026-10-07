"""PDF 책갈피(outline) 읽기와 HWPX 구획 짝짓기, 쪽 범위."""
from __future__ import annotations
import difflib
from dataclasses import dataclass
from .sections import Anchor, normalize_name
from . import zh


@dataclass
class PdfAnchor:
    title: str
    level: int
    page: int                # PDF 쪽(1부터)
    y: float | None = None   # 쪽 안 세로 위치(pt). 없으면 None

    @property
    def key(self) -> str:
        return zh.compare_key(normalize_name(self.title))


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
    """HWPX 기준마다 PDF 책갈피 하나를 고른다. 이름을 정규화(부호 제거·이체자 접기·간체화)해 정확 일치 → 비슷한 이름 순.
    같은 PDF 책갈피를 두 HWPX 기준이 가져가지 않도록 하되, 문서 순서가 어긋나면 점수를 깎는다."""
    pkeys = [p.key for p in pdf_anchors]
    taken: set[int] = set()
    out: list[Match] = []
    last_pdf = -1
    for a in anchors:
        k = zh.compare_key(a.key)
        best = None
        if k:
            for j, pk in enumerate(pkeys):
                if j in taken or pk != k:
                    continue
                best = (1.0, j, "exact"); break
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
