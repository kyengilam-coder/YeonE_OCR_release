"""Isolated Surya OCR 2 worker using a verified private llama.cpp server.

The official Surya recognition/parser pipeline produces block HTML and model
coordinates. Raw model responses and HTML are preserved beside a derived plain
text view. This worker performs no Unicode/script/punctuation normalization and
does not download or contact external services during recognition.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
from html.parser import HTMLParser
import importlib.metadata
import io
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ENGINE_VERSION = "0.22.1"
MODEL_NAME = "datalab-to/surya-ocr-2"


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_output(path: Path, result: dict) -> None:
    if path.exists():
        raise FileExistsError("OCR output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     delete=False, suffix=".tmp") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        if os.name == "nt":
            os.rename(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class PlainText(HTMLParser):
    """Deterministic HTML text extraction; preserves model text code points."""
    block_tags = frozenset({"p", "div", "tr", "li", "h1", "h2", "h3", "h4",
                            "h5", "h6", "blockquote", "pre", "table"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.block_tags:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(value: str) -> str:
    parser = PlainText()
    parser.feed(value)
    parser.close()
    # These are structural separators added by the extractor, not OCR spelling.
    return "".join(parser.parts)


def allow_only_loopback(port: int) -> None:
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def approved(address):
        if not isinstance(address, tuple) or len(address) < 2 or address[1] != port:
            raise RuntimeError("External network is disabled during OCR")
        try:
            permitted = ipaddress.ip_address(address[0]).is_loopback
        except ValueError:
            permitted = False
        if not permitted:
            raise RuntimeError("External network is disabled during OCR")

    def connect(connection, address):
        approved(address)
        return original_connect(connection, address)

    def connect_ex(connection, address):
        approved(address)
        return original_connect_ex(connection, address)

    def getaddrinfo(host, requested_port, *args, **kwargs):
        approved((host, requested_port))
        return original_getaddrinfo(host, requested_port, *args, **kwargs)

    def denied(*_args, **_kwargs):
        raise RuntimeError("External network is disabled during OCR")

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo
    socket.socket.sendto = denied


def verify_installation(model_dir: Path) -> tuple[dict, Path]:
    target = model_dir.resolve().parent
    manifest = json.loads((target / "installation.json").read_text(encoding="utf-8"))
    if manifest.get("engine") != "surya" or manifest.get("engine_version") != ENGINE_VERSION:
        raise ValueError("Incompatible Surya installation manifest")
    if importlib.metadata.version("surya-ocr") != ENGINE_VERSION:
        raise ValueError("Surya package version differs from manifest")
    for record in manifest["models"]:
        path = (model_dir / record["file"]).resolve()
        if not path.is_relative_to(model_dir.resolve()) or digest_file(path) != record["sha256"]:
            raise ValueError("Surya model SHA-256 mismatch")
    runtime = manifest.get("runtime_files", [])
    if not runtime:
        raise ValueError("Surya backend binary hashes are not recorded")
    for record in runtime:
        path = (target / record["file"]).resolve()
        if not path.is_relative_to(target) or digest_file(path) != record["sha256"]:
            raise ValueError("Surya backend SHA-256 mismatch")
    runtime_root = target / manifest.get("runtime_directory", "llama.cpp")
    actual_runtime = {path.relative_to(target).as_posix() for path in runtime_root.rglob("*")
                      if path.is_file()}
    if actual_runtime != {record["file"] for record in runtime}:
        raise ValueError("Unrecorded Surya backend files exist")
    for record in manifest.get("package_files", []):
        path = (target / record["file"]).resolve()
        if not path.is_relative_to(target) or digest_file(path) != record["sha256"]:
            raise ValueError("Surya package source SHA-256 mismatch")
    binaries = [target / record["file"] for record in runtime
                if Path(record["file"]).name == "llama-server.exe"]
    if len(binaries) != 1:
        raise ValueError("Exactly one verified llama-server binary is required")
    return manifest, binaries[0]


class LocalManager:
    """Duck-typed official predictor manager; no shared/unknown server reuse."""

    def __init__(self, client):
        self.client = client
        self.responses = []

    def generate(self, batch):
        from surya.inference.backends.openai_client import chat_completions_batch
        result = chat_completions_batch(batch, client=self.client,
                                       model_name=MODEL_NAME, max_workers=1,
                                       max_retries=0, timeout=600.0,
                                       request_logprobs_default=False)
        for item in result:
            self.responses.append(asdict(item))
        return result


class CompletionRecorder:
    def __init__(self, client, records):
        self.client = client
        self.records = records
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        response = self.client.chat.completions.create(**kwargs)
        self.records.append(response.model_dump(mode="json"))
        return response


class Worker:
    def __init__(self, model_dir: Path, device=None):
        self.model_dir = model_dir.resolve()
        self.manifest, self.binary = verify_installation(self.model_dir)
        if device is not None:
            if device not in ("cpu", "cuda"):
                raise ValueError("Invalid Surya device")
            if device == "cuda" and self.manifest.get("backend_device", "cpu") != "cuda":
                raise ValueError("CUDA backend is not explicitly installed")
            self.manifest = dict(self.manifest, backend_device=device)
        self.process = None
        self.client = None
        self.port = None
        self.manager = None
        self.predictor = None
        self.completions = []
        self.current_generation_start = 0
        self.current_completion_start = 0

    def failure_evidence(self, destination, error):
        if self.manager is None:
            return
        evidence_path = destination.with_name(destination.name + ".failure.json")
        if not evidence_path.exists():
            atomic_output(evidence_path, {"engine": "surya", "status": "failed",
                "error_type": type(error).__name__,
                "worker_session": dict(getattr(self, "_session_request", {}),
                                       server_pid=getattr(self.process, "pid", None)),
                "raw_generations": self.manager.responses[self.current_generation_start:],
                "raw_completions": self.completions[self.current_completion_start:],
                "text_policy": "unmodified failed raw model output; not accepted OCR text"})

    def start(self) -> None:
        if self.process is not None:
            return
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ.pop(key, None)
        os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                          HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1",
                          DISABLE_TQDM="1", TORCH_DEVICE="cpu",
                          SURYA_FULLPAGE_REGEN="false", SURYA_INFERENCE_PARALLEL="1",
                          SURYA_MAX_TOKENS_FULL_PAGE="12288", SURYA_MAX_TOKENS_LAYOUT="3072",
                          SURYA_MAX_TOKENS_BLOCK_CEILING="8192", SURYA_GUIDED_LAYOUT="true",
                          BBOX_SCALE="1000")
        command = [str(self.binary), "-m", str(self.model_dir / "surya-2.gguf"),
                   "--mmproj", str(self.model_dir / "surya-2-mmproj.gguf"),
                   "--host", "127.0.0.1", "--port", str(self.port),
                   "--parallel", "1", "--ctx-size", "32768", "--threads", "6",
                   "--n-gpu-layers", "99" if self.manifest.get("backend_device") == "cuda" else "0",
                   "--alias", MODEL_NAME,
                   "--jinja", "--log-disable"]
        if self.manifest.get("backend_device", "cpu") == "cpu":
            command.append("--no-mmproj-offload")
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                  "env": {key: value for key, value in os.environ.items() if not key.startswith("LLAMA_ARG_")}}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.process = subprocess.Popen(command, **kwargs)
        allow_only_loopback(self.port)
        base_url = f"http://127.0.0.1:{self.port}"
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Verified llama.cpp backend exited during startup")
            try:
                with urllib.request.urlopen(base_url + "/health", timeout=2) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, TimeoutError):
                pass
            time.sleep(0.5)
        else:
            raise TimeoutError("Private llama.cpp backend startup timeout")
        from openai import OpenAI
        logging.disable(logging.CRITICAL)
        from surya.recognition import RecognitionPredictor
        self.client = OpenAI(api_key="LOCAL-NO-KEY", base_url=base_url + "/v1", max_retries=0)
        self.manager = LocalManager(CompletionRecorder(self.client, self.completions))
        self.predictor = RecognitionPredictor(self.manager)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=15)

    def recognize(self, image_path: Path, languages: str) -> dict:
        if not image_path.is_file():
            raise ValueError("OCR input must be an existing local image")
        from PIL import Image
        image_bytes = image_path.read_bytes()
        with Image.open(io.BytesIO(image_bytes)) as source:
            image = source.convert("RGB")
        started = time.perf_counter()
        self.start()
        raw_start = len(self.manager.responses)
        completion_start = len(self.completions)
        self.current_generation_start = raw_start
        self.current_completion_start = completion_start
        prediction = self.predictor([image])[0]
        raw_prediction = prediction.model_dump(mode="json")
        words = []
        texts = []
        for block in raw_prediction["blocks"]:
            if block.get("error"):
                raise ValueError("Surya returned an OCR block error")
            if block.get("skipped"):
                continue
            text = html_to_text(block["html"])
            bbox = block.get("bbox")
            if bbox is None:
                polygon = block["polygon"]
                bbox = [min(point[0] for point in polygon), min(point[1] for point in polygon),
                        max(point[0] for point in polygon), max(point[1] for point in polygon)]
            if len(bbox) != 4 or not all(math.isfinite(float(value)) for value in bbox):
                raise ValueError("Surya coordinates/confidence are not finite")
            if not text:
                continue
            texts.append(text)
            words.append({"text": text, "bbox": [float(value) for value in bbox],
                          "confidence": None,
                          "block": block["reading_order"] + 1, "paragraph": 1, "line": 1})
        text = "\n".join(texts)
        raw_generations = self.manager.responses[raw_start:]
        completions = self.completions[completion_start:]
        if not completions or any(not response.get("choices")
                                  or any(choice.get("finish_reason") != "stop"
                                         for choice in response["choices"])
                                  for response in completions):
            raise ValueError("Surya generation was not completed")
        return {"text": text, "words": words, "image_width": image.width,
                "image_height": image.height, "duration_seconds": time.perf_counter() - started,
                "engine_version": ENGINE_VERSION,
                "engine_metadata": {
                    "model_name": "Surya OCR 2 (650M)", "model_repository": self.manifest["model_repository"],
                    "model_revision": self.manifest["model_revision"], "models": self.manifest["models"],
                    "backend": self.manifest["backend"], "runtime_files": self.manifest["runtime_files"],
                    "llama_cpp_release": self.manifest["llama_cpp_release"],
                    "generation_complete": True,
                    "provider": self.manifest.get("backend_device", "cpu").upper(),
                    "threads": 6, "context_tokens": 32768,
                    "generation_settings": {"temperature": 0.0, "top_p": 0.1,
                                            "max_tokens_full_page": 12288, "max_tokens_layout": 3072,
                                            "max_tokens_block_ceiling": 8192, "parallel": 1,
                                            "full_page_regeneration": False, "http_retries": 0,
                                            "token_logprobs_requested": False, "bbox_scale": 1000},
                    "input_file_sha256": hashlib.sha256(image_bytes).hexdigest(),
                    "requested_languages": languages, "language_route": "single multilingual model",
                    "network": "private loopback only; public inference network disabled",
                    "reading_order": "official Surya block order; no custom reordering",
                    "image_resize_policy": "official scale_to_fit: area <=3072*2048, dimensions on 28px grid; model bbox scaled to original input pixels",
                    "bbox_granularity": "block", "confidence_granularity": "unavailable",
                    "confidence_policy": "token logprobs not requested; exposed confidence null; raw_prediction contains official parser's default confidence=1.0, not measured probability",
                    "text_policy": "HTMLParser entity decoding and structural LF/tab; no Unicode/script/punctuation folding",
                    "raw_prediction": raw_prediction, "raw_generations": raw_generations,
                    "raw_completions": completions,
                    "derivation": {"method": "surya-html-to-text-v1", "source": "raw_prediction.blocks[].html",
                                   "output_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()},
                    "generation_policy": "official full-page predictor; official layout/block fallback if parse fails; regeneration off; HTTP/client retries 0"}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--languages", default="chi_tra")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--device", choices=("cpu", "cuda"))
    parser.add_argument("--session-root", type=Path)
    parser.add_argument("--max-requests", type=int, default=8)
    args = parser.parse_args()
    worker = None
    destination = None
    try:
        worker = Worker(args.model_dir, device=args.device)
        if args.session_root:
            import importlib.util
            spec = importlib.util.spec_from_file_location("yeone_worker_protocol", Path(__file__).with_name("worker_sessions.py"))
            protocol = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(protocol)
            return protocol.serve_requests(worker, args.session_root, args.languages, atomic_output,
                failure_evidence=lambda instance, output, error: instance.failure_evidence(output, error),
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
            result = worker.recognize(Path(item["input"]), item.get("languages", args.languages))
            atomic_output(destination, result)
            print(json.dumps({"completed": index, "total": len(items),
                              "seconds": round(result["duration_seconds"], 3)}), flush=True)
        return 0
    except Exception as error:
        if worker is not None and destination is not None and worker.manager is not None:
            evidence_path = destination.with_name(destination.name + ".failure.json")
            if not evidence_path.exists():
                atomic_output(evidence_path, {"engine": "surya", "status": "failed",
                              "error_type": type(error).__name__,
                              "raw_generations": worker.manager.responses[worker.current_generation_start:],
                              "raw_completions": worker.completions[worker.current_completion_start:],
                              "text_policy": "unmodified failed raw model output; not accepted OCR text"})
        print("Surya worker failed (" + type(error).__name__
              + "). Check the isolated installation, local model and image.", file=sys.stderr)
        return 1
    finally:
        if worker is not None:
            worker.close()


if __name__ == "__main__":
    raise SystemExit(main())
