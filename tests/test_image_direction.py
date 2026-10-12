"""0.7.4: raster-only reading direction, without APIs or platform fonts."""
import io
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageOps

from yeone_ocr.direction import image_direction


def printed_image(direction="horizontal_zh", *, korean=False, lines=6,
                  size=(780, 620), font_size=20, line_gap=42, char_gap=23):
    """Actual CJK font outlines, rasterized so the fixture has no text layer."""
    text = ("한글 문장을 가로로 읽고 중국 한자도 함께 확인합니다" if korean else
            "天地玄黃宇宙洪荒日月盈昃辰宿列張寒來暑往秋收冬藏")
    with pymupdf.open() as doc:
        page = doc.new_page(width=size[0], height=size[1])
        for n in range(lines):
            if direction == "horizontal_zh":
                page.insert_text((30, 45 + n * line_gap), text,
                                 fontname="korea" if korean else "china-t", fontsize=font_size)
            else:
                for j, char in enumerate(text):
                    page.insert_text((size[0] - 65 - n * line_gap, 40 + j * char_gap), char,
                                     fontname="china-t", fontsize=font_size)
        pix = page.get_pixmap()
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def raster_pdf(images):
    doc = pymupdf.open()
    for image in images:
        raw = io.BytesIO()
        image.save(raw, format="PNG")
        page = doc.new_page(width=image.width, height=image.height)
        page.insert_image(page.rect, stream=raw.getvalue())
    return doc


class RasterDirection(unittest.TestCase):
    def test_real_horizontal_korean_and_chinese_fonts(self):
        for korean in (False, True):
            for size in ((780, 620), (780, 1100)):
                with self.subTest(korean=korean, size=size):
                    evidence = image_direction(printed_image(korean=korean, size=size))
                    self.assertEqual(evidence.mode, "horizontal_zh", evidence)

    def test_upright_vertical_cjk_does_not_follow_page_shape(self):
        for size in ((780, 620), (1050, 620)):
            with self.subTest(size=size):
                evidence = image_direction(printed_image("vertical", size=size))
                self.assertEqual(evidence.mode, "vertical", evidence)

    def test_dark_screenshots_and_different_text_sizes(self):
        for direction in ("horizontal_zh", "vertical"):
            for ratio in (0.65, 1, 2.5):
                with self.subTest(direction=direction, ratio=ratio):
                    img = ImageOps.invert(printed_image(direction))
                    img = img.resize((round(img.width * ratio), round(img.height * ratio)))
                    evidence = image_direction(img)
                    self.assertEqual(evidence.mode, direction, evidence)

    def test_single_line_and_small_screenshot(self):
        img = printed_image(lines=1, korean=True).crop((20, 20, 590, 65))
        self.assertEqual(image_direction(img).mode, "horizontal_zh")

    def test_tight_horizontal_leading_and_mild_scan_skew(self):
        for gap in (24, 28):
            with self.subTest(gap=gap):
                evidence = image_direction(printed_image(line_gap=gap))
                self.assertEqual(evidence.mode, "horizontal_zh", evidence)
        for direction in ("horizontal_zh", "vertical"):
            for degrees in (-2, 2):
                with self.subTest(direction=direction, degrees=degrees):
                    img = printed_image(direction).rotate(degrees, fillcolor="white")
                    self.assertEqual(image_direction(img).mode, direction)

    def test_blank_picture_and_noise_do_not_claim_text_direction(self):
        diagram = Image.new("RGB", (780, 620), "white")
        draw = ImageDraw.Draw(diagram)
        draw.rectangle((30, 25, 745, 595), outline="black", width=3)
        draw.ellipse((60, 60, 400, 400), fill="gray", outline="black", width=5)
        for y in range(450, 580, 30):
            draw.line((60, y, 700, y), fill="black", width=3)
        noise = Image.fromarray(np.random.default_rng(42).integers(0, 256, (620, 780), dtype=np.uint8))
        for img in (Image.new("RGB", (780, 620), "white"),
                    Image.new("RGB", (780, 620), "black"), diagram, noise):
            with self.subTest(image=img):
                self.assertIsNone(image_direction(img).mode)

    def test_equal_spacing_square_character_grid_is_ambiguous(self):
        # Both axes genuinely contain aligned glyphs: do not guess from page shape.
        img = printed_image("vertical", lines=12, size=(620, 620), line_gap=23)
        self.assertIsNone(image_direction(img).mode)


