"""Offline, isolated RapidOCR 3.10 / PP-OCRv6 medium worker.

Kept separate from v5 to preserve its models, dependencies and result identity.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import time
import unicodedata

# -I excludes the script directory from sys.path; load only the app-owned sibling.
_spec = importlib.util.spec_from_file_location("yeone_v5_worker_helpers", Path(__file__).with_name("rapidocr_worker.py"))
_shared = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_shared)
SUPPORTED_LANGUAGES = ("chi_sim", "chi_tra", "chi_sim+eng", "chi_tra+eng")
DETECTOR = "PP-OCRv6_det_medium.onnx"
RECOGNIZER = "PP-OCRv6_rec_medium.onnx"
HORIZONTAL_RECOGNITION_POLICY = "ppocrv6-traditional-original-crop-verified-v2"


def _han(char):
    return 0x3400 <= ord(char) <= 0x4dbf or 0x4e00 <= ord(char) <= 0x9fff


def punctuation_consensus_candidate(native, alternatives):
    """Propose Han substitutions only; a later full-line image proof is required.

    Refuse letters, numbers, whitespace and combining sequences. Retry outputs
    may omit native punctuation, but cannot introduce/reorder any punctuation or
    change the Han count. Never fill missing Han or choose font-generated text.
    """
    def parts(text):
        if not text or any(not _han(c) and not unicodedata.category(c).startswith("P") for c in text):
            return None
        return [c for c in text if _han(c)], [c for c in text if not _han(c)]
    original = parts(native)
    readings = [parts(text) for text in alternatives]
    if original is None or len(readings) < 2 or any(item is None for item in readings):
        return None
    han, punctuation = original
    agreed = readings[0][0]
    if not han or len(agreed) != len(han) or agreed == han or any(item[0] != agreed for item in readings):
        return None
    for _, marks in readings:
        remaining = iter(punctuation)
        if any(not any(mark == source for source in remaining) for mark in marks):
            return None
    characters = iter(agreed)
    return "".join(next(characters) if _han(c) else c for c in native)


def verify_models(model_dir, languages):
    if languages not in SUPPORTED_LANGUAGES:
        raise ValueError("PP-OCRv6 medium supports Chinese profiles only")
    manifest = json.loads((model_dir.parent / "installation.json").read_text(encoding="utf-8"))
    if (manifest.get("engine") != "ppocrv6" or manifest.get("rapidocr_version") != "3.10.0"
            or manifest.get("model_family") != "PP-OCRv6" or manifest.get("model_profile") != "medium"):
        raise ValueError("Incompatible PP-OCRv6 medium manifest")
    records = []
    for name in (DETECTOR, RECOGNIZER):
        candidates = [r for r in manifest["models"] if r.get("file") == name]
        if len(candidates) != 1:
            raise ValueError("Required medium model missing or duplicated")
        with (model_dir / name).open("rb") as stream:
            hasher = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(block)
            digest = hasher.hexdigest()
        if digest != candidates[0]["sha256"]:
            raise ValueError("Local model SHA-256 mismatch")
        records.append({"file": name, "sha256": digest})
    return manifest, records


class Worker:
    def __init__(self, model_dir):
        self.model_dir = model_dir.resolve()
        self.engines = {}
        self.glyph_verifier = None

    def refine_horizontal(self, engine, image_path, boxes, words):
        """Verify native complete-line or Han-consensus recrop proposals."""
        import numpy as np
        from PIL import Image
        glyph = _shared._helper_module("glyph_verifier")
        if self.glyph_verifier is None:
            self.glyph_verifier = glyph.GlyphVerifier(glyph.font_sources())
        if not self.glyph_verifier.sources:
            return words, [], self.glyph_verifier.sources
        with Image.open(image_path) as image:
            rgb = np.asarray(image.convert("RGB"))
            gray = np.asarray(image.convert("L"))
        output, records = [], []
        for index, (quad, word) in enumerate(zip([] if boxes is None else boxes, words)):
            record = {"source_index": index, "input_text": word["text"],
                      "source_quad": [[float(v) for v in point] for point in quad],
                      "accepted": False}
            output.append(word)
            records.append(record)
            left, top, right, bottom = word["bbox"]
            left, top, right, bottom = math.floor(left), math.floor(top), math.ceil(right), math.ceil(bottom)
            width, height = right-left, bottom-top
            if (len(quad) != 4 or not (0 <= left < right <= rgb.shape[1] and 0 <= top < bottom <= rgb.shape[0])
                    or width < 2*height or height > 512 or width*height > 8_000_000
                    or abs(quad[0][1]-quad[1][1]) > .1*height
                    or abs(quad[3][1]-quad[2][1]) > .1*height):
                record["status"] = "native retained: not a near-horizontal line"
                continue
            crop = rgb[top:bottom, left:right, ::-1].copy()
            pad = max(2, round(height*.2))
            padded = np.pad(crop, ((pad,pad),(pad,pad),(0,0)), constant_values=255)
            retry = engine.recognize_txt([crop, padded])
            candidates = list(retry.txts) if retry.txts is not None else []
            scores = [float(value) for value in retry.scores] if retry.scores is not None else []
            record.update(source_bbox=[left,top,right,bottom], white_border=pad,
                          retry_texts=candidates, retry_scores=scores,
                          source_crop_sha256=hashlib.sha256(crop.tobytes()).hexdigest(),
                          padded_crop_sha256=hashlib.sha256(padded.tobytes()).hexdigest())
            candidate = None
            if (len(candidates) == 2 and candidates[0] == candidates[1] and candidates[0]
                    and len(candidates[0]) == len(word["text"])):
                candidate = candidates[0]
                record["candidate_source"] = "complete native recrop consensus"
            elif len(candidates) == 2 and punctuation_consensus_candidate(word["text"], [candidates[1]]*2) is not None:
                # A second padding amount must independently confirm the Han
                # sequence; punctuation is retained only pending image proof.
                extra_pad = pad+1
                extra = np.pad(crop, ((extra_pad,extra_pad),(extra_pad,extra_pad),(0,0)), constant_values=255)
                confirmed = engine.recognize_txt([extra])
                extra_texts = list(confirmed.txts) if confirmed.txts is not None else []
                record["punctuation_retry"] = {"white_border": extra_pad,
                    "texts": extra_texts,
                    "scores": [float(s) for s in confirmed.scores] if confirmed.scores is not None else [],
                    "crop_sha256": hashlib.sha256(extra.tobytes()).hexdigest()}
                if len(extra_texts) == 1:
                    candidate = punctuation_consensus_candidate(word["text"], [candidates[1],extra_texts[0]])
                    if candidate is not None:
                        record["candidate_source"] = "native Han consensus; original punctuation pending visual proof"
            if candidate is None:
                record["status"] = "native retained: recrop consensus/alignment unavailable"
                continue
            record["candidate_text"] = candidate
            if candidate == word["text"]:
                record["status"] = "native retained: recrop agrees"
                continue
            # The font stage verifies a model prediction; it never supplies the text.
            verified, proof = glyph.refine_horizontal(self.glyph_verifier, gray, [dict(word, text=candidate)])
            record["glyph_verification"] = proof
            accepted = (len(verified) == 1 and verified[0]["text"] == candidate
                and len(proof) == 1 and proof[0].get("glyph_verification",{}).get("accepted",False))
            record.update(accepted=accepted, status="verified native recrop accepted" if accepted
                          else "native retained: visual verification unavailable or disagrees")
            if accepted:
                output[-1] = dict(word, text=candidate, confidence=None)
        return output, records, self.glyph_verifier.sources

    def engine(self, languages):
        if languages not in SUPPORTED_LANGUAGES:
            raise ValueError("Unsupported PP-OCRv6 language")
        route = "chinese_cht" if languages.startswith("chi_tra") else "ch"
        if route in self.engines:
            return self.engines[route]
        manifest, models = verify_models(self.model_dir, languages)
        import onnxruntime
        onnxruntime.disable_telemetry_events()
        from rapidocr import RapidOCR, ModelType, OCRVersion, EngineType
        for package, key in (("rapidocr", "rapidocr_version"), ("onnxruntime", "onnxruntime_version")):
            if importlib.metadata.version(package) != manifest[key]:
                raise ValueError("Runtime version differs from installation manifest")
        params = {
            "Global.log_level": "critical", "Global.text_score": 0.0,
            "Global.max_side_len": 4096, "Global.use_cls": False,
            "Global.model_root_dir": str(self.model_dir),
            "Det.model_path": str(self.model_dir / DETECTOR),
            "Det.engine_type": EngineType.ONNXRUNTIME, "Det.ocr_version": OCRVersion.PPOCRV6,
            "Det.model_type": ModelType.MEDIUM, "Det.lang_type": "multi",
            "Rec.model_path": str(self.model_dir / RECOGNIZER),
            "Rec.engine_type": EngineType.ONNXRUNTIME, "Rec.ocr_version": OCRVersion.PPOCRV6,
            "Rec.model_type": ModelType.MEDIUM, "Rec.lang_type": route,
            "Rec.rec_batch_num": 6,
            "EngineConfig.onnxruntime.intra_op_num_threads": min(8, os.cpu_count() or 1),
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
        engine = RapidOCR(params=params)
        metadata = {"model_family": "PP-OCRv6", "recognition_profile": "medium", "models": models,
            "language_route": route, "provider": "CPUExecutionProvider", "network": "disabled",
            "onnxruntime_version": manifest["onnxruntime_version"], "telemetry": "disabled",
            "text_policy": "unmodified engine lines joined with LF; no Unicode/script/space conversion",
            "reading_order": "RapidOCR detected-line order; vertical document order is experimental",
            "bbox_granularity": "line", "confidence_granularity": "line",
            "limitations": "Chinese profiles only; no Korean/Hanja mixed recognition; no variant normalization",
            "settings": {key: str(value) if hasattr(value, "value") else value
                         for key, value in params.items() if "path" not in key and "root_dir" not in key}}
        self.engines[route] = engine, metadata
        return engine, metadata

    def recognize(self, image_path, languages, direction="horizontal"):
        if direction not in ("horizontal", "vertical"):
            raise ValueError("Unsupported reading direction")
        if not image_path.is_file():
            raise ValueError("OCR input must be an existing local image")
        from PIL import Image
        with Image.open(image_path) as image:
            width, height = image.size
        started = time.perf_counter()
        engine, metadata = self.engine(languages)
        result = engine(str(image_path.resolve()))
        text, words = _shared.serialize_lines(result.boxes, result.txts, result.scores)
        metadata = dict(metadata, requested_languages=languages,
            stage_seconds=[None if value is None else float(value) for value in (result.elapse_list or [])],
            detection_resize_policy="RapidOCR 3.10 PP-OCRv6 medium defaults")
        if direction == "horizontal" and languages.startswith("chi_tra"):
            native = {"text": text, "words": words}
            words, evidence, sources = self.refine_horizontal(engine, image_path, result.boxes, words)
            text = "\n".join(word["text"] for word in words)
            metadata.update(recognition_policy=HORIZONTAL_RECOGNITION_POLICY,
                recognition_pipeline="PP-OCRv6 medium + verified original-resolution recrop",
                model_result=native, horizontal_preprocessing={"regions": evidence},
                glyph_font_sources=sources,
                text_policy="native model recrop chosen only on dual-input consensus and full-line glyph proof; no script conversion",
                punctuation_policy="Han substitutions may retain native punctuation only after two padded readings agree and complete-line image verification passes",
                confidence_granularity="null for recrop selections; all native and retry scores retained")
        return {"text": text, "words": words, "image_width": width, "image_height": height,
                "duration_seconds": time.perf_counter() - started,
                "engine_version": importlib.metadata.version("rapidocr"), "engine_metadata": metadata}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--languages", choices=SUPPORTED_LANGUAGES, required=True)
    parser.add_argument("--direction", choices=("horizontal", "vertical"), default="horizontal")
    args = parser.parse_args()
    _shared.block_network()
    try:
        _shared.atomic_output(args.output, Worker(args.model_dir).recognize(args.input, args.languages, args.direction))
        return 0
    except Exception as exc:
        print("PP-OCRv6 worker failed (" + type(exc).__name__ + "). Check local runtime and models.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
