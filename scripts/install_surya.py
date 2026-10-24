"""Install the official Surya OCR 2 CPU backend into an isolated local folder.

Only installation downloads public software and weights. OCR workers use local
files and a private loopback-only llama.cpp server. No API keys are needed.
The embedded CPython 3.12 avoids changing the app's Python 3.14 environment and
satisfies Surya 0.22.1's Pillow<11 dependency using supported binary wheels.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCK = Path(__file__).with_name("surya-lock.json")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path, expected: str | None = None) -> dict:
    if destination.exists():
        digest = sha256(destination)
        if expected is not None and digest != expected:
            raise ValueError("Existing public download has a different SHA-256")
        return {"file": destination.name, "url": url, "sha256": digest,
                "size": destination.stat().st_size}
    temporary = destination.with_name(destination.name + ".download")
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "YeonE-OCR-local-installer"})
    with urllib.request.urlopen(request, timeout=120) as source, temporary.open("wb") as target:
        while chunk := source.read(4 * 1024 * 1024):
            target.write(chunk)
    digest = sha256(temporary)
    if expected is not None and digest != expected:
        raise ValueError("Public download SHA-256 mismatch")
    os.replace(temporary, destination)
    return {"file": destination.name, "url": url, "sha256": digest,
            "size": destination.stat().st_size}


def extract(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            destination = (target / member.filename).resolve()
            if not destination.is_relative_to(target.resolve()):
                raise ValueError("Archive path escaped installation directory")
        source.extractall(target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=ROOT / "tools" / "surya")
    args = parser.parse_args()
    target = args.target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    records = []
    python_archive = target / "python-3.12.10-embed-amd64.zip"
    records.append(download(lock["python_url"], python_archive, lock["python_sha256"]))
    python_root = target / "python"
    if not (python_root / "python.exe").exists():
        extract(python_archive, python_root)
    # Enable site-packages only within this embedded installation.
    path_file = python_root / "python312._pth"
    path_file.write_text("python312.zip\n.\nLib/site-packages\nimport site\n", encoding="utf-8")
    python = python_root / "python.exe"
    get_pip = target / "get-pip.py"
    records.append(download("https://bootstrap.pypa.io/get-pip.py", get_pip, lock["get_pip_sha256"]))
    environment = dict(os.environ, PYTHONUTF8="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    environment["TEMP"] = environment["TMP"] = str(target / "temp")
    (target / "temp").mkdir(exist_ok=True)
    subprocess.run([str(python), str(get_pip), "--no-warn-script-location"], check=True, env=environment)
    report_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    subprocess.run([str(python), "-m", "pip", "install", "--only-binary=:all:",
                    "--index-url", "https://download.pytorch.org/whl/cpu", "torch==2.10.0+cpu",
                    "torchvision==0.25.0+cpu", "--report", str(target / ("torch-install-report-" + report_stamp + ".json"))],
                   check=True, env=environment)
    subprocess.run([str(python), "-m", "pip", "install", "--only-binary=:all:",
                    "-r", str(Path(__file__).with_name("surya-requirements.txt")),
                    "--report", str(target / ("surya-install-report-" + report_stamp + ".json"))], check=True, env=environment)
    with urllib.request.urlopen(urllib.request.Request(
        "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/" + lock["llama_cpp_release"],
        headers={"User-Agent": "YeonE-OCR-local-installer"}), timeout=60) as source:
        release = json.load(source)
    asset = next(item for item in release["assets"] if item["name"] == lock["llama_cpp_asset"])
    expected_asset = lock["llama_cpp_asset_sha256"]
    if asset.get("digest") != "sha256:" + expected_asset:
        raise ValueError("Official llama.cpp release digest differs from lock")
    archive = target / lock["llama_cpp_asset"]
    records.append(download(asset["browser_download_url"], archive, expected_asset))
    extract(archive, target / "llama.cpp")
    models = []
    for item in lock["models"]:
        url = ("https://huggingface.co/" + lock["model_repository"] + "/resolve/"
               + lock["model_revision"] + "/" + item["file"])
        record = download(url, target / "models" / item["file"], item["sha256"])
        if record["size"] != item["size"]:
            raise ValueError("Model size differs from the frozen official record")
        models.append(record)
    license_url = ("https://huggingface.co/" + lock["model_repository"] + "/resolve/"
                   + lock["model_revision"] + "/LICENSE")
    records.append(download(license_url, target / "models" / "LICENSE"))
    package_output = subprocess.check_output([str(python), "-m", "pip", "list", "--format=json"],
                                             env=environment, text=True, encoding="utf-8")
    manifest = {"engine": "surya", "engine_version": lock["surya_version"],
                "model_name": lock["model_name"], "model_repository": lock["model_repository"],
                "model_revision": lock["model_revision"], "models": models,
                "python_version": "3.12.10", "backend": lock["backend"],
                "python_executable": "python/python.exe", "runtime_root": ".",
                "language_models": {language: ["surya-2.gguf", "surya-2-mmproj.gguf"]
                                    for language in ("chi_sim", "chi_tra", "kor", "eng",
                                                     "chi_sim+eng", "chi_tra+eng", "kor+eng",
                                                     "kor+chi_tra", "kor+chi_tra+eng")},
                "llama_cpp_release": lock["llama_cpp_release"], "public_downloads": records,
                "packages": json.loads(package_output), "network_at_inference": "loopback only",
                "model_layout": "relative-paths-v1",
                "runtime_files": [{"file": path.relative_to(target).as_posix(),
                                   "sha256": sha256(path), "size": path.stat().st_size}
                                  for path in sorted((target / "llama.cpp").rglob("*"))
                                  if path.is_file()],
                "package_files": [{"file": path.relative_to(target).as_posix(),
                                   "sha256": sha256(path), "size": path.stat().st_size}
                                  for path in sorted((python_root / "Lib/site-packages/surya").rglob("*.py"))],
                "model_license": "Modified AI Pubs Open Rail-M; see official model LICENSE",
                "source": "https://github.com/datalab-to/surya"}
    temporary = target / "installation.json.tmp"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target / "installation.json")
    print("Surya OCR 2 CPU installation complete.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
