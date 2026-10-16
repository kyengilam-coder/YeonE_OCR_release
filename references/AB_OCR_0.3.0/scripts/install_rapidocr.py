"""Install the isolated, versioned RapidOCR CPU runtime and PP-OCRv5 models.

Network access is needed only by this explicit installer. No document is read.
Model digests are from RapidOCR 3.9.2's official default_models.yaml.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request
import venv

VERSION = "3.9.2"
ORT_VERSION = "1.25.1"
BASE = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv5/"
MODELS = [
    ("det/ch_PP-OCRv5_det_server.onnx", "0f8846b1d4bba223a2a2f9d9b44022fbc22cc019051a602b41a7fda9667e4cad"),
    ("rec/ch_PP-OCRv5_rec_server.onnx", "e09385400eaaaef34ceff54aeb7c4f0f1fe014c27fa8b9905d4709b65746562a"),
    ("rec/korean_PP-OCRv5_rec_mobile.onnx", "cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b"),
    ("cls/ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx", "54379ae5174d026780215fc748a7f31910dee36818e63d49e17dc598ecc82df7"),
    ("rec/ch_PP-OCRv5_rec_mobile.onnx", "5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5"),
]


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=Path(__file__).resolve().parents[1] / "tools" / "rapidocr")
    parser.add_argument("--models-only", action="store_true", help="Use the already installed isolated runtime")
    args = parser.parse_args()
    target = args.target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    runtime = target / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not args.models_only:
        if not runtime.exists():
            venv.EnvBuilder(with_pip=True).create(target / "venv")
        next_report = target / "pip-install-report.next.json"
        subprocess.run([str(runtime), "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir",
                        "--report", str(next_report),
                        f"rapidocr=={VERSION}", f"onnxruntime=={ORT_VERSION}"], check=True)
        # pip reports only packages installed during this invocation. Retain the
        # original wheel URLs/hashes when a repeat install has nothing to do.
        report_path = target / "pip-install-report.json"
        previous = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {"install": []}
        current = json.loads(next_report.read_text(encoding="utf-8"))
        by_name = {item["metadata"]["name"].lower().replace("_", "-"): item for item in previous["install"]}
        by_name.update({item["metadata"]["name"].lower().replace("_", "-"): item for item in current["install"]})
        current["install"] = list(by_name.values())
        atomic_json(report_path, current)
        next_report.unlink(missing_ok=True)
    if not runtime.exists():
        raise RuntimeError("Isolated runtime is missing; run without --models-only")
    models_dir = target / "models"
    models_dir.mkdir(exist_ok=True)
    records = []
    for relative, expected in MODELS:
        name = Path(relative).name
        output = models_dir / name
        url = BASE + relative
        if not output.exists() or sha256(output) != expected:
            temporary = output.with_suffix(".download")
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "AB_OCR-local-engine-installer/1"})
                with urllib.request.urlopen(request, timeout=180) as source, temporary.open("wb") as dest:
                    while block := source.read(1024 * 1024):
                        dest.write(block)
                    dest.flush()
                    os.fsync(dest.fileno())
                if sha256(temporary) != expected:
                    raise RuntimeError("Official model SHA-256 mismatch: " + name)
                os.replace(temporary, output)
            finally:
                temporary.unlink(missing_ok=True)
        records.append({"file": name, "sha256": expected, "url": url, "bytes": output.stat().st_size})
        print("verified " + name, flush=True)
    common = [Path(MODELS[0][0]).name, Path(MODELS[3][0]).name]
    language_models = {}
    for language in ("chi_sim", "chi_tra", "chi_sim+eng", "chi_tra+eng", "kor", "kor+eng"):
        rec = MODELS[2 if language.startswith("kor") else 4][0]
        language_models[language] = common + [Path(rec).name]
    installed = json.loads(subprocess.check_output([str(runtime), "-m", "pip", "list", "--format=json"], text=True))
    python_version = subprocess.check_output([str(runtime), "-c", "import platform; print(platform.python_version())"], text=True).strip()
    atomic_json(target / "installation.json", {
        "schema_version": 1, "engine": "rapidocr", "rapidocr_version": VERSION,
        "onnxruntime_version": ORT_VERSION, "runtime_python": str(runtime), "python_version": python_version,
        "model_family": "PP-OCRv5", "default_profile": "mobile", "models": records, "packages": installed,
        "languages": list(language_models), "language_models": language_models,
        "package_source": "https://pypi.org/", "package_provenance": "pip-install-report.json",
        "model_source": "https://github.com/RapidAI/RapidOCR/blob/v3.9.2/python/rapidocr/default_models.yaml",
        "inference_network": "disabled by worker; explicit installer only",
        "notes": ["Chinese model is shared by chi_sim and chi_tra; no script conversion is applied.",
                  "Korean and Chinese recognizers are separate; Korean/Hanja mixed recognition is not advertised."]
    })
    print("RapidOCR isolated installation verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
