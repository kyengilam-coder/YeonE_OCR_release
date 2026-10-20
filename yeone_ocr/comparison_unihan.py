"""Pinned official Han variant relationships, for preservation only.

No text is normalized or converted. Direct relationships are symmetric for
protection; semantic relationships are deliberately not transitively equated.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path

DATA = Path(__file__).with_name("data") / "unihan_source_protection_17_0_0.json"
DATA_SHA256 = "e199944a4d357f92ea60df24b746d7db22afc13593374bd6c52e390f2487dace"


@lru_cache(maxsize=1)
def _load():
    try:
        raw = DATA.read_bytes()
        if hashlib.sha256(raw).hexdigest() != DATA_SHA256:
            raise ValueError("Unihan protection data changed")
        data = json.loads(raw)
        pairs = {}
        for field, a, b in data["relations"]:
            left, right = chr(int(a, 16)), chr(int(b, 16))
            pairs.setdefault(frozenset((left, right)), set()).add(field)
        readings = {chr(int(cp, 16)): frozenset(values) for cp, values in data["mandarin"].items()}
        return {pair: tuple(sorted(fields)) for pair, fields in pairs.items()}, {
            "available": True, "version": "unihan-source-protection-v1",
            "unicode_version": data["unicode_version"], "sha256": DATA_SHA256,
            "relation_count": len(data["relations"]),
            "properties": sorted(data["property_counts"]), "normalization": False,
            "closure": "direct-symmetric-no-semantic-transitive-closure",
            "mandarin_reading_count": len(readings),
            "shared_reading_policy": "hold-new-window-change-for-additional-evidence-not-variant-proof",
        }, readings
    except (OSError, ValueError, TypeError, KeyError):
        return {}, {"available": False, "version": "unihan-source-protection-v1",
                    "expected_sha256": DATA_SHA256}, {}


def manifest():
    return dict(_load()[1])


def relationship(left, right):
    """Return official field names; absence never proves two glyphs differ."""
    if len(left) != 1 or len(right) != 1 or left == right:
        return ()
    return _load()[0].get(frozenset((left, right)), ())


def shared_reading(left, right):
    """A caution flag for new proposals, never a claim of character equality."""
    return left != right and bool(_load()[2].get(left, frozenset()) & _load()[2].get(right, frozenset()))
