import io
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ab_ocr import ocr

TSV = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
       "5\t1\t1\t1\t1\t1\t10\t20\t30\t20\t87.5\t天地\n")


class OCRTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def test_missing_engine_is_actionable(self):
        with patch.object(ocr, "find_tesseract", return_value=None):
            info = ocr.doctor()
        self.assertFalse(info["available"])
        self.assertIn("Tesseract", info["message"])
        self.assertEqual(info["languages"], [])

    def test_missing_selected_language_never_falls_back(self):
        source = self.root / "input.png"
        source.write_bytes(b"source")
        with patch.object(ocr, "doctor", return_value={"available": True, "languages": ["eng"]}):
            with patch.object(ocr, "_render") as render:
                with self.assertRaisesRegex(ocr.OCRError, "kor"):
                    ocr.recognize_file(source, languages="kor+eng")
                render.assert_not_called()

    def test_subprocess_timeout_and_pre_cancel(self):
        with self.assertRaisesRegex(ocr.OCRError, "제한 시간"):
            ocr._run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)
        with patch("ab_ocr.ocr.subprocess.Popen") as popen:
            with self.assertRaises(ocr.OCRCancelled):
                ocr._run(["unused"], should_stop=lambda: True)
            popen.assert_not_called()

    def test_tsv_coordinates_confidence_and_no_text_normalization(self):
        words = ocr.parse_tsv(TSV, 100, 100)
        self.assertEqual(words[0]["bbox"], [10, 20, 40, 40])
        self.assertEqual(words[0]["confidence"], 87.5)
        self.assertEqual(words[0]["text"], "天地")
        with self.assertRaises(ocr.OCRError):
            ocr.parse_tsv(TSV, 15, 15)
        with self.assertRaises(ocr.OCRError):
            ocr.parse_tsv(TSV.replace("87.5", "nan"), 100, 100)

    def test_recognition_preserves_source_and_records_engine(self):
        source = self.root / "input.png"
        source.write_bytes(b"immutable-source")
        info = {"available": True, "languages": ["kor", "chi_tra"], "executable": "test-engine", "version": "test-version"}
        def run(argv, **kwargs):
            output = Path(argv[2])
            output.with_suffix(".txt").write_bytes("樂雲\r\n".encode("utf-8"))
            output.with_suffix(".tsv").write_text(TSV, encoding="utf-8")
            return ""
        with patch.object(ocr, "doctor", return_value=info), patch.object(ocr, "_render", return_value=(100, 100)), patch.object(ocr, "_run", side_effect=run):
            result = ocr.recognize_file(source, languages="kor+chi_tra")
        self.assertEqual(result.text, "樂雲\r\n")
        self.assertEqual(source.read_bytes(), b"immutable-source")
        self.assertEqual(result.engine_version, "test-version")
        self.assertEqual(result.to_dict()["coordinate_system"], "rendered-image pixels, xyxy")

    def test_source_mutation_during_recognition_rejected(self):
        source = self.root / "input.png"
        source.write_bytes(b"before")
        info = {"available": True, "languages": ["kor"], "executable": "test", "version": "test"}
        def run(argv, **kwargs):
            base = Path(argv[2])
            base.with_suffix(".txt").write_text("字", encoding="utf-8")
            base.with_suffix(".tsv").write_text(TSV, encoding="utf-8")
            source.write_bytes(b"after")
        with patch.object(ocr, "doctor", return_value=info), patch.object(ocr, "_render", return_value=(100, 100)), patch.object(ocr, "_run", side_effect=run):
            with self.assertRaisesRegex(ocr.OCRError, "원본 파일이 변경"):
                ocr.recognize_file(source, languages="kor")

    def test_real_pdf_render_is_selected_page_and_read_only(self):
        try:
            import pymupdf
            from PIL import Image
        except ImportError:
            self.skipTest("optional OCR image dependencies not installed")
        source = self.root / "two-pages.pdf"
        with pymupdf.open() as document:
            document.new_page(width=100, height=140)
            document.new_page(width=140, height=100)
            document.save(source)
        before = source.read_bytes()
        width, height = ocr._render(source, self.root / "render.png", 2, 144)
        self.assertEqual((width, height), (280, 200))
        self.assertEqual(source.read_bytes(), before)
        with self.assertRaises(ocr.OCRError):
            ocr._render(source, self.root / "invalid.png", 3, 144)

    def test_tsv_keeps_literal_quotes_and_requires_headers(self):
        self.assertEqual(ocr.parse_tsv(TSV.replace("天地", '"quoted"'), 100, 100)[0]["text"], '"quoted"')
        with self.assertRaises(ocr.OCRError):
            ocr.parse_tsv("not a TSV file", 100, 100)

    def test_doctor_cancellation_is_not_missing_engine(self):
        with self.assertRaises(ocr.OCRCancelled):
            ocr.doctor(should_stop=lambda: True)
        with patch.object(ocr, "find_tesseract", return_value=Path("engine")), patch.object(ocr, "_run", side_effect=ocr.OCRCancelled("cancel")):
            with self.assertRaises(ocr.OCRCancelled):
                ocr.doctor()

    def test_high_bit_grayscale_preserves_contrast(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is optional")
        source = self.root / "gray.tiff"
        image = Image.new("I", (3, 1))
        image.putdata([256, 16384, 32768])
        image.save(source)
        before = source.read_bytes()
        details = {}
        output = self.root / "gray.png"
        ocr._render(source, output, 1, 300, details=details)
        with Image.open(output) as converted:
            values = [converted.getpixel((i, 0))[0] for i in range(3)]
        self.assertEqual(values[0], 0)
        self.assertTrue(120 <= values[1] <= 130)
        self.assertEqual(values[2], 255)
        self.assertIn("high_bit_grayscale", details)
        self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
