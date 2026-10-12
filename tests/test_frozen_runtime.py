"""설치판의 출력 경로·실행 분기·업데이트 경계를 네트워크/사용자 설정 없이 검사한다."""
import contextlib
import ctypes
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from yeone_ocr import gui, launcher, runtime, state, updater, windows_app


class FrozenRuntime(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.install = self.root / "installed"
        self.install.mkdir()
        self.exe = self.install / "YeonE_OCR.exe"
        self.exe.write_bytes(b"test")

    def frozen(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(sys, "frozen", True, create=True))
        stack.enter_context(mock.patch.object(sys, "executable", str(self.exe)))
        stack.enter_context(mock.patch.object(sys, "_MEIPASS", str(self.install / "_internal"), create=True))
        stack.enter_context(mock.patch.object(runtime, "user_data_dir", return_value=self.root / "user-data"))
        return stack

    def test_source_paths_remain_compatible(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertEqual(gui.work_dir_for("book.pdf"), Path("book_YeonE_OCR"))
            self.assertEqual(state.default_work_dir("book.pdf"), Path("work/book"))

    def test_installed_defaults_use_user_folder_and_do_not_collide(self):
        source = self.install / "한문 책.pdf"
        with self.frozen():
            gui_work = gui.work_dir_for(str(source))
            cli_work = state.default_work_dir(str(source))
            for output in (gui_work, cli_work):
                self.assertTrue(output.is_relative_to(self.root / "user-data"))
                self.assertFalse(output.is_relative_to(self.install))
            self.assertEqual(gui_work, gui.work_dir_for(str(source)))
            self.assertEqual(cli_work, state.default_work_dir(str(source)))
            self.assertNotEqual(cli_work, state.default_work_dir(str(self.root / "other" / source.name)))
            explicit = self.root / "existing-result"
            self.assertEqual(state.Work(str(source), str(explicit)).dir, explicit)

    def test_frozen_gui_retains_existing_adjacent_work_for_normal_documents(self):
        source = self.root / "books" / "book.pdf"
        with self.frozen():
            self.assertEqual(gui.work_dir_for(str(source)), source.with_name("book_YeonE_OCR"))
            # A sibling with a shared prefix is not inside the install directory.
            adjacent = self.root / "installed-other" / "book.pdf"
            self.assertEqual(gui.work_dir_for(str(adjacent)), adjacent.with_name("book_YeonE_OCR"))

    def test_bundled_images_and_corrected_hwpx_use_safe_output(self):
        from yeone_ocr.collate.run import corrected_path
        image = self.install / "page.png"
        image.write_bytes(b"image contents only used for a fingerprint")
        with self.frozen():
            self.assertTrue(gui.App._bundle_path([image]).is_relative_to(self.root / "user-data"))
            corrected = Path(corrected_path(str(self.install / "book.hwpx")))
            self.assertTrue(corrected.is_relative_to(self.root / "user-data"))
            self.assertEqual(Path(corrected_path(str(corrected))), corrected)

    def test_package_check_never_executes_frozen_executable_as_python(self):
        app = SimpleNamespace(running=False, check_packages=mock.Mock())
        with self.frozen(), mock.patch.object(gui.subprocess, "Popen") as popen:
            gui.App.install_packages(app)
        app.check_packages.assert_called_once_with()
        popen.assert_not_called()

    def test_missing_bundled_component_requests_reinstallation_without_pip(self):
        app = SimpleNamespace(running=False, v_pkg=mock.Mock(), _append=mock.Mock())
        with self.frozen(), mock.patch.object(gui.messagebox, "showerror") as error, \
             mock.patch.object(gui.messagebox, "askyesno") as ask:
            gui.App._pkg_result(app, ["opencc-python-reimplemented"])
        self.assertIn("다시 설치", error.call_args.args[1])
        ask.assert_not_called()

    def test_installed_updater_rejects_exe_replacement_before_reading_download(self):
        (self.install / "_internal").mkdir()
        with self.frozen(), mock.patch.object(updater.subprocess, "Popen") as popen:
            self.assertTrue(runtime.is_installed_bundle())
            self.assertFalse(runtime.can_replace_executable())
            with self.assertRaisesRegex(updater.UpdateError, "Setup"):
                updater.install_update(self.root / "not-downloaded.exe", self.exe, os.getpid())
        popen.assert_not_called()
        self.assertEqual(self.exe.read_bytes(), b"test")

    def test_installed_gui_update_guides_to_setup_without_download(self):
        (self.install / "_internal").mkdir()
        app = SimpleNamespace(running=False, _update_installing=False)
        info = SimpleNamespace(url="https://github.com/kyengilam-coder/YeonE_OCR_release/releases/tag/v9.0.0")
        with self.frozen(), mock.patch.object(gui.messagebox, "showinfo") as show, \
             mock.patch.object(updater, "download_update") as download:
            gui.App._start_update(app, info)
        self.assertIn("Setup", show.call_args.args[1])
        download.assert_not_called()
        with self.frozen():
            (self.install / "_internal").rmdir()
            self.assertTrue(runtime.can_replace_executable())

    def test_external_dll_directory_is_restored_even_if_open_fails(self):
        kernel = mock.Mock()
        def directory(length, buf):
            if buf is not None:
                buf.value = "C:\\bundled"
            return len("C:\\bundled")
        kernel.GetDllDirectoryW.side_effect = directory
        with self.frozen(), mock.patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel), create=True):
            with self.assertRaisesRegex(OSError, "cannot open"):
                with runtime.external_dll_search():
                    raise OSError("cannot open")
        self.assertEqual(kernel.SetDllDirectoryW.call_args_list, [mock.call(None), mock.call("C:\\bundled")])

    def test_launcher_dispatches_smoke_and_cli_without_gui(self):
        report = self.root / "smoke.json"
        fake_smoke = SimpleNamespace(run=mock.Mock(return_value=0))
        fake_cli = SimpleNamespace(main=mock.Mock(return_value=3))
        with mock.patch("multiprocessing.freeze_support"), \
             mock.patch.dict(sys.modules, {"yeone_ocr.frozen_smoke": fake_smoke, "yeone_ocr.cli": fake_cli}), \
             mock.patch.object(windows_app, "_normal_gui") as normal:
            self.assertEqual(launcher.main(["--smoke-test", str(report)]), 0)
            self.assertEqual(launcher.main(["--cli", "report", "book.pdf"]), 3)
        fake_smoke.run.assert_called_once_with(report)
        fake_cli.main.assert_called_once_with(["report", "book.pdf"])
        normal.assert_not_called()

    def test_windowless_streams_can_print_and_existing_streams_are_preserved(self):
        captured = io.StringIO()
        with mock.patch.object(sys, "stdin", None), mock.patch.object(sys, "stdout", captured), mock.patch.object(sys, "stderr", None):
            runtime.ensure_standard_streams()
            try:
                self.assertIs(sys.stdout, captured)
                self.assertEqual(sys.stdin.read(), "")
                print("diagnostic", file=sys.stderr, flush=True)
            finally:
                sys.stdin.close()
                sys.stderr.close()

    def test_logs_redact_all_provider_key_formats(self):
        keys = ["AIza" + "a" * 35, "sk-proj-" + "b" * 48, "sk-ant-api03-" + "c" * 48]
        text = windows_app.redact_log(" ".join(keys))
        self.assertTrue(all(key not in text for key in keys))
        self.assertEqual(text.count("[REDACTED]"), 3)


if __name__ == "__main__":
    unittest.main()
