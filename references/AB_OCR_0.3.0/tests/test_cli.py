import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ab_ocr.__main__ import main


class CLITests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def test_compare_creates_both_scores_and_provenance(self):
        reference, result, report = [self.root / name for name in ("reference.txt", "result.txt", "report.json")]
        reference.write_bytes("한 字\r\n".encode("utf-8"))
        result.write_bytes("한字\n".encode("utf-8"))
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["compare", str(reference), str(result), "--whitespace", "remove", "--output", str(report)])
        self.assertEqual(code, 0)
        value = json.loads(report.read_text(encoding="utf-8"))
        self.assertGreater(value["raw"]["cer"], 0)
        self.assertEqual(value["normalized"]["cer"], 0)
        self.assertIn("sha256", value["inputs"]["reference"])

    def test_compare_refuses_overwrite_input_and_invalid_utf8(self):
        reference, result = self.root / "truth.json", self.root / "ocr.txt"
        reference.write_bytes("漢".encode("utf-8"))
        result.write_bytes("字".encode("utf-8"))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(["compare", str(reference), str(result), "--output", str(reference)])
        self.assertEqual(code, 2)
        self.assertEqual(reference.read_bytes(), "漢".encode("utf-8"))
        result.write_bytes(b"\xff")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["compare", str(reference), str(result)]), 2)

    def test_example_benchmark_cli_and_report(self):
        fixture = Path(__file__).resolve().parents[1] / "examples" / "benchmark" / "manifest.json"
        report = self.root / "benchmark.html"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["benchmark", str(fixture), "--output", str(report)]), 0)
        self.assertIn("합성", report.read_text(encoding="utf-8"))

    def test_recognize_existing_pair_stops_before_engine_call(self):
        source, output = self.root / "image.png", self.root / "result.txt"
        source.write_bytes(b"source")
        output.with_suffix(".ocr.json").write_bytes(b"previous metadata")
        with patch("ab_ocr.ocr.recognize_file") as recognize, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["recognize", str(source), "--output", str(output)]), 2)
        recognize.assert_not_called()
        self.assertFalse(output.exists())

    def test_doctor_no_engine_is_clear_failure_code(self):
        with patch("ab_ocr.ocr.doctor", return_value={"available": False, "message": "missing"}), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["doctor"]), 2)
        self.assertIn("missing", output.getvalue())

    def test_doctor_routes_engine_and_recognize_rejects_tesseract_only_option(self):
        with patch("ab_ocr.ocr.doctor", return_value={"available": True}) as doctor, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["doctor", "--engine", "rapidocr"]), 0)
            doctor.assert_called_once_with(engine="rapidocr")
        source, output = self.root / "scan.png", self.root / "raw.txt"
        source.write_bytes(b"source")
        with patch("ab_ocr.ocr.recognize_file") as recognize, contextlib.redirect_stderr(io.StringIO()):
            code = main(["recognize", str(source), "--engine", "easyocr", "--languages", "chi_tra",
                         "--psm", "3", "--output", str(output)])
        self.assertEqual(code, 2)
        recognize.assert_not_called()

    def test_editor_default_and_project_route(self):
        with patch("ab_ocr.editor_gui.main", return_value=0) as launch:
            self.assertEqual(main([]), 0)
            launch.assert_called_once_with([])
        with patch("ab_ocr.editor_gui.main", return_value=0) as launch:
            self.assertEqual(main(["gui", "--project", "draft.abocr.json"]), 0)
            launch.assert_called_once_with(["--project", "draft.abocr.json"])

    def test_accuracy_workbench_remains_accessible(self):
        with patch("ab_ocr.gui.main", return_value=0) as launch:
            self.assertEqual(main(["evaluate-gui"]), 0)
            launch.assert_called_once_with([])


if __name__ == "__main__":
    unittest.main()
