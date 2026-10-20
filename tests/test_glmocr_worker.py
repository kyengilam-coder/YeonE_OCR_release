"""The GLM-OCR boundary must preserve raw text and own its local server."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from ab_ocr import glmocr_worker as worker


class GLMWorkerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.models = self.root / "models"
        self.runtime = self.root / "runtime"
        self.models.mkdir()
        self.runtime.mkdir()
        (self.models / "blobs").mkdir()
        (self.models / "blobs" / "model.gguf").write_bytes(b"synthetic weights")
        (self.runtime / "ollama.exe").write_bytes(b"synthetic runtime")
        self.manifest = {
            "engine": "glmocr", "model_layout": "relative-paths-v1",
            "model_id": "glm-ocr:bf16", "model_digest": "a" * 64,
            "ollama_version": "0.40.5", "models": [self.record(self.models, "blobs/model.gguf")],
            "runtime_files": [self.record(self.runtime, "ollama.exe")]}
        self.save_manifest()

    @staticmethod
    def record(directory, name):
        path = directory / name
        return {"filename": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def save_manifest(self):
        (self.root / "installation.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_nested_models_are_pinned_without_network(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            manifest, models, exe = worker.verify_installation(self.models)
        self.assertEqual(manifest["model_id"], "glm-ocr:bf16")
        self.assertEqual(models[0]["filename"], "blobs/model.gguf")
        self.assertEqual(exe, self.runtime / "ollama.exe")

    def test_changed_model_bytes_fail(self):
        (self.models / "blobs" / "model.gguf").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            worker.verify_installation(self.models)

    def test_changed_runtime_bytes_fail(self):
        (self.runtime / "ollama.exe").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            worker.verify_installation(self.models)

    def test_unpinned_runtime_library_fails(self):
        (self.runtime / "foreign.dll").write_bytes(b"unexpected")
        with self.assertRaisesRegex(ValueError, "Unrecorded"):
            worker.verify_installation(self.models)

    def test_duplicate_model_records_fail(self):
        self.manifest["models"].append(dict(self.manifest["models"][0]))
        self.save_manifest()
        with self.assertRaises(ValueError):
            worker.verify_installation(self.models)

    def test_model_path_cannot_escape_installation(self):
        (self.root / "outside.gguf").write_bytes(b"outside")
        self.manifest["models"] = [self.record(self.models, "../outside.gguf")]
        self.save_manifest()
        with self.assertRaises(ValueError):
            worker.verify_installation(self.models)

    def test_missing_runtime_identity_fails(self):
        self.manifest["runtime_files"] = []
        self.save_manifest()
        with self.assertRaises(ValueError):
            worker.verify_installation(self.models)

    def test_wrong_engine_or_layout_fails(self):
        for key, value in (("engine", "other"), ("model_layout", "flat")):
            with self.subTest(key=key):
                original = self.manifest[key]
                self.manifest[key] = value
                self.save_manifest()
                with self.assertRaises(ValueError):
                    worker.verify_installation(self.models)
                self.manifest[key] = original

    def test_raw_transcription_and_script_are_unchanged(self):
        from PIL import Image
        image = self.root / "input.png"
        Image.new("RGB", (240, 130), "white").save(image)
        instance = worker.Worker(self.models)
        text = "  乾薑 干姜 𠮷 神\r\n한글。\n"
        response = {"response": text, "done": True, "done_reason": "stop", "eval_count": 12}
        with patch.object(instance, "start") as started, patch.object(instance, "_request", side_effect=[
                response, {"models": [{"digest": "a" * 64, "size_vram": 12345}]}]) as request:
            result = instance.recognize(image, "kor+chi_tra")
        started.assert_called_once()
        self.assertEqual(result["text"], text)
        self.assertEqual(result["words"], [])
        self.assertEqual(result["image_width"], 240)
        metadata = result["engine_metadata"]
        self.assertEqual(metadata["bbox_granularity"], "none")
        self.assertTrue(metadata["generation_complete"])
        self.assertEqual(metadata["input_image_sha256"], hashlib.sha256(image.read_bytes()).hexdigest())
        payload = request.call_args_list[0].args[1]
        self.assertEqual(payload["prompt"], "Text Recognition:")
        self.assertNotIn("reference", payload)

    def test_truncated_generation_is_retained_as_incomplete(self):
        from PIL import Image
        image = self.root / "input.png"
        Image.new("RGB", (20, 20), "white").save(image)
        instance = worker.Worker(self.models, device="cpu")
        with patch.object(instance, "start"), patch.object(instance, "_request", side_effect=[
                {"response": "未完", "done": True, "done_reason": "length"}, {"models": []}]):
            result = instance.recognize(image, "chi_tra")
        self.assertEqual(result["text"], "未完")
        self.assertFalse(result["engine_metadata"]["generation_complete"])
        self.assertTrue(result["engine_metadata"]["warnings"])

    def test_cuda_without_actual_gpu_is_not_a_successful_reading(self):
        from PIL import Image
        image = self.root / "cuda-missing.png"
        Image.new("RGB", (20, 20), "white").save(image)
        instance = worker.Worker(self.models, device="cuda")
        with patch.object(instance, "start"), patch.object(instance, "_request", side_effect=[
                {"response": "原始，文字。", "done": True, "done_reason": "stop"},
                {"models": [{"digest": "a" * 64, "size_vram": 0}]}]):
            result = instance.recognize(image, "chi_tra")
        self.assertEqual(result["text"], "原始，文字。")
        self.assertFalse(result["engine_metadata"]["generation_complete"])
        self.assertFalse(result["engine_metadata"]["device_requirement_satisfied"])
        self.assertEqual(result["engine_metadata"]["device_requested"], "cuda")

    def test_input_change_during_generation_is_rejected(self):
        from PIL import Image
        image = self.root / "input.png"
        Image.new("RGB", (20, 20), "white").save(image)
        instance = worker.Worker(self.models)
        def respond(*_args, **_kwargs):
            image.write_bytes(b"changed")
            return {"response": "text", "done": True, "done_reason": "stop"}
        with patch.object(instance, "start"), patch.object(instance, "_request", side_effect=respond):
            with self.assertRaisesRegex(ValueError, "changed"):
                instance.recognize(image, "chi_sim")

    def test_nonlocal_image_is_rejected_without_server_start(self):
        instance = worker.Worker(self.models)
        with patch.object(instance, "start") as started:
            with self.assertRaises((FileNotFoundError, OSError)):
                instance.recognize(Path("https://example.invalid/private.png"), "chi_sim")
        started.assert_not_called()

    def test_close_only_terminates_owned_child(self):
        instance = worker.Worker(self.models)
        process = MagicMock()
        process.poll.return_value = None
        instance.process = process
        with patch.object(instance, "_request", side_effect=OSError("unavailable")):
            instance.close()
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        self.assertIsNone(instance.process)
        instance.close()
        process.terminate.assert_called_once()

    def test_close_kills_owned_child_if_it_does_not_exit(self):
        instance = worker.Worker(self.models)
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("synthetic", 5), None]
        instance.process = process
        with patch.object(instance, "_request", return_value={}):
            instance.close()
        process.kill.assert_called_once()

    def test_start_failure_still_closes_child_from_main(self):
        instance = MagicMock()
        instance.recognize.side_effect = RuntimeError("synthetic failure")
        with patch.object(worker, "Worker", return_value=instance), patch("sys.stderr"):
            code = worker.main(["--model-dir", str(self.models), "--input", str(self.root / "input.png"),
                                "--output", str(self.root / "output.json")])
        self.assertEqual(code, 1)
        instance.close.assert_called_once()

    def test_output_is_exclusive_and_preserves_unicode(self):
        output = self.root / "result.json"
        original = {"text": "乾薑·干姜·한글·𠮷"}
        worker.atomic_output(output, original)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), original)
        with self.assertRaises(FileExistsError):
            worker.atomic_output(output, {"text": "changed"})
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), original)


if __name__ == "__main__":
    unittest.main()
