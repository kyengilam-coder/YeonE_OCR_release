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


COLUMN_BODY_WIDTH = 250        # a body-column line spans at least 25% of the page width (0-1000)
COLUMN_BODY_FILL = 0.7         # ... and at least 70% of its column's width
COLUMN_BODY_SHARE = 0.5        # at least half of each column's lines are such full lines
COLUMN_BODY_LINES = 5          # and at least this many per column
COLUMN_JOIN_GAP = 12           # pieces of one printed line closer than this (0-1000) are one line
COLUMN_MAX_CROSSING = 0.15     # titles/running heads may cross the gutter: at most this share of the lines (always 2)


def _joined_lines(side):
    """Rejoin pieces of one printed line: EasyOCR splits lines at commas (gaps ~0.5-1% of
    the page width). Pieces that share most of their height and nearly touch become one."""
    rows = []
    for box in sorted(side, key=lambda b: (b[0], b[1])):
        for row in rows:
            overlap = min(row[2], box[2]) - max(row[0], box[0])
            if (overlap >= 0.5 * min(row[2] - row[0], box[2] - box[0])
                    and -COLUMN_JOIN_GAP <= box[1] - row[3] <= COLUMN_JOIN_GAP):
                row[:] = [min(row[0], box[0]), row[1], max(row[2], box[2]), max(row[3], box[3])]
                break
        else:
            rows.append(list(box))
    return rows


def column_order(regions):
    """Reorder line regions of a two-column horizontal page; unchanged if no clear gutter.

    The gutter is the vertical position crossed by the fewest lines with at
    least COLUMN_MIN_LINES column lines on each side, and each side must look
    like body text: mostly long lines filling the column (not table cells or
    captions). A few lines may cross it
    (a centred title, a running head); like full-width lines they split the page
    into bands. Inside a band the left column is read top to bottom, then the
    right column.
    """
    boxes = [(r, r.bbox) for r in regions]                    # bbox: [y0, x0, y1, x1] on 0-1000
    if len(boxes) < 2 * COLUMN_MIN_LINES:
        return list(regions)
    best = None
    for gutter in range(300, 701, 5):
        crossing = sum(1 for _, b in boxes if b[1] < gutter < b[3])
        left = sum(1 for _, b in boxes if b[3] <= gutter and b[3] - b[1] < COLUMN_MAX_WIDTH)
        right = sum(1 for _, b in boxes if b[1] >= gutter and b[3] - b[1] < COLUMN_MAX_WIDTH)
        if left < COLUMN_MIN_LINES or right < COLUMN_MIN_LINES or crossing > max(2, COLUMN_MAX_CROSSING * len(boxes)):
            continue
        key = (crossing, -min(left, right))
        if best is None or key < best[0]:
            best = (key, gutter)
    if best is None:
        return list(regions)
    gutter = best[1]
    # Body text, not a table or a grid of captions: in two-column body text each column is
    # filled by long lines of about the column's width. Table cells and photo captions are
    # short pieces (대조도감: tables, captions under paired photos); their row order is right.
    for side in ([b for _, b in boxes if b[3] <= gutter], [b for _, b in boxes if b[1] >= gutter]):
        side = _joined_lines(side)
        extent = max(b[3] for b in side) - min(b[1] for b in side)
        full = [b for b in side if b[3] - b[1] >= max(COLUMN_BODY_WIDTH, COLUMN_BODY_FILL * extent)]
        if len(full) < COLUMN_BODY_LINES or len(full) < COLUMN_BODY_SHARE * len(side):
            return list(regions)
    ordered, band = [], []

    def flush():
        band.sort(key=lambda item: (0 if item[1][3] <= gutter else 1, item[1][0], item[1][1]))
        ordered.extend(r for r, _ in band)
        band.clear()
    for region, box in sorted(boxes, key=lambda item: (item[1][0], item[1][1])):
        if box[1] < gutter < box[3]:
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
