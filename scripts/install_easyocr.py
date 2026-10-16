"""Install CPU EasyOCR in an isolated, project-local environment.

Only this installer downloads software/models. The inference worker never downloads.
Official model MD5s are checked; SHA-256 and wheel URLs/hashes are recorded locally.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request
import venv
import zipfile


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tools" / "easyocr"
EASYOCR_VERSION = "1.7.2"
TORCH_VERSION = "2.10.0+cpu"
TORCHVISION_VERSION = "0.25.0+cpu"
LOCK_PATH = Path(__file__).with_name("easyocr-lock.json")


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     delete=False, suffix=".tmp") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def prepare_models(target: Path) -> None:
    from easyocr.config import detection_models, recognition_models

    model_dir = target / "models"
    model_dir.mkdir(parents=True, exist_ok=True)
    records = []
    locked = {item["filename"]: item for item in json.loads(
        LOCK_PATH.read_text(encoding="utf-8"))["models"]}
    configs = [detection_models["craft"],
               recognition_models["gen2"]["zh_sim_g2"],
               recognition_models["gen1"]["zh_tra_g1"],
               recognition_models["gen2"]["korean_g2"]]
    for config in configs:
        filename, expected = config["filename"], config["md5sum"]
        if (locked[filename]["md5"] != expected
                or locked[filename]["source_url"] != config["url"]):
            raise RuntimeError("EasyOCR model configuration does not match the lock")
        destination = model_dir / filename
        if destination.exists():
            content = destination.read_bytes()
            if (hashlib.md5(content).hexdigest() != expected
                    or hashlib.sha256(content).hexdigest() != locked[filename]["sha256"]):
                raise RuntimeError(f"Existing model checksum mismatch: {filename}")
        else:
            print(f"Downloading official EasyOCR model: {filename}", flush=True)
            archive_path = target / (filename + ".download.zip")
            urllib.request.urlretrieve(config["url"], archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                names = [name for name in archive.namelist()
                         if Path(name).name == filename and not name.endswith("/")]
                if len(names) != 1:
                    raise RuntimeError("Unexpected model archive")
                content = archive.read(names[0])
            if (hashlib.md5(content).hexdigest() != expected
                    or hashlib.sha256(content).hexdigest() != locked[filename]["sha256"]):
                raise RuntimeError(f"Official model checksum mismatch: {filename}")
            temporary = destination.with_suffix(".tmp")
            temporary.write_bytes(content)
            os.replace(temporary, destination)
            archive_path.unlink()
        records.append({"filename": filename, "sha256": hashlib.sha256(content).hexdigest(),
                        "md5": expected, "source_url": config["url"]})
    packages = {dist.metadata["Name"]: dist.version
                for dist in importlib.metadata.distributions()}
    atomic_json(target / "installation.json", {
        "engine": "easyocr", "engine_version": EASYOCR_VERSION,
        "python_version": sys.version.split()[0], "device": "cpu",
        "packages": packages, "models": records,
        "package_reports": ["torch-install-report.json", "easyocr-install-report.json"],
        "source": "https://github.com/JaidedAI/EasyOCR/tree/v1.7.2",
        "languages": ["chi_sim", "chi_tra", "kor"],
        "language_models": {"chi_sim": ["craft_mlt_25k.pth", "zh_sim_g2.pth"],
                            "chi_tra": ["craft_mlt_25k.pth", "chinese.pth"],
                            "kor": ["craft_mlt_25k.pth", "korean_g2.pth"]},
        "mixed_korean_chinese_supported": False,
    })
    print("EasyOCR CPU models ready; inference downloads disabled.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--root", type=Path, help="Engine home containing tools/easyocr")
    location.add_argument("--target", type=Path, help="Legacy direct tools/easyocr directory")
    parser.add_argument("--prepare-models", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--models-only", action="store_true")
    args = parser.parse_args(argv)
    target = (args.target if args.target else (args.root or ROOT) / "tools" / "easyocr").expanduser().resolve()
    if args.prepare_models:
        prepare_models(target)
        return 0
    target.mkdir(parents=True, exist_ok=True)
    environment = target / "venv"
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not args.models_only:
        venv.EnvBuilder(with_pip=True).create(environment)
        lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        constraints = target / "dependency-pins.txt"
        constraints.write_text("\n".join(f"{name}=={version}" for name, version in
                                        lock["verified_runtime"]["packages"].items()) + "\n",
                               encoding="utf-8")
        base = [str(interpreter), "-m", "pip", "install", "--no-cache-dir",
                "--constraint", str(constraints)]
        subprocess.run(base + ["--report", str(target / "torch-install-report.json"),
                       f"torch=={TORCH_VERSION}", f"torchvision=={TORCHVISION_VERSION}",
                       "--index-url", "https://download.pytorch.org/whl/cpu"], check=True)
        subprocess.run(base + ["--report", str(target / "easyocr-install-report.json"),
                       f"easyocr=={EASYOCR_VERSION}"], check=True)
    subprocess.run([str(interpreter), str(Path(__file__).resolve()),
                    "--target", str(target), "--prepare-models"], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
