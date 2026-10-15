"""붙여넣은 이미지와 기존 묶음을 순서·원본 해상도 그대로 이어 붙인다."""
from pathlib import Path
import tempfile
import unittest

import pymupdf
from PIL import Image, ImageChops

from yeone_ocr import pdfio
from yeone_ocr.image_bundle import save_sources_as_pdf


class ImageAppend(unittest.TestCase):
    def test_explicit_order_and_original_pixels_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = Image.new("RGB", (240, 360), (230, 10, 20))
            second = Image.new("RGB", (320, 180), (10, 20, 230))
            first.save(root / "z.png", dpi=(400, 400))
            second.save(root / "a.png", dpi=(200, 200))
            sources = [root / "z.png", root / "a.png"]
            originals = [path.read_bytes() for path in sources]
            updates = []
            output = root / "combined.pdf"

            self.assertEqual(save_sources_as_pdf(sources, output, lambda done, total: updates.append((done, total))), str(output))

            self.assertEqual(updates, [(1, 2), (2, 2)])
            with pymupdf.open(output) as document:
                self.assertEqual(len(document), 2)
                for number, expected in enumerate((first, second), 1):
                    actual, description = pdfio.page_image(document, number)
                    self.assertIn("내장 스캔", description)
                    self.assertEqual(actual.size, expected.size)
                    self.assertIsNone(ImageChops.difference(actual, expected).getbbox())
                self.assertAlmostEqual(document[0].rect.width, 240 * 72 / 400, places=2)
                self.assertAlmostEqual(document[1].rect.width, 320 * 72 / 200, places=2)
            self.assertEqual([path.read_bytes() for path in sources], originals)

    def test_existing_pdf_pages_remain_before_incoming_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "existing.pdf"
            with pymupdf.open() as document:
                for text in ("first image", "second image"):
                    page = document.new_page(width=210, height=300)
                    page.insert_text((20, 40), text)
                document.save(old)
            original = old.read_bytes()
            Image.new("RGB", (250, 350), "red").save(root / "a.png")

            save_sources_as_pdf([old, root / "a.png"], root / "combined.pdf")

            with pymupdf.open(root / "combined.pdf") as document:
                self.assertEqual(len(document), 3)
                self.assertEqual([document[index].get_text().strip() for index in (0, 1)], ["first image", "second image"])
                self.assertEqual(pdfio.page_image(document, 3)[0].size, (250, 350))
                self.assertEqual(pdfio.page_image(document, 3)[0].getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(old.read_bytes(), original)

    def test_every_tiff_frame_is_appended_in_frame_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frames = [Image.new("L", (120, 180), value) for value in (40, 90, 150)]
            frames[0].save(root / "scans.tiff", save_all=True, append_images=frames[1:], dpi=(300, 300))
            Image.new("L", (140, 200), 220).save(root / "last.png")

            save_sources_as_pdf([root / "scans.tiff", root / "last.png"], root / "combined.pdf")

            with pymupdf.open(root / "combined.pdf") as document:
                self.assertEqual(len(document), 4)
                self.assertEqual([pdfio.page_image(document, number)[0].convert("L").getpixel((0, 0)) for number in range(1, 5)], [40, 90, 150, 220])
                self.assertEqual([pdfio.page_image(document, number)[0].size for number in range(1, 5)], [(120, 180)] * 3 + [(140, 200)])

    def test_rotated_jpeg_keeps_original_compressed_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "phone.jpg"
            exif = Image.Exif()
            exif[0x0112] = 6
            Image.new("RGB", (240, 360), (180, 30, 10)).save(source, exif=exif, dpi=(300, 300))
            original = source.read_bytes()

            save_sources_as_pdf([source], root / "combined.pdf")

            with pymupdf.open(root / "combined.pdf") as document:
                image_reference = document[0].get_images(full=True)[0][0]
                self.assertEqual(document.extract_image(image_reference)["image"], original)
                self.assertEqual(pdfio.page_image(document, 1)[0].size, (360, 240))

    def test_bad_source_does_not_modify_previous_pdf_or_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("L", (100, 150), 40).save(root / "first.png")
            old = root / "existing.pdf"
            pdfio.save_images_as_pdf([root / "first.png"], old)
            original = old.read_bytes()
            output = root / "combined.pdf"
            output.write_bytes(b"previous destination")
            broken = root / "broken.png"
            broken.write_bytes(b"invalid image")

            for invalid in (broken, root / "missing.png"):
                with self.subTest(source=invalid.name), self.assertRaises(Exception):
                    save_sources_as_pdf([old, invalid], output)
                self.assertEqual(old.read_bytes(), original)
                self.assertEqual(output.read_bytes(), b"previous destination")

    def test_empty_sources_leave_destination_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "combined.pdf"
            output.write_bytes(b"previous destination")
            with self.assertRaisesRegex(ValueError, "이미지가 없습니다"):
                save_sources_as_pdf([], output)
            self.assertEqual(output.read_bytes(), b"previous destination")

    def test_destination_cannot_overwrite_an_input_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "first.png"
            Image.new("L", (100, 150), 40).save(source)
            original = source.read_bytes()
            with self.assertRaisesRegex(ValueError, "원본 파일"):
                save_sources_as_pdf([source], source)
            self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