class DocumentDirection(unittest.TestCase):
    def tearDown(self):
        from yeone_ocr.cli import apply_mode
        apply_mode(Namespace(mode="vertical"))

    def test_png_image_directory_and_image_bundle_pdf(self):
        from yeone_ocr.cli import detect_mode
        from yeone_ocr.pdfio import images_to_pdf, open_pdf
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "images"
            folder.mkdir()
            paths = []
            for n in (1, 2):
                path = folder / f"screenshot{n}.png"
                printed_image(korean=True).save(path)
                paths.append(path)
            bundle = Path(td) / "bundle.pdf"
            bundle.write_bytes(images_to_pdf(paths))
            for source in (paths[0], folder, bundle):
                with self.subTest(source=source), open_pdf(source) as doc:
                    self.assertFalse(doc[0].get_text().strip())
                    mode, why = detect_mode(doc)
                    self.assertEqual(mode, "horizontal_zh", why)
                    self.assertIn("이미지 글줄", why)

    def test_scanned_vertical_pdf_and_dark_horizontal_pdf(self):
        from yeone_ocr.cli import detect_mode
        for expected, image in (("vertical", printed_image("vertical")),
                                ("horizontal_zh", ImageOps.invert(printed_image(korean=True)))):
            with self.subTest(expected=expected), raster_pdf([image]) as doc:
                self.assertEqual(detect_mode(doc)[0], expected)

    def test_sufficient_text_layer_takes_priority_without_image_analysis(self):
        from yeone_ocr.cli import detect_mode
        for expected, rotation in (("horizontal_zh", 0), ("vertical", 270)):
            with self.subTest(expected=expected), raster_pdf([printed_image()]) as doc:
                for n in range(3):
                    doc[0].insert_text((80 + n * 30, 80), "天地玄黃宇宙洪荒日月盈昃辰宿列張",
                                       fontname="china-t", fontsize=12, rotate=rotation)
                with patch("yeone_ocr.cli.page_image", side_effect=AssertionError("no raster analysis")) as render:
                    mode, why = detect_mode(doc)
                render.assert_not_called()
                self.assertEqual(mode, expected)
                self.assertIn("글자층 표본", why)

    def test_blank_pages_do_not_outvote_actual_text(self):
        from yeone_ocr.cli import detect_mode
        images = [Image.new("RGB", (780, 620), "white"), printed_image(),
                  Image.new("RGB", (780, 620), "white")]
        with raster_pdf(images) as doc:
            self.assertEqual(detect_mode(doc)[0], "horizontal_zh")

    def test_conflicting_or_unreadable_samples_explicitly_fall_back(self):
        from yeone_ocr.cli import _detect_mode
        with raster_pdf([printed_image(), printed_image("vertical")]) as doc:
            mode, why, source = _detect_mode(doc)
            self.assertEqual((mode, source), ("vertical", "fallback"))
            self.assertIn("서로 달라", why)
            with patch("yeone_ocr.cli.page_image", side_effect=OSError("bad sample")):
                mode, why, source = _detect_mode(doc)
            self.assertEqual((mode, source), ("vertical", "fallback"))
            self.assertIn("분석 실패 2쪽", why)

    def test_legacy_vertical_cache_updates_but_preserves_all_ocr_files_and_statuses(self):
        from yeone_ocr.cli import partial_path, resolve_mode
        from yeone_ocr.direction import DETECTION_VERSION
        from yeone_ocr.state import Work
        with tempfile.TemporaryDirectory() as td, raster_pdf([printed_image()]) as doc:
            work = Work(Path(td) / "input.pdf", Path(td) / "work")
            pages = {"1": {"statuses": ["원본 대조 완료(사람)"], "notes": ["keep"]}}
            work.save_state({"mode": "vertical", "pages": pages, "custom": "preserve"})
            files = [work.record_path(1), partial_path(work, 2)]
            for path in files:
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b'{"mode":"vertical","sentinel":"keep-original-bytes"}')
            before = [p.read_bytes() for p in files]
            with patch("yeone_ocr.cli.create_reader", side_effect=AssertionError("no paid API")), \
                    patch("yeone_ocr.cli.log") as log:
                self.assertEqual(resolve_mode(Namespace(mode="auto"), doc, work), "horizontal_zh")
            state = work.load_state()
            self.assertEqual(state["mode_detection"]["source"], "image_geometry")
            self.assertEqual(state["mode_detection"]["version"], DETECTION_VERSION)
            self.assertEqual(state["pages"], pages)
            self.assertEqual(state["custom"], "preserve")
            self.assertEqual([p.read_bytes() for p in files], before)
            self.assertTrue(any("완료 쪽을 자동으로 다시 읽지" in str(c) for c in log.call_args_list))
            with patch("yeone_ocr.cli._detect_mode", side_effect=AssertionError("must reuse cache")):
                self.assertEqual(resolve_mode(Namespace(mode="auto"), doc, work), "horizontal_zh")

    def test_explicit_direction_is_respected_and_is_not_overwritten_by_auto(self):
        from yeone_ocr.cli import resolve_mode
        from yeone_ocr.state import Work
        with tempfile.TemporaryDirectory() as td, raster_pdf([printed_image()]) as doc:
            work = Work(Path(td) / "input.pdf", Path(td) / "work")
            with patch("yeone_ocr.cli._detect_mode", side_effect=AssertionError("manual mode")):
                self.assertEqual(resolve_mode(Namespace(mode="vertical"), doc, work), "vertical")
                self.assertEqual(resolve_mode(Namespace(mode="auto"), doc, work), "vertical")
            self.assertEqual(work.load_state()["mode_detection"]["source"], "manual")

    def test_reusing_args_does_not_turn_automatic_decision_into_manual(self):
        from yeone_ocr.cli import resolve_mode
        from yeone_ocr.state import Work
        with tempfile.TemporaryDirectory() as td, raster_pdf([printed_image()]) as doc:
            work = Work(Path(td) / "input.pdf", Path(td) / "work")
            args = Namespace(mode="auto")
            self.assertEqual(resolve_mode(args, doc, work), "horizontal_zh")
            resolve_mode(args, doc, work)
            self.assertEqual(work.load_state()["mode_detection"]["source"], "image_geometry")

    def test_legacy_direction_without_positive_raster_evidence_is_preserved(self):
        from yeone_ocr.cli import resolve_mode
        from yeone_ocr.state import Work
        with tempfile.TemporaryDirectory() as td, pymupdf.open() as doc:
            page = doc.new_page()
            for n in range(4):
                page.insert_text((60, 60 + n * 30), "天地玄黃宇宙洪荒日月盈昃辰宿列張", fontname="china-t")
            work = Work(Path(td) / "input.pdf", Path(td) / "work")
            work.save_state({"mode": "vertical", "pages": {}})
            self.assertEqual(resolve_mode(Namespace(mode="auto"), doc, work), "vertical")
            self.assertEqual(work.load_state()["mode_detection"]["source"], "legacy")


if __name__ == "__main__":
    unittest.main()
