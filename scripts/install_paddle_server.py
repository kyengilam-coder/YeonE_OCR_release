"""Explicitly install local PP-OCRv5 server models in an independent CPU runtime.

The ONNX conversions and exact dependency pins come from RapidOCR 3.9.2. This
installer never reads documents. Inference uses rapidocr_worker.py --profile
server with networking disabled. The existing mobile installation is untouched.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil

try:
    from . import install_rapidocr as runtime_installer
except ImportError:
    import install_rapidocr as runtime_installer


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--root", type=Path, help="Engine home containing tools/paddle_server")
    location.add_argument("--target", type=Path, help="Direct independent installation folder")
    parser.add_argument("--models-only", action="store_true")
    parser.add_argument("--model-cache", type=Path, help="Optional existing public RapidOCR models; verified before copying")
    args = parser.parse_args(argv)
    target = (args.target if args.target else
              (args.root or Path(__file__).resolve().parents[1]) / "tools" / "paddle_server").expanduser().resolve()
    if target.name.lower() in ("rapidocr", "easyocr", "tesseract"):
        raise ValueError("Use a separate paddle_server directory; existing engine installations are preserved")
    lock = json.loads(Path(__file__).with_name("paddle-server-lock.json").read_text(encoding="utf-8"))
    # Cache copying is limited to known public filenames and exact pinned bytes.
    # No original files, user settings, credentials, or OCR records are copied.
    if args.model_cache:
        cache = args.model_cache.expanduser().resolve()
        destination = target / "models"
        destination.mkdir(parents=True, exist_ok=True)
        for relative, expected in runtime_installer.MODELS:
            source = cache / Path(relative).name
            output = destination / source.name
            if source.is_file() and runtime_installer.sha256(source) == expected:
                if not output.exists() or runtime_installer.sha256(output) != expected:
                    temporary = output.with_suffix(".copy")
                    try:
                        shutil.copyfile(source, temporary)
                        if runtime_installer.sha256(temporary) != expected:
                            raise RuntimeError("Public cached model changed during copy")
                        temporary.replace(output)
                    finally:
                        temporary.unlink(missing_ok=True)
    install_args = ["--target", str(target)]
    if args.models_only:
        install_args.append("--models-only")
    runtime_installer.main(install_args)
    manifest_path = target / "installation.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = lock["models"]
    manifest.update({
        "engine": "paddle_server", "default_profile": "server",
        "execution_provider": "cpu", "python_executable": "venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python",
        "models": [item for item in manifest["models"] if item["file"] in required],
        "languages": lock["languages"],
        "language_models": {language: list(required) for language in lock["languages"]},
        "supported_scripts": lock["supported_scripts"],
        "unsupported_scripts": lock["unsupported_scripts"],
        "text_policy": lock["text_policy"],
        "model_provenance": lock["model_provenance"],
        "upstream_model": lock["upstream_model"],
        "notes": ["PP-OCRv5 server-size models run locally on the CPU, not an external OCR service.",
                  "Simplified/Traditional Chinese share one recognition model; output is not converted.",
                  "Hangul and Korean/Hanja mixed recognition are unsupported; no mobile fallback is applied."],
    })
    runtime_installer.atomic_json(manifest_path, manifest)
    print("PaddleOCR server-size local CPU installation verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
