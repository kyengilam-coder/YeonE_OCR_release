"""Cooperative PC-wide FIFO admission for YeonE local GPU workers.

Tickets contain process identities, never document paths/text. Windows global
kernel mutexes provide exclusion even if a ticket DB is damaged. Dead owners
are removed only after process identity checks; access denial is treated alive.
Only our mutex handles/queue rows are released. No process is ever terminated.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid


def _cancel(should_stop):
    if should_stop is not None and should_stop():
        from .ocr import OCRCancelled
        raise OCRCancelled("GPU 대기를 취소했습니다.")


class _Mutex:
    def __init__(self, name):
        from ctypes import wintypes as wt
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateMutexExW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.DWORD, wt.DWORD]
        self.api.CreateMutexExW.restype = wt.HANDLE
        self.api.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
        self.api.WaitForSingleObject.restype = wt.DWORD
        self.api.ReleaseMutex.argtypes = [wt.HANDLE]
        self.api.ReleaseMutex.restype = wt.BOOL
        self.api.CloseHandle.argtypes = [wt.HANDLE]
        self.api.CloseHandle.restype = wt.BOOL
        self.api.LocalFree.argtypes = [ctypes.c_void_p]
        self.api.LocalFree.restype = ctypes.c_void_p
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wt.BOOL

        class Security(ctypes.Structure):
            _fields_ = [("length", wt.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wt.BOOL)]

        descriptor = ctypes.c_void_p()
        # Authenticated users may wait/release, but not change the object's ACL.
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                "D:P(A;;0x00100001;;;AU)(A;;GA;;;SY)(A;;GA;;;BA)", 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            attributes = Security(ctypes.sizeof(Security), descriptor, False)
            self.handle = self.api.CreateMutexExW(ctypes.byref(attributes), name, 0, 0x00100001)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.api.LocalFree(descriptor)
        self.owned = False
        self.abandoned = False

    def wait(self, milliseconds):
        result = self.api.WaitForSingleObject(self.handle, milliseconds)
        if result in (0, 0x80):
            self.owned = True
            self.abandoned = result == 0x80
            return True
        if result == 0x102:
            return False
        raise ctypes.WinError(ctypes.get_last_error())

    def release(self):
        if self.owned:
            if not self.api.ReleaseMutex(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.owned = False

    def close(self):
        if self.handle:
            try:
                self.release()
            finally:
                self.api.CloseHandle(self.handle)
                self.handle = None


def _identity(pid):
    """Return OS creation identity, False for proven dead, None for unknown."""
    if os.name != "nt":
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        except FileNotFoundError:
            return False
        except (OSError, IndexError):
            return None
    from ctypes import wintypes as wt
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    api.OpenProcess.restype = wt.HANDLE
    api.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    api.WaitForSingleObject.restype = wt.DWORD
    api.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
    api.GetProcessTimes.restype = wt.BOOL
    api.CloseHandle.argtypes = [wt.HANDLE]
    handle = api.OpenProcess(0x00101000, False, pid)
    if not handle:
        return False if ctypes.get_last_error() == 87 else None
    try:
        if api.WaitForSingleObject(handle, 0) == 0:
            return False
        values = [wt.FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
            return None
        return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
    finally:
        api.CloseHandle(handle)


def _shared_directory():
    if os.name != "nt":
        raise OSError("PC-wide GPU queue currently requires Windows")
    # A per-process environment override must not split the shared FIFO DB.
    from ctypes import wintypes as wt
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.SHGetFolderPathW.argtypes = [wt.HWND, ctypes.c_int, wt.HANDLE, wt.DWORD, wt.LPWSTR]
    shell.SHGetFolderPathW.restype = ctypes.c_long
    buffer = ctypes.create_unicode_buffer(32768)
    if shell.SHGetFolderPathW(None, 0x23, None, 0, buffer) != 0:  # CSIDL_COMMON_APPDATA
        raise OSError("The shared Windows ProgramData directory is unavailable")
    base = Path(buffer.value).resolve(strict=True)
    runtime = base / "YeonE_OCR" / "Runtime"
    folder = runtime / "GPUQueue-v1"
    _no_reparse(base / "YeonE_OCR")
    _no_reparse(runtime)
    _no_reparse(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.resolve().is_relative_to(base):
        raise OSError("GPU queue path escaped ProgramData")
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wt.BOOL
    adv.SetFileSecurityW.argtypes = [wt.LPCWSTR, wt.DWORD, ctypes.c_void_p]
    adv.SetFileSecurityW.restype = wt.BOOL
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "D:P(A;OICI;0x1301bf;;;AU)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)",
            1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not adv.SetFileSecurityW(str(folder), 4, descriptor):
            # Existing shared directory can belong to another authenticated
            # user. Its inherited modify ACL is enough; never take ownership.
            if ctypes.get_last_error() != 5:
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)
    return folder


def _no_reparse(path):
    """Never follow queue files/directories redirected by a reparse link."""
    try:
        metadata = Path(path).lstat()
    except FileNotFoundError:
        return
    if Path(path).is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise OSError("GPU queue path is a reparse link")


class GPULease:
    """A cancellation-aware FIFO ticket plus native GPU-0 lease.

    Test isolation can explicitly supply queue_dir/name. Production callers use
    the fixed defaults shared across GUI/CLI processes and Windows sessions.
    Context entry and exit must occur on the same worker thread.
    """
    def __init__(self, engine, *, should_stop=None, on_wait=None, queue_dir=None,
                 name="YeonE_OCR_GPU0_v1", timeout=None):
        if (not isinstance(engine, str) or not 1 <= len(engine) <= 32
                or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in engine)):
            raise ValueError("GPU queue engine must be a fixed engine identity")
        self.engine, self.should_stop, self.on_wait = engine, should_stop, on_wait
        self.queue_dir, self.name, self.timeout = queue_dir, name, timeout
        self.token = uuid.uuid4().hex
        self.gate = self.lease = None
        self.ticket = None
        self.connection = None
        self.thread = None
        self.wait_seconds = 0.0
        self.recovered = False

    def _guard(self):
        while not self.gate.wait(100):
            _cancel(self.should_stop)

    def _prune(self):
        for token, pid, created in self.connection.execute("SELECT token,pid,created FROM tickets"):
            actual = _identity(pid)
            if actual is False or (actual is not None and actual != created):
                self.connection.execute("DELETE FROM tickets WHERE token=?", (token,))
                self.recovered = True

    def __enter__(self):
        from .ocr import OCRError
        if self.connection is not None:
            raise OCRError("GPU 대기열 lease를 중복 사용할 수 없습니다.")
        self.thread = threading.get_ident()
        started = last_notice = time.monotonic()
        try:
            _cancel(self.should_stop)
            self.gate = _Mutex("Global\\" + self.name + "_gate")
            self.lease = _Mutex("Global\\" + self.name + "_lease")
            self._guard()
            try:
                folder = Path(self.queue_dir) if self.queue_dir is not None else _shared_directory()
                _no_reparse(folder)
                folder.mkdir(parents=True, exist_ok=True)
                for suffix in ("", "-journal", "-wal", "-shm"):
                    _no_reparse(folder / ("gpu-queue.sqlite3" + suffix))
                self.connection = sqlite3.connect(folder / "gpu-queue.sqlite3", timeout=2, isolation_level=None)
                self.connection.execute("PRAGMA busy_timeout=2000")
                self.connection.execute("CREATE TABLE IF NOT EXISTS tickets ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,token TEXT UNIQUE NOT NULL,"
                    "pid INTEGER NOT NULL,created TEXT NOT NULL,engine TEXT NOT NULL)")
                identity = _identity(os.getpid())
                if not isinstance(identity, str):
                    raise OCRError("GPU 대기 작업의 프로세스 지문을 확인하지 못했습니다.")
                self._prune()
                row = self.connection.execute("INSERT INTO tickets(token,pid,created,engine) VALUES(?,?,?,?)",
                    (self.token, os.getpid(), identity, self.engine))
                self.ticket = row.lastrowid
            finally:
                self.gate.release()
            while True:
                _cancel(self.should_stop)
                self._guard()
                try:
                    self._prune()
                    first = self.connection.execute("SELECT token FROM tickets ORDER BY id LIMIT 1").fetchone()
                    position = self.connection.execute("SELECT count(*) FROM tickets WHERE id<=?", (self.ticket,)).fetchone()[0]
                    if first and first[0] == self.token and self.lease.wait(0):
                        self.recovered |= self.lease.abandoned
                        self.wait_seconds = time.monotonic() - started
                        return self
                finally:
                    self.gate.release()
                elapsed = time.monotonic() - started
                if self.timeout is not None and elapsed >= self.timeout:
                    raise OCRError("GPU 대기 제한 시간을 넘겼습니다.")
                if self.on_wait and (last_notice == started or time.monotonic() - last_notice >= 1):
                    self.on_wait({"phase": "gpu_wait", "engine": self.engine, "device": "cuda",
                        "position": position, "elapsed_seconds": elapsed})
                    last_notice = time.monotonic()
                time.sleep(0.1)
        except BaseException as error:
            try:
                self.close()
            except (OSError, sqlite3.Error, OCRError):
                pass  # Preserve cancellation/the original failure after cleanup.
            if isinstance(error, (OSError, sqlite3.Error)):
                raise OCRError("공유 GPU 대기열을 사용할 수 없습니다. 완료 판독은 보존됩니다. "
                               "대기열 상태를 확인하세요. (" + type(error).__name__ + ")") from None
            raise

    def close(self):
        if self.thread is not None and self.thread != threading.get_ident():
            raise RuntimeError("GPU lease must be closed on its owning thread")
        try:
            if self.connection is not None:
                # Cancellation must not prevent removal of our own ticket.
                while not self.gate.wait(100):
                    pass
                try:
                    if self.lease is not None:
                        self.lease.release()
                    self.connection.execute("DELETE FROM tickets WHERE token=?", (self.token,))
                finally:
                    self.gate.release()
                    self.connection.close()
                    self.connection = None
        except (OSError, sqlite3.Error) as error:
            from .ocr import OCRError
            raise OCRError("공유 GPU 대기열 정리를 완료하지 못했습니다. "
                           "독립 대기열로 우회하지 않습니다. (" + type(error).__name__ + ")") from None
        finally:
            if self.lease is not None:
                self.lease.close()
                self.lease = None
            if self.gate is not None:
                self.gate.close()
                self.gate = None

    def __exit__(self, *_args):
        self.close()

    def evidence(self):
        return {"schema": "pc-gpu-fifo-v1", "ticket": self.ticket,
            "ticket_token_sha256": hashlib.sha256(self.token.encode("ascii")).hexdigest(),
            "wait_seconds": self.wait_seconds, "dead_owner_recovered": self.recovered,
            "gpu_index": 0, "scope": "Windows Global namespace; cooperative YeonE processes"}
