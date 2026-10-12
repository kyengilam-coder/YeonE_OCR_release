"""HWPX 의 책갈피·제목 색인과 구획 범위.

구획의 기준(anchor)은 두 가지다.
- 책갈피: 한컴 '책갈피'(hp:bookmark). 급은 모두 같다고 본다.
- 제목 스타일 문단: 스타일 이름이 제목으로 보이는 문단(기본: 이름에 '제목'이 들어가거나 '개요 N'). 급은 스타일 목록의 순서
  (예: 제목-장 1, 제목-절 2, 제목-본초명 3, 제목-본초명(소) 4). 업로드된 책처럼 책갈피가 하나도 없는 파일에서도 구획을 나눌 수 있다.

구획 = 기준 문단부터 다음 '같은 급 이상' 기준 직전까지.
"""
from __future__ import annotations
import re, unicodedata
from dataclasses import dataclass, field


@dataclass
class Anchor:
    kind: str            # bookmark | heading
    name: str            # 책갈피 이름 또는 제목 글
    para: int            # 문단 번호(0부터, 본문 섹션 전체 기준)
    level: int = 1       # 1이 가장 큰 단위
    style: str = ""      # 제목 스타일 이름

    @property
    def key(self) -> str:
        return normalize_name(self.name)


@dataclass
class Section:
    anchor: Anchor
    start: int           # 기준 문단(포함)
    end: int             # 끝 문단(미포함)
    next_anchor: Anchor | None = None

    @property
    def n_paras(self) -> int:
        return self.end - self.start


_PUNCT = re.compile(r"[\s·ㆍ・•\.。,，、:：;；!！?？()（）\[\]【】『』「」《》〈〉“”\"'‘’—─―－\-_/／\\|*＊]+")


def normalize_name(s: str) -> str:
    """이름 비교용: NFC, 공백·부호·기호(…·『』 등 모든 문장부호·기호 범주) 제거, 소문자. 간체·번체 차이는 zh.compare_key 로 따로 접는다."""
    s = unicodedata.normalize("NFC", s or "")
    s = _PUNCT.sub("", s)
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] not in "PSZC")
    return s.lower()


HEADING_STYLE_RE = re.compile(r"제목|개요\s*\d|heading|outline|title", re.I)


def heading_styles(style_names: list[tuple[str, str]], pattern: re.Pattern | None = None) -> dict[str, int]:
    """(스타일 id, 이름) 목록에서 제목으로 보이는 스타일 → 급. 목록 순서대로 1, 2, 3…"""
    pat = pattern or HEADING_STYLE_RE
    out: dict[str, int] = {}
    for sid, name in style_names:
        if pat.search(name or ""):
            out[sid] = len(out) + 1
    return out


