"""Make a relocatable, offline Windows PP-OCRv6 distribution from a pinned install.

Run with the engine venv Python. Copies distribution RECORD entries (including
licenses and native DLLs), never arbitrary files from the build environment.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import shutil
import sys


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(source: Path, destination: Path, lock_path: Path, msvc_runtime: Path):
    if sys.platform != "win32" or sys.version_info[:2] != (3, 13):
        raise ValueError("Use the pinned Windows Python 3.13 engine environment")
    source, destination = source.resolve(), destination.resolve()
    if destination.exists():
        raise ValueError("Use a new output directory")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    installed = json.loads((source / "installation.json").read_text(encoding="utf-8"))
    if installed.get("engine") != "ppocrv6":
        raise ValueError("PP-OCRv6 installation required")
    packages = lock["packages"]
    for name, version in packages.items():
        if metadata.version(name) != version:
            raise ValueError(f"Pinned dependency mismatch: {name}")
    # Validate models and upstream license against the committed lock, not the
    # editable local installation manifest.
    records = [*lock["models"], lock["license_file"]]
    for record in records:
        path = source / ("models" if record in lock["models"] else "") / record["file"]
        if digest(path) != record["sha256"]:
            raise ValueError(f"Model/license hash mismatch: {record['file']}")
    for record in lock["models"]:
        if not any(r.get("file") == record["file"] and r.get("sha256") == record["sha256"]
                   for r in installed["models"]):
            raise ValueError("Installation manifest differs from model lock")
    destination.mkdir(parents=True)
    runtime = destination / "runtime"
    base = Path(sys.base_prefix).resolve()

    def copy(src, dst):
        if src.is_symlink() or not src.is_file():
            raise ValueError(f"Regular distribution file required: {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    for name in ("python.exe", "pythonw.exe", "python313.dll", "python3.dll", "LICENSE.txt"):
        copy(base / name, runtime / name)
    for src in base.glob("vcruntime*.dll"):
        copy(src, runtime / src.name)
    crt_provenance = json.loads((msvc_runtime / "msvc-provenance.json").read_text(encoding="utf-8"))
    for name, expected in crt_provenance["files"].items():
        if Path(name).name != name or "/" in name or "\\" in name or digest(msvc_runtime / name) != expected:
            raise ValueError("CRT payload differs from its verified extraction")
        copy(msvc_runtime / name, runtime / name if name.endswith(".dll") else destination / name)
    for name in ("msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        if not (runtime / name).is_file():
            raise ValueError("Required app-local CRT file missing")
    copy(msvc_runtime / "msvc-provenance.json", destination / "msvc-provenance.json")
    excluded = {"site-packages", "test", "tests", "__pycache__", "idlelib", "tkinter", "turtledemo", "ensurepip"}
    for directory in ("Lib", "DLLs"):
        for src in sorted((base / directory).rglob("*")):
            rel = src.relative_to(base)
            if any(part in excluded for part in rel.parts) or not src.is_file():
                continue
            if directory == "DLLs" and src.suffix.lower() not in (".dll", ".pyd"):
                continue
            if src.suffix.lower() in (".pyc", ".pyo"):
                continue
            copy(src, runtime / rel)
    site = Path(sys.prefix) / "Lib" / "site-packages"
    for name in packages:
        dist = metadata.distribution(name)
        if not dist.files:
            raise ValueError(f"Missing distribution RECORD: {name}")
        for entry in dist.files:
            # Ignore console scripts outside site-packages, and bytecode caches.
            if ".." in entry.parts or "__pycache__" in entry.parts or entry.suffix == ".pyc":
                continue
            src = Path(dist.locate_file(entry))
            src.resolve().relative_to(site.resolve())
            copy(src, runtime / "Lib" / "site-packages" / str(entry))
    (runtime / "python313._pth").write_text(".\nDLLs\nLib\nLib/site-packages\nimport site\n", encoding="ascii")
    for record in records:
        rel = Path("models") / record["file"] if record in lock["models"] else Path(record["file"])
        copy(source / rel, destination / rel)
    copy(lock_path, destination / "ppocrv6-lock.json")
    provenance = source / "pip-install-report.json"
    if provenance.is_file():
        copy(provenance, destination / "pip-install-report.json")
    installed.update(runtime_layout="standalone-python", python_version=sys.version.split()[0])
    installed["packages"] = [{"name": name, "version": version} for name, version in packages.items()]
    (destination / "installation.json").write_text(json.dumps(installed, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / "THIRD_PARTY_NOTICES.txt").write_text(
        "PP-OCRv6 medium: Apache-2.0; see LICENSE.RapidOCR.txt.\n"
        "Python: see runtime/LICENSE.txt. Dependency licenses are preserved in\n"
        "Microsoft C++ Runtime: see MSVC-LICENSE.html and msvc-provenance.json.\n"
        "runtime/Lib/site-packages/*.dist-info and upstream package directories.\n"
        + "\n".join(f"{name}=={version}" for name, version in packages.items()) + "\n", encoding="utf-8")
    files = {p.relative_to(destination).as_posix(): {"sha256": digest(p), "bytes": p.stat().st_size}
             for p in sorted(destination.rglob("*")) if p.is_file()}
    manifest = {"schema_version": 1, "engine": "ppocrv6", "python_version": sys.version.split()[0], "files": files}
    (destination / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"files": len(files), "bytes": sum(r["bytes"] for r in files.values()), "destination": str(destination)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=Path(__file__).resolve().parents[1] / "scripts/ppocrv6-lock.json")
    parser.add_argument("--msvc-runtime", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.output, args.lock, args.msvc_runtime)))
