"""설치판과 소스 실행이 공유하는 경로·프로세스 처리(외부 패키지 불필요)."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sys


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_installed_bundle() -> bool:
    """폴더형 설치판의 실행 파일 하나만 교체하면 의존성이 어긋나므로 구분한다."""
    return is_frozen() and (Path(sys.executable).resolve().parent / "_internal").is_dir()


def can_replace_executable() -> bool:
    return is_frozen() and sys.platform == "win32" and not is_installed_bundle()


def user_data_dir() -> Path:
    """설치 위치와 무관하게 현재 사용자가 쓸 수 있는 작업 폴더의 루트."""
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return root / "YeonE_OCR"


def user_work_path(path: str | Path, source: str | Path) -> Path:
    """같은 이름의 서로 다른 원본을 섞지 않으며 재실행 시 같은 폴더를 사용한다."""
    identity = os.path.normcase(str(Path(source).resolve()))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return user_data_dir() / "work" / digest / Path(path).name


def safe_output_path(path: str | Path, source: str | Path) -> Path:
    """설치판의 프로그램·번들 폴더에 결과를 쓰지 않는다. 일반 문헌 옆 경로는 보존한다."""
    path = Path(path)
    if not is_frozen():
        return path
    roots = [Path(sys.executable).resolve().parent]
    if getattr(sys, "_MEIPASS", None):
        roots.append(Path(sys._MEIPASS).resolve())
    for candidate in (path.resolve(), Path(source).resolve()):
        if any(candidate.is_relative_to(root) for root in roots):
            return user_work_path(path, source)
    return path


def ensure_standard_streams() -> None:
    """pythonw/무창 실행기의 CLI·검사 경로도 print/flush 를 안전하게 쓸 수 있게 한다."""
    for name, mode in (("stdin", "r"), ("stdout", "w"), ("stderr", "w")):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, mode, encoding="utf-8"))


@contextmanager
def external_dll_search():
    """한컴 등 바깥 프로그램이 설치판의 Python DLL 검색 경로를 상속하지 않게 한다."""
    kernel = None
    previous = ""
    if is_frozen() and sys.platform == "win32":
        import ctypes
        kernel = ctypes.windll.kernel32
        length = kernel.GetDllDirectoryW(0, None)
        buf = ctypes.create_unicode_buffer(length + 1)
        kernel.GetDllDirectoryW(len(buf), buf)
        previous = buf.value
        kernel.SetDllDirectoryW(None)
    try:
        yield
    finally:
        if kernel is not None:
            kernel.SetDllDirectoryW(previous or None)
