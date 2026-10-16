"""Exact Unicode code-point CER, with explicit and reproducible comparison policy.

No OCR engine, network service, or third-party library is involved. ``raw`` always
compares the input strings verbatim. ``normalized`` applies only the selected
policy; NFC deliberately exempts CJK compatibility ideographs and variation
selectors. Error positions address the respective compared strings, not bytes,
UTF-16 units, grapheme clusters, or original positions after normalization.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import unicodedata
from typing import Any


DEFAULT_MAX_CELLS = 10_000_000
DEFAULT_MAX_ERRORS = 500
_CONTEXT_RADIUS = 12
_SCRIPTS = ("hangul", "hanja", "latin", "digit", "whitespace", "punctuation",
            "symbol", "combining", "variation_selector", "other")


class ComparisonLimitError(ValueError):
    """The exact alignment exceeds its declared computation/memory budget."""


@dataclass(frozen=True)
class NormalizationPolicy:
    unicode_form: str = "none"
    whitespace: str = "preserve"

    def __post_init__(self) -> None:
        if self.unicode_form not in ("none", "NFC"):
            raise ValueError("unicode_form must be 'none' or 'NFC'; compatibility folding is unsupported")
        if self.whitespace not in ("preserve", "collapse", "remove"):
            raise ValueError("whitespace must be 'preserve', 'collapse', or 'remove'")

    @property
    def id(self) -> str:
        return f"unicode-{self.unicode_form}-whitespace-{self.whitespace}-cjk-preserve-v1"

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "unicode_form": self.unicode_form, "whitespace": self.whitespace,
                "unit": "unicode_code_point", "cjk_compatibility_ideographs": "preserve",
                "variation_selectors": "preserve", "simplified_traditional_folding": False,
                "whitespace_definition": "Python str.isspace / installed Unicode database",
                "collapse_rule": "replace each whitespace run by U+0020, including at string edges"}


def _policy(policy: NormalizationPolicy | dict[str, Any] | None) -> NormalizationPolicy:
    if policy is None:
        return NormalizationPolicy()
    if isinstance(policy, NormalizationPolicy):
        return policy
    if isinstance(policy, dict):
        # Accept both a compact input policy and this module's serialized policy.
        keys = set(NormalizationPolicy().as_dict())
        if set(policy) - keys:
            raise ValueError("Unknown normalization policy fields: " + ", ".join(sorted(set(policy) - keys)))
        result = NormalizationPolicy(unicode_form=policy.get("unicode_form", "none"),
                                     whitespace=policy.get("whitespace", "preserve"))
        canonical = result.as_dict()
        if any(value != canonical[key] for key, value in policy.items()):
            raise ValueError("Serialized normalization policy does not match its declared settings")
        return result
    raise TypeError("policy must be NormalizationPolicy, a dictionary, or None")


def _variation_selector(code: int) -> bool:
    # Standard and supplementary selectors, plus Mongolian free selectors.
    return 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF or code in (0x180B, 0x180C, 0x180D, 0x180F)


def _protected(code: int) -> bool:
    # NFC itself canonically folds many CJK compatibility ideographs. Protect
    # the complete blocks, including code points unknown to this Python version.
    return 0xF900 <= code <= 0xFAFF or 0x2F800 <= code <= 0x2FA1F or _variation_selector(code)


def normalize_text(text: str, policy: NormalizationPolicy | dict[str, Any] | None = None) -> str:
    """Apply the stated policy without silently merging CJK character variants."""
    if not isinstance(text, str):
        raise TypeError("text must be a Unicode string")
    selected = _policy(policy)
    if selected.unicode_form == "NFC":
        parts: list[str] = []
        begin = 0
        for index, character in enumerate(text):
            if _protected(ord(character)):
                parts.append(unicodedata.normalize("NFC", text[begin:index]))
                parts.append(character)
                begin = index + 1
        parts.append(unicodedata.normalize("NFC", text[begin:]))
        text = "".join(parts)
    if selected.whitespace == "remove":
        return "".join(character for character in text if not character.isspace())
    if selected.whitespace == "collapse":
        parts = []
        whitespace = False
        for character in text:
            if character.isspace():
                if not whitespace:
                    parts.append(" ")
                whitespace = True
            else:
                parts.append(character)
                whitespace = False
        return "".join(parts)
    return text


def classify_character(character: str) -> str:
    """Conservative character class, using this runtime's Unicode assignment data.

    Hanja means assigned CJK unified/compatibility ideographs, including every
    extension known to the installed database. Radicals, unassigned code points,
    and Han-looking symbols are not guessed to be ideographs. Jamo and Hangul
    compatibility/halfwidth letters belong to Hangul, not to the Hanja class.
    """
    if not isinstance(character, str) or len(character) != 1:
        raise ValueError("classify_character requires exactly one Unicode code point")
    if _variation_selector(ord(character)):
        return "variation_selector"
    if character.isspace():
        return "whitespace"
    name = unicodedata.name(character, "")
    if name.startswith(("CJK UNIFIED IDEOGRAPH-", "CJK COMPATIBILITY IDEOGRAPH-")):
        return "hanja"
    if name.startswith(("HANGUL ", "HALFWIDTH HANGUL ")):
        return "hangul"
    if name.startswith(("LATIN ", "FULLWIDTH LATIN ")):
        return "latin"
    category = unicodedata.category(character)
    if category == "Nd":
        return "digit"
    if category.startswith("M"):
        return "combining"
    if category.startswith("P"):
        return "punctuation"
    if category.startswith("S"):
        return "symbol"
    return "other"


def _nonnegative_int(value: int, name: str, *, positive: bool = False) -> None:
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError(f"{name} must be {'a positive' if positive else 'a nonnegative'} integer")


def _context(text: str, position: int, width: int) -> str:
    return text[max(0, position - _CONTEXT_RADIUS):min(len(text), position + width + _CONTEXT_RADIUS)]


def _compare(reference: str, hypothesis: str, max_cells: int, max_errors: int) -> dict[str, Any]:
    # Equal edges can be included in an optimal unit-cost Levenshtein alignment;
    # trimming them is exact. It also avoids rejecting a long unchanged document.
    prefix = 0
    while prefix < min(len(reference), len(hypothesis)) and reference[prefix] == hypothesis[prefix]:
        prefix += 1
    ref_end, hyp_end = len(reference), len(hypothesis)
    while ref_end > prefix and hyp_end > prefix and reference[ref_end - 1] == hypothesis[hyp_end - 1]:
        ref_end -= 1
        hyp_end -= 1
    ref, hyp = reference[prefix:ref_end], hypothesis[prefix:hyp_end]
    n, m = len(ref), len(hyp)
    cells = (n + 1) * (m + 1) if n and m else 0
    if cells > max_cells:
        raise ComparisonLimitError(
            f"Exact alignment requires {cells:,} cells after equal-edge trimming; limit is {max_cells:,}. "
            "Compare smaller pages/regions or explicitly raise max_cells. No approximate score was produced."
        )

    by_script = {script: {"reference_length": 0, "hypothesis_length": 0,
                          "substitutions": 0, "deletions": 0, "insertions": 0,
                          "substitutions_to": 0} for script in _SCRIPTS}
    for character, count in Counter(reference).items():
        by_script[classify_character(character)]["reference_length"] += count
    for character, count in Counter(hypothesis).items():
        by_script[classify_character(character)]["hypothesis_length"] += count
    counts = {"substitutions": 0, "deletions": 0, "insertions": 0}
    confusions: Counter = Counter()
    # Traceback runs backwards; keep the earliest max_errors in the final text.
    errors: deque = deque(maxlen=max_errors)

    def error(operation: str, rpos: int, hpos: int, rchar: str, hchar: str) -> None:
        counts[operation + "s"] += 1
        rclass = classify_character(rchar) if rchar else None
        hclass = classify_character(hchar) if hchar else None
        attributed = hclass if operation == "insertion" else rclass
        by_script[attributed][operation + "s"] += 1
        if operation == "substitution":
            by_script[hclass]["substitutions_to"] += 1
        confusions[(operation, rchar, hchar, rclass, hclass)] += 1
        if max_errors:
            errors.append({"operation": operation, "reference_position": rpos,
                           "hypothesis_position": hpos, "reference": rchar, "hypothesis": hchar,
                           "reference_context": _context(reference, rpos, len(rchar)),
                           "hypothesis_context": _context(hypothesis, hpos, len(hchar)),
                           "reference_script": rclass, "hypothesis_script": hclass})

    if not n or not m:
        # No grid is needed for an empty middle. Still count every insertion or
        # deletion, even when error details have an output limit of zero.
        for i in range(n - 1, -1, -1):
            error("deletion", prefix + i, prefix, ref[i], "")
        for j in range(m - 1, -1, -1):
            error("insertion", prefix, prefix + j, "", hyp[j])
        distance = n + m
    else:
        # Use the shorter string as columns for O(min(n,m)) integer-row memory.
        # Directions are relative to original ref/hyp, even in a transposed grid,
        # so a tie always prefers diagonal, then deletion, then insertion.
        transposed = m > n
        rows, columns = (hyp, ref) if transposed else (ref, hyp)
        width = len(columns) + 1
        trace = bytearray(cells)
        previous = list(range(width))
        for j in range(1, width):
            trace[j] = 2 if transposed else 3
        for i, row_char in enumerate(rows, 1):
            current = [i] + [0] * (width - 1)
            row_start = i * width
            trace[row_start] = 3 if transposed else 2
            for j, col_char in enumerate(columns, 1):
                diagonal = previous[j - 1] + (row_char != col_char)
                deletion = (current[j - 1] if transposed else previous[j]) + 1
                insertion = (previous[j] if transposed else current[j - 1]) + 1
                if diagonal <= deletion and diagonal <= insertion:
                    current[j], trace[row_start + j] = diagonal, 1
                elif deletion <= insertion:
                    current[j], trace[row_start + j] = deletion, 2
                else:
                    current[j], trace[row_start + j] = insertion, 3
            previous = current
        distance = previous[-1]
        ri, hi = n, m
        while ri or hi:
            i, j = (hi, ri) if transposed else (ri, hi)
            direction = trace[i * width + j]
            if direction == 1:
                ri -= 1
                hi -= 1
                if ref[ri] != hyp[hi]:
                    error("substitution", prefix + ri, prefix + hi, ref[ri], hyp[hi])
            elif direction == 2:
                ri -= 1
                error("deletion", prefix + ri, prefix + hi, ref[ri], "")
            elif direction == 3:
                hi -= 1
                error("insertion", prefix + ri, prefix + hi, "", hyp[hi])
            else:  # Internal invariant, never a fallback to an approximate score.
                raise AssertionError("Incomplete Levenshtein traceback")

    if distance != sum(counts.values()):
        raise AssertionError("Edit counts do not equal exact Levenshtein distance")
    confusion_list = [{"operation": op, "reference": rc, "hypothesis": hc,
                       "reference_script": rs, "hypothesis_script": hs, "count": count}
                      for (op, rc, hc, rs, hs), count in confusions.items()]
    confusion_list.sort(key=lambda item: (-item["count"], item["operation"], item["reference"], item["hypothesis"]))
    cer = distance / len(reference) if reference else None
    return {"reference_length": len(reference), "hypothesis_length": len(hypothesis),
            "distance": distance, "edit_distance": distance, **counts,
            "cer": cer, "cer_percent": cer * 100 if cer is not None else None,
            "errors": list(reversed(errors)), "error_count": distance,
            "errors_truncated": distance > max_errors, "reported_error_count": len(errors),
            "confusions": confusion_list, "by_script": by_script,
            "script_attribution": "substitution/deletion: reference character; insertion: hypothesis character; no script CER inferred",
            "alignment": {"algorithm": "exact_unit_cost_levenshtein", "traceback": "byte_per_cell",
                          "tie_break": ["diagonal", "deletion", "insertion"],
                          "equal_prefix_length": prefix, "equal_suffix_length": len(reference) - ref_end,
                          "computed_cells": cells, "max_cells": max_cells,
                          "position_unit": "zero_based_unicode_code_point"}}


def compare_texts(reference: str, hypothesis: str,
                  policy: NormalizationPolicy | dict[str, Any] | None = None, *,
                  max_cells: int = DEFAULT_MAX_CELLS,
                  max_errors: int = DEFAULT_MAX_ERRORS) -> dict[str, Any]:
    """Return JSON-serializable strict and explicitly normalized exact CER reports.

    ``cer`` is a fraction, may exceed 1, and is None for an empty reference,
    including two empty strings. Counts always include every edit. ``max_errors``
    limits only detailed error rows, never distance/counts/confusion frequencies.
    ``max_cells`` bounds each exact DP grid after equal prefix/suffix trimming.
    Comparisons exceeding that budget raise ComparisonLimitError, never report
    a heuristic score. Empty-sided middles use a linear exact path without a grid.

    Multiple optimal alignments may have different S/D/I breakdowns. The reported
    deterministic alignment is one exact optimum, not proof of the OCR engine's
    causal character boundaries. Per-script attribution is deliberately not CER.
    """
    if not isinstance(reference, str) or not isinstance(hypothesis, str):
        raise TypeError("reference and hypothesis must be Unicode strings")
    _nonnegative_int(max_cells, "max_cells", positive=True)
    _nonnegative_int(max_errors, "max_errors")
    selected = _policy(policy)
    normalized_reference = normalize_text(reference, selected)
    normalized_hypothesis = normalize_text(hypothesis, selected)
    raw = _compare(reference, hypothesis, max_cells, max_errors)
    # Separate dictionaries prevent a caller annotating one report from changing
    # the other, even when normalization is disabled or has no effect.
    if normalized_reference == reference and normalized_hypothesis == hypothesis:
        import copy
        normalized = copy.deepcopy(raw)
    else:
        normalized = _compare(normalized_reference, normalized_hypothesis, max_cells, max_errors)
    return {"schema_version": 1, "policy": selected.as_dict(), "policy_id": selected.id,
            "unicode_version": unicodedata.unidata_version, "raw": raw, "normalized": normalized}
