"""Private file-ack worker protocol and owned persistent worker process.

This file is also loaded by isolated worker scripts, so module-level imports are
stdlib only. Requests contain local paths; stdout/stderr are never the protocol.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def serve_requests(worker, root, languages, write_result, *, failure_evidence=None, max_requests=8):
    """Sequential independent recognitions; a failed request ends this worker."""
    root = Path(root).resolve(strict=True)
    if type(max_requests) is not int or not 1 <= max_requests <= 64:
        raise ValueError("Invalid session request limit")
    for ordinal in range(1, max_requests + 1):
        line = sys.stdin.buffer.readline(65537)
        if not line:
            return 0
        if len(line) > 65536:
            return 1
        request = json.loads(line.decode("utf-8"))
        if request.get("operation") == "close":
            return 0
        token = request.get("id")
        if not isinstance(token, str) or len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
            return 1
        paths = {k: (root / request[k]).resolve() for k in ("input", "output", "ack")}
        if any(not path.is_relative_to(root) for path in paths.values()):
            return 1
        source, output, ack = (paths[k] for k in ("input", "output", "ack"))
        if output.exists() or ack.exists() or source == output or source == ack or output == ack:
            return 1
        if request.get("languages") != languages:
            return 1
        worker._session_request = {"schema": "local-worker-session-v1", "session_id": root.name,
            "request_id": token, "request_ordinal": ordinal, "worker_pid": os.getpid(),
            "context_policy": "independent image request; no prior-page conversation", "hidden_inference_retries": 0}
        try:
            result = worker.recognize(source, languages)
            process = getattr(worker, "process", None)
            result["engine_metadata"]["worker_session"] = dict(worker._session_request,
                server_pid=getattr(process, "pid", None))
            write_result(output, result)
            write_result(ack, {"id": token, "status": "ok", "ordinal": ordinal})
        except Exception as error:
            if failure_evidence is not None:
                failure_evidence(worker, output, error)
            write_result(ack, {"id": token, "status": "failed", "ordinal": ordinal,
                               "error_type": type(error).__name__})
            return 1
    return 0


def load_session_protocol(script_path):
    """Isolated -I workers load only their allowlisted adjacent helper."""
    import importlib.util
    path = Path(script_path).with_name("worker_sessions.py")
    spec = importlib.util.spec_from_file_location("yeone_private_worker_protocol", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkerProcess:
    """One application-owned worker tree; never discovers another server."""
    def __init__(self, argv, root, *, should_stop=None, timeout=300):
        self.root, self.should_stop, self.timeout = Path(root), should_stop, timeout
        self.stack = ExitStack()
        self.process = None
        from .runtime import external_process_search, process_tree, subprocess_environment
        flags = (subprocess.CREATE_NO_WINDOW | 0x00000004) if os.name == "nt" else 0
        try:
            with external_process_search():
                self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=flags, env=subprocess_environment())
            self.stack.enter_context(process_tree(self.process, suspended=os.name == "nt"))
        except BaseException:
            self.close()
            raise

    def request(self, request):
        from .ocr import OCRError, _cancelled
        _cancelled(self.should_stop)
        if self.process is None or self.process.poll() is not None:
            raise OCRError("전용 OCR 작업자가 종료되었습니다. 자동 재시도하지 않습니다.")
        started = time.monotonic()
        try:
            self.process.stdin.write(json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")
            self.process.stdin.flush()
            ack = self.root / request["ack"]
            while not ack.is_file():
                _cancelled(self.should_stop)
                if self.process.poll() is not None:
                    raise OCRError("전용 OCR 작업자가 응답 전에 종료되었습니다.")
                if time.monotonic() - started >= self.timeout:
                    raise OCRError("전용 OCR 요청 제한 시간을 넘겼습니다. 자동 재시도하지 않습니다.")
                time.sleep(0.05)
            if ack.stat().st_size > 4096:
                raise OCRError("전용 OCR 응답 상태가 올바르지 않습니다.")
            result = json.loads(ack.read_text(encoding="utf-8"))
            if result.get("id") != request["id"] or result.get("status") not in ("ok", "failed"):
                raise OCRError("전용 OCR 응답 ID가 일치하지 않습니다.")
            return result
        except BaseException:
            self.close()
            raise

    def close(self):
        process, self.process = self.process, None
        try:
            if process is not None:
                if process.poll() is None:
                    try:
                        process.stdin.write(b'{"operation":"close"}\n')
                        process.stdin.flush()
                        process.wait(timeout=3)
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        pass
                # Closing our own job kills any remaining private descendants.
                self.stack.close()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdin is not None:
                    process.stdin.close()
        finally:
            self.stack.close()
