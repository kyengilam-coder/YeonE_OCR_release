"""Engine homes are explicit; source code and inference remain app-owned."""
from __future__ import annotations

import hashlib
import ctypes
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch
from types import SimpleNamespace

from ab_ocr import local_engines, ocr, runtime


class LocalRuntimeTests(unittest.TestCase):
    def test_selected_home_is_authoritative_and_not_path_fallback(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True), \
                patch("ab_ocr.ocr.shutil.which", return_value="unrelated/tesseract.exe") as which:
            self.assertIsNone(ocr.find_tesseract(engine_root=folder))
            which.assert_not_called()
            self.assertEqual(runtime.default_engine_root(folder), Path(folder).resolve())

    def test_explicit_user_choice_precedes_environment_and_environment_precedes_discovery(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                "YEONE_OCR_ENGINE_ROOT": str(Path(folder) / "chosen"),
                "AB_OCR_ENGINE_ROOT": str(Path(folder) / "legacy")}, clear=True):
            self.assertEqual(runtime.default_engine_root(Path(folder) / "saved"),
                             (Path(folder) / "saved").resolve())
            self.assertEqual(runtime.default_engine_root(), (Path(folder) / "chosen").resolve())
            del os.environ["YEONE_OCR_ENGINE_ROOT"]
            self.assertEqual(runtime.default_engine_root(), (Path(folder) / "legacy").resolve())
            self.assertEqual(runtime.default_engine_root(Path(folder) / "saved"),
                             (Path(folder) / "saved").resolve())

    def test_default_development_home_and_frozen_user_home(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            local = Path(folder) / "YeonE_OCR"
            sibling = Path(folder) / "AB_OCR"
            engine = sibling / "tools/easyocr"
            engine.mkdir(parents=True)
            (engine / "installation.json").write_text("{}", encoding="utf-8")
            per_user = Path(folder) / "user-engines"
            with patch.object(runtime, "application_root", return_value=local), \
                    patch.object(runtime, "user_engine_root", return_value=per_user), \
                    patch.object(runtime.sys, "frozen", False, create=True):
                self.assertEqual(runtime.default_engine_root(), sibling.resolve())
            with patch.object(runtime, "application_root", return_value=local), \
                    patch.object(runtime, "user_engine_root", return_value=per_user), \
                    patch.object(runtime.sys, "frozen", True, create=True):
                self.assertEqual(runtime.default_engine_root(), per_user)

    def test_frozen_worker_comes_from_application_resources(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(runtime.sys, "frozen", True, create=True), \
                patch.object(runtime.sys, "_MEIPASS", folder, create=True):
            self.assertEqual(runtime.worker_path("easyocr"),
                             Path(folder).resolve() / "ab_ocr/easyocr_worker.py")

    def test_model_home_reuse_keeps_vendored_worker_and_detects_changed_model(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root = Path(folder)
            installation = root / "tools/easyocr"
            executable = installation / "venv/Scripts/python.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"not executed during inspection")
            model = installation / "models/test.pth"
            model.parent.mkdir()
            model.write_bytes(b"public synthetic test weights")
            manifest = {"engine": "easyocr", "models": [{"filename": model.name,
                        "sha256": hashlib.sha256(model.read_bytes()).hexdigest()}],
                        "language_models": {"chi_tra": [model.name]}}
            (installation / "installation.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = ocr.doctor(engine="easyocr", engine_root=root)
            self.assertTrue(result["available"], result)
            self.assertEqual(result["languages"], ["chi_tra"])
            self.assertEqual(Path(result["worker"]), runtime.worker_path("easyocr"))
            self.assertNotEqual(Path(result["worker"]).parent.parent, root)
            model.write_bytes(b"changed model bytes")
            result = ocr.doctor(engine="easyocr", engine_root=root)
            self.assertFalse(result["available"])
            self.assertIn("변경", result["message"])

    def test_engine_root_is_forwarded_without_environment_mutation(self):
        before = dict(os.environ)
        with patch.object(local_engines, "recognize_local_file", return_value="result") as recognize:
            self.assertEqual(ocr.recognize_file("not opened", engine="easyocr", languages="chi_tra",
                                              engine_root="chosen-home"), "result")
        self.assertEqual(recognize.call_args.kwargs["engine_root"], "chosen-home")
        self.assertEqual(dict(os.environ), before)

    def test_subprocess_environment_disables_telemetry_without_mutating_parent(self):
        with patch.dict(os.environ, {"PYTHONPATH": "unrelated-source", "PYTHONHOME": "unrelated-runtime",
                                     "PATH": "native-dll-search"}, clear=True):
            before = dict(os.environ)
            environment = runtime.subprocess_environment()
            self.assertNotIn("PYTHONPATH", environment)
            self.assertNotIn("PYTHONHOME", environment)
            self.assertEqual(environment["PATH"], "native-dll-search")
            self.assertEqual(environment["HF_HUB_OFFLINE"], "1")
            self.assertEqual(dict(os.environ), before)

    def test_frozen_external_dll_search_is_restored_on_failure(self):
        kernel = Mock()
        def get_directory(_length, buffer):
            if buffer is not None:
                buffer.value = "C:\\app-bundle"
            return len("C:\\app-bundle")
        kernel.GetDllDirectoryW.side_effect = get_directory
        with patch.object(runtime.sys, "frozen", True, create=True), \
                patch.object(runtime.sys, "platform", "win32"), \
                patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel), create=True):
            with self.assertRaisesRegex(OSError, "spawn failed"):
                with runtime.external_process_search():
                    raise OSError("spawn failed")
        self.assertEqual(kernel.SetDllDirectoryW.call_args_list,
                         [call(None), call("C:\\app-bundle")])

    def test_child_omp_threads_are_bounded_and_respect_a_smaller_user_limit(self):
        with patch.object(runtime.os, "cpu_count", return_value=64), \
                patch.dict(os.environ, {"OMP_THREAD_LIMIT": "64"}, clear=True):
            self.assertEqual(runtime.subprocess_environment()["OMP_THREAD_LIMIT"], "8")
            self.assertEqual(os.environ["OMP_THREAD_LIMIT"], "64")
            os.environ["OMP_THREAD_LIMIT"] = "2"
            self.assertEqual(runtime.subprocess_environment()["OMP_THREAD_LIMIT"], "2")


if __name__ == "__main__":
    unittest.main()
