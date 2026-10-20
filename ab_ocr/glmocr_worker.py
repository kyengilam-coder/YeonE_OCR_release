"""Pinned GLM-OCR transcription through a private, child-owned Ollama server.

Documents are sent only to a loopback server started by this worker. No hosted
endpoint, model pull, script conversion, text cleanup or coordinate invention is
performed. The model does not return character geometry: ``words`` stays empty.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

LANGUAGES = ("chi_sim", "chi_tra", "kor", "kor+chi_tra", "kor+chi_sim",
             "chi_sim+eng", "chi_tra+eng", "kor+eng", "kor+chi_tra+eng")
PROMPT = "Text Recognition:"


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _local_file(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if relative.is_absolute() or not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        raise ValueError("Invalid installation-relative filename")
    path = root.joinpath(*relative.parts).resolve(strict=True)
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Installation filename escapes its directory")
    return path


def verify_installation(model_dir: Path) -> tuple[dict, list[dict], Path]:
    root = model_dir.parent.resolve()
    manifest = json.loads((root / "installation.json").read_text(encoding="utf-8-sig"))
    if (manifest.get("engine") != "glmocr" or manifest.get("model_layout") != "relative-paths-v1"
            or not manifest.get("model_id") or not manifest.get("model_digest")
            or not manifest.get("ollama_version")):
        raise ValueError("Incompatible GLM-OCR installation manifest")
    records = manifest.get("models")
    runtime_files = manifest.get("runtime_files")
    if not isinstance(records, list) or not records or not isinstance(runtime_files, list) or not runtime_files:
        raise ValueError("Missing GLM-OCR installation hashes")
    for directory, group in ((model_dir, records), (root / "runtime", runtime_files)):
        names = set()
        for record in group:
            name, digest = record.get("filename"), record.get("sha256")
            if (not isinstance(name, str) or name in names or not isinstance(digest, str)
                    or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
                raise ValueError("Invalid GLM-OCR file identity")
            names.add(name)
            if _digest(_local_file(directory, name)) != digest:
                raise ValueError("GLM-OCR local file checksum mismatch")
        if directory == root / "runtime":
            actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
            if actual != names:
                raise ValueError("Unrecorded GLM-OCR executable or library file")
    executable = _local_file(root / "runtime", "ollama.exe")
    if not any(item["filename"] == "ollama.exe" for item in runtime_files):
        raise ValueError("GLM-OCR runtime executable is not hash-pinned")
    return manifest, records, executable


def atomic_output(path: Path, value: dict) -> None:
    if path.exists():
        raise FileExistsError("OCR output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         delete=False, suffix=".tmp") as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            os.rename(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Worker:
    def __init__(self, model_dir: Path, *, device: str = "cuda", threads: int = 8,
                 timeout: float = 300, max_tokens: int = 4096):
        self.model_dir = model_dir.resolve(strict=True)
        self.manifest, self.models, self.executable = verify_installation(self.model_dir)
        if device not in ("cpu", "cuda") or not 1 <= threads <= 128 or not 128 <= max_tokens <= 16384:
            raise ValueError("Invalid GLM-OCR device or limits")
        self.device, self.threads = device, threads
        self.timeout, self.max_tokens = timeout, max_tokens
        self.process = None
        self.base_url = ""
        self.client = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, endpoint: str, body: dict | None = None, *, timeout: float = 5) -> dict:
        # base_url is constructed internally from a loopback address, never user input.
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base_url + endpoint, data=encoded,
                                         headers={"Content-Type": "application/json"})
        with self.client.open(request, timeout=timeout) as response:
            content = response.read(32_000_001)
        if len(content) > 32_000_000:
            raise ValueError("GLM-OCR response exceeds the size limit")
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError("GLM-OCR response must be an object")
        return value

    def start(self) -> None:
        if self.process is not None:
            return
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        environment = dict(os.environ)
        environment.update(OLLAMA_HOST=f"127.0.0.1:{port}", OLLAMA_MODELS=str(self.model_dir),
                           OLLAMA_NO_CLOUD="1", OLLAMA_DEBUG="0", OLLAMA_KEEP_ALIVE="0",
                           OLLAMA_MAX_LOADED_MODELS="1", OLLAMA_NUM_PARALLEL="1")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen([str(self.executable), "serve"], env=environment,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, creationflags=flags)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Private GLM-OCR server did not start")
            try:
                version = self._request("/api/version", timeout=1)
                if version.get("version") != self.manifest["ollama_version"]:
                    raise ValueError("Private GLM-OCR runtime version mismatch")
                tags = self._request("/api/tags")
                installed = next((item for item in tags.get("models", [])
                                  if item.get("name") == self.manifest["model_id"]), None)
                if not installed or installed.get("digest") != self.manifest["model_digest"]:
                    raise ValueError("Private GLM-OCR loaded model identity mismatch")
                if self.process.poll() is not None:
                    raise RuntimeError("Private GLM-OCR server exited")
                return
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.1)
        raise TimeoutError("Private GLM-OCR server startup timed out")

    def close(self) -> None:
        if self.process is None:
            return
        process, self.process = self.process, None
        if process.poll() is None:
            try:
                self._request("/api/generate", {"model": self.manifest["model_id"], "keep_alive": 0}, timeout=10)
            except (OSError, ValueError):
                pass
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def recognize(self, image_path: Path, languages: str) -> dict:
        if languages not in LANGUAGES:
            raise ValueError("Unsupported requested language profile")
        image_path = image_path.resolve(strict=True)
        if not image_path.is_file():
            raise ValueError("OCR input must be an existing local image")
        from PIL import Image
        with Image.open(image_path) as image:
            width, height = image.size
        image_digest = _digest(image_path)
        started = time.perf_counter()
        self.start()
        options = {"temperature": 0, "seed": 0, "num_predict": self.max_tokens,
                   "num_ctx": 8192, "num_thread": self.threads,
                   "num_gpu": 0 if self.device == "cpu" else 99}
        result = self._request("/api/generate", {
            "model": self.manifest["model_id"], "prompt": PROMPT,
            "images": [base64.b64encode(image_path.read_bytes()).decode("ascii")],
            "stream": False, "keep_alive": "5m", "options": options}, timeout=self.timeout)
        if _digest(image_path) != image_digest:
            raise ValueError("OCR input changed during recognition")
        text = result.get("response")
        if not isinstance(text, str) or not isinstance(result.get("done"), bool):
            raise ValueError("GLM-OCR response has no raw transcription")
        complete = result["done"] and result.get("done_reason") == "stop"
        state = self._request("/api/ps")
        running = next((item for item in state.get("models", [])
                        if item.get("digest") == self.manifest["model_digest"]), {})
        device_ok = self.device == "cpu" or (type(running.get("size_vram")) is int and running["size_vram"] > 0)
        complete = complete and device_ok
        metadata = {
            "engine": "glmocr", "model_family": "GLM-OCR", "model_id": self.manifest["model_id"],
            "model_digest": self.manifest["model_digest"], "models": self.models,
            "ollama_version": self.manifest["ollama_version"], "device_requested": self.device,
            "actual_size_vram": running.get("size_vram"), "model_details": self.manifest.get("model_details", {}),
            "device_requirement_satisfied": device_ok,
            "requested_languages": languages, "language_route": "single multilingual transcription model",
            "input_image_sha256": image_digest,
            "network": "private child-owned loopback server only; cloud disabled; no pull endpoint",
            "server_logs": "discarded; no request body or document text logging",
            "prompt": PROMPT, "settings": options, "generation_complete": complete,
            "done_reason": result.get("done_reason"), "raw_response_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "prompt_eval_count": result.get("prompt_eval_count"), "eval_count": result.get("eval_count"),
            "total_duration_ns": result.get("total_duration"), "load_duration_ns": result.get("load_duration"),
            "prompt_eval_duration_ns": result.get("prompt_eval_duration"), "eval_duration_ns": result.get("eval_duration"),
            "bbox_granularity": "none", "confidence_granularity": "none",
            "input_region_bounds": [0, 0, width, height],
            "text_policy": "raw generated text; no stripping, normalization, punctuation or script conversion",
            "limitations": "Generative text may contain omissions or formatting; no character/line geometry or confidence",
            "warnings": ([] if complete else ["Requested CUDA device unavailable; raw response is not a successful GPU reading"
                          if not device_ok else "Model generation did not finish normally; retain as incomplete raw evidence"])}
        return {"text": text, "words": [], "image_width": width, "image_height": height,
                "duration_seconds": time.perf_counter() - started,
                "engine_version": "GLM-OCR/" + self.manifest["ollama_version"], "engine_metadata": metadata}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--languages", default="chi_sim")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--session-root", type=Path)
    parser.add_argument("--max-requests", type=int, default=8)
    args = parser.parse_args(argv)
    worker = None
    try:
        if args.session_root:
            import importlib.util
            spec = importlib.util.spec_from_file_location("yeone_worker_protocol", Path(__file__).with_name("worker_sessions.py"))
            protocol = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(protocol)
            worker = Worker(args.model_dir, device=args.device, threads=args.threads,
                            timeout=args.timeout, max_tokens=args.max_tokens)
            return protocol.serve_requests(worker, args.session_root, args.languages, atomic_output,
                                           max_requests=args.max_requests)
        if args.batch:
            items = json.loads(args.batch.read_text(encoding="utf-8-sig"))["items"]
        elif args.input and args.output:
            items = [{"input": str(args.input), "output": str(args.output), "languages": args.languages}]
        else:
            parser.error("Use --input and --output, or --batch")
        worker = Worker(args.model_dir, device=args.device, threads=args.threads,
                        timeout=args.timeout, max_tokens=args.max_tokens)
        for index, item in enumerate(items, 1):
            destination = Path(item["output"])
            if destination.exists():
                raise FileExistsError("OCR output already exists")
            result = worker.recognize(Path(item["input"]), item.get("languages", args.languages))
            atomic_output(destination, result)
            print(json.dumps({"completed": index, "total": len(items),
                              "seconds": round(result["duration_seconds"], 3),
                              "complete": result["engine_metadata"]["generation_complete"]}), flush=True)
        return 0
    except Exception as exc:
        print("GLM-OCR worker failed (" + type(exc).__name__ + "). Check local installation and device limits.", file=sys.stderr)
        return 1
    finally:
        if worker is not None:
            worker.close()


if __name__ == "__main__":
    raise SystemExit(main())
