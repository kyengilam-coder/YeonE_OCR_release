"""Explicitly install isolated PP-OCRv6 medium (CPU); never reads documents.

The existing RapidOCR/v5 environment is not modified. Only this installer may
download packages and weights; inference validates hashes and blocks networking.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import venv

if __package__:
    from .install_rapidocr import atomic_json, sha256
else:
    from install_rapidocr import atomic_json, sha256


def download_verified(record, output):
    if output.exists() and sha256(output) == record["sha256"]:
        return
    temporary = output.with_suffix(".download")
    try:
        if not record["url"].startswith("https://"):
            raise ValueError("Only pinned HTTPS downloads are allowed")
        request = urllib.request.Request(record["url"], headers={"User-Agent": "YeonE-OCR-installer/1"})
        with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as stream:
            if not response.url.startswith("https://"):
                raise ValueError("Refusing a non-HTTPS redirect")
            while block := response.read(1024 * 1024):
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        if sha256(temporary) != record["sha256"]:
            raise RuntimeError("Official artifact SHA-256 mismatch: " + record["file"])
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Engine home containing tools/")
    parser.add_argument("--models-only", action="store_true")
    args = parser.parse_args(argv)
    lock = json.loads(Path(__file__).with_name("ppocrv6-lock.json").read_text(encoding="utf-8"))
    target = args.root.expanduser().resolve() / "tools" / "ppocrv6"
    target.mkdir(parents=True, exist_ok=True)
    runtime = target / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not args.models_only:
        if not runtime.exists():
            venv.EnvBuilder(with_pip=True).create(target / "venv")
        report = target / "pip-install-report.next.json"
        pins = lock.get("packages", {})
        constraints = target / "dependency-pins.txt"
        constraints.write_text("".join(f"{key}=={value}\n" for key, value in pins.items()), encoding="utf-8")
        subprocess.run([str(runtime), "-m", "pip", "install", "--disable-pip-version-check",
                        "--constraint", str(constraints), "--report", str(report),
                        f"rapidocr=={lock['rapidocr_version']}",
                        f"onnxruntime=={lock['onnxruntime_version']}"], check=True)
        destination = target / "pip-install-report.json"
        previous = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else {"install": []}
        current = json.loads(report.read_text(encoding="utf-8"))
        records = {r["metadata"]["name"].lower().replace("_", "-"): r for r in previous["install"]}
        records.update({r["metadata"]["name"].lower().replace("_", "-"): r for r in current["install"]})
        current["install"] = list(records.values())
        atomic_json(destination, current)
        report.unlink()
    if not runtime.is_file():
        raise RuntimeError("Isolated runtime missing; run without --models-only")
    models = target / "models"
    models.mkdir(exist_ok=True)
    records = []
    for record in lock["models"]:
        output = models / record["file"]
        download_verified(record, output)
        records.append(dict(record, bytes=output.stat().st_size))
        print("verified " + record["file"], flush=True)
    download_verified(lock["license_file"], target / lock["license_file"]["file"])
    versions = json.loads(subprocess.check_output([str(runtime), "-c",
        "import json,importlib.metadata as m; print(json.dumps({p:m.version(p) for p in ['rapidocr','onnxruntime']}))"], text=True))
    if versions != {"rapidocr": lock["rapidocr_version"], "onnxruntime": lock["onnxruntime_version"]}:
        raise RuntimeError("Installed runtime versions differ from lock")
    installed = json.loads(subprocess.check_output([str(runtime), "-m", "pip", "list", "--format=json"], text=True))
    languages = ("chi_sim", "chi_tra", "chi_sim+eng", "chi_tra+eng")
    atomic_json(target / "installation.json", {
        **{key: lock[key] for key in ("schema_version", "engine", "rapidocr_version", "onnxruntime_version",
                                    "model_family", "model_profile", "license", "source")},
        "models": records, "packages": installed,
        "language_models": {language: [r["file"] for r in records] for language in languages},
        "inference_network": "disabled", "package_provenance": "pip-install-report.json",
        "notes": ["Simplified and Traditional Chinese share the medium recognizer; no script conversion.",
                  "Korean/Hanja mixed recognition is unsupported; vertical layout is experimental."]})
    print("PP-OCRv6 medium isolated installation verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
