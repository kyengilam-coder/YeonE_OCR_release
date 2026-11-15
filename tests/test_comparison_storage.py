"""Large comparison projects are stored gzip-compressed; small ones stay plain JSON."""
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from ab_ocr import ocr
from yeone_ocr import comparison
from yeone_ocr.comparison import ComparisonError, ComparisonProject


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.png"
        Image.new("RGB", (400, 200), "white").save(self.source)
        self.project = ComparisonProject.open(self.source)
        raw = ocr.RecognitionResult("甲乙丙" * 400, [], str(self.source), self.project.source_sha256, 1, 400, 200,
                                    "synthetic", "chi_tra", 6, 0, engine="tesseract")
        self.project.add_result(raw, request={"profile": "traditional", "direction": "horizontal", "dpi": 300,
                                                         "engine_root": None, "policy": "local-three-engine-v1"})
        self.project.set_text(1, "甲乙丁" * 400)
        self.target = self.root / "project.yeone-compare.json"

    def test_small_project_stays_plain_json(self):
        self.project.save(self.target)
        self.assertEqual(json.loads(self.target.read_bytes())["format"], comparison.FORMAT)

    def test_large_project_is_compressed_and_reopens_identically(self):
        with patch.object(comparison, "COMPRESS_ABOVE", 1000):
            self.project.save(self.target)
            data = self.target.read_bytes()
            self.assertEqual(data[:2], b"\x1f\x8b")
            self.assertLess(len(data), len(self.project._encoded()) // 5)
            self.assertEqual(json.loads(gzip.decompress(data))["document_id"], self.project._id)
            reopened = ComparisonProject.load(self.target)
            self.assertEqual(reopened.to_dict(), self.project.to_dict())
            self.project.save()                              # unchanged content → the same bytes (digest checks)
            self.assertEqual(self.target.read_bytes(), data)
            self.project.set_text(1, "乙" * 10)
            self.project.save()                              # the file this object wrote is recognised as its own
            reopened.set_text(1, "丙")
            with self.assertRaisesRegex(ComparisonError, "다른 창에서"):
                reopened.save()                               # stale copy still cannot overwrite newer work
            recovery = self.root / "recovery.yeone-compare.json"
            self.project.save_recovery(recovery)
            self.assertEqual(recovery.read_bytes()[:2], b"\x1f\x8b")
            self.assertEqual(ComparisonProject.load(recovery).text(1), "乙" * 10)

    def test_plain_project_written_earlier_still_opens(self):
        self.project.save(self.target)
        with patch.object(comparison, "COMPRESS_ABOVE", 1000):
            self.assertEqual(ComparisonProject.load(self.target).text(1), "甲乙丁" * 400)

    def test_limits_on_json_and_file_size(self):
        with patch.object(comparison, "MAX_JSON_BYTES", 1000):
            with self.assertRaisesRegex(ComparisonError, "허용 크기"):
                self.project.save(self.target)
        with patch.object(comparison, "COMPRESS_ABOVE", 1000), patch.object(comparison, "MAX_BYTES", 100):
            with self.assertRaisesRegex(ComparisonError, "허용 크기"):
                self.project.save(self.target)
        self.assertFalse(self.target.exists())

    def test_expansion_is_bounded_and_damaged_files_are_refused(self):
        bomb = self.root / "bomb.json"
        bomb.write_bytes(gzip.compress(b" " * 5_000_000, mtime=0))
        with patch.object(comparison, "MAX_JSON_BYTES", 1_000_000):
            with self.assertRaisesRegex(ComparisonError, "허용 크기"):
                ComparisonProject.load(bomb)
        with patch.object(comparison, "COMPRESS_ABOVE", 1000):
            self.project.save(self.target)
        data = self.target.read_bytes()
        for damaged in (data[:len(data) // 2], data + gzip.compress(b"{}", mtime=0), data[:20] + b"\0" * 40 + data[60:]):
            with self.subTest(size=len(damaged)):
                broken = self.root / "broken.json"
                broken.write_bytes(damaged)
                with self.assertRaises(ComparisonError):
                    ComparisonProject.load(broken)


if __name__ == "__main__":
    unittest.main()
