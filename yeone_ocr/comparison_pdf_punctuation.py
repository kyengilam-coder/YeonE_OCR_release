"""Optional born-digital PDF punctuation provenance, never an OCR/text replacement.

Only an existing single-punctuation proposal can be held. Unavailable evidence
leaves the existing OCR policy unchanged. Han, source text and proposals are
never rewritten, normalized, or inferred from glyph width.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import unicodedata
from pathlib import Path

CONTRACT = "native-pdf-punctuation-v1"
MAX_CHARS = 20_000
MAX_PIXELS = 40_000_000


class PDFPunctuationError(ValueError):
    """Malformed, mismatched or no-longer-current source evidence."""


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _text_sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _hex(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _box(value, *, nonempty=False):
    return (isinstance(value, (list, tuple)) and len(value) == 4
            and all(type(v) in (int, float) and math.isfinite(v) for v in value)
            and value[2] >= value[0] and value[3] >= value[1]
            and (not nonempty or (value[2] > value[0] and value[3] > value[1])))


def _area(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def _inside(inner, outer, tolerance=0.0):
    return (inner[0] >= outer[0] - tolerance and inner[1] >= outer[1] - tolerance
            and inner[2] <= outer[2] + tolerance and inner[3] <= outer[3] + tolerance)


def _separator(char):
    return char.isspace() or unicodedata.category(char).startswith("P")


def _punctuation(char):
    return isinstance(char, str) and len(char) == 1 and unicodedata.category(char).startswith("P")


def _gap(text, left, right):
    indices = [i for i, c in enumerate(text) if not _separator(c)]
    content = "".join(text[i] for i in indices)
    needle = left + right
    offset = content.find(needle)
    if offset < 0 or content.find(needle, offset + 1) >= 0:
        return None
    return indices[offset + len(left) - 1] + 1, indices[offset + len(left)]


def _address_indices(text, left, right):
    indices = [i for i, c in enumerate(text) if not _separator(c)]
    content = "".join(text[i] for i in indices)
    offset = content.find(left + right)
    if offset < 0 or content.find(left + right, offset + 1) >= 0:
        return None
    return indices[offset:offset + len(left) + len(right)]


def _input_box(bounds, dpi, crop):
    scale = dpi / 72
    return [bounds[0] * scale - crop[0], bounds[1] * scale - crop[1],
            bounds[2] * scale - crop[0], bounds[3] * scale - crop[1]]


def _pixel_box(bounds, width, height):
    if not _box(bounds, nonempty=True) or not _inside(bounds, [0, 0, width, height]):
        return None
    return [math.floor(bounds[0]), math.floor(bounds[1]), math.ceil(bounds[2]), math.ceil(bounds[3])]


def _occluded(value, char, *, anchor=False):
    for seqno in range(char["seqno"] + 1, len(value["paints"])):
        paint = value["paints"][seqno]
        if _area(char["bbox"], paint["bbox"]) <= 0:
            continue
        if anchor and paint["type"] in ("fill-text", "stroke-text"):
            later = [c for c in value["chars"] if c["seqno"] == seqno]
            if later and not any(_area(char["bbox"], c["bbox"]) > 0 for c in later):
                continue
        return True
    return False


def _sealed(value):
    value["sha256"] = _digest(value)
    return value


def _unavailable(base, reason):
    return _sealed({**base, "status": "unavailable", "reason_codes": [reason]})


def _word_regions(run):
    raw = run.get("raw", {})
    text, words = raw.get("text"), raw.get("words")
    if not isinstance(text, str) or len(text) > MAX_CHARS or not isinstance(words, list):
        return None
    indices = [i for i, c in enumerate(text) if not c.isspace()]
    parts = []
    for word in words:
        if not isinstance(word, dict) or not isinstance(word.get("text"), str) or not _box(word.get("bbox"), nonempty=True):
            return None
        parts.append("".join(c for c in word["text"] if not c.isspace()))
    if "".join(parts) != "".join(text[i] for i in indices):
        return None
    regions, offset = [], 0
    for word, part in zip(words, parts):
        if part:
            regions.append({"start": indices[offset], "end": indices[offset + len(part) - 1] + 1, "bbox": list(word["bbox"])})
        offset += len(part)
    return {"run_id": run["id"], "raw_sha256": run["raw_sha256"], "regions": regions}


def _restricted_drawings(page):
    for draw in page.get_drawings(extended=True):
        if draw.get("layer"):
            return True
        if draw["type"] == "group" and (draw.get("opacity") != 1 or draw.get("blendmode") != "Normal" or draw.get("knockout")):
            return True
        if draw["type"] == "clip":
            items = draw.get("items", [])
            if (len(items) != 1 or items[0][0] != "re"
                    or not _inside(list(page.rect), list(items[0][1]), 0.01)):
                return True
    return False


def capture(project, number, *, pdf_path=None, pdf_page=None, dpi=None, crop=None):
    """Freeze PDF metadata only after exact native-render/source-image binding.

    ``crop`` is an integer [x0,y0,x1,y1] in the full PDF render at ``dpi``.
    PNG projects require explicit PDF path/page. Direct PDF projects use their
    canonical source and page; altered mappings are never inferred.
    """
    base = {"contract": CONTRACT, "source_sha256": project.source_sha256, "page_number": number}
    try:
        if type(number) is not int or not 1 <= number <= project.page_count:
            return _unavailable(base, "invalid-project-page")
        source = Path(project.source_path).resolve()
        if _sha(source) != project.source_sha256:
            return _unavailable(base, "project-source-changed")
        page_data = project.page(number)
        baseline = next((r for r in page_data["runs"] if r["id"] == page_data["baseline_run_id"]), None)
        if baseline is None:
            return _unavailable(base, "baseline-unavailable")
        snapshot = project.text(number)
        if not isinstance(snapshot, str) or not 1 <= len(snapshot) <= MAX_CHARS:
            return _unavailable(base, "snapshot-outside-bound")
        raw = baseline["raw"]
        recorded_dpi = baseline.get("request", {}).get("dpi") or raw.get("preprocessing", {}).get("render_dpi") or 300
        dpi = recorded_dpi if dpi is None else dpi
        if type(dpi) is not int or not 72 <= dpi <= 600 or dpi != recorded_dpi:
            return _unavailable(base, "render-dpi-mismatch")
        direct = source.suffix.lower() == ".pdf"
        if direct:
            target = source if pdf_path is None else Path(pdf_path).resolve()
            selected_page = number if pdf_page is None else pdf_page
            if target != source or selected_page != number or crop is not None:
                return _unavailable(base, "direct-pdf-origin-mismatch")
        else:
            if pdf_path is None or pdf_page is None:
                return _unavailable(base, "explicit-pdf-origin-required")
            target, selected_page = Path(pdf_path).resolve(), pdf_page
        if target.suffix.lower() != ".pdf" or type(selected_page) is not int or selected_page < 1:
            return _unavailable(base, "invalid-pdf-origin")
        pdf_sha = _sha(target)
        import pymupdf
        from PIL import Image, ImageOps
        with pymupdf.open(target) as document:
            if document.needs_pass or selected_page > len(document):
                return _unavailable(base, "pdf-page-unavailable")
            page = document[selected_page - 1]
            if page.rotation or abs(page.rect.x0) > 0.01 or abs(page.rect.y0) > 0.01 or page.cropbox != page.mediabox:
                return _unavailable(base, "unsupported-pdf-rotation-or-page-crop")
            if page.rect.width * page.rect.height * (dpi / 72) ** 2 > MAX_PIXELS:
                return _unavailable(base, "render-exceeds-bound")
            if _restricted_drawings(page):
                return _unavailable(base, "unsupported-clipping-transparency-or-layer")
            pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False, annots=True)
            full_size = [pixmap.width, pixmap.height]
            box = [0, 0, *full_size] if crop is None else crop
            if (not _box(box, nonempty=True) or any(type(v) is not int for v in box)
                    or not _inside(box, [0, 0, *full_size])):
                return _unavailable(base, "invalid-render-crop")
            rgb = Image.frombytes("RGB", tuple(full_size), pixmap.samples)
            selected = rgb.crop(tuple(box))
            pixels = selected.tobytes()
            if not direct:
                with Image.open(source) as image:
                    image.seek(number - 1)
                    input_image = ImageOps.exif_transpose(image)
                    if input_image.mode in ("I", "F") or input_image.mode.startswith("I;16"):
                        return _unavailable(base, "unsupported-high-bit-input")
                    if "A" in input_image.getbands() or "transparency" in input_image.info:
                        rgba = input_image.convert("RGBA")
                        input_image = Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba)
                    input_image = input_image.convert("RGB")
                    if input_image.size != selected.size or input_image.tobytes() != pixels:
                        return _unavailable(base, "pdf-input-rgb-pixel-mismatch")
            if selected.size != (raw["image_width"], raw["image_height"]):
                return _unavailable(base, "baseline-render-size-mismatch")
            paints = [{"type": kind, "bbox": list(bounds)} for kind, bounds in page.get_bboxlog()]
            if len(paints) > MAX_CHARS:
                return _unavailable(base, "paint-log-exceeds-bound")
            chars = []
            for span in page.get_texttrace():
                for codepoint, gid, origin, bounds in span["chars"]:
                    if not 0 <= codepoint <= 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                        return _unavailable(base, "unsupported-native-character")
                    pixels_box = _pixel_box(_input_box(bounds, dpi, box), selected.width, selected.height)
                    extrema = [list(v) for v in selected.crop(tuple(pixels_box)).getextrema()] if pixels_box is not None else None
                    chars.append({"char": chr(codepoint), "gid": gid, "origin": list(origin), "bbox": list(bounds),
                        "type": span["type"], "opacity": span["opacity"], "seqno": span["seqno"],
                        "dir": list(span["dir"]), "wmode": span["wmode"], "layer": span["layer"],
                        "font": span["font"], "size": span["size"], "color": list(span["color"]),
                        "pixel_bbox": pixels_box, "rgb_extrema": extrema})
                    if len(chars) > MAX_CHARS:
                        return _unavailable(base, "native-text-exceeds-bound")
            if not chars:
                return _unavailable(base, "native-text-unavailable")
            regions = [value for run in page_data["runs"]
                       if run.get("raw", {}).get("source_sha256") == project.source_sha256
                       and run["raw"].get("page_number") == number
                       and (run["raw"].get("image_width"), run["raw"].get("image_height")) == selected.size
                       and (value := _word_regions(run)) is not None]
            value = {**base, "status": "available", "reason_codes": [], "source_path": str(source),
                "source_kind": "pdf" if direct else "image", "snapshot_text": snapshot, "snapshot_sha256": _text_sha(snapshot),
                "baseline_run_id": baseline["id"], "baseline_raw_sha256": baseline["raw_sha256"],
                "pdf_path": str(target), "pdf_sha256": pdf_sha, "pdf_page": selected_page,
                "pdf_page_rect": list(page.rect), "pdf_rotation": 0, "dpi": dpi, "crop": list(box),
                "full_render_size": full_size, "image_width": selected.width, "image_height": selected.height,
                "full_render_rgb_sha256": hashlib.sha256(pixmap.samples).hexdigest(), "input_rgb_sha256": hashlib.sha256(pixels).hexdigest(),
                "source_binding": "direct-pdf-render" if direct else "native-render-crop-exact-RGB-identity",
                "renderer": f"PyMuPDF-{pymupdf.VersionBind}-RGB-annotsTrue", "ocr_regions": regions, "chars": chars, "paints": paints,
                "native_metadata_is_not_image_ocr": True, "source_normalization": False,
                "pixel_visibility_policy": "nonuniform-required-only; not-glyph-or-codepoint-proof"}
        if _sha(target) != pdf_sha or _sha(source) != project.source_sha256:
            return _unavailable(base, "source-changed-during-capture")
        value = _sealed(value)
        validate_capture(value, project.source_sha256, number)
        return value
    except (OSError, ValueError, KeyError, TypeError, IndexError, ImportError, RuntimeError):
        return _unavailable(base, "capture-unavailable")


def validate_capture(value, source_sha256, page_number):
    """Pure structural/hash replay. Does not open PDF, project or model files."""
    def require(condition):
        if not condition:
            raise PDFPunctuationError("Invalid or mismatched PDF punctuation capture.")
    require(isinstance(value, dict) and value.get("contract") == CONTRACT)
    require(_hex(source_sha256) and type(page_number) is int and page_number > 0)
    require(value.get("source_sha256") == source_sha256 and value.get("page_number") == page_number)
    require(_hex(value.get("sha256")) and value["sha256"] == _digest({k: v for k, v in value.items() if k != "sha256"}))
    require(value.get("status") in ("available", "unavailable") and isinstance(value.get("reason_codes"), list))
    if value["status"] == "unavailable":
        require(bool(value["reason_codes"]) and all(isinstance(r, str) for r in value["reason_codes"]))
        return True
    require(value["reason_codes"] == [] and value.get("source_normalization") is False)
    require(value.get("native_metadata_is_not_image_ocr") is True)
    require(value.get("pixel_visibility_policy") == "nonuniform-required-only; not-glyph-or-codepoint-proof")
    for key in ("source_path", "pdf_path", "renderer", "baseline_run_id"):
        require(isinstance(value.get(key), str) and bool(value[key]))
    for key in ("pdf_sha256", "baseline_raw_sha256", "input_rgb_sha256", "full_render_rgb_sha256", "snapshot_sha256"):
        require(_hex(value.get(key)))
    text = value.get("snapshot_text")
    require(isinstance(text, str) and 1 <= len(text) <= MAX_CHARS and _text_sha(text) == value["snapshot_sha256"])
    require(value.get("source_kind") in ("pdf", "image") and value.get("source_binding") in ("direct-pdf-render", "native-render-crop-exact-RGB-identity"))
    require(type(value.get("pdf_page")) is int and value["pdf_page"] > 0 and value.get("pdf_rotation") == 0)
    require(type(value.get("dpi")) is int and 72 <= value["dpi"] <= 600 and _box(value.get("pdf_page_rect"), nonempty=True))
    size = value.get("full_render_size")
    require(isinstance(size, list) and len(size) == 2 and all(type(v) is int and v > 0 for v in size) and math.prod(size) <= MAX_PIXELS)
    box = value.get("crop")
    require(_box(box, nonempty=True) and all(type(v) is int for v in box) and _inside(box, [0, 0, *size]))
    require(value.get("image_width") == box[2] - box[0] and value.get("image_height") == box[3] - box[1])
    if value["source_kind"] == "pdf":
        require(value["source_binding"] == "direct-pdf-render" and value["source_path"] == value["pdf_path"])
        require(value["source_sha256"] == value["pdf_sha256"] and value["pdf_page"] == page_number)
        require(box == [0, 0, *size] and value["input_rgb_sha256"] == value["full_render_rgb_sha256"])
    else:
        require(value["source_binding"] == "native-render-crop-exact-RGB-identity")
    require(isinstance(value.get("chars"), list) and 1 <= len(value["chars"]) <= MAX_CHARS)
    require(isinstance(value.get("paints"), list) and len(value["paints"]) <= MAX_CHARS)
    for paint in value["paints"]:
        require(isinstance(paint, dict) and isinstance(paint.get("type"), str) and _box(paint.get("bbox")))
    for char in value["chars"]:
        require(isinstance(char, dict) and isinstance(char.get("char"), str) and len(char["char"]) == 1 and not 0xD800 <= ord(char["char"]) <= 0xDFFF)
        require(_box(char.get("bbox")) and type(char.get("gid")) is int and char["gid"] >= 0)
        require(type(char.get("seqno")) is int and 0 <= char["seqno"] < len(value["paints"]))
        require(type(char.get("type")) is int and type(char.get("opacity")) in (int, float) and math.isfinite(char["opacity"]) and 0 <= char["opacity"] <= 1)
        require(char.get("dir") in ([1, 0], [1.0, 0.0]) and char.get("wmode") == 0 and isinstance(char.get("layer"), str))
        require(isinstance(char.get("color"), list) and len(char["color"]) in (1, 3)
                and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in char["color"]))
        require(isinstance(char.get("font"), str) and type(char.get("size")) in (int, float) and math.isfinite(char["size"]) and char["size"] > 0)
        require(isinstance(char.get("origin"), list) and len(char["origin"]) == 2 and all(type(v) in (int, float) and math.isfinite(v) for v in char["origin"]))
        require(char.get("pixel_bbox") == _pixel_box(_input_box(char["bbox"], value["dpi"], box), value["image_width"], value["image_height"]))
        extrema = char.get("rgb_extrema")
        if char["pixel_bbox"] is None:
            require(extrema is None)
        else:
            require(isinstance(extrema, list) and len(extrema) == 3
                    and all(isinstance(v, list) and len(v) == 2 and all(type(n) is int and 0 <= n <= 255 for n in v) and v[0] <= v[1] for v in extrema))
    require(isinstance(value.get("ocr_regions"), list))
    for run in value["ocr_regions"]:
        require(isinstance(run.get("run_id"), str) and _hex(run.get("raw_sha256")) and isinstance(run.get("regions"), list))
        for region in run["regions"]:
            require(type(region.get("start")) is int and type(region.get("end")) is int and 0 <= region["start"] < region["end"] <= MAX_CHARS)
            require(_box(region.get("bbox"), nonempty=True) and _inside(region["bbox"], [0, 0, value["image_width"], value["image_height"]]))
    return True


def assert_fresh(value):
    """Optional external hash check; never re-extract text or modify a source."""
    validate_capture(value, value.get("source_sha256"), value.get("page_number"))
    if value["status"] != "available":
        raise PDFPunctuationError("PDF punctuation capture is unavailable.")
    try:
        matched = _sha(value["pdf_path"]) == value["pdf_sha256"] and _sha(value["source_path"]) == value["source_sha256"]
    except OSError as exc:
        raise PDFPunctuationError("PDF punctuation source is unavailable.") from exc
    if not matched:
        raise PDFPunctuationError("PDF punctuation source changed.")
    return True


def assess(value, item):
    """Return a veto or auxiliary agreement; never return replacement text."""
    if not isinstance(value, dict) or not isinstance(item, dict):
        return {"contract": CONTRACT, "status": "unavailable", "blocked": False, "reason_codes": ["invalid-capture-or-proposal"]}
    result = {"contract": CONTRACT, "capture_sha256": value.get("sha256"), "proposal_id": item.get("id"),
              "proposal_sha256": None, "status": "unavailable", "blocked": False, "reason_codes": []}
    def stop(reason):
        result["reason_codes"] = [reason]
        return result
    try:
        result["proposal_sha256"] = _digest(item)
        validate_capture(value, value.get("source_sha256"), value.get("page_number"))
        if value["status"] != "available":
            return stop("capture-unavailable")
        if item.get("kind") != "punctuation" or not _punctuation(item.get("before")) or not _punctuation(item.get("after")) or item["before"] == item["after"]:
            return stop("not-a-single-punctuation-change")
        if item.get("snapshot_sha256") != value["snapshot_sha256"]:
            return stop("snapshot-mismatch")
        text = value["snapshot_text"]; start, end = item.get("start"), item.get("end")
        if type(start) is not int or end != start + 1 or not 0 <= start < len(text) or text[start:end] != item["before"]:
            return stop("proposal-position-mismatch")
        proof = item.get("guard_evidence", {})
        if (item.get("baseline_run_id", proof.get("baseline_run_id")) != value["baseline_run_id"]
                or item.get("baseline_raw_sha256", proof.get("baseline_raw_sha256")) != value["baseline_raw_sha256"]
                or item.get("source_sha256", value["source_sha256"]) != value["source_sha256"]
                or item.get("page_number", value["page_number"]) != value["page_number"]):
            return stop("baseline-or-source-mismatch")
        left, right = proof.get("content_left"), proof.get("content_right")
        if not all(isinstance(s, str) and len(s) == 4 and not any(_separator(c) for c in s) for s in (left, right)):
            return stop("content-flanks-unavailable")
        snapshot_gap = _gap(text, left, right)
        if snapshot_gap is None:
            return stop("snapshot-address-ambiguous")
        offsets = [i for i in range(*snapshot_gap) if _punctuation(text[i])]
        ordinal, count = proof.get("punctuation_ordinal"), proof.get("expected_punctuation_count")
        if type(ordinal) is not int or type(count) is not int or len(offsets) != count or not 0 <= ordinal < count or offsets[ordinal] != start:
            return stop("punctuation-ordinal-or-count-mismatch")
        evidence = item.get("evidence", {})
        run = next((r for r in value["ocr_regions"] if r["run_id"] == evidence.get("run_id") and r["raw_sha256"] == evidence.get("raw_sha256")), None)
        a, b = evidence.get("context_start"), evidence.get("context_end")
        if run is None or type(a) is not int or type(b) is not int or not 0 <= a < b <= MAX_CHARS:
            return stop("actual-ocr-region-unavailable")
        boxes = [r["bbox"] for r in run["regions"] if r["start"] < b and r["end"] > a]
        union = [min(q[0] for q in boxes), min(q[1] for q in boxes), max(q[2] for q in boxes), max(q[3] for q in boxes)] if boxes else None
        if not _box(evidence.get("bbox"), nonempty=True) or union is None or any(abs(x - y) > 1e-6 for x, y in zip(union, evidence["bbox"])):
            return stop("actual-ocr-region-mismatch")
        native = "".join(c["char"] for c in value["chars"])
        gap = _gap(native, left, right)
        if gap is None:
            return stop("native-content-address-ambiguous")
        native_offsets = [i for i in range(*gap) if _punctuation(native[i])]
        if len(native_offsets) != count:
            return stop("native-punctuation-count-mismatch")
        for index, offset in enumerate(native_offsets):
            if index != ordinal and native[offset] != text[offsets[index]]:
                return stop("neighbor-punctuation-mismatch")
        target_index = native_offsets[ordinal]
        address_indices = _address_indices(native, left, right)
        checked = sorted(set(address_indices + native_offsets))
        checked_glyphs = []
        for index in checked:
            glyph = value["chars"][index]
            if (glyph["type"] not in (0, 1) or glyph["opacity"] != 1 or glyph["gid"] <= 0 or glyph["layer"]
                    or not _inside(glyph["bbox"], value["pdf_page_rect"]) or all(v == 1 for v in glyph["color"])):
                return stop("native-address-glyph-hidden-or-unsupported")
            if _occluded(value, glyph, anchor=index in address_indices):
                return stop("native-address-glyph-potentially-occluded")
            extrema = glyph["rgb_extrema"]
            if extrema is None or not any(v[0] < v[1] for v in extrema):
                return stop("native-address-glyph-uniform-pixels")
            glyph_box = _input_box(glyph["bbox"], value["dpi"], value["crop"])
            if not _inside(glyph_box, [0, 0, value["image_width"], value["image_height"]]) or _area(glyph_box, evidence["bbox"]) <= 0:
                return stop("native-address-glyph-ocr-geometry-mismatch")
            checked_glyphs.append({"native_index": index, "input_bbox": glyph_box, "pixel_bbox": glyph["pixel_bbox"], "rgb_extrema": extrema,
                                   "seqno": glyph["seqno"], "address_content": index in address_indices})
        char = value["chars"][target_index]
        box = _input_box(char["bbox"], value["dpi"], value["crop"])
        result["address_glyph_evidence"] = checked_glyphs
        result["native_glyph"] = copy.deepcopy(char)
        result["native_glyph"]["input_bbox"] = box
        result["actual_ocr_region"] = copy.deepcopy(evidence)
        result["content_address"] = {"left": left, "right": right, "ordinal": ordinal, "count": count}
        if char["char"] == item["before"]:
            result.update(status="blocked", blocked=True, reason_codes=["visible-original-pdf-punctuation-supports-before"])
        elif char["char"] == item["after"]:
            result.update(status="source_supports_candidate", reason_codes=["visible-original-pdf-punctuation-supports-after"])
        else:
            return stop("native-glyph-is-third-literal")
        return result
    except (PDFPunctuationError, ValueError, TypeError, KeyError, IndexError):
        return stop("invalid-capture-or-proposal")
