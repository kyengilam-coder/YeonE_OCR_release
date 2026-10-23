"""Opt-in Chinese line verification; native TXT and TSV remain audit evidence."""
from __future__ import annotations

import csv
import io

RECOGNITION_POLICY = "tesseract-chinese-line-glyph-verified-v1"


def line_regions(tsv, width, height):
    """Use level-4 envelopes, never overlapping/partial level-5 word boxes."""
    regions = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        if row.get("level") != "4":
            continue
        try:
            x, y, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
            key = tuple(int(row[k]) for k in ("block_num", "par_num", "line_num"))
        except (ValueError, KeyError, TypeError):
            return []
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x+w > width or y+h > height:
            return []
        # White border retains edge strokes and allows native font bearings.
        pad = max(4, round(min(w, h)*.25))
        regions.append({"key": key, "bbox": [max(0,x-pad), max(0,y-pad),
                                                min(width,x+w+pad), min(height,y+h+pad)]})
    return regions


def refine(image_path, text, tsv, words, *, vertical=False, check=lambda: None):
    from PIL import Image
    import numpy as np
    from . import glyph_verifier as glyph

    sources = glyph.font_sources()
    metadata = {"recognition_policy": RECOGNITION_POLICY, "glyph_font_sources": sources,
                "model_result": {"text": text, "words": words, "tsv": tsv},
                "glyph_verification": [], "glyph_verification_applied": False}
    if not sources:
        return text, words, {**metadata, "status": "installed verification fonts unavailable"}
    with Image.open(image_path) as image:
        gray = np.asarray(image.convert("L"))
    regions = line_regions(tsv, gray.shape[1], gray.shape[0])
    parts = text.splitlines(keepends=True)
    nonempty = [i for i, line in enumerate(parts) if line.strip()]
    compact = lambda value: "".join(value.split())
    # Native TXT is independently emitted; refuse uncertain line correspondence.
    if len(regions) != len(nonempty) or not regions:
        return text, words, {**metadata, "status": "TXT/TSV line alignment unavailable"}
    grouped = []
    for region, index in zip(regions, nonempty):
        indices = [i for i,w in enumerate(words)
                   if tuple(w.get(k) for k in ("block","paragraph","line")) == region["key"]]
        native = "".join(words[i]["text"] for i in indices)
        if not native or compact(native) != compact(parts[index]):
            return text, words, {**metadata, "status": "TXT/TSV text alignment unavailable"}
        grouped.append((region, index, indices, native))
    if sorted(i for _,_,indices,_ in grouped for i in indices) != list(range(len(words))):
        return text, words, {**metadata, "status": "unassigned or duplicate word geometry"}
    verifier = glyph.GlyphVerifier(sources)
    replacements = {}
    for region, index, indices, native in grouped:
        check()
        box = region["bbox"]
        line = {"text": native, "bbox": box, "confidence": None,
                **dict(zip(("block","paragraph","line"), region["key"]))}
        if vertical:
            refined, records = glyph.refine_vertical_columns(verifier, gray, [line], [[0]])
            accepted = bool(records) and all(r.get("accepted") for r in records)
        else:
            refined, records = glyph.refine_horizontal(verifier, gray, [line])
            accepted = bool(records) and all(r.get("glyph_verification",{}).get("accepted") for r in records)
        check()
        metadata["glyph_verification"].append({"line_key": list(region["key"]),
            "source_indices": indices, "input_txt_line": parts[index], "accepted": accepted,
            "evidence": records})
        if accepted:
            output = refined[0]["text"]
            ending = parts[index][len(parts[index].rstrip("\r\n")):]
            parts[index] = output+ending
            replacements[indices[0]] = (set(indices), {**line, "text": output})
    removed = set()
    for indices, _ in replacements.values():
        removed.update(indices)
    output_words = []
    for index, word in enumerate(words):
        if index in replacements:
            output_words.append(replacements[index][1])
        elif index not in removed:
            output_words.append(word)
    metadata["glyph_verification_applied"] = bool(replacements)
    metadata["status"] = "verified lines replaced; all other native lines retained"
    return "".join(parts), output_words, metadata
