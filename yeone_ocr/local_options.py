"""Opt-in local-first refinements, all off by default until measured on a corpus.

- blank pages: skip every engine on a page with (almost) no ink
- fast engine plan: after a few sample pages, read with the engines that
  actually won, and fall back to all engines when a page scores poorly
- engine vote: accept an engine candidate that a strict majority of eligible
  engines agree on over the selected reading (recorded, reversible)
- two-column order: emit the lines of a two-column horizontal page column by
  column in the document (the comparison project text is not reordered)
- text-layer witness: mark glyphs where an existing PDF text layer disagrees

Each one is a heuristic, not measured accuracy. They never change the raw
engine readings; vote and witness results are visible as grades.
"""
from __future__ import annotations

from collections import Counter
import difflib

# ---------------------------------------------------------------- blank pages
BLANK_DARK_LEVEL = 160          # 0-255 gray; darker counts as ink
BLANK_INK_SHARE = 0.0002        # below this share of dark pixels the page is blank


def is_blank(image, *, dark=BLANK_DARK_LEVEL, share=BLANK_INK_SHARE) -> bool:
    gray = image.convert("L")
    histogram = gray.histogram()
    ink = sum(histogram[:dark])
    return ink < share * gray.width * gray.height


# ---------------------------------------------------------- fast engine plan
FAST_SAMPLE_PAGES = 5           # pages read with every engine before narrowing
FAST_MIN_SHARE = 0.2            # an engine is kept if it won at least this share of sample pages
FAST_MAX_ENGINES = 2
FAST_FALLBACK_SCORE = 0.55      # a page whose selected reading scores below this is re-read with all engines


class FastPlan:
    """Learns which engines win on this book, then narrows the engine set."""

    def __init__(self, sample=FAST_SAMPLE_PAGES):
        self.sample, self.wins, self.seen = sample, Counter(), 0

    def engines(self):
        """None = every engine (still sampling); otherwise the narrowed list."""
        if self.seen < self.sample or not self.wins:
            return None
        ranked = [e for e, n in self.wins.most_common() if n >= max(1, FAST_MIN_SHARE * self.seen)]
        return ranked[:FAST_MAX_ENGINES] or [self.wins.most_common(1)[0][0]]

    def observe(self, selection, narrowed):
        engine = (selection or {}).get("selected_engine")
        if engine and not narrowed:
            self.wins[engine] += 1
            self.seen += 1

    @staticmethod
    def needs_fallback(selection) -> bool:
        engine = (selection or {}).get("selected_engine")
        if not engine:
            return True
        score = next((r["score"] for r in selection.get("scores", []) if r.get("engine") == engine), 0.0)
        return score < FAST_FALLBACK_SCORE


# --------------------------------------------------------------- engine vote
VOTE_MIN_ENGINES = 3


def apply_votes(project, number) -> int:
    """Accept candidates backed by a strict majority of eligible engines.

    Only on pages without human edits; only non-whitespace differences; the
    winning value needs at least two engines and more support than the
    selected engine's value. Acceptance goes through accept_candidate, so it
    is recorded like a manual choice and shows green in the document.
    """
    from .comparison import ComparisonError
    page = project.page(number)
    if page.get("reviewed") or page.get("edits"):
        return 0
    accepted = 0
    for suggestion in list(page.get("suggestions", [])):
        if suggestion.get("decision") != "pending" or suggestion.get("kind") == "whitespace":
            continue
        eligible = [e["engine"] for e in suggestion.get("evidence", []) if e.get("eligible")]
        if len(eligible) < VOTE_MIN_ENGINES:
            continue
        votes = Counter(suggestion["candidates"][e] for e in eligible)
        value, support = votes.most_common(1)[0]
        baseline_support = votes.get(suggestion["baseline_text"], 0)
        tied = sum(1 for n in votes.values() if n == support) > 1
        if value == suggestion["baseline_text"] or support < 2 or support <= baseline_support or tied:
            continue
        if support * 2 <= len(eligible):
            continue                                            # strict majority of the eligible engines
        engine = next(e for e in eligible if suggestion["candidates"][e] == value)
        try:
            project.accept_candidate(number, suggestion["id"], engine)
        except ComparisonError:
            continue
        accepted += 1
    return accepted


# --------------------------------------------------------- two-column order
COLUMN_MIN_LINES = 3            # lines needed on each side of the gutter
COLUMN_MAX_WIDTH = 600          # a column line is narrower than 60% of the page (normalized 0-1000)


def column_order(regions):
    """Reorder line regions of a two-column horizontal page; unchanged if no clear gutter.

    Full-width lines split the page into bands; inside a band the left column is
    read top to bottom, then the right column.
    """
    boxes = [(r, r.bbox) for r in regions]                    # bbox: [y0, x0, y1, x1] on 0-1000
    narrow = [b for _, b in boxes if b[3] - b[1] < COLUMN_MAX_WIDTH]
    if len(narrow) < 2 * COLUMN_MIN_LINES:
        return list(regions)
    best = None
    for gutter in range(300, 701, 10):
        if any(b[1] < gutter < b[3] for b in narrow):
            continue
        left = sum(1 for b in narrow if b[3] <= gutter)
        right = sum(1 for b in narrow if b[1] >= gutter)
        if left >= COLUMN_MIN_LINES and right >= COLUMN_MIN_LINES:
            balance = min(left, right)
            if best is None or balance > best[0]:
                best = (balance, gutter)
    if best is None:
        return list(regions)
    gutter = best[1]
    ordered, band = [], []

    def flush():
        band.sort(key=lambda item: (0 if item[1][3] <= gutter else 1, item[1][0], item[1][1]))
        ordered.extend(r for r, _ in band)
        band.clear()
    for region, box in sorted(boxes, key=lambda item: (item[1][0], item[1][1])):
        spans_gutter = box[1] < gutter < box[3]
        if spans_gutter:
            flush()
            ordered.append(region)
        else:
            band.append((region, box))
    flush()
    return ordered


# ---------------------------------------------------------- text-layer witness
def witness_spans(current: str, witness: str):
    """(start, end) ranges of ``current`` where a text layer reads differently.

    Whitespace is ignored on both sides; positions refer to ``current``. A
    witness with little in common (another page, another script) is ignored.
    """
    positions = [i for i, c in enumerate(current) if not c.isspace()]
    mine = "".join(current[i] for i in positions)
    theirs = "".join(c for c in witness if not c.isspace())
    if not mine or not theirs:
        return []
    matcher = difflib.SequenceMatcher(None, mine, theirs, autojunk=False)
    if matcher.ratio() < 0.5:
        return []
    out = []
    for tag, a0, a1, _b0, _b1 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if a0 == a1:                                            # the layer has extra text here: mark the neighbour
            a0, a1 = max(0, a0 - 1), max(1, a0)
        out.append((positions[a0], positions[a1 - 1] + 1))
    return out
