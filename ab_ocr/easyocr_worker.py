"""Isolated, CPU-only EasyOCR worker. Never downloads models or sends documents.

Single request: --input IMAGE --output JSON --languages chi_sim --model-dir DIR [--direction vertical]
Batch request: --batch REQUEST.json --model-dir DIR
REQUEST is a list (or {"items": [...]}) of input/output/languages dictionaries.
Each batch item may override the command direction (default: horizontal).
Result boxes are rendered image pixel coordinates; text is never normalized.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time


LANGUAGES = {"chi_sim": ("ch_sim", "zh_sim_g2.pth"),
             "chi_tra": ("ch_tra", "chinese.pth"),
             "kor": ("ko", "korean_g2.pth")}
VERTICAL_RECOGNITION_POLICY = "easyocr-vertical-dual-chinese-local-glyph-v3"
HORIZONTAL_RECOGNITION_POLICY = "easyocr-horizontal-font-layout-verified-v1"
PRIMARY_GLYPH_CONFIDENCE_MAX = 0.8
AUXILIARY_GLYPH_CONFIDENCE_MIN = 0.9
AUXILIARY_PADDING_RATIO = 0.1
MAX_REFLOW_WIDTH = 8192
MAX_REFLOW_PIXELS = 8_000_000


def language_config(language: str) -> tuple[str, str]:
    try:
        return LANGUAGES[language]
    except KeyError:
        raise ValueError("EasyOCR requires chi_sim, chi_tra, or kor separately; "
                         "a combined Korean/Chinese model is not available.") from None


def split_vertical_box(gray, box):
    """Split a tall upright region at whitespace, never at a fixed glyph count.

    Input/output use EasyOCR rectangles [left, right, top, bottom]. Only clear
    gaps wider than a quarter of the region width separate glyphs; nearby ink
    strokes stay together. Unseparable/blank regions remain intact. Each crop
    keeps its source coordinates and enough vertical padding for thin glyphs.
    """
    import cv2
    import numpy as np
    x0, x1, y0, y1 = box
    width, height = x1 - x0, y1 - y0
    if height < 1.8 * width:
        return [box]
    crop = gray[y0:y1, x0:x1]
    _, mask = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    rows = np.flatnonzero(np.any(mask > 0, axis=1))
    groups = []
    for row in rows:
        if groups and row - groups[-1][1] <= max(2, round(width * 0.25)):
            groups[-1][1] = int(row) + 1
        else:
            groups.append([int(row), int(row) + 1])
    if len(groups) < 2:
        return [box]
    cuts = [0] + [(a[1] + b[0]) // 2 for a, b in zip(groups, groups[1:])] + [height]
    segments = []
    for index, (start, end) in enumerate(groups):
        center = (start + end) / 2
        half = max(width / 2, (end - start) / 2 + max(2, round(width * 0.1)))
        top = y0 + max(cuts[index], math.floor(center - half))
        bottom = y0 + min(cuts[index + 1], math.ceil(center + half))
        segments.append([x0, x1, top, bottom])
    return segments


def _column_indices(words):
    if __package__:
        from .reading_order import vertical_column_indices
    else:
        # -I excludes the script directory; load only the shipped sibling.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "yeone_vertical_geometry", Path(__file__).with_name("reading_order.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        vertical_column_indices = module.vertical_column_indices
    return vertical_column_indices(words)


def _glyph_module():
    if __package__:
        from . import glyph_verifier
        return glyph_verifier
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "yeone_local_glyph_verifier", Path(__file__).with_name("glyph_verifier.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reflow_chunks(segments):
    """Bound recognizer inputs; retain an unsplittable tall region on its own."""
    chunk, width, height = [], 0, 0
    for segment in segments:
        left, right, top, bottom = segment["box"]
        w, h = right - left, bottom - top
        if w > MAX_REFLOW_WIDTH or w * h > MAX_REFLOW_PIXELS:
            raise ValueError("Vertical recognition region exceeds the image limit")
        tall = h >= 1.8 * w
        if chunk and (tall or width + w > MAX_REFLOW_WIDTH
                      or (width + w) * max(height, h) > MAX_REFLOW_PIXELS):
            yield chunk
            chunk, width, height = [], 0, 0
        if tall:
            yield [segment]
        else:
            chunk.append(segment)
            width += w
            height = max(height, h)
    if chunk:
        yield chunk


def reflow_image(gray, segments):
    """Move upright source crops into a horizontal strip, without rescaling."""
    import numpy as np
    height = max(s["box"][3] - s["box"][2] for s in segments)
    width = sum(s["box"][1] - s["box"][0] for s in segments)
    strip = np.full((height, width), 255, dtype=np.uint8)
    mapping, offset = [], 0
    for segment in segments:
        left, right, top, bottom = segment["box"]
        crop = gray[top:bottom, left:right]
        y = (height - crop.shape[0]) // 2
        strip[y:y + crop.shape[0], offset:offset + crop.shape[1]] = crop
        mapping.append({"source_bbox": [left, top, right, bottom],
                        "source_detection_index": segment["detection"],
                        "reflow_bbox": [offset, y, offset + crop.shape[1], y + crop.shape[0]]})
        offset += crop.shape[1]
    return strip, mapping


def _recognize_crop(reader, crop):
    h, w = crop.shape
    result = reader.recognize(crop, horizontal_list=[[0, w, 0, h]], free_list=[],
        detail=1, paragraph=False, decoder="greedy", batch_size=1, workers=0, reformat=False)
    if len(result) != 1:
        raise ValueError("Expected one recognition result per reflowed region")
    text, confidence = result[0][1], float(result[0][2])
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("Invalid EasyOCR recognition confidence")
    return {"text": str(text), "confidence": confidence}


def _single_han(text):
    # Restrict auxiliary decisions to one unmodified CJK code point. Variation
    # selectors, punctuation and uncertain segmentation are not reinterpreted.
    return len(text) == 1 and (0x3400 <= ord(text) <= 0x4dbf or 0x4e00 <= ord(text) <= 0x9fff)


def refine_vertical_region(reader, auxiliary_reader, gray, segments, text):
    """Recheck uncertain traditional glyphs with EasyOCR's second Chinese model.

    No reference text, conversion table or character-specific rule is used.
    Sequence and isolated recognition must agree before a glyph is eligible;
    the auxiliary model must agree on the original and white-padded crop.
    Scores are decision gates, not calibrated probabilities across models.
    """
    import numpy as np
    evidence = {"status": "checked", "primary_text": text, "glyphs": [], "changes": []}
    if len(text) != len(segments):
        evidence["status"] = "skipped: text and segment counts differ"
        return text, evidence
    output = list(text)
    for index, (char, segment) in enumerate(zip(text, segments)):
        left, right, top, bottom = segment["box"]
        if not _single_han(char) or not 0.5 < (right-left)/(bottom-top) < 1.8:
            continue
        crop = gray[top:bottom, left:right]
        primary = _recognize_crop(reader, crop)
        record = {"index": index, "source_bbox": [left, top, right, bottom],
                  "primary": primary, "accepted": False}
        evidence["glyphs"].append(record)
        if primary["text"] != char or primary["confidence"] >= PRIMARY_GLYPH_CONFIDENCE_MAX:
            continue
        auxiliary = auxiliary_reader()
        candidate = _recognize_crop(auxiliary, crop)
        record["auxiliary"] = candidate
        if (not _single_han(candidate["text"]) or candidate["text"] == char
                or candidate["confidence"] < AUXILIARY_GLYPH_CONFIDENCE_MIN):
            continue
        padding = max(2, round(max(crop.shape) * AUXILIARY_PADDING_RATIO))
        padded = np.pad(crop, padding, mode="constant", constant_values=255)
        repeated = _recognize_crop(auxiliary, padded)
        record.update({"auxiliary_padded": repeated, "padding_pixels": padding})
        if (repeated["text"] != candidate["text"]
                or repeated["confidence"] < AUXILIARY_GLYPH_CONFIDENCE_MIN):
            continue
        output[index] = candidate["text"]
        record["accepted"] = True
        evidence["changes"].append({"index": index, "before": char, "after": candidate["text"]})
    return "".join(output), evidence


def recognize_vertical(reader, gray, auxiliary_reader=None, glyph_verifier=None):
    """Separate CRAFT regions, reflow upright glyphs, and map back to the page."""
    horizontal, free = reader.detect(gray, min_size=20, text_threshold=0.7,
        low_text=0.4, link_threshold=0.4, canvas_size=2560, mag_ratio=1.0,
        ycenter_ths=0, height_ths=0, width_ths=0, add_margin=0.1)
    # detect returns one outer list for each image; this request is one page.
    polygons = [[[float(b[0]), float(b[2])], [float(b[1]), float(b[2])],
                 [float(b[1]), float(b[3])], [float(b[0]), float(b[3])]] for b in horizontal[0]]
    polygons += [[[float(x), float(y)] for x, y in poly] for poly in free[0]]
    height, width = gray.shape
    words, rectangles = [], []
    for polygon in polygons:
        if not polygon or not all(math.isfinite(v) for point in polygon for v in point):
            raise ValueError("Invalid vertical detection coordinates")
        left = max(0, math.floor(min(p[0] for p in polygon)))
        right = min(width, math.ceil(max(p[0] for p in polygon)))
        top = max(0, math.floor(min(p[1] for p in polygon)))
        bottom = min(height, math.ceil(max(p[1] for p in polygon)))
        if not (left < right and top < bottom):
            raise ValueError("Vertical detection lies outside the image")
        rectangles.append([left, right, top, bottom])
        words.append({"bbox": [left, top, right, bottom]})
    results, regions, unresolved = [], [], 0
    for column in _column_indices(words):
        segments = []
        for index in column:
            for box in split_vertical_box(gray, rectangles[index]):
                segments.append({"box": box, "detection": index})
                unresolved += int(box[3] - box[2] >= 1.8 * (box[1] - box[0]))
        for chunk in reflow_chunks(segments):
            strip, mapping = reflow_image(gray, chunk)
            h, w = strip.shape
            primary = _recognize_crop(reader, strip)
            text, confidence = primary["text"], primary["confidence"]
            refinement = None
            if auxiliary_reader is not None:
                text, refinement = refine_vertical_region(reader, auxiliary_reader, gray, chunk, text)
                refinement["primary_confidence"] = confidence
                # A composite string has no single-model sequence confidence.
                if refinement["changes"]:
                    confidence = None
            glyph_evidence = None
            if glyph_verifier is not None:
                text, glyph_evidence = _glyph_module().refine_region(glyph_verifier, gray, chunk, text)
                glyph_evidence["input_confidence"] = confidence
                if glyph_evidence["changes"]:
                    confidence = None
            left = min(s["box"][0] for s in chunk)
            right = max(s["box"][1] for s in chunk)
            top = min(s["box"][2] for s in chunk)
            bottom = max(s["box"][3] for s in chunk)
            results.append(([[left, top], [right, top], [right, bottom], [left, bottom]],
                            text, confidence))
            regions.append({"source_bbox": [left, top, right, bottom], "segments": mapping,
                            "reflow_size": [w, h], "reflow_sha256": hashlib.sha256(strip.tobytes()).hexdigest(),
                            **({"recognition_refinement": refinement} if refinement is not None else {}),
                            **({"glyph_refinement": glyph_evidence} if glyph_evidence is not None else {})})
    return results, {"recognition_policy": VERTICAL_RECOGNITION_POLICY,
        "vertical_preprocessing": {"detection_polygons": polygons, "regions": regions,
            "unresolved_tall_regions": unresolved,
            "detector_grouping": {"ycenter_ths": 0, "height_ths": 0, "width_ths": 0, "add_margin": 0.1},
            "segmentation": "Otsu dark ink; whitespace gap > 0.25 region width; tall threshold 1.8",
            "reflow": "upright crops top-to-bottom placed left-to-right without rotation or rescaling",
            "coordinate_policy": "output boxes cover source crops; exact source-to-strip crop mapping retained"},
        **({"vertical_refinement": {"primary_model": "chinese.pth", "auxiliary_model": "zh_sim_g2.pth",
            "primary_glyph_confidence_below": PRIMARY_GLYPH_CONFIDENCE_MAX,
            "auxiliary_confidence_at_least": AUXILIARY_GLYPH_CONFIDENCE_MIN,
            "auxiliary_padding_ratio": AUXILIARY_PADDING_RATIO,
            "agreement": "one Han code point; sequence agrees with isolated primary; auxiliary agrees on two crops",
            "confidence_policy": "changed regions have null confidence; original model scores retained on 0..1 scale",
            "conversion_policy": "none; every candidate comes from image recognition"}}
          if auxiliary_reader is not None else {})}


def verify_models(model_dir: Path, language: str) -> list[dict]:
    _, model_name = language_config(language)
    manifest_path = model_dir.parent / "installation.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("engine") != "easyocr":
        raise ValueError("EasyOCR installation manifest is invalid")
    indexed = {item["filename"]: item for item in manifest["models"]}
    verified = []
    for filename in ("craft_mlt_25k.pth", model_name):
        record = indexed[filename]
        digest = hashlib.sha256((model_dir / filename).read_bytes()).hexdigest()
        if digest != record["sha256"]:
            raise ValueError("EasyOCR model checksum mismatch; reinstall required")
        verified.append(dict(record))
    return verified


def convert_results(results: list, image_size: tuple[int, int] | None = None) -> tuple[str, list[dict]]:
    """Keep EasyOCR sequence and Unicode, with one newline per detected box."""
    words = []
    for index, (polygon, text, confidence) in enumerate(results, 1):
        points = [(float(point[0]), float(point[1])) for point in polygon]
        if not points or not all(math.isfinite(v) for point in points for v in point):
            raise ValueError("Invalid OCR geometry")
        score = None if confidence is None else float(confidence) * 100
        if score is not None and not math.isfinite(score):
            score = None
        elif score is not None and not 0 <= score <= 100:
            raise ValueError("Invalid OCR confidence")
        bbox = [min(x for x, _ in points), min(y for _, y in points),
                max(x for x, _ in points), max(y for _, y in points)]
        word = {"text": str(text), "bbox": bbox,
                "confidence": score, "block": 1, "paragraph": 1, "line": index}
        if image_size is not None:
            width, height = image_size
            clipped = [max(0.0, min(float(width), bbox[0])),
                       max(0.0, min(float(height), bbox[1])),
                       max(0.0, min(float(width), bbox[2])),
                       max(0.0, min(float(height), bbox[3]))]
            if clipped != bbox:
                word["original_bbox"] = bbox
                word["bbox"] = clipped
                word["coordinate_warning"] = "clipped to rendered image bounds"
        words.append(word)
    return "\n".join(word["text"] for word in words), words


def write_result(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         delete=False, suffix=".tmp") as handle:
            temporary = Path(handle.name)
            json.dump(result, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class EasyOCRWorker:
    def __init__(self, model_dir: Path, threads: int = 2):
        self.model_dir = model_dir.resolve()
        self.threads = threads
        self.readers = {}
        self.models = {}
        self.glyph_verifier = None

    def reader(self, language: str, *, detector: bool = True):
        key = (language, detector)
        if key not in self.readers:
            easy_language, _ = language_config(language)
            self.models[language] = verify_models(self.model_dir, language)
            os.environ["EASYOCR_MODULE_PATH"] = str(self.model_dir.parent)
            import torch
            import easyocr
            torch.set_num_threads(self.threads)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.readers[key] = easyocr.Reader(
                    [easy_language], gpu=False, model_storage_directory=str(self.model_dir),
                    user_network_directory=str(self.model_dir.parent / "user_network"),
                    download_enabled=False, verbose=False, quantize=True, detector=detector)
        return self.readers[key]

    def recognize(self, input_path: Path, language: str, direction: str = "horizontal") -> dict:
        if direction not in ("horizontal", "vertical"):
            raise ValueError("Invalid reading direction")
        # Accept existing local files only, so EasyOCR's URL-input feature cannot run.
        input_path = input_path.resolve(strict=True)
        if not input_path.is_file():
            raise ValueError("Input must be a local image file")
        reader = self.reader(language)
        from PIL import Image
        with Image.open(input_path) as image:
            image_width, image_height = image.size
        started = time.perf_counter()
        layout_metadata = {}
        auxiliary = None
        glyph_verifier = None
        used_models = list(self.models[language])
        if direction == "vertical" and language == "chi_tra":
            # This dependency is also recorded by the host for cache invalidation.
            auxiliary_models = verify_models(self.model_dir, "chi_sim")
            used_models.extend(m for m in auxiliary_models if m["filename"] != "craft_mlt_25k.pth")
            auxiliary = lambda: self.reader("chi_sim", detector=False)
        if ((direction == "vertical" and language == "chi_tra")
                or (direction == "horizontal" and language in ("chi_sim", "chi_tra"))):
            module = _glyph_module()
            if self.glyph_verifier is None:
                self.glyph_verifier = module.GlyphVerifier(module.font_sources())
            glyph_verifier = self.glyph_verifier
            layout_metadata.update({"recognition_pipeline": "EasyOCR + local printed-glyph verification",
                "glyph_verification_policy": module.POLICY,
                "glyph_font_sources": glyph_verifier.sources,
                "glyph_verification_thresholds": {"min_similarity": module.MIN_SIMILARITY,
                                                   "min_margin": module.MIN_MARGIN},
                "glyph_verification_status": "enabled" if glyph_verifier.sources else "disabled: no installed font sources"})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            if direction == "vertical":
                import numpy as np
                with Image.open(input_path) as image:
                    gray = np.asarray(image.convert("L"))
                results, vertical_metadata = recognize_vertical(reader, gray, auxiliary_reader=auxiliary,
                                                                 glyph_verifier=glyph_verifier)
                layout_metadata.update(vertical_metadata)
            else:
                results = reader.readtext(str(input_path), detail=1, paragraph=False,
                                          decoder="greedy", batch_size=1, workers=0,
                                          canvas_size=2560, mag_ratio=1.0)
                if glyph_verifier is not None:
                    import numpy as np
                    with Image.open(input_path) as image:
                        gray = np.asarray(image.convert("L"))
                    original_text, original_words = convert_results(results, (image_width, image_height))
                    refined, evidence = _glyph_module().refine_horizontal(glyph_verifier, gray, original_words)
                    results = []
                    for word in refined:
                        left, top, right, bottom = word["bbox"]
                        score = word["confidence"]
                        results.append(([[left, top], [right, top], [right, bottom], [left, bottom]],
                                        word["text"], None if score is None else score/100))
                    layout_metadata.update({"recognition_policy": HORIZONTAL_RECOGNITION_POLICY,
                        "horizontal_preprocessing": {"raw_engine_result": {"text": original_text,
                            "words": original_words}, "regions": evidence,
                            "grouping": "consecutive row fragments; >0.5 height overlap; gap < smaller height",
                            "recognition": "font/size/origin anchor; glyph shapes and advances; complete-line rendering verification",
                            "confidence_policy": "verified composite lines have null confidence; original scores retained"}})
        text, words = convert_results(results, (image_width, image_height))
        import easyocr
        import torch
        duration_seconds = time.perf_counter() - started
        return {"text": text, "words": words, "engine_version": easyocr.__version__,
                "duration_seconds": duration_seconds,
                "image_width": image_width, "image_height": image_height,
                "engine_metadata": {"engine": "easyocr", "device": "cpu",
                                    "languages": language, "easyocr_language": LANGUAGES[language][0],
                                    "torch_version": torch.__version__, "threads": self.threads,
                                    "decoder": "greedy", "paragraph": False,
                                    "canvas_size": 2560, "mag_ratio": 1.0, "psm": None,
                                    "text_threshold": 0.7, "low_text": 0.4,
                                    "link_threshold": 0.4, "contrast_ths": 0.1,
                                    "adjust_contrast": 0.5, "filter_ths": 0.003,
                                    "word_unit": "source region of upright reflowed crops" if direction == "vertical" else ("verified source line or original detection region" if glyph_verifier is not None else "detected text region, often a full line"),
                                    "coordinate_policy": "source crop envelope; reflow mappings retained" if direction == "vertical" else "axis-aligned polygon envelope clipped to image; original_bbox retained if changed",
                                    "coordinate_warnings": sum("original_bbox" in word for word in words),
                                    "download_enabled": False, "quantize": True,
                                    "models": used_models,
                                    "recognition_seconds": duration_seconds,
                                    "text_join_policy": "reflowed source region order; newline between regions" if direction == "vertical" else ("newline between verified lines; original fragments retained on abstention" if glyph_verifier is not None else "engine order; newline between detected boxes"),
                                    "unicode_normalization": "none", **layout_metadata}}


class _Directed:
    """Session requests carry no direction; the session pins one at start."""
    def __init__(self, worker, direction):
        self.worker, self.direction = worker, direction

    def recognize(self, source, languages):
        return self.worker.recognize(source, languages, self.direction)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--languages", choices=list(LANGUAGES))
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--direction", choices=("horizontal", "vertical"), default="horizontal")
    parser.add_argument("--session-root", type=Path)
    parser.add_argument("--max-requests", type=int, default=8)
    args = parser.parse_args(argv)
    if args.threads < 1:
        parser.error("--threads must be positive")
    if args.session_root:
        # Resident worker: models are verified and loaded once for up to --max-requests pages.
        import importlib.util
        spec = importlib.util.spec_from_file_location("yeone_worker_protocol", Path(__file__).with_name("worker_sessions.py"))
        protocol = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(protocol)
        return protocol.serve_requests(_Directed(EasyOCRWorker(args.model_dir, args.threads), args.direction),
                                       args.session_root, args.languages, write_result, max_requests=args.max_requests)
    if args.batch:
        data = json.loads(args.batch.read_text(encoding="utf-8-sig"))
        jobs = data.get("items", data.get("jobs")) if isinstance(data, dict) else data
    elif args.input and args.output and args.languages:
        jobs = [{"input": str(args.input), "output": str(args.output), "languages": args.languages}]
    else:
        parser.error("Provide input/output/languages, or batch")
    worker = EasyOCRWorker(args.model_dir, args.threads)
    for job in jobs:
        source, output = Path(job["input"]), Path(job["output"])
        if source.resolve() == output.resolve():
            raise ValueError("Input and output must differ")
        result = worker.recognize(source, job["languages"], job.get("direction", args.direction))
        write_result(output, result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Do not print input paths, recognized text or third-party exception payloads.
        print(f"EasyOCR worker failed ({type(error).__name__}); "
              "verify installation, language and local image input.", file=sys.stderr)
        raise SystemExit(1)
