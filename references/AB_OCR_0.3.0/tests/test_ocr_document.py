"""Read-only source inspection/rendering and recorded local-engine identity."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ab_ocr import ocr


class OCRDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="abocr-document-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_multipage_tiff_selection_is_detached_and_read_only(self):
        from PIL import Image
        source = self.root / "pages.tiff"
        first, second = Image.new("RGB", (32, 48), "red"), Image.new("RGB", (40, 24), "blue")
        first.save(source, save_all=True, append_images=[second])
        before = source.read_bytes()
        info = ocr.document_info(source)
        self.assertEqual(info["page_count"], 2)
        self.assertEqual(info["source_sha256"], hashlib.sha256(before).hexdigest())
        image = ocr.render_page(source, page_number=2)
        self.addCleanup(image.close)
        self.assertEqual(image.size, (40, 24))
        self.assertEqual(image.getpixel((0, 0)), (0, 0, 255))
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(source.read_bytes(), before)

    def test_rotated_pdf_preview_matches_recognition_render(self):
        import pymupdf
        from PIL import Image
        source = self.root / "rotated.pdf"
        with pymupdf.open() as document:
            document.new_page(width=100, height=200)
            page = document.new_page(width=160, height=240)
            page.draw_rect(pymupdf.Rect(15, 20, 80, 100), fill=(0, 0, 0))
            page.set_rotation(90)
            document.save(source)
        self.assertEqual(ocr.document_info(source)["page_count"], 2)
        expected = self.root / "expected.png"
        self.assertEqual(ocr._render(source, expected, 2, 144), (480, 320))
        with Image.open(expected) as baseline, ocr.render_page(source, page_number=2, dpi=144) as image:
            self.assertEqual(image.size, baseline.size)
            self.assertEqual(image.tobytes(), baseline.tobytes())

    def test_info_rejects_source_mutation_and_pre_cancel(self):
        from PIL import Image
        source = self.root / "source.png"
        Image.new("RGB", (20, 20), "white").save(source)
        with patch.object(ocr, "file_sha256", side_effect=["before", "after"]):
            with self.assertRaisesRegex(ocr.OCRError, "변경"):
                ocr.document_info(source)
        with self.assertRaises(ocr.OCRCancelled):
            ocr.document_info(source, should_stop=lambda: True)
        with self.assertRaises(ocr.OCRCancelled):
            ocr.render_page(source, should_stop=lambda: True)

    def test_image_exif_orientation_is_shared_by_preview_and_ocr(self):
        from PIL import Image
        source = self.root / "rotated.jpg"
        image = Image.new("RGB", (20, 40), "white")
        exif = Image.Exif()
        exif[274] = 6
        image.save(source, exif=exif)
        with ocr.render_page(source) as preview:
            self.assertEqual(preview.size, (40, 20))
            self.assertEqual(preview.info["ab_ocr_preprocessing"]["exif_orientation"], 6)

    def test_installed_models_have_independent_hashes_in_result_metadata(self):
        engine = self.root / "tesseract.exe"
        engine.write_bytes(b"test-runtime")
        data = self.root / "tessdata"
        data.mkdir()
        (data / "kor.traineddata").write_bytes(b"korean-model")
        (self.root / "installation.json").write_text(json.dumps({"models_family": "tessdata_best", "models_commit": "fixture"}), encoding="utf-8")
        metadata = ocr._engine_metadata({"executable": str(engine), "tessdata_dir": str(data)}, ["kor"])
        self.assertEqual(metadata["executable_sha256"], hashlib.sha256(b"test-runtime").hexdigest())
        self.assertEqual(metadata["model_files"]["kor"]["sha256"], hashlib.sha256(b"korean-model").hexdigest())
        self.assertEqual(metadata["installation"]["models_family"], "tessdata_best")
        self.assertEqual(ocr._data_options(executable=engine), ["--tessdata-dir", str(data)])

    def test_tesseract_zero_area_and_overflow_boxes_preserve_valid_geometry(self):
        header = "level\tleft\ttop\twidth\theight\tconf\ttext\tblock_num\tpar_num\tline_num\n"
        rows = ["5\t0\t0\t0\t100\t95\t문\t1\t1\t1\n",
                "5\t-3\t10\t30\t20\t80\t字\t1\t1\t1\n",
                "5\t40\t30\t10\t20\t90\t한\t1\t1\t1\n"]
        warnings = []
        words = ocr.parse_tsv(header + "".join(rows), 100, 100, strict=False, warnings=warnings)
        self.assertEqual(len(words), 2)
        self.assertEqual(words[0]["bbox"], [0, 10, 27, 30])
        self.assertEqual(words[0]["original_bbox"], [-3, 10, 27, 30])
        self.assertEqual([warning["kind"] for warning in warnings], ["omitted_box", "clipped_box"])
        with self.assertRaisesRegex(ocr.OCRError, "모든"):
            ocr.parse_tsv(header + rows[0], 100, 100, strict=False)

    def test_geometry_repair_does_not_reconstruct_or_drop_raw_text(self):
        source = self.root / "source.png"
        source.write_bytes(b"source")
        raw = "영면적문자 字\r\n"
        header = "level\tleft\ttop\twidth\theight\tconf\ttext\tblock_num\tpar_num\tline_num\n"
        tsv = header + "5\t0\t0\t0\t100\t95\t영면적문자\t1\t1\t1\n5\t10\t20\t20\t20\t80\t字\t1\t1\t1\n"
        def run(argv, **kwargs):
            self.assertIn("tessedit_create_txt=1", argv)
            self.assertIn("tessedit_create_tsv=1", argv)
            Path(argv[2]).with_suffix(".txt").write_bytes(raw.encode("utf-8"))
            Path(argv[2]).with_suffix(".tsv").write_bytes(tsv.encode("utf-8"))
        info = {"available": True, "languages": ["kor"], "executable": "fake", "version": "fixture"}
        with patch.object(ocr, "doctor", return_value=info), patch.object(ocr, "_render", return_value=(100, 100)), patch.object(ocr, "_run", side_effect=run):
            result = ocr.recognize_file(source, languages="kor")
        self.assertEqual(result.text, raw)
        self.assertEqual(result.preprocessing["omitted_word_boxes"], 1)
        self.assertTrue(result.preprocessing["raw_text_preserved"])


if __name__ == "__main__":
    unittest.main()
