import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ab_ocr.evaluation import compare_texts
from ab_ocr.reports import atomic_write, read_utf8, render_html, write_report, write_recognition
from ab_ocr import reports


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def test_input_protection_includes_paths_inside_report(self):
        source = self.root / "original.json"
        source.write_text("unchanged", encoding="utf-8")
        result = compare_texts("雲", "云")
        result["protected_input_paths"] = [str(source)]
        with self.assertRaises(ValueError):
            write_report(result, source)
        self.assertEqual(source.read_text(encoding="utf-8"), "unchanged")

    def test_atomic_failure_preserves_previous_report(self):
        output = self.root / "result.json"
        output.write_bytes(b"previous valid report")
        with patch("ab_ocr.reports.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                atomic_write(output, "new")
        self.assertEqual(output.read_bytes(), b"previous valid report")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["result.json"])

    def test_html_escapes_ocr_content_and_metadata(self):
        report = compare_texts("<script>alert(1)</script>", "<img src=x>")
        report["name"] = "</pre><script>bad()</script>"
        rendered = render_html(report)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<img", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_json_roundtrip_empty_reference_is_null(self):
        path = self.root / "result.json"
        write_report(compare_texts("", "한자"), path)
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsNone(report["raw"]["cer"])
        self.assertEqual(report["raw"]["insertions"], 2)

    def test_csv_and_html_contain_actual_counts(self):
        report = compare_texts("天地玄黃", "天地黃")
        csv_path = write_report(report, self.root / "result.csv")
        self.assertIn("0,1,0,1,0.25", csv_path.read_text(encoding="utf-8-sig"))
        self.assertIn("25.00%", render_html(report))

    def test_read_utf8_preserves_line_endings_removes_transport_bom(self):
        path = self.root / "text.txt"
        path.write_bytes(b"\xef\xbb\xbf" + "한\r\n字".encode("utf-8"))
        self.assertEqual(read_utf8(path), "한\r\n字")
        with self.assertRaises(ValueError):
            read_utf8(path, max_bytes=2)

    def test_csv_records_normalization_policy(self):
        result = compare_texts("한 字", "한字", policy={"whitespace": "remove"})
        path = write_report(result, self.root / "policy.csv")
        self.assertIn("remove", path.read_text(encoding="utf-8-sig"))

    def test_recognition_pair_rolls_back_on_metadata_failure(self):
        source = self.root / "image.png"
        source.write_bytes(b"original")
        output = self.root / "output.txt"
        result = {"source_path": str(source), "text": "樂\r\n雲"}
        real_link = reports._publish_new
        count = 0
        def failing_link(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("metadata publish failed")
            real_link(*args)
        with patch("ab_ocr.reports._publish_new", side_effect=failing_link):
            with self.assertRaises(OSError):
                write_recognition(result, output)
        self.assertFalse(output.exists())
        self.assertFalse(output.with_suffix(".ocr.json").exists())
        self.assertEqual(source.read_bytes(), b"original")
        write_recognition(result, output)
        self.assertEqual(output.read_bytes(), "樂\r\n雲".encode("utf-8"))
        with self.assertRaises(ValueError):
            write_recognition(result, output)


if __name__ == "__main__":
    unittest.main()
