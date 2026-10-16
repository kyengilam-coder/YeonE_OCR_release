import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from ab_ocr.rapidocr_worker import atomic_output, language_route, serialize_lines, verify_models, DETECTOR, CLASSIFIER, CHINESE, CHINESE_MOBILE


class RapidOCRWorkerTests(unittest.TestCase):
    def test_unsupported_mixed_language_is_not_silently_replaced(self):
        for language in ("kor+chi_tra", "kor+chi_sim+eng", "eng", "chi_tra_vert", ""):
            with self.subTest(language=language), self.assertRaises(ValueError):
                language_route(language)
        self.assertEqual(language_route("chi_tra"), "ch")
        self.assertEqual(language_route("chi_sim+eng"), "ch")
        self.assertEqual(language_route("kor"), "korean")

    def test_raw_variant_codepoints_spacing_and_low_confidence_are_preserved(self):
        boxes = [[[8, 2], [30, 4], [29, 15], [7, 14]], [[0, 20], [10, 20], [10, 30], [0, 30]]]
        text, words = serialize_lines(boxes, ["漢  汉 金", "한글\u3000。"], [0.99, 0.01])
        self.assertEqual(text, "漢  汉 金\n한글\u3000。")
        self.assertEqual(words[0]["bbox"], [7, 2, 30, 15])
        self.assertEqual(words[1]["confidence"], 1)
        self.assertEqual(words[1]["line"], 2)

    def test_invalid_line_geometry_is_rejected(self):
        with self.assertRaises(ValueError):
            serialize_lines([], ["字"], [0.9])
        with self.assertRaises(ValueError):
            serialize_lines([[[float("nan"), 0], [10, 0], [10, 10], [0, 10]]], ["字"], [0.9])

    def test_empty_output_is_valid(self):
        self.assertEqual(serialize_lines(None, None, None), ("", []))

    def test_atomic_output_does_not_overwrite_raw_ocr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            atomic_output(path, {"text": "原文\n한글"})
            with self.assertRaises(FileExistsError):
                atomic_output(path, {"text": "changed"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["text"], "原文\n한글")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_unrecorded_model_is_not_downloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            models = path / "models"
            models.mkdir()
            (path / "installation.json").write_text(json.dumps({"engine": "rapidocr", "rapidocr_version": "3.9.2", "models": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                verify_models(models, "chi_sim")
            self.assertEqual(list(models.iterdir()), [])

    def test_exact_profile_models_are_verified_and_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            models = path / "models"
            models.mkdir()
            records = []
            for name in (DETECTOR, CLASSIFIER, CHINESE, CHINESE_MOBILE):
                data = name.encode()
                (models / name).write_bytes(data)
                records.append({"file": name, "sha256": hashlib.sha256(data).hexdigest()})
            (path / "installation.json").write_text(json.dumps({"engine": "rapidocr", "rapidocr_version": "3.9.2", "models": records}), encoding="utf-8")
            _, selected = verify_models(models, "chi_tra", "mobile")
            self.assertIn(CHINESE_MOBILE, [model["file"] for model in selected])
            self.assertNotIn(CHINESE, [model["file"] for model in selected])
            (models / CHINESE_MOBILE).write_bytes(b"modified")
            with self.assertRaises(ValueError):
                verify_models(models, "chi_tra", "mobile")


if __name__ == "__main__":
    unittest.main()
