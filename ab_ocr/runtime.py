"""Application-owned resources and explicitly selected, offline engine homes.

An engine home contains ``tools/tesseract``, ``tools/rapidocr`` and/or
``tools/easyocr`` / ``tools/ppocrv6``. It is independent of this package's location. In particular,
reusing a development installation does not import another project's Python.
"""
from __future__ import annotations

import os
import json
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

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
    from .engine_registry import ENGINES
    return any((root / "tools" / engine / "installation.json").is_file()
               for engine in ENGINES)


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
    names = {"rapidocr": "rapidocr", "easyocr": "easyocr", "paddle_server": "rapidocr",
             "glmocr": "glmocr", "surya": "surya", "ppocrv6": "ppocrv6"}
    if engine not in names:
        raise ValueError("지원하지 않는 로컬 OCR 작업자입니다.")
    return resource_root() / "ab_ocr" / f"{names[engine]}_worker.py"


def bundled_ppocrv6_root() -> Path | None:
    """The app-owned engine is independent of a saved legacy engine home.

    Return its home even if its payload is damaged: the verifier must report the
    error instead of silently using another copy of the same engine.
    """
    root = application_root()
    folder = root / "tools/ppocrv6"
    manifest = folder / "installation.json"
    if not manifest.is_file():
        return root if (folder / "runtime/python.exe").is_file() else None
    if getattr(sys, "frozen", False):
        return root
    try:
        if manifest.stat().st_size <= 1_000_000:
            data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and data.get("runtime_layout") == "standalone-python":
                return root
    except (OSError, ValueError):
        if (folder / "runtime/python.exe").is_file():
            return root
    return None


def engine_root_for(engine: str, selected_root=None) -> Path:
    """Resolve each engine; bundled v6 takes precedence over legacy settings."""
    from .engine_registry import ENGINES
    if engine not in ENGINES:
        raise ValueError("지원하지 않는 로컬 OCR 엔진입니다.")
    if engine == "ppocrv6" and (bundled := bundled_ppocrv6_root()) is not None:
        return bundled
    if selected_root or any(os.environ.get(key, "").strip() for key in ("YEONE_OCR_ENGINE_ROOT", "AB_OCR_ENGINE_ROOT")):
        return default_engine_root(selected_root)
    local, per_user = application_root(), user_engine_root()
    candidates = [local, per_user]
    if not getattr(sys, "frozen", False):
        candidates.append(local.parent / "AB_OCR")
    for root in candidates:
        if (root / "tools" / engine / "installation.json").is_file():
            return root.resolve()
    return default_engine_root()


def subprocess_environment() -> dict[str, str]:
    """Keep native DLL discovery while isolating user Python/module settings."""
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        environment.pop(name, None)
    environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                       PYTHONDONTWRITEBYTECODE="1",
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


def _resume_suspended_process(process):
    """Resume only the sole initial thread of our suspended worker process.

    CPython closes CreateProcess's primary-thread handle before Popen returns.
    The documented Toolhelp/OpenThread APIs recover that handle without resuming
    any unrelated process or relying on the undocumented NtResumeProcess call.
    """
    import ctypes
    from ctypes import wintypes as wt

    from .ocr import OCRError

    class ThreadEntry(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ThreadID", wt.DWORD), ("th32OwnerProcessID", wt.DWORD),
                    ("tpBasePri", wt.LONG), ("tpDeltaPri", wt.LONG), ("dwFlags", wt.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetProcessId.argtypes = [wt.HANDLE]
    kernel.GetProcessId.restype = wt.DWORD
    kernel.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wt.HANDLE
    for name in ("Thread32First", "Thread32Next"):
        function = getattr(kernel, name)
        function.argtypes = [wt.HANDLE, ctypes.POINTER(ThreadEntry)]
        function.restype = wt.BOOL
    kernel.OpenThread.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenThread.restype = wt.HANDLE
    kernel.ResumeThread.argtypes = [wt.HANDLE]
    kernel.ResumeThread.restype = wt.DWORD
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    kernel.CloseHandle.restype = wt.BOOL
    if (type(getattr(process, "pid", None)) is not int
            or kernel.GetProcessId(wt.HANDLE(int(process._handle))) != process.pid):
        raise OCRError("로컬 OCR 작업의 소유 프로세스를 확인하지 못했습니다.")
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if snapshot == ctypes.c_void_p(-1).value:
        raise OCRError("로컬 OCR의 대기 스레드를 확인하지 못했습니다.")
    try:
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = []
        available = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while available:
            if entry.th32OwnerProcessID == process.pid:
                found.append(entry.th32ThreadID)
            entry.dwSize = ctypes.sizeof(entry)
            available = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        if len(found) != 1:
            raise OCRError("로컬 OCR의 유일한 시작 스레드를 확인하지 못했습니다.")
        thread = kernel.OpenThread(0x0002, False, found[0])  # THREAD_SUSPEND_RESUME
        if not thread:
            raise OCRError("로컬 OCR 시작 스레드를 열지 못했습니다.")
        try:
            if kernel.ResumeThread(thread) != 1:
                raise OCRError("로컬 OCR 작업을 안전하게 시작하지 못했습니다.")
        finally:
            kernel.CloseHandle(thread)
    finally:
        kernel.CloseHandle(snapshot)


@contextmanager
def process_tree(process, *, suspended=False):
    """Own this Windows worker's descendants, including private model servers.

    A suspended worker is attached before its initial thread is resumed, so even
    a server spawned immediately at startup belongs to this job. Closing the job
    on success, cancel, timeout or termination releases only this process tree.
    """
    if sys.platform != "win32" or not isinstance(getattr(process, "_handle", None), int):
        yield
        return
    import ctypes
    from ctypes import wintypes as wt

    from .ocr import OCRError

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wt.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wt.DWORD), ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

    class IOCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IOCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    kernel.CreateJobObjectW.restype = wt.HANDLE
    kernel.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    kernel.SetInformationJobObject.restype = wt.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    kernel.AssignProcessToJobObject.restype = wt.BOOL
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    kernel.CloseHandle.restype = wt.BOOL
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise OCRError("로컬 OCR 하위 작업의 정리 장치를 만들지 못했습니다.")
    try:
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(job, wt.HANDLE(int(process._handle)))):
            raise OCRError("로컬 OCR 하위 작업을 안전하게 관리할 수 없어 실행을 중단했습니다.")
        if suspended:
            _resume_suspended_process(process)
        yield
    finally:
        kernel.CloseHandle(job)