class HwpxBook:
    """python-hwpx 문서를 감싸 문단 글·스타일·책갈피를 빠르게 훑는다. 편집은 hwpx_edit 가 맡는다."""

    def __init__(self, path: str):
        from hwpx.document import HwpxDocument
        self.path = path
        self.doc = HwpxDocument.open(path)
        self.paragraphs = self.doc.paragraphs
        self.style_names = self._style_names()
        self._texts: list[str] | None = None

    def _style_names(self) -> list[tuple[str, str]]:
        out = []
        try:
            st = self.doc.styles
            for sid in list(st):
                s = st.get(sid)
                name = getattr(s, "name", None) or (s.get("name") if isinstance(s, dict) else "") or ""
                out.append((str(sid), str(name)))
        except Exception:
            pass
        if not out:
            # 헤더 XML 을 직접 읽는 우회
            try:
                import xml.etree.ElementTree as ET
                for el in self.doc.headers[0].element.iter():
                    if el.tag.endswith("}style"):
                        out.append((el.get("id", ""), el.get("name", "")))
            except Exception:
                pass
        return out

    def style_of(self, i: int) -> str:
        sid = self.paragraphs[i].element.get("styleIDRef", "0")
        return dict(self.style_names).get(sid, sid)

    def texts(self) -> list[str]:
        if self._texts is None:
            self._texts = [p.text for p in self.paragraphs]
        return self._texts

    def heading_style_names(self) -> list[str]:
        """제목으로 보이는 스타일 이름들(스타일 목록 순서)."""
        hs = heading_styles(self.style_names)
        return [name for sid, name in self.style_names if sid in hs]

    def style_counts(self) -> list[tuple[str, int]]:
        """(스타일 이름, 글이 있는 문단 수) — 스타일 고르기 창에 보여 준다."""
        names = dict(self.style_names); cnt: dict[str, int] = {}
        for i, p in enumerate(self.paragraphs):
            if self.texts()[i].strip():
                nm = names.get(p.element.get("styleIDRef", "0"), "?"); cnt[nm] = cnt.get(nm, 0) + 1
        return [(name, cnt.get(name, 0)) for _, name in self.style_names if cnt.get(name)]

    def anchors(self, heading_pattern: re.Pattern | None = None, styles: list[str] | None = None) -> list[Anchor]:
        """책갈피(hp:bookmark)와 제목 스타일 문단을 기준으로 삼는다(책갈피는 급 1).
        styles 를 주면(스타일 이름 목록) 그 스타일 문단만 제목으로 보고, 급은 준 순서대로 1, 2, 3…"""
        out: list[Anchor] = []
        if styles:
            wanted = [s.strip() for s in styles if s.strip()]
            by_name = {name: sid for sid, name in self.style_names}
            hs = {by_name[n]: i + 1 for i, n in enumerate(wanted) if n in by_name}
        else:
            hs = heading_styles(self.style_names, heading_pattern)
        texts = self.texts()
        for i, p in enumerate(self.paragraphs):
            el = p.element
            for sub in el.iter():
                if sub.tag.endswith("}bookmark"):
                    name = sub.get("name") or ""
                    if name:
                        out.append(Anchor("bookmark", name, i, 1))
            sid = el.get("styleIDRef", "0")
            if sid in hs and texts[i].strip():
                out.append(Anchor("heading", texts[i].strip(), i, hs[sid], dict(self.style_names).get(sid, sid)))
        out.sort(key=lambda a: (a.para, 0 if a.kind == "bookmark" else 1))
        return out

    def anchors_from_titles(self, titles: list[tuple[str, int]], slack: int = 6) -> list[tuple[int, Anchor | None]]:
        """PDF 책갈피 (제목, 급) 목록을 HWPX 본문에서 찾아 기준으로 삼는다(책갈피도 제목 스타일도 없는 HWPX 용).
        문서 순서대로 앞에서부터 찾으며, 제목과 같은 글(정규화·간체 접기)인 문단, 없으면 제목으로 시작하는 짧은 문단(제목 길이+slack 이내)을 고른다.
        돌려주는 값은 [(PDF 책갈피 번호, Anchor 또는 None)]."""
        from . import reading as rd
        syls = [rd.syllables(t) for t in self.texts()]
        out: list[tuple[int, Anchor | None]] = []
        pos = 0
        for j, (title, level) in enumerate(titles):
            k = rd.syllables(title)
            found = None
            if k:
                for i in range(pos, len(syls)):
                    if len(syls[i]) == len(k) and rd.seq_equal(syls[i], k):
                        found = i; break
                if found is None:
                    for i in range(pos, len(syls)):
                        si = syls[i]
                        if len(k) <= len(si) <= len(k) + slack and rd.seq_find(si[:len(k)], k) == 0:
                            found = i; break
            if found is None:
                out.append((j, None)); continue
            out.append((j, Anchor("pdf", self.texts()[found].strip() or title, found, max(1, level), "PDF 책갈피로 찾음")))
            pos = found + 1
        return out

    def section(self, anchors: list[Anchor], idx: int, flat: bool = False) -> Section:
        """구획 범위. 기본은 다음 '같은 급 이상' 기준 직전까지(하위 구획 포함). flat=True 면 급과 무관하게 바로 다음 기준 직전까지 —
        전체 교정처럼 구획을 빠짐없이·겹침 없이 차례로 돌 때 쓴다."""
        a = anchors[idx]
        nxt = None
        for b in anchors[idx + 1:]:
            if b.para > a.para and (flat or b.level <= a.level):
                nxt = b; break
        end = nxt.para if nxt else len(self.paragraphs)
        return Section(a, a.para, end, nxt)

    def section_texts(self, sec: Section) -> list[str]:
        return self.texts()[sec.start:sec.end]


def find_anchor(anchors: list[Anchor], query: str) -> list[Anchor]:
    """이름으로 기준 찾기: 정확히 → 정규화 일치 → 부분 포함 순. 여러 개면 모두 돌려준다(사람이 고른다)."""
    q = (query or "").strip()
    if not q:
        return []
    from . import zh, reading
    exact = [a for a in anchors if a.name.strip() == q]
    if exact:
        return exact
    nq = zh.compare_key(normalize_name(q))          # 간체·번체·이체자 차이를 접고 비교
    same = [a for a in anchors if zh.compare_key(a.key) == nq]
    if same:
        return same
    part = [a for a in anchors if nq and nq in zh.compare_key(a.key)]
    if part:
        return part
    # 한글 독음으로(대계 → 大薊, 인삼 → 人參·人蔘)
    return [a for a in anchors if reading.matches(a.name, q)]
