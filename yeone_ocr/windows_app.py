"""Windows executable entry point and explicitly requested offline smoke test.

Normal startup delegates to the same GUI as the source distribution. Self-test
never loads GUI configuration, starts an updater, or opens a network connection.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import sys
import threading
import traceback

from . import __version__


def redact_log(text: str, secrets=()) -> str:
    """Redact credentials, including API keys configured after process startup."""
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    text = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[REDACTED]", text)
    text = re.sub(r"sk-[0-9A-Za-z_-]{12,}", "[REDACTED]", text)
    text = re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+", r"\1[REDACTED]", text)
    return re.sub(r"(?i)((?:api[_-]?key|x-goog-api-key|[?&]key)\s*[=:]\s*)[^\s&,;]+", r"\1[REDACTED]", text)


class SafeLog(io.TextIOBase):
    """Line-buffered stream; never store a partial credential between writes."""
    def __init__(self, path: Path):
        super().__init__()
        self._file = path.open("a", encoding="utf-8", buffering=1)
        self._pending = ""
        self._lock = threading.RLock()
        self._secrets = tuple(os.environ.get(k, "") for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"))

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True

    def write(self, text):
        text = str(text)
        with self._lock:
            self._pending += text
            while "\n" in self._pending:
                line, self._pending = self._pending.split("\n", 1)
                self._file.write(redact_log(line, self._secrets) + "\n")
            # Avoid unbounded output on a malformed library log. Drop an
            # unterminated oversized line instead of leaking partial keys.
            if len(self._pending) > 65536:
                self._pending = "[oversized log line omitted]"
        return len(text)

    def flush(self):
        with self._lock:
            if not self._file.closed:
                self._file.flush()

    def finish(self):
        with self._lock:
            if self._file.closed:
                return
            if self._pending:
                self._file.write(redact_log(self._pending, self._secrets) + "\n")
                self._pending = ""
            self._file.flush()

    def close(self):
        if not self.closed:
            self.finish()
            super().close()
            self._file.close()


def _console_message(message: str) -> None:
    """Support --version even when the windowed bootloader sets stdout=None."""
    if sys.stdout is not None:
        print(message)
        return
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel.WriteFile.restype = wintypes.BOOL
    handle = kernel.GetStdHandle(-11 & 0xFFFFFFFF)
    if handle and handle != ctypes.c_void_p(-1).value:
        data = (message + "\n").encode("utf-8")
        written = wintypes.DWORD()
        if kernel.WriteFile(handle, data, len(data), ctypes.byref(written), None):
            return
    # Attach only to an existing terminal; do not create a visible console.
    if kernel.AttachConsole(-1):
        try:
            with open("CONOUT$", "w", encoding="utf-8") as stream:
                stream.write(message + "\n")
        finally:
            kernel.FreeConsole()


def _acknowledge_update_ready(path: str | None, token: str | None) -> bool:
    """Confirm this launch only after Tk has processed its first idle event.

    The updater creates the parent directory and a random per-launch token.
    Do not create arbitrary missing directories or acknowledge malformed jobs.
    """
    if not path or not token or not re.fullmatch(r"[0-9a-f]{64}", token):
        return False
    destination = Path(path)
    if (not destination.is_absolute() or destination.name != "application-ready"
            or not re.fullmatch(r"install-[0-9a-f]{32}", destination.parent.name)
            or not destination.parent.is_dir() or destination.exists() or destination.is_symlink()):
        return False
    import tempfile
    pending = None
    try:
        configuration_path = destination.parent / "install.json"
        if configuration_path.is_symlink():
            return False
        with configuration_path.open("r", encoding="utf-8") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            return False
        configuration = json.loads(raw)
        if (not isinstance(configuration, dict) or type(configuration.get("schema_version")) is not int
                or configuration["schema_version"] != 1 or configuration.get("ready_token") != token
                or not isinstance(configuration.get("target"), str)
                or Path(configuration["target"]).resolve() != Path(sys.executable).resolve()):
            return False
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=destination.name + ".", suffix=".tmp", delete=False) as stream:
            pending = Path(stream.name)
            json.dump({"token": token, "version": __version__, "pid": os.getpid()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, destination)
        return True
    except (OSError, ValueError):
        # Let the helper retain its rollback copy if this acknowledgement
        # cannot be written. Never log the job token or its environment value.
        return False
    finally:
        if pending is not None and pending.exists():
            try:
                pending.unlink()
            except OSError:
                pass


def _normal_gui() -> int:
    stream = None
    original_streams = (sys.stdin, sys.stdout, sys.stderr)
    null_input = None
    # Consume before GUI/SDK workers can inherit this single-use handshake.
    ready_path = os.environ.pop("YEONE_UPDATE_READY_FILE", None)
    ready_token = os.environ.pop("YEONE_UPDATE_READY_TOKEN", None)
    try:
        if sys.stdout is None or sys.stderr is None:
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            logs = base / "YeonE_OCR" / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            path = logs / "startup.log"
            if path.exists() and path.stat().st_size > 2_000_000:
                path.replace(logs / "startup.previous.log")
            stream = SafeLog(path)
            if sys.stdout is None:
                sys.stdout = stream
            if sys.stderr is None:
                sys.stderr = stream
        if sys.stdin is None:
            null_input = sys.stdin = open(os.devnull, "r", encoding="utf-8")
        from .gui import App
        app = App()
        if ready_path and ready_token:
            app.after_idle(lambda: _acknowledge_update_ready(ready_path, ready_token))
        app.mainloop()
        return 0
    except Exception:
        if sys.stderr is not None:
            traceback.print_exc()
        # GUI initialization errors must be visible in a windowed build.
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,
                "프로그램을 시작하지 못했습니다.\n%LOCALAPPDATA%\\YeonE_OCR\\logs\\startup.log를 확인해 주세요.",
                "YeonE_OCR", 0x10)
        return 1
    finally:
        if stream is not None:
            sys.stdout, sys.stderr = original_streams[1:]
            stream.close()
        if null_input is not None:
            sys.stdin = original_streams[0]
            null_input.close()


def _self_test(output: Path) -> int:
    """Exercise bundled resources and the real mock PDF→HWPX/DOCX pipeline."""
    import importlib.metadata
    import socket
    import tempfile
    import time
    from unittest.mock import patch
    import zipfile

    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_dir = output / ("run-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    run_dir.mkdir()
    (run_dir / "temp").mkdir()
    report = {"version": __version__, "frozen": bool(getattr(sys, "frozen", False)),
              "purpose": "offline dependency and artifact smoke test; not an OCR accuracy benchmark",
              "run_directory": str(run_dir), "checks": {}, "dependencies": {},
              "blocked_external_calls": [], "passed": False}
    started = time.monotonic()
    log = SafeLog(run_dir / "self-test.log")

    def denied(name):
        def call(*args, **kwargs):
            report["blocked_external_calls"].append(name)
            raise RuntimeError("Offline self-test forbids external calls: " + name)
        return call

    def check(name, value):
        report["checks"][name] = bool(value)
        if not value:
            raise AssertionError("Self-test failed: " + name)

    try:
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(log))
            stack.enter_context(redirect_stderr(log))
            stack.enter_context(patch.dict(os.environ, {"TEMP": str(run_dir / "temp"),
                                                       "TMP": str(run_dir / "temp"),
                                                       "GEMINI_API_KEY": "", "GOOGLE_API_KEY": "", "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": ""}))
            stack.enter_context(patch.object(tempfile, "tempdir", str(run_dir / "temp")))
            # Block DNS as well as socket connections, including an unexpected
            # SDK/update import side effect. No credentials or payloads recorded.
            for name in ("connect", "connect_ex"):
                stack.enter_context(patch.object(socket.socket, name, denied("socket." + name)))
            for name in ("create_connection", "getaddrinfo"):
                stack.enter_context(patch.object(socket, name, denied("socket." + name)))

            import cv2
            import docx
            from google.genai import types
            import hanja
            from hwpx import HwpxDocument
            import numpy as np
            import opencc
            from PIL import Image
            import pymupdf
            import tkinter
            from . import cli
            from .collate.zh import ambiguous_map
            from .pdfio import text_layer
            from .prompts import GUIDE_CANDIDATES, load_guide
            stack.enter_context(patch("yeone_ocr.reader_gemini.GeminiReader.__init__", denied("GeminiReader")))
            stack.enter_context(patch("yeone_ocr.estimate.fetch_fx_krw", denied("fetch_fx_krw")))

            for name in ("pymupdf", "pillow", "numpy", "opencv-python-headless", "pydantic",
                         "google-genai", "python-hwpx", "opencc-python-reimplemented", "hanja", "python-docx"):
                report["dependencies"][name] = importlib.metadata.version(name)
            check("dependency_metadata", len(report["dependencies"]) == 10)
            check("guide_bundled", any(p.is_file() for p in GUIDE_CANDIDATES) and len(load_guide()) > 1000)
            check("hanja_dictionary", hanja.translate("漢字", "substitution") == "한자")
            check("opencc_dictionary", opencc.OpenCC("s2t").convert("汉字") == "漢字" and bool(ambiguous_map()))
            check("genai_pydantic_models", types.GenerateContentConfig(temperature=0).temperature == 0)
            check("opencv_numpy", cv2.cvtColor(np.zeros((2, 2, 3), dtype=np.uint8), cv2.COLOR_RGB2GRAY).shape == (2, 2))
            with Image.new("RGB", (2, 2), "white") as image:
                image.save(run_dir / "pillow.png")
            check("pillow_png", (run_dir / "pillow.png").is_file())
            app = tkinter.Tk()
            try:
                app.withdraw()
                app.update_idletasks()
                check("tk_runtime", bool(app.tk.call("info", "patchlevel")))
            finally:
                app.destroy()

            sample = run_dir / "sample_ko_hanja.pdf"
            with pymupdf.open() as document:
                for number in (1, 2):
                    page = document.new_page(width=320, height=420)
                    page.insert_text((30, 30), f"YeonE OCR offline smoke / page {number}", fontsize=11)
                    page.insert_text((270, 65), "天地玄黃宇宙洪荒", fontname="korea", fontsize=18, rotate=270)
                    page.insert_text((225, 65), "한글漢字혼용검증", fontname="korea", fontsize=18, rotate=270)
                    page.insert_text((180, 65), "자료보존文字確認", fontname="korea", fontsize=18, rotate=270)
                    page.insert_text((145, 398), f"- {number} -", fontsize=10)
                document.save(sample)
            with pymupdf.open(sample) as document:
                check("synthetic_pdf_text", all(len(text_layer(document, p).vertical_columns) == 3 for p in (1, 2)))

            work = run_dir / "pipeline"
            rc = cli.main(["run", str(sample), "--work", str(work), "--pages", "1-2", "--reader", "mock",
                           "--docx", "--mode", "vertical", "--dpi", "120", "--upscale", "1",
                           "--workers", "2", "--workers-start", "2", "--checkpoint", "0",
                           "--no-second-pass", "--no-adjudicate", "--no-suspects", "--no-context", "--no-normalize"])
            check("pipeline_exit_zero", rc in (0, None))
            hwpx_files = sorted((work / "out").glob("*.hwpx"))
            docx_files = sorted((work / "out").glob("*.docx"))
            check("document_outputs", bool(hwpx_files and docx_files))
            for path in hwpx_files + docx_files:
                with zipfile.ZipFile(path) as archive:
                    check(path.suffix[1:] + "_zip_valid", archive.testzip() is None)
            with HwpxDocument.open(hwpx_files[0]) as document:
                text = "\n".join(p.text for p in document.paragraphs)
            check("hwpx_korean_hanja", "한글" in text and "天地" in text)
            text = "\n".join(p.text for p in docx.Document(docx_files[0]).paragraphs)
            check("docx_korean_hanja", "한글" in text and "天地" in text)
            check("review_html", (work / "review.html").stat().st_size > 0)
            check("report_markdown", (work / "report.md").stat().st_size > 0)
            records = [json.loads(p.read_text(encoding="utf-8")) for p in (work / "readings").glob("p*.json")]
            check("two_page_records", sorted(r["pdf_page"] for r in records) == [1, 2])
            check("no_paid_calls", all(not (r.get("usage") or {}).get("calls", 0) for r in records))
            check("no_external_calls", not report["blocked_external_calls"])
            # gui is only imported through CLI imports; no App/config methods
            # are called. Assert the offline path did not import the GUI at all.
            check("no_gui_configuration", "yeone_ocr.gui" not in sys.modules)
            report["passed"] = all(report["checks"].values())
    except Exception:
        report["exception"] = redact_log(traceback.format_exc(), log._secrets)
    finally:
        log.close()
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        content = json.dumps(report, ensure_ascii=False, indent=2)
        (run_dir / "self-test.json").write_text(content, encoding="utf-8")
        # Stable path for build automation; complete per-run reports are kept.
        (output / "self-test.json").write_text(content, encoding="utf-8")
    _console_message(json.dumps({"passed": report["passed"], "report": str(output / "self-test.json")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--cli":
        from .runtime import ensure_standard_streams
        ensure_standard_streams()
        from .cli import main as cli_main
        return cli_main(argv[1:])
    parser = argparse.ArgumentParser(description="YeonE_OCR Windows application")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--version", action="store_true", help="print the application version and exit")
    mode.add_argument("--self-test", action="store_true", help="run an offline packaging smoke test")
    mode.add_argument("--smoke-test", type=Path, help="run the current offline packaging test and write a JSON report")
    parser.add_argument("--output", type=Path, help="self-test output directory; required with --self-test")
    args = parser.parse_args(argv)
    if args.self_test != (args.output is not None):
        parser.error("--self-test and --output DIRECTORY must be specified together")
    if args.version:
        _console_message("YeonE_OCR " + __version__)
        return 0
    if args.smoke_test:
        from .runtime import ensure_standard_streams
        ensure_standard_streams()
        from .frozen_smoke import run
        return run(args.smoke_test)
    if args.self_test:
        return _self_test(args.output)
    return _normal_gui()
