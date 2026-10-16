"""Isolated, CPU-only EasyOCR worker. Never downloads models or sends documents.

Single request: --input IMAGE --output JSON --languages chi_sim --model-dir DIR
Batch request: --batch REQUEST.json --model-dir DIR
REQUEST is a list (or {"items": [...]}) of input/output/languages dictionaries.
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


def language_config(language: str) -> tuple[str, str]:
    try:
        return LANGUAGES[language]
    except KeyError:
        raise ValueError("EasyOCR requires chi_sim, chi_tra, or kor separately; "
                         "a combined Korean/Chinese model is not available.") from None


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
        score = float(confidence) * 100
        if not math.isfinite(score):
            score = None
        elif not 0 <= score <= 100:
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

    def reader(self, language: str):
        if language not in self.readers:
            easy_language, _ = language_config(language)
            self.models[language] = verify_models(self.model_dir, language)
            os.environ["EASYOCR_MODULE_PATH"] = str(self.model_dir.parent)
            import torch
            import easyocr
            torch.set_num_threads(self.threads)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.readers[language] = easyocr.Reader(
                    [easy_language], gpu=False, model_storage_directory=str(self.model_dir),
                    user_network_directory=str(self.model_dir.parent / "user_network"),
                    download_enabled=False, verbose=False, quantize=True)
        return self.readers[language]

    def recognize(self, input_path: Path, language: str) -> dict:
        # Accept existing local files only, so EasyOCR's URL-input feature cannot run.
        input_path = input_path.resolve(strict=True)
        if not input_path.is_file():
            raise ValueError("Input must be a local image file")
        reader = self.reader(language)
        from PIL import Image
        with Image.open(input_path) as image:
            image_width, image_height = image.size
        started = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            results = reader.readtext(str(input_path), detail=1, paragraph=False,
                                      decoder="greedy", batch_size=1, workers=0,
                                      canvas_size=2560, mag_ratio=1.0)
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
                                    "word_unit": "detected text region, often a full line",
                                    "coordinate_policy": "axis-aligned polygon envelope clipped to image; original_bbox retained if changed",
                                    "coordinate_warnings": sum("original_bbox" in word for word in words),
                                    "download_enabled": False, "quantize": True,
                                    "models": self.models[language],
                                    "recognition_seconds": duration_seconds,
                                    "text_join_policy": "engine order; newline between detected boxes",
                                    "unicode_normalization": "none"}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--languages", choices=list(LANGUAGES))
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args(argv)
    if args.threads < 1:
        parser.error("--threads must be positive")
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
        result = worker.recognize(source, job["languages"])
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
