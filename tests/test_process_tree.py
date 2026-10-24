"""Real Windows workers cannot spawn a server before their cleanup job exists."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as wt
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ab_ocr import ocr, runtime


@unittest.skipUnless(sys.platform == "win32", "Windows owned process-tree boundary")
class ProcessTreeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="owned-ocr-tree-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.marker = self.root / "child.pid"
        self.started = self.root / "worker-started.txt"
        self.script = self.root / "worker.py"

    def write_worker(self, *, stay_alive=True):
        self.script.write_text(
            "import pathlib,subprocess,sys,time\n"
            "pathlib.Path(sys.argv[2]).write_text('started')\n"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
            "creationflags=subprocess.CREATE_NO_WINDOW)\n"
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n" +
            ("time.sleep(30)\n" if stay_alive else "print('complete')\n"), encoding="utf-8")
        return [sys.executable, "-B", str(self.script), str(self.marker), str(self.started)]

    def assert_exited(self, pid):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel.OpenProcess.restype = wt.HANDLE
        kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
        kernel.WaitForSingleObject.restype = wt.DWORD
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            self.assertEqual(ctypes.get_last_error(), 87)  # Process ID no longer exists.
            return
        try:
            self.assertEqual(kernel.WaitForSingleObject(handle, 3000), 0)
        finally:
            kernel.CloseHandle(handle)

    def test_delayed_job_attach_does_not_run_worker_then_cancel_kills_only_owned_tree(self):
        command = self.write_worker()
        unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(lambda: (unrelated.kill(), unrelated.wait()) if unrelated.poll() is None else None)
        own = runtime.process_tree
        observed = []

        @contextmanager
        def delayed(process, *, suspended=False):
            time.sleep(.3)  # Simulate parent descheduling before job attachment.
            self.assertFalse(self.started.exists(), "Worker ran before the cleanup job was attached")
            self.assertTrue(suspended)
            observed.append(process.pid)
            with own(process, suspended=suspended):
                yield

        with patch.object(runtime, "process_tree", delayed):
            with self.assertRaises(ocr.OCRCancelled):
                ocr._run(command, timeout=10, should_stop=self.marker.exists)
        self.assertEqual(len(observed), 1)
        self.assertTrue(self.marker.is_file())
        self.assert_exited(int(self.marker.read_text()))
        self.assertIsNone(unrelated.poll())

    def test_timeout_kills_immediately_spawned_server(self):
        command = self.write_worker()
        with self.assertRaisesRegex(ocr.OCRError, "제한 시간"):
            ocr._run(command, timeout=.8)
        self.assertTrue(self.marker.is_file())
        self.assert_exited(int(self.marker.read_text()))

    def test_success_also_closes_detached_server_owned_by_worker(self):
        command = self.write_worker(stay_alive=False)
        self.assertEqual(ocr._run(command, timeout=10).strip(), "complete")
        self.assert_exited(int(self.marker.read_text()))

    def test_resume_failure_never_runs_worker_code(self):
        command = self.write_worker()
        with patch.object(runtime, "_resume_suspended_process", side_effect=ocr.OCRError("synthetic resume failure")):
            with self.assertRaisesRegex(ocr.OCRError, "resume failure"):
                ocr._run(command, timeout=5)
        self.assertFalse(self.started.exists())
        self.assertFalse(self.marker.exists())

    def test_mock_popen_without_native_handle_remains_supported(self):
        fake = SimpleNamespace(_handle="fake_handle", pid=1234, returncode=0,
                               communicate=Mock(return_value=(b"synthetic", b"")), poll=Mock(return_value=0))
        with patch.object(subprocess, "Popen", return_value=fake), patch.object(runtime, "_resume_suspended_process") as resume:
            self.assertEqual(ocr._run(["synthetic-worker"], timeout=1), "synthetic")
        resume.assert_not_called()


if __name__ == "__main__":
    unittest.main()
