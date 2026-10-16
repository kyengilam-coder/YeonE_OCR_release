"""Offline worker contracts, provenance, cancellation and immutable input checks."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ab_ocr import local_engines, ocr
from ab_ocr.document import DocumentProject


class LocalEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.png"
        self.source.write_bytes(b"immutable source")
        self.payload = {"text": "樂汉漢\r\n", "words": [
            {"text": "樂汉漢", "bbox": [1, 2, 40, 20], "confidence": 72.5, "block": 1, "paragraph": 1, "line": 1}],
            "engine_version": "test-version", "engine_metadata": {"normalization": "none"}}
        self.info = {"available": True, "engine": "easyocr", "languages": ["chi_tra"],
                     "executable": "test-python", "worker": "test-worker.py", "model_dir": "test-models",
                     "models": {"test.pth": {"sha256": "a" * 64}}, "language_models": {"chi_tra": ["test.pth"]},
                     "installation": {"engine": "easyocr", "source": "official"}}

    def write_worker(self, argv, **_options):
        Path(argv[argv.index("--output") + 1]).write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")
        return ""

    def recognize(self, **options):
        with patch.object(local_engines, "inspect_engine", return_value=self.info), \
                patch.object(ocr, "_render", return_value=(100, 100)), \
                patch.object(ocr, "_run", side_effect=self.write_worker):
            return ocr.recognize_file(self.source, engine="easyocr", languages="chi_tra", **options)

    def test_worker_preserves_characters_provenance_and_project_history(self):
        result = self.recognize()
        self.assertEqual(result.text, "樂汉漢\r\n")
        self.assertEqual(result.engine, "easyocr")
        self.assertIsNone(result.psm)
        self.assertFalse(result.engine_metadata["psm_applicable"])
        self.assertEqual(result.engine_metadata["model_files"]["test.pth"]["sha256"], "a" * 64)
        self.assertEqual(self.source.read_bytes(), b"immutable source")
        project = DocumentProject(self.source, ocr.file_sha256(self.source), 1, "image")
        project.apply_recognition(result)
        project.set_text(1, "사람의 교정")
        prior = result.to_dict()
        prior["engine"], prior["text"], prior["psm"] = "tesseract", "이전 판독", 3
        project.apply_recognition(prior)
        self.assertEqual(project.page(1).previous_recognitions[0]["engine"], "easyocr")
        self.assertEqual(project.text(1), "사람의 교정")
        saved = project.save(self.root / "review.abocr.json")
        with patch.object(DocumentProject, "open", return_value=DocumentProject(self.source, ocr.file_sha256(self.source), 1, "image")):
            reopened = DocumentProject.load(saved)
        self.assertEqual(reopened.page(1).previous_recognitions[0]["text"], result.text)
        self.assertEqual(reopened.page(1).raw["engine"], "tesseract")
        self.assertEqual(reopened.text(1), "사람의 교정")

    def test_unsupported_combination_and_psm_do_not_start_worker(self):
        with patch.object(ocr, "_run") as run:
            with self.assertRaisesRegex(ValueError, "언어 조합"):
                ocr.recognize_file(self.source, engine="easyocr", languages="kor+chi_tra")
            with self.assertRaisesRegex(ValueError, "PSM"):
                ocr.recognize_file(self.source, engine="easyocr", languages="chi_tra", psm=6)
            with self.assertRaisesRegex(ValueError, "Tesseract"):
                ocr.recognize_file(self.source, engine="rapidocr", languages="chi_sim", executable="unrelated")
            run.assert_not_called()

    def test_missing_engine_or_language_never_falls_back(self):
        with patch.object(local_engines, "inspect_engine", return_value={"available": False, "message": "missing local model"}), \
                patch.object(ocr, "_run") as run:
            with self.assertRaisesRegex(ocr.OCRError, "missing local model"):
                ocr.recognize_file(self.source, engine="easyocr", languages="chi_tra")
            run.assert_not_called()
        self.info["languages"] = ["chi_sim"]
        with self.assertRaisesRegex(ocr.OCRError, "설치되지"):
            self.recognize()

    def test_source_mutation_rejects_worker_result(self):
        writer = self.write_worker
        def mutate(argv, **options):
            writer(argv, **options)
            self.source.write_bytes(b"changed")
        self.write_worker = mutate
        with self.assertRaisesRegex(ocr.OCRError, "원본 파일이 변경"):
            self.recognize()

    def test_cancelled_worker_propagates_without_partial_result(self):
        def stop(*_args, **_options):
            raise ocr.OCRCancelled("cancelled")
        self.write_worker = stop
        with self.assertRaises(ocr.OCRCancelled):
            self.recognize()
        self.assertEqual(self.source.read_bytes(), b"immutable source")

    def test_worker_rejects_geometry_nonfinite_and_duplicate_json(self):
        output = self.root / "worker.json"
        for modify in (lambda p: p["words"][0].update(bbox=[0, 0, 101, 100]),
                       lambda p: p["words"][0].update(confidence=float("nan")),
                       lambda p: p["engine_metadata"].update(nonfinite=float("inf")),
                       lambda p: p.update(text=None),
                       lambda p: p["words"][0].update(line="1")):
            payload = copy.deepcopy(self.payload)
            modify(payload)
            output.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ocr.OCRError):
                local_engines.parse_worker_result(output, 100, 100)
        output.write_text('{"text":"first", "text":"second"}', encoding="utf-8")
        with self.assertRaises(ocr.OCRError):
            local_engines.parse_worker_result(output, 100, 100)

    def install_fixture(self):
        folder = self.root / "tools" / "easyocr"
        executable = folder / "venv" / "Scripts" / "python.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"test runtime")
        scripts = self.root / "ab_ocr"
        scripts.mkdir()
        (scripts / "easyocr_worker.py").write_text("# fixture", encoding="utf-8")
        models = folder / "models"
        models.mkdir()
        model = models / "test.pth"
        model.write_bytes(b"pinned test model")
        manifest = {"engine": "easyocr", "models": [{"filename": "test.pth", "sha256": ocr.file_sha256(model)}],
                    "language_models": {"chi_tra": ["test.pth"], "kor": ["missing.pth"]}}
        (folder / "installation.json").write_text(json.dumps(manifest), encoding="utf-8")
        return model

    def test_doctor_verifies_models_and_reports_only_installed_languages(self):
        model = self.install_fixture()
        with patch.object(local_engines, "PROJECT_ROOT", self.root), patch.object(ocr, "_run") as run:
            info = ocr.doctor(engine="easyocr")
            self.assertTrue(info["available"], info)
            self.assertEqual(info["languages"], ["chi_tra"])
            model.write_bytes(b"modified test model")
            rejected = ocr.doctor(engine="easyocr")
            self.assertFalse(rejected["available"])
            self.assertIn("변경", rejected["message"])
            run.assert_not_called()  # Doctor performs no downloads or inference.

    def test_doctor_cancellation_not_reported_as_missing_install(self):
        with self.assertRaises(ocr.OCRCancelled):
            ocr.doctor(engine="rapidocr", should_stop=lambda: True)


if __name__ == "__main__":
    unittest.main()
