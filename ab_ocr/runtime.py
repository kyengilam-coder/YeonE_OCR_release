"""Application-owned resources and explicitly selected, offline engine homes.

An engine home contains ``tools/tesseract``, ``tools/rapidocr`` and/or
``tools/easyocr``. It is independent of this package's location. In particular,
reusing a development installation does not import another project's Python.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
import sys
import threading

_SPAWN_LOCK = threading.RLock()


def resource_root() -> Path:
    """Root containing allowlisted worker scripts, including frozen installs."""
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS).resolve()
    return Path(__file__).resolve().parent.parent


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def user_engine_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return (base / "YeonE_OCR" / "local-engines").resolve()


def _has_engine_home(root: Path) -> bool:
    return any((root / "tools" / engine / "installation.json").is_file()
               for engine in ("tesseract", "rapidocr", "easyocr"))


def default_engine_root(selected_root: str | Path | None = None) -> Path:
    """Resolve one engine home without modifying process-wide environment.

    A configured path is authoritative even when missing. Never silently select
    another installation after a configured engine/model fails verification.
    The sibling AB_OCR candidate only reuses existing, verified engine files on
    developer machines; installed builds have no such sibling dependency.
    """
    if selected_root and str(selected_root).strip():
        return Path(selected_root).expanduser().resolve()
    for variable in ("YEONE_OCR_ENGINE_ROOT", "AB_OCR_ENGINE_ROOT"):
        if configured := os.environ.get(variable, "").strip().strip('"'):
            return Path(configured).expanduser().resolve()
    local = application_root()
    if _has_engine_home(local):
        return local
    per_user = user_engine_root()
    if _has_engine_home(per_user):
        return per_user
    if not getattr(sys, "frozen", False):
        sibling = local.parent / "AB_OCR"
        if sibling != local and _has_engine_home(sibling):
            return sibling.resolve()
    return per_user


def worker_path(engine: str) -> Path:
    if engine not in ("rapidocr", "easyocr"):
        raise ValueError("지원하지 않는 로컬 OCR 작업자입니다.")
    return resource_root() / "ab_ocr" / f"{engine}_worker.py"


def subprocess_environment() -> dict[str, str]:
    """Keep native DLL discovery while isolating user Python/module settings."""
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        environment.pop(name, None)
    environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                       HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                       DO_NOT_TRACK="1", ANONYMIZED_TELEMETRY="False")
    thread_limit = min(8, os.cpu_count() or 1)
    try:
        configured_limit = int(environment.get("OMP_THREAD_LIMIT", thread_limit))
        if configured_limit > 0:
            thread_limit = min(thread_limit, configured_limit)
    except ValueError:
        pass
    environment["OMP_THREAD_LIMIT"] = str(thread_limit)
    if getattr(sys, "frozen", False):
        bundled = resource_root()
        environment["PATH"] = os.pathsep.join(
            value for value in environment.get("PATH", "").split(os.pathsep)
            if value and not Path(value).resolve().is_relative_to(bundled))
        if "LD_LIBRARY_PATH_ORIG" in environment:
            environment["LD_LIBRARY_PATH"] = environment["LD_LIBRARY_PATH_ORIG"]
        else:
            environment.pop("LD_LIBRARY_PATH", None)
    return environment


@contextmanager
def external_process_search():
    """External Python must not inherit the frozen app's private DLL directory.

    The native search change lasts only for process creation, never inference.
    Local engine spawns serialize this tiny section and restore it on errors.
    """
    with _SPAWN_LOCK:
        kernel, previous = None, ""
        if getattr(sys, "frozen", False) and sys.platform == "win32":
            import ctypes
            kernel = ctypes.windll.kernel32
            length = kernel.GetDllDirectoryW(0, None)
            buffer = ctypes.create_unicode_buffer(length + 1)
            kernel.GetDllDirectoryW(len(buffer), buffer)
            previous = buffer.value
            kernel.SetDllDirectoryW(None)
        try:
            yield
        finally:
            if kernel is not None:
                kernel.SetDllDirectoryW(previous or None)
