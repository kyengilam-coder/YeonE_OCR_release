from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ab_ocr.easyocr_worker import EasyOCRWorker, convert_results, language_config, verify_models, write_result


class EasyOCRWorkerTests(unittest.TestCase):
    def test_text_and_unicode_remain_unchanged_and_geometry_converted(self):
        raw = [( [[12, 4], [37, 5], [36, 18], [11, 17]], "藥药 金·김", 0.75),
               ( [[2, 20], [5, 20], [5, 23], [2, 23]], "\u1100\u1161", 0.25)]
        text, words = convert_results(raw)
        self.assertEqual(text, "藥药 金·김\n\u1100\u1161")
        self.assertEqual(words[0]["bbox"], [11.0, 4.0, 37.0, 18.0])
        self.assertEqual(words[0]["confidence"], 75.0)
        self.assertEqual(words[1]["line"], 2)

    def test_mixed_language_rejected_without_silent_fallback(self):
        with self.assertRaises(ValueError):
            language_config("kor+chi_tra")

    def test_clipping_preserves_raw_geometry_and_text(self):
        text, words = convert_results([([[-1, -2], [15, -2], [15, 22], [-1, 22]], "藥", 0.5)], (10, 20))
        self.assertEqual(text, "藥")
        self.assertEqual(words[0]["bbox"], [0.0, 0.0, 10.0, 20.0])
        self.assertEqual(words[0]["original_bbox"], [-1.0, -2.0, 15.0, 22.0])

    def test_model_tampering_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            models = root / "models"
            models.mkdir()
            records = []
            for filename in ("craft_mlt_25k.pth", "korean_g2.pth"):
                (models / filename).write_bytes(b"synthetic model")
                records.append({"filename": filename,
                                "sha256": hashlib.sha256(b"synthetic model").hexdigest()})
            (root / "installation.json").write_text(json.dumps({"engine": "easyocr", "models": records}), encoding="utf-8")
            self.assertEqual(len(verify_models(models, "kor")), 2)
            (models / "korean_g2.pth").write_bytes(b"changed")
            with self.assertRaises(ValueError):
                verify_models(models, "kor")

    def test_atomic_json_preserves_raw_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            result = {"text": "藥药 金·김\n\u1100\u1161"}
            write_result(path, result)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), result)
            self.assertEqual(len(list(path.parent.iterdir())), 1)

    def test_reader_explicitly_disables_downloads(self):
        reader_factory = Mock(return_value=object())
        fake_torch = SimpleNamespace(set_num_threads=Mock())
        fake_easyocr = SimpleNamespace(Reader=reader_factory)
        with patch.dict(sys.modules, {"torch": fake_torch, "easyocr": fake_easyocr}), \
             patch.dict(os.environ), \
             patch("ab_ocr.easyocr_worker.verify_models", return_value=[]):
            worker = EasyOCRWorker(Path("synthetic-models"))
            first = worker.reader("chi_sim")
            self.assertIs(worker.reader("chi_sim"), first)
        self.assertEqual(reader_factory.call_count, 1)
        self.assertFalse(reader_factory.call_args.kwargs["download_enabled"])
        self.assertFalse(reader_factory.call_args.kwargs["gpu"])

    def test_failed_atomic_replace_preserves_previous_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            write_result(path, {"text": "原文"})
            with patch("ab_ocr.easyocr_worker.os.replace", side_effect=OSError("synthetic failure")):
                with self.assertRaises(OSError):
                    write_result(path, {"text": "교정"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["text"], "原文")
            self.assertEqual(len(list(path.parent.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
