"""Install the pinned Windows OCR runtime into this project only.

Downloads official artifacts, verifies SHA-256, and extracts the runtime with
an existing Bandizip or 7-Zip executable. Does not execute the installer, change
PATH/registry, request administrator privileges, or send any user documents.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def matches(path: Path, expected: str) -> bool:
    return path.is_file() and sha256(path) == expected


def download(url: str, destination: Path, expected: str) -> None:
    if matches(destination, expected):
        return
    if not url.startswith("https://"):
        raise ValueError("Only pinned HTTPS downloads are allowed.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".download")
    print(f"Downloading {destination.name}", flush=True)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "AB_OCR-local-runtime-installer/1"})
        with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as stream:
            if not response.url.startswith("https://"):
                raise ValueError("Refusing a non-HTTPS redirect.")
            shutil.copyfileobj(response, stream, 1024 * 1024)
        if sha256(partial) != expected:
            raise ValueError(f"SHA-256 mismatch: {destination.name}")
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def find_extractor(explicit: str | None) -> Path:
    if explicit:
        result = Path(explicit).expanduser().resolve()
        if result.is_file():
            return result
        raise ValueError("The selected archive extractor does not exist.")
    paths = []
    for name in ("bz.exe", "7z.exe", "7za.exe"):
        if found := shutil.which(name):
            paths.append(Path(found))
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        if base := os.environ.get(variable):
            paths.extend((Path(base) / "Bandizip" / "bz.exe", Path(base) / "7-Zip" / "7z.exe"))
    for path in paths:
        if path.is_file():
            return path.resolve()
    raise ValueError("Bandizip or 7-Zip is required to extract the official NSIS package. Use --extractor PATH.")


def run(args: list[str]) -> str:
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    result = subprocess.run(args, capture_output=True, stdin=subprocess.DEVNULL,
                            timeout=120, shell=False, **options)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): " + result.stderr.decode("utf-8", errors="replace")[-1000:])
    return result.stdout.decode("utf-8", errors="replace")


def verify(target: Path, lock: dict) -> dict:
    engine = target / "tesseract.exe"
    if not matches(engine, lock["engine_sha256"]):
        raise ValueError("Tesseract executable is missing or does not match the pinned hash.")
    for config in ("txt", "tsv"):
        if not (target / "tessdata" / "configs" / config).is_file():
            raise ValueError(f"Missing output configuration: {config}")
    for model in lock["models"]:
        path = target / "tessdata" / (model["language"] + ".traineddata")
        if not matches(path, model["sha256"]):
            raise ValueError(f"Model missing or SHA-256 mismatch: {model['language']}")
    version = run([str(engine), "--version"]).splitlines()[0]
    languages = run([str(engine), "--list-langs", "--tessdata-dir", str(target / "tessdata")])
    for model in lock["models"]:
        if model["language"] not in languages.splitlines():
            raise ValueError(f"Engine cannot find installed model: {model['language']}")
    return {"version": version, "languages": [model["language"] for model in lock["models"]]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install/verify pinned Tesseract in AB_OCR/tools/tesseract")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--extractor", help="Existing Bandizip bz.exe or 7-Zip 7z.exe")
    parser.add_argument("--verify-only", action="store_true", help="Verify installation without downloads or writes")
    args = parser.parse_args(argv)
    root = args.project_root.expanduser().resolve()
    lock_path = Path(__file__).with_name("tesseract-lock.json")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    target = root / "tools" / "tesseract"
    if os.name != "nt":
        raise ValueError("This pinned binary package is for Windows x64.")
    if not (root / "ab_ocr" / "__init__.py").is_file():
        raise ValueError("--project-root must be an AB_OCR project directory.")
    if target.resolve() != target:
        raise ValueError("The dedicated installation directory cannot be a symlink/junction.")
    if args.verify_only:
        print(json.dumps(verify(target, lock), ensure_ascii=False, indent=2))
        return 0
    if target.is_dir() and any(target.iterdir()) and not matches(target / "tesseract.exe", lock["engine_sha256"]):
        raise ValueError("The target contains a different installation or unrelated files; it will not be overwritten.")
    archive = root / "work" / "downloads" / lock["distribution_filename"]
    download(lock["distribution_url"], archive, lock["distribution_sha256"])
    needed = [target / "tesseract.exe", target / "tessdata" / "configs" / "txt",
              target / "tessdata" / "configs" / "tsv", target / "doc" / "LICENSE"]
    if not all(path.is_file() for path in needed):
        extractor = find_extractor(args.extractor)
        target.mkdir(parents=True, exist_ok=True)
        patterns = ["tesseract.exe", "*.dll", "tessdata\\*", "tessdata\\configs\\*",
                    "tessdata\\tessconfigs\\*", "doc\\*", "doc\\licenses\\*", "LICENSE*", "COPYING*"]
        output_option = "-o:" + str(target) if extractor.name.lower() == "bz.exe" else "-o" + str(target)
        run([str(extractor), "x", "-y", output_option, str(archive), *patterns])
    for model in lock["models"]:
        download(model["url"], target / "tessdata" / (model["language"] + ".traineddata"), model["sha256"])
    license_info = lock["models_license"]
    download(license_info["url"], target / "tessdata" / "LICENSE.tessdata_best", license_info["sha256"])
    installed = verify(target, lock)
    provenance = {"schema_version": 1, "installed_at_utc": datetime.now(timezone.utc).isoformat(),
                  "installation_method": "extract verified official NSIS payload; no installer execution",
                  "distribution_url": lock["distribution_url"], "distribution_sha256": lock["distribution_sha256"],
                  "engine_sha256": lock["engine_sha256"], "models_commit": lock["models_commit"],
                  "models_family": "tessdata_best", "models": lock["models"],
                  "lock_sha256": sha256(lock_path), **installed}
    pending = target / "installation.json.tmp"
    pending.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(target / "installation.json")
    print(json.dumps(installed, ensure_ascii=False, indent=2))
    print("Verified project-local installation: " + str(target))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
