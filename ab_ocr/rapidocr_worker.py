"""Offline subprocess bridge for the isolated RapidOCR/PP-OCRv5 runtime.

No script conversion, language fallback or model download. Chinese output may
include separately recorded local glyph verification; original neural lines
are retained. Composite verified regions have null confidence.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import socket
import sys
import tempfile
import time

SUPPORTED_LANGUAGES = ("chi_sim", "chi_tra", "chi_sim+eng", "chi_tra+eng", "kor", "kor+eng")
DETECTOR = "ch_PP-OCRv5_det_server.onnx"
CLASSIFIER = "ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx"
CHINESE = "ch_PP-OCRv5_rec_server.onnx"
CHINESE_MOBILE = "ch_PP-OCRv5_rec_mobile.onnx"
KOREAN = "korean_PP-OCRv5_rec_mobile.onnx"
RECOGNITION_POLICY = "ppocrv5-local-glyph-verified-v1"


def _helper_module(name):
    if name not in ("glyph_verifier", "reading_order"):
        raise ValueError("Unknown local helper")
    import importlib
    import importlib.util
    if __package__:
        return importlib.import_module("."+name, __package__)
    spec = importlib.util.spec_from_file_location("yeone_"+name, Path(__file__).with_name(name+".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def language_route(languages: str) -> str:
    if languages not in SUPPORTED_LANGUAGES:
        raise ValueError("Unsupported RapidOCR language combination; select chi_sim, chi_tra or kor (optionally +eng).")
    return "korean" if languages.startswith("kor") else "ch"


def block_network() -> None:
    def denied(*_args, **_kwargs):
        raise RuntimeError("Network access is disabled during OCR inference")
    socket.create_connection = denied
    socket.getaddrinfo = denied
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.socket.sendto = denied


def recognition_route(languages: str, profile: str) -> str:
    if profile not in ("server", "mobile"):
        raise ValueError("Unsupported recognition profile")
    route = language_route(languages)
    if profile == "server" and route == "korean":
        raise ValueError("PP-OCRv5 server recognition does not support Korean; no mobile fallback is applied")
    return route


def verify_models(model_dir: Path, languages: str, profile: str = "mobile") -> tuple[dict, list[dict]]:
    route = recognition_route(languages, profile)
    manifest = json.loads((model_dir.parent / "installation.json").read_text(encoding="utf-8"))
    if manifest.get("engine") not in ("rapidocr", "paddle_server") or manifest.get("rapidocr_version") != "3.9.2":
        raise ValueError("RapidOCR installation manifest is incompatible")
    if manifest.get("engine") == "paddle_server" and profile != "server":
        raise ValueError("Paddle server installation requires the server profile")
    wanted = [DETECTOR, CLASSIFIER, KOREAN if route == "korean" else (CHINESE if profile == "server" else CHINESE_MOBILE)]
    records = []
    for name in wanted:
        record = next((item for item in manifest["models"] if item.get("file") == name), None)
        if not record:
            raise ValueError("Required local model is not recorded")
        path = model_dir / name
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(block)
        digest = hasher.hexdigest()
        if digest != record["sha256"]:
            raise ValueError("Local model SHA-256 mismatch")
        records.append({"file": name, "sha256": digest})
    return manifest, records


def serialize_lines(boxes, texts, scores) -> tuple[str, list[dict]]:
    if texts is None:
        return "", []
    if boxes is None or scores is None or not (len(boxes) == len(texts) == len(scores)):
        raise ValueError("OCR line geometry is inconsistent")
    words = []
    for line, (box, text, score) in enumerate(zip(boxes, texts, scores), 1):
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        confidence = float(score) * 100
        if not all(math.isfinite(value) for value in xs + ys + [confidence]):
            raise ValueError("OCR coordinates or confidence are not finite")
        words.append({"text": str(text), "bbox": [min(xs), min(ys), max(xs), max(ys)],
                      "confidence": max(0.0, min(100.0, confidence)), "block": 1,
                      "paragraph": 1, "line": line})
    return "\n".join(str(text) for text in texts), words


def atomic_output(path: Path, result: dict) -> None:
    if path.exists():
        raise FileExistsError("OCR output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        # Windows rename is atomic and refuses an existing target. POSIX rename
        # would replace it, so use an atomic exclusive hard-link there instead.
        if os.name == "nt":
            os.rename(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def verify_providers(engine, provider: str) -> dict[str, list[str]]:
    expected = "CUDAExecutionProvider" if provider == "cuda" else "CPUExecutionProvider"
    sessions = {name: getattr(engine, name).session.session for name in ("text_det", "text_cls", "text_rec")}
    actual = {name: session.get_providers() for name, session in sessions.items()}
    if any(not providers or providers[0] != expected for providers in actual.values()):
        raise ValueError("Requested inference provider is unavailable; no automatic backend fallback is applied")
    return actual


def preload_nvrtc(manifest: dict) -> tuple[list, list]:
    """Load both pinned Windows NVRTC DLLs before constructing GPU sessions."""
    import ctypes
    nvrtc = importlib.metadata.distribution("nvidia-cuda-nvrtc-cu12")
    expected = manifest.get("gpu_packages", {}).get("nvidia-cuda-nvrtc-cu12")
    if not expected or nvrtc.version != expected:
        raise ValueError("Pinned NVIDIA NVRTC runtime is not installed")
    directory = Path(nvrtc.locate_file("nvidia/cuda_nvrtc/bin")).resolve()
    search = os.add_dll_directory(str(directory))
    try:
        dlls = [ctypes.WinDLL(str(directory / name)) for name in ("nvrtc-builtins64_128.dll", "nvrtc64_120_0.dll")]
        return dlls, [search]
    except Exception:
        search.close()
        raise


class Worker:
    def __init__(self, model_dir: Path, profile: str = "mobile", provider: str = "cpu"):
        self.model_dir = model_dir.resolve()
        if profile not in ("server", "mobile"):
            raise ValueError("Unsupported recognition profile")
        self.profile = profile
        if provider not in ("cpu", "cuda"):
            raise ValueError("Unsupported inference provider")
        if provider == "cuda" and profile != "server":
            raise ValueError("CUDA backend is enabled only for the explicit server profile")
        self.provider = provider
        self.cuda_dlls = []
        self.cuda_dll_directories = []
        self.engines = {}
        self.glyph_verifier = None

    def engine(self, languages: str):
        route = recognition_route(languages, self.profile)
        if route in self.engines:
            return self.engines[route]
        manifest, models = verify_models(self.model_dir, languages, self.profile)
        import onnxruntime
        onnxruntime.disable_telemetry_events()
        from rapidocr import RapidOCR, ModelType, OCRVersion, EngineType
        if importlib.metadata.version("rapidocr") != manifest["rapidocr_version"]:
            raise ValueError("RapidOCR package version differs from installation manifest")
        ort_package = manifest.get("onnxruntime_package", "onnxruntime")
        if ort_package not in ("onnxruntime", "onnxruntime-gpu"):
            raise ValueError("Unsupported ONNX Runtime distribution")
        if importlib.metadata.version(ort_package) != manifest["onnxruntime_version"]:
            raise ValueError("ONNX Runtime version differs from installation manifest")
        if self.provider == "cuda":
            if ort_package != "onnxruntime-gpu" or "CUDAExecutionProvider" not in onnxruntime.get_available_providers():
                raise ValueError("CUDA Runtime is not installed; no automatic CPU fallback is applied")
            onnxruntime.preload_dlls(directory="")
            # Some cuDNN kernels load NVRTC lazily for particular line shapes.
            # Resolve the pinned, installed NVIDIA wheel before any inference so
            # a missing runtime cannot fail halfway through a document batch.
            if os.name == "nt":
                self.cuda_dlls, self.cuda_dll_directories = preload_nvrtc(manifest)
        params = {
            "Global.log_level": "critical", "Global.text_score": 0.0,
            "Global.max_side_len": 4096, "Global.use_cls": True,
            "Global.model_root_dir": str(self.model_dir),
            "Det.model_path": str(self.model_dir / DETECTOR),
            "Det.engine_type": EngineType.ONNXRUNTIME, "Det.ocr_version": OCRVersion.PPOCRV5,
            "Det.model_type": ModelType.SERVER, "Det.lang_type": "ch",
            "Det.limit_side_len": 960, "Det.limit_type": "max",
            "Det.mean": [0.485, 0.456, 0.406], "Det.std": [0.229, 0.224, 0.225],
            "Det.thresh": 0.3, "Det.box_thresh": 0.5, "Det.unclip_ratio": 1.6,
            "Cls.model_path": str(self.model_dir / CLASSIFIER),
            "Cls.engine_type": EngineType.ONNXRUNTIME, "Cls.ocr_version": OCRVersion.PPOCRV5,
            "Cls.model_type": ModelType.MOBILE,
            "Rec.model_path": str(self.model_dir / (KOREAN if route == "korean" else (CHINESE if self.profile == "server" else CHINESE_MOBILE))),
            "Rec.engine_type": EngineType.ONNXRUNTIME, "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_type": ModelType.MOBILE if route == "korean" or self.profile == "mobile" else ModelType.SERVER,
            "Rec.lang_type": route, "Rec.rec_batch_num": 6,
            "EngineConfig.onnxruntime.intra_op_num_threads": min(8, os.cpu_count() or 1),
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
        if self.provider == "cuda":
            params.update({"EngineConfig.onnxruntime.use_cuda": True,
                           "EngineConfig.onnxruntime.cuda_ep_cfg.device_id": 0,
                           "EngineConfig.onnxruntime.cuda_ep_cfg.use_tf32": 0,
                           "EngineConfig.onnxruntime.cuda_ep_cfg.cudnn_conv_algo_search": "HEURISTIC",
                           "EngineConfig.onnxruntime.cuda_ep_cfg.gpu_mem_limit": 3 * 1024 ** 3})
        engine = RapidOCR(params=params)
        providers = verify_providers(engine, self.provider)
        metadata = {"model_family": "PP-OCRv5", "recognition_profile": self.profile, "language_route": route, "models": models,
                    "model_name": "PP-OCRv5_server_rec" if self.profile == "server" else ("korean_PP-OCRv5_mobile_rec" if route == "korean" else "PP-OCRv5_mobile_rec"),
                    "runtime_adapter": "RapidOCR ONNX Runtime; PaddleOCR-derived official model conversion",
                    "onnxruntime_version": importlib.metadata.version(ort_package), "onnxruntime_package": ort_package,
                    "provider": "CUDAExecutionProvider" if self.provider == "cuda" else "CPUExecutionProvider",
                    "verified_session_providers": providers, "network": "disabled", "telemetry": "disabled",
                    "text_policy": "unmodified engine lines joined with LF; no Unicode/script/space conversion",
                    "reading_order": "RapidOCR detected-line order; no document layout reconstruction",
                    "bbox_granularity": "line", "confidence_granularity": "line",
                    "recognition_score_filter": 0.0, "detection_box_threshold": 0.5,
                    "settings": {key: str(value) if hasattr(value, "value") else value
                                 for key, value in params.items() if "path" not in key and "root_dir" not in key},
                    "limitations": "Separate Korean and Chinese recognizers; no mixed Korean/Hanja route; no variant normalization"}
        self.engines[route] = (engine, metadata)
        return engine, metadata

    def recognize(self, image_path: Path, languages: str, direction: str = "horizontal") -> dict:
        if direction not in ("horizontal", "vertical"):
            raise ValueError("Unsupported reading direction")
        # Enforce local files before the library can interpret URLs as images.
        if not image_path.is_file():
            raise ValueError("OCR input must be an existing local image")
        from PIL import Image
        with Image.open(image_path) as image:
            width, height = image.size
        started = time.perf_counter()
        engine, metadata = self.engine(languages)
        result = engine(str(image_path.resolve()))
        text, words = serialize_lines(result.boxes, result.txts, result.scores)
        metadata = dict(metadata)
        if language_route(languages) == "ch":
            import numpy as np
            module = _helper_module("glyph_verifier")
            if self.glyph_verifier is None:
                self.glyph_verifier = module.GlyphVerifier(module.font_sources())
            with Image.open(image_path) as image:
                gray = np.asarray(image.convert("L"))
            original = {"text": text, "words": words}
            if direction == "horizontal":
                words, evidence = module.refine_horizontal(self.glyph_verifier, gray, words)
                stage = "horizontal_preprocessing"
            else:
                columns = _helper_module("reading_order").vertical_column_indices(words)
                words, evidence = module.refine_vertical_columns(self.glyph_verifier, gray, words, columns)
                stage = "vertical_preprocessing"
            words = [dict(word, line=line) for line, word in enumerate(words, 1)]
            text = "\n".join(w["text"] for w in words)
            metadata.update({"recognition_policy": RECOGNITION_POLICY,
                "recognition_pipeline": "PP-OCRv5 "+self.profile+" + local printed-glyph verification",
                "model_result": original, stage: {"regions": evidence},
                "glyph_font_sources": self.glyph_verifier.sources,
                "glyph_verification_policy": module.POLICY,
                "glyph_verification_status": "enabled" if self.glyph_verifier.sources else "disabled: no installed font sources",
                "glyph_verification_thresholds": {"min_similarity": module.MIN_SIMILARITY, "min_margin": module.MIN_MARGIN},
                "text_policy": "verified glyph output; unmodified neural lines retained in model_result; no script conversion",
                "confidence_granularity": "null for verified composites; native line scores retained in model_result"})
        metadata["stage_seconds"] = [float(value) for value in (result.elapse_list or [])]
        metadata["detection_resize_policy"] = "RapidOCR max-side policy: 960 below 960px, 1500 below 1500px, otherwise 2000px; dimensions rounded to multiples of 32"
        return {"text": text, "words": words, "image_width": width, "image_height": height,
                "duration_seconds": time.perf_counter() - started,
                "engine_version": importlib.metadata.version("rapidocr"),
                "engine_metadata": dict(metadata, requested_languages=languages)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--languages", default="chi_sim")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--profile", choices=("server", "mobile"), default="mobile")
    parser.add_argument("--provider", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--direction", choices=("horizontal", "vertical"), default="horizontal")
    parser.add_argument("--session-root", type=Path)
    parser.add_argument("--max-requests", type=int, default=8)
    args = parser.parse_args()
    block_network()
    try:
        worker = Worker(args.model_dir, args.profile, args.provider)
        if args.session_root:
            import importlib.util
            spec = importlib.util.spec_from_file_location("yeone_worker_protocol", Path(__file__).with_name("worker_sessions.py"))
            protocol = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(protocol)
            return protocol.serve_requests(worker, args.session_root, args.languages, atomic_output,
                                           max_requests=args.max_requests)
        if args.batch:
            items = json.loads(args.batch.read_text(encoding="utf-8"))["items"]
        elif args.input and args.output:
            items = [{"input": str(args.input), "output": str(args.output), "languages": args.languages}]
        else:
            parser.error("Use --input and --output, or --batch")
        for index, item in enumerate(items, 1):
            destination = Path(item["output"])
            if destination.exists():
                raise FileExistsError("OCR output already exists")
            result = worker.recognize(Path(item["input"]), item.get("languages", args.languages),
                                      item.get("direction", args.direction))
            atomic_output(destination, result)
            # Progress intentionally contains no document text or source path.
            print(json.dumps({"completed": index, "total": len(items), "seconds": round(result["duration_seconds"], 3)}), flush=True)
        return 0
    except Exception as exc:
        # Third-party exceptions may include private inputs; keep stderr generic.
        print("RapidOCR worker failed (" + type(exc).__name__ + "). Check local model installation, language and image.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
