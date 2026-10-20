"""Read-only, conservative links from an editing caret to PDF text geometry.

PDF text is only a navigation aid, never correction evidence. Coordinates are
normalized in the displayed (including /Rotate) page coordinate system. An
unresolved duplicate or missing text layer produces no link, not a guess.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
import math
from statistics import median
from types import SimpleNamespace
from typing import Iterable

from . import zh
from .collate import note_spans


Box = tuple[float, float, float, float]
_MIN_ANCHOR = 8
_NEAR_LIMIT = 12
# SequenceMatcher's inner loop visits pairs of equal characters. Repetitive
# long OCR passages can otherwise take minutes, even when no unique anchor can
# be accepted. Fail closed above this conservative work estimate; manual PDF
# navigation and editing remain available. Identical strings need no search.
_ALIGN_PAIR_BUDGET = 8_000_000


@dataclass(frozen=True)
class CursorLocation:
    page: int
    boxes: tuple[Box, ...]
    caret_box: Box | None
    precise: bool
    message: str


@dataclass(frozen=True)
class _Glyph:
    page: int
    box: Box
    line_box: Box


@dataclass(frozen=True)
class CursorPagesSnapshot:
    """Plain immutable data; no PyMuPDF objects survive PDF extraction."""
    key: str
    glyphs: tuple[_Glyph, ...]
    page_font_heights: tuple[tuple[int, float], ...]
    page_font_widths: tuple[tuple[int, float], ...]


@dataclass(frozen=True)
class _Reference:
    key: str
    mapping: tuple[int | None, ...]


@dataclass(frozen=True)
class _Candidate:
    glyph: int
    precise: bool
    distance: int


@lru_cache(maxsize=8192)
def _char_key(char: str) -> str:
    return "".join(c for c in zh.compare_key(char).casefold() if c.isalnum())


def _normalized(text: str) -> tuple[str, tuple[int, ...]]:
    """Keep Python code-point offsets, including supplementary characters."""
    excluded = note_spans(text)
    chars, positions = [], []
    for offset, char in enumerate(text):
        if any(start <= offset < end for start, end in excluded):
            continue
        key = _char_key(char)
        chars.extend(key)
        positions.extend([offset] * len(key))
    return "".join(chars), tuple(positions)


def _unique(text: str, part: str) -> bool:
    first = text.find(part)
    return first >= 0 and text.find(part, first + 1) < 0


def _trusted_equal_map(source: str, target: str) -> dict[int, int]:
    """Only map equal blocks whose full context resolves both occurrences.

    Whole-section alignment incorporates the surrounding paragraphs. Requiring
    uniqueness on both sides rejects repeated passages when order alone cannot
    tell which occurrence was omitted or inserted.
    """
    if min(len(source), len(target)) < _MIN_ANCHOR:
        return {}
    if source == target:
        return {i: i for i in range(len(source))}
    source_counts, target_counts = Counter(source), Counter(target)
    if sum(count * target_counts.get(char, 0) for char, count in source_counts.items()) > _ALIGN_PAIR_BUDGET:
        return {}
    result = {}
    for block in SequenceMatcher(None, source, target, autojunk=False).get_matching_blocks():
        if block.size < _MIN_ANCHOR:
            continue
        context = source[block.a:block.a + block.size]
        if _unique(source, context) and _unique(target, context):
            result.update((block.a + delta, block.b + delta) for delta in range(block.size))
    return result


def _box(raw, width: float, height: float) -> Box | None:
    if len(raw) != 4 or width <= 0 or height <= 0:
        return None
    values = tuple(float(v) for v in raw)
    if not all(math.isfinite(v) for v in values):
        return None
    x0, y0, x1, y1 = values
    result = (max(0., min(1., x0 / width)), max(0., min(1., y0 / height)),
              max(0., min(1., x1 / width)), max(0., min(1., y1 / height)))
    return result if result[0] < result[2] and result[1] < result[3] else None


class SectionCursorMap:
    """Immutable PDF index; locating a caret never reads or writes a file."""

    def __init__(self, references: dict[int, tuple[_Reference, ...]], glyphs: tuple[_Glyph, ...],
                 page_font_heights=(), page_font_widths=()):
        self._references = references
        self._glyphs = glyphs
        self._page_font_heights = dict(page_font_heights)
        self._page_font_widths = dict(page_font_widths)
        self.available = any(g is not None for refs in references.values()
                             for ref in refs for g in ref.mapping)

    def page_font_height(self, page: int) -> float | None:
        """Median displayed glyph-box height / displayed page height.

        This is the text-layer box, not the font's em size or measured ink.
        """
        return self._page_font_heights.get(page)

    def page_font_width(self, page: int) -> float | None:
        """Median displayed CJK width (all letters if absent) / page width."""
        return self._page_font_widths.get(page)

    @staticmethod
    def _candidate(reference: _Reference, current_key: str, position: int,
                   on_character: bool) -> _Candidate | None:
        if current_key == reference.key:
            linked = {i: glyph for i, glyph in enumerate(reference.mapping) if glyph is not None}
        else:
            # Reversing this map keeps inserted current characters unlinked.
            aligned = _trusted_equal_map(reference.key, current_key)
            linked = {current: reference.mapping[baseline] for baseline, current in aligned.items()
                      if reference.mapping[baseline] is not None}
        if position in linked and on_character:
            return _Candidate(linked[position], True, 0)
        if not linked:
            return None
        nearest = min(linked, key=lambda i: (abs(i - position), i > position))
        distance = abs(nearest - position)
        if distance > _NEAR_LIMIT:
            return None
        return _Candidate(linked[nearest], False, distance)

    def locate(self, index: int, current_text: str, offset: int) -> CursorLocation | None:
        references = self._references.get(index)
        if not references or not current_text or not self.available:
            return None
        key, positions = _normalized(current_text)
        if not key:
            return None
        offset = max(0, min(len(current_text), int(offset)))
        position = min(len(positions) - 1, bisect_left(positions, offset))
        on_character = positions[position] == offset or (
            offset == len(current_text) and positions[position] == offset - 1)
        candidates = [candidate for reference in references
                      if (candidate := self._candidate(reference, key, position, on_character)) is not None]
        if not candidates:
            return None
        exact = [candidate for candidate in candidates if candidate.precise]
        if exact:
            if len({candidate.glyph for candidate in exact}) != 1:
                return None
            best = exact[0]
        else:
            best = min(candidates, key=lambda c: c.distance)
            # Different original/corrected contexts must not pull the cursor to
            # unrelated lines. The GUI can honestly report that no link exists.
            chosen = self._glyphs[best.glyph]
            if any((g := self._glyphs[c.glyph]).page != chosen.page or g.line_box != chosen.line_box
                   for c in candidates):
                return None
        glyph = self._glyphs[best.glyph]
        message = (f"커서 위치 · PDF {glyph.page}쪽" if best.precise else
                   f"주변 문맥 · PDF {glyph.page}쪽 (수정한 글자 또는 글자층 차이)")
        return CursorLocation(glyph.page, (glyph.line_box,), glyph.box if best.precise else None,
                              best.precise, message)


def read_cursor_pages(pdf_path, pdf_pages: tuple[int, int]) -> CursorPagesSnapshot:
    """Extract in the same serial worker used for all other PyMuPDF work.

    The returned snapshot can be handed to a separate CPU alignment worker.
    Coordinates and glyph dimensions follow the page as it is displayed.
    """
    import pymupdf
    from ..pdfio import layer_chars

    first, last = pdf_pages
    if type(first) is not int or type(last) is not int or first < 1 or last < first:
        raise ValueError("커서 연결에 사용할 구획 범위가 올바르지 않습니다.")
    glyphs, pdf_chars = [], []
    heights, widths = [], []
    with pymupdf.open(pdf_path) as document:
        if last > len(document):
            raise ValueError("커서 연결 범위가 PDF 쪽 수를 벗어났습니다.")
        for page_number in range(first, last + 1):
            page = document[page_number - 1]
            dimensions, cjk_dimensions = [], []
            if page.rotation:
                # Establish reading order before /Rotate. Sorting displayed
                # coordinates would reverse an upside-down line or paragraph.
                # This proxy only changes the coordinate interpretation; the
                # PDF Page and file are never mutated.
                unrotated = SimpleNamespace(rotation=0, get_text=page.get_text)
                lines = layer_chars([unrotated], 1, own=True)
                for line in lines:
                    line["bbox"] = tuple(pymupdf.Rect(line["bbox"]) * page.rotation_matrix)
                    line["chars"] = [(char, tuple(pymupdf.Rect(box) * page.rotation_matrix))
                                     for char, box in line["chars"]]
            else:
                lines = layer_chars(document, page_number, own=True)
            for line in lines:
                line_box = _box(line["bbox"], page.rect.width, page.rect.height)
                if line_box is None:
                    continue
                for char, raw_box in line["chars"]:
                    box = _box(raw_box, page.rect.width, page.rect.height)
                    if box is None:
                        continue
                    key = _char_key(char)
                    if key:
                        size = (box[2] - box[0], box[3] - box[1])
                        dimensions.append(size)
                        if any("\u3400" <= c <= "\u9fff" or "\uf900" <= c <= "\ufaff"
                               or 0x20000 <= ord(c) <= 0x323AF for c in char):
                            cjk_dimensions.append(size)
                    for key_char in key:
                        pdf_chars.append(key_char)
                        glyphs.append(_Glyph(page_number, box, line_box))
            body = cjk_dimensions or dimensions
            if body:
                widths.append((page_number, median(size[0] for size in body)))
                heights.append((page_number, median(size[1] for size in body)))
    return CursorPagesSnapshot("".join(pdf_chars), tuple(glyphs), tuple(heights), tuple(widths))


def align_cursor_map(snapshot: CursorPagesSnapshot, paragraphs: Iterable) -> SectionCursorMap:
    """Build the section alignment without importing or calling PyMuPDF."""
    paragraphs = tuple(paragraphs)
    if len({p.index for p in paragraphs}) != len(paragraphs):
        raise ValueError("커서 연결에 사용할 문단 번호가 중복되었습니다.")
    ordered = sorted(paragraphs, key=lambda p: p.index)
    pdf_key = snapshot.key
    references: dict[int, list[_Reference]] = {p.index: [] for p in ordered}
    if pdf_key:
        for field in ("corrected_text", "original_text"):
            keys = [(p.index, _normalized(getattr(p, field))[0]) for p in ordered]
            corpus = "".join(key for _, key in keys)
            aligned = _trusted_equal_map(corpus, pdf_key)
            start = 0
            for index, key in keys:
                reference = _Reference(key, tuple(aligned.get(start + i) for i in range(len(key))))
                if reference not in references[index]:
                    references[index].append(reference)
                start += len(key)
    return SectionCursorMap({index: tuple(refs) for index, refs in references.items()}, snapshot.glyphs,
                            snapshot.page_font_heights, snapshot.page_font_widths)


def build_cursor_map(pdf_path, pdf_pages: tuple[int, int], paragraphs: Iterable) -> SectionCursorMap:
    """Compatibility wrapper for callers already running in one PDF worker."""
    return align_cursor_map(read_cursor_pages(pdf_path, pdf_pages), paragraphs)
