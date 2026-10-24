"""Explicit, hash-pinned public GLM-OCR/Ollama installation for Windows.

This setup downloads public software/models only. It never opens a document or
reads application keys. Inference is a separate command and cannot pull models.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def verify_files(root: Path, records: list[dict]) -> None:
    for record in records:
        relative = PurePosixPath(record["filename"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid installation-relative path")
        path = root.joinpath(*relative.parts).resolve(strict=True)
        if not path.is_relative_to(root.resolve()) or digest(path) != record["sha256"]:
            raise ValueError("Pinned file hash mismatch")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=Path("tools/glmocr"))
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--lock", type=Path, default=Path(__file__).with_name("glmocr-models.lock.json"))
    parser.add_argument("--download-cache", type=Path, default=Path("downloads/glmocr"))
    args = parser.parse_args(argv)
    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    target = args.target.resolve()
    if target.exists() and any(target.iterdir()):
        raise FileExistsError("Target must be a new empty installation directory")
    args.download_cache.mkdir(parents=True, exist_ok=True)
    archive = args.download_cache / ("ollama-windows-amd64-v" + lock["ollama_version"] + ".zip")
    if not archive.exists():
        temporary = archive.with_suffix(".partial")
        try:
            with urllib.request.urlopen(lock["runtime_source"], timeout=120) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output, 1024 * 1024)
            if digest(temporary) != lock["runtime_archive_sha256"]:
                raise ValueError("Official Ollama archive checksum mismatch")
            os.replace(temporary, archive)
        finally:
            temporary.unlink(missing_ok=True)
    if digest(archive) != lock["runtime_archive_sha256"]:
        raise ValueError("Cached Ollama archive checksum mismatch")
    target.mkdir(parents=True, exist_ok=True)
    runtime, models = target / "runtime", target / "models"
    runtime.mkdir()
    models.mkdir()
    with zipfile.ZipFile(archive) as package:
        for entry in package.infolist():
            destination = (runtime / entry.filename).resolve()
            if not destination.is_relative_to(runtime.resolve()):
                raise ValueError("Runtime archive member escapes installation directory")
        package.extractall(runtime)
    verify_files(runtime, lock["runtime_files"])
    subprocess.run([str(args.python.resolve()), "-m", "venv", str(target / "venv")], check=True)
    python = target / "venv" / "Scripts" / "python.exe"
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                    "Pillow==" + lock["pillow_version"]], check=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    host = f"127.0.0.1:{port}"
    environment = dict(os.environ, OLLAMA_HOST=host, OLLAMA_MODELS=str(models),
                       OLLAMA_NO_CLOUD="1", OLLAMA_DEBUG="0", OLLAMA_KEEP_ALIVE="0")
    executable = runtime / "ollama.exe"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    process = subprocess.Popen([str(executable), "serve"], env=environment,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=flags)
    try:
        for _ in range(300):
            if process.poll() is not None:
                raise RuntimeError("Local setup server did not start")
            try:
                with client.open("http://" + host + "/api/version", timeout=1) as response:
                    if json.load(response).get("version") != lock["ollama_version"]:
                        raise ValueError("Ollama setup server version mismatch")
                break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.1)
        else:
            raise TimeoutError("Local setup server startup timed out")
        subprocess.run([str(executable), "pull", lock["model_id"]], env=environment, check=True)
        with client.open("http://" + host + "/api/tags", timeout=10) as response:
            tags = json.load(response)
        model = next((item for item in tags.get("models", []) if item.get("name") == lock["model_id"]), None)
        if not model or model.get("digest") != lock["model_digest"]:
            raise ValueError("Published model tag changed; review and update the model lock explicitly")
        verify_files(models, lock["models"])
        manifest = dict(lock, python_version=subprocess.check_output(
            [str(python), "-c", "import platform; print(platform.python_version())"], text=True).strip())
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target, delete=False) as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, target / "installation.json")
        print(json.dumps({"engine": "glmocr", "model_digest": lock["model_digest"],
                          "verified_models": len(lock["models"]), "verified_runtime_files": len(lock["runtime_files"])}))
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
