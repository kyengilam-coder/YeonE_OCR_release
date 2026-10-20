"""Synthetic deployment boundaries and real owned-process cancellation."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ab_ocr import local_engines, ocr, runtime


class NewLocalEngines(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def installation(self, engine="glmocr"):
        folder = self.root / "tools" / engine
        python = folder / "python/python.exe"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"synthetic interpreter; never executed")
        model = folder / "models/blobs/weights"
        model.parent.mkdir(parents=True)
        model.write_bytes(b"synthetic weights")
        binary = folder / "runtime/model-server.exe"
        binary.parent.mkdir()
        binary.write_bytes(b"synthetic runtime")
        manifest = {"engine": engine, "model_layout": "relative-paths-v1",
                    "python_executable": "python/python.exe", "runtime_root": "runtime",
                    "models": [{"filename": "blobs/weights", "sha256": ocr.file_sha256(model)}],
                    "runtime_files": [{"file": binary.name, "sha256": ocr.file_sha256(binary)}],
                    "language_models": {"kor+chi_tra": ["blobs/weights"]}}
        (folder / "installation.json").write_text(json.dumps(manifest), encoding="utf-8")
        return folder, model, binary, manifest

    def test_nested_models_and_explicit_python_are_pinned_and_runtime_change_blocks_resume(self):
        folder, model, binary, _ = self.installation()
        info = local_engines.inspect_engine("glmocr", engine_root=self.root)
        self.assertTrue(info["available"], info)
        self.assertEqual(info["languages"], ["kor+chi_tra"])
        self.assertEqual(Path(info["executable"]), folder / "python/python.exe")
        metadata = {"model_files": info["models"], "runtime_files": info["runtime_files"],
                    "installation_sha256": info["installation_sha256"], "worker_sha256": info["worker_sha256"],
                    "requested_languages": "kor+chi_tra"}
        self.assertTrue(local_engines.installed_run_unchanged("glmocr", self.root, metadata))
        for incomplete in ({"runtime_files": {}}, {**metadata, "model_files": {}},
                           {**metadata, "runtime_files": {}}, {**metadata, "requested_languages": "chi_sim"}):
            self.assertFalse(local_engines.installed_run_unchanged("glmocr", self.root, incomplete))
        binary.write_bytes(b"tampered runtime with different size")
        self.assertFalse(local_engines.inspect_engine("glmocr", engine_root=self.root)["available"])
        self.assertFalse(local_engines.installed_run_unchanged("glmocr", self.root, metadata))
        self.assertEqual(model.read_bytes(), b"synthetic weights")

    def test_manifest_paths_reject_traversal_drives_alternate_streams_and_empty_segments(self):
        for name in ("../weights", "a/../weights", "/weights", "C:/weights", "C:weights",
                     "a\\weights", "a//weights", "a/./weights", "weights:stream", "", "a/"):
            with self.subTest(name=name), self.assertRaises(ocr.OCRError):
                local_engines.installation_file(self.root, name, nested=True)
        self.assertEqual(local_engines.installation_file(self.root, "a/weights", nested=True),
                         self.root / "a/weights")
        with self.assertRaises(ocr.OCRError):
            local_engines.installation_file(self.root, "a/weights")

    def test_missing_runtime_is_unavailable_and_no_language_fallback(self):
        _, _, binary, _ = self.installation()
        binary.unlink()
        info = local_engines.inspect_engine("glmocr", engine_root=self.root)
        self.assertFalse(info["available"])
        self.assertEqual(info["languages"], [])
        source = self.root / "synthetic.png"
        source.write_bytes(b"not decoded for an unsupported language")
        with self.assertRaisesRegex(ValueError, "자동 대체"):
            local_engines.recognize_local_file(source, engine="paddle_server", languages="kor",
                                              page_number=1, dpi=300, timeout=1, should_stop=None,
                                              engine_root=self.root)

    def test_worker_boundary_preserves_variant_characters_and_absence_of_coordinates(self):
        target = self.root / "raw.json"
        original = "神 神 神\ufe00，, 汉 漢 한글\r\n"
        value = {"text": original, "words": [], "engine_version": "synthetic",
                 "engine_metadata": {"generation_complete": False, "raw_response": original}}
        target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        result = local_engines.parse_worker_result(target, 300, 200)
        self.assertEqual(result, value)
        self.assertEqual(result["text"], original)
        self.assertFalse(result["engine_metadata"]["generation_complete"])
        value["engine_metadata"]["generation_complete"] = "true"
        target.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ocr.OCRError):
            local_engines.parse_worker_result(target, 300, 200, engine="glmocr")

    def test_failed_surya_response_survives_temporary_worker_cleanup_without_becoming_text(self):
        from PIL import Image
        self.installation("surya")
        source = self.root / "synthetic.png"
        Image.new("RGB", (300, 200), "white").save(source)
        before = source.read_bytes()
        failure = {"engine": "surya", "status": "failed", "error_type": "ValueError",
                   "raw_completions": [{"text": "原文，한글 未完"}]}

        def failed_worker(argv, **_kwargs):
            output = Path(argv[argv.index("--output") + 1])
            output.with_name(output.name + ".failure.json").write_text(json.dumps(failure), encoding="utf-8")
            raise ocr.OCRError("generic worker failure")

        with patch.object(ocr, "_run", side_effect=failed_worker):
            result = local_engines.recognize_local_file(source, engine="surya", languages="kor+chi_tra",
                                                       page_number=1, dpi=300, timeout=10,
                                                       should_stop=None, engine_root=self.root)
        self.assertEqual(result.text, "")
        self.assertEqual(result.words, [])
        self.assertFalse(result.engine_metadata["generation_complete"])
        self.assertEqual(result.engine_metadata["raw_failure"], failure)
        self.assertEqual(source.read_bytes(), before)

    def test_all_engine_worker_resources_are_application_owned(self):
        self.assertEqual(runtime.worker_path("paddle_server"), runtime.worker_path("rapidocr"))
        for engine in ("glmocr", "surya"):
            self.assertTrue(runtime.worker_path(engine).is_file())
        self.assertEqual(runtime.subprocess_environment()["PYTHONDONTWRITEBYTECODE"], "1")

    def test_large_runtime_proof_stays_compact_and_still_detects_one_changed_library(self):
        import copy
        from PIL import Image
        self.installation()
        info = local_engines.inspect_engine("glmocr", engine_root=self.root)
        records = {f"runtime/library-{index}.dll": {"sha256": "a" * 64, "bytes": 100}
                   for index in range(200)}
        info["runtime_files"] = records
        info["installation"]["runtime_files"] = [{"filename": name, **value} for name, value in records.items()]
        original_manifest = copy.deepcopy(info["installation"])
        source = self.root / "synthetic.png"
        Image.new("RGB", (30, 20), "white").save(source)

        def complete_worker(argv, **_kwargs):
            output = Path(argv[argv.index("--output") + 1])
            output.write_text(json.dumps({"text": "神 神\ufe00 한글，", "words": [], "engine_version": "synthetic",
                                          "engine_metadata": {"generation_complete": True}}), encoding="utf-8")

        with patch.object(local_engines, "inspect_engine", return_value=info), \
                patch.object(ocr, "_run", side_effect=complete_worker):
            result = local_engines.recognize_local_file(source, engine="glmocr", languages="kor+chi_tra",
                                                       page_number=1, dpi=300, timeout=10,
                                                       should_stop=None, engine_root=self.root)
            metadata = result.engine_metadata
            self.assertNotIn("runtime_files", metadata)
            self.assertNotIn("runtime_files", metadata["installation"])
            self.assertEqual(metadata["runtime_inventory"]["files"], 200)
            self.assertLess(len(json.dumps(metadata)), 10_000)
            self.assertEqual(info["installation"], original_manifest)
            self.assertTrue(local_engines.installed_run_unchanged("glmocr", self.root, metadata))
            records["runtime/library-37.dll"]["sha256"] = "b" * 64
            self.assertFalse(local_engines.installed_run_unchanged("glmocr", self.root, metadata))
        self.assertEqual(result.text, "神 神\ufe00 한글，")

    @unittest.skipUnless(sys.platform == "win32", "Windows process ownership contract")
    def test_cancellation_kills_only_owned_worker_and_its_model_server(self):
        import ctypes
        from ctypes import wintypes as wt
        marker = self.root / "model-server.pid"
        script = self.root / "worker.py"
        script.write_text("import pathlib,subprocess,sys,time\n"
                          "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
                          "creationflags=subprocess.CREATE_NO_WINDOW)\n"
                          "pathlib.Path(sys.argv[1]).write_text(str(p.pid))\n"
                          "time.sleep(60)\n", encoding="utf-8")
        unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"],
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(lambda: (unrelated.kill(), unrelated.wait()) if unrelated.poll() is None else None)
        with self.assertRaises(ocr.OCRCancelled):
            ocr._run([sys.executable, "-B", str(script), str(marker)], timeout=10,
                     should_stop=marker.exists)
        self.assertIsNone(unrelated.poll())
        pid = int(marker.read_text())
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel.OpenProcess.restype = wt.HANDLE
        kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
        kernel.WaitForSingleObject.restype = wt.DWORD
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if handle:
            try:
                self.assertEqual(kernel.WaitForSingleObject(handle, 3000), 0)
            finally:
                kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
