"""실제 클립보드/모델 API 없이 스크린샷 보관과 기존 OCR 입력 경로를 확인한다."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from zipfile import ZipFile

from PIL import Image

from yeone_ocr import screenshots


class ScreenshotStorage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def image(self, mode="RGB", size=(40, 30), color="white"):
        image = Image.new(mode, size, color)
        self.addCleanup(image.close)
        return image

    def test_default_location_and_repeat_preserves_original(self):
        image = self.image(size=(901, 601))
        image.putpixel((900, 600), (7, 19, 203))
        with mock.patch.object(screenshots.runtime, "user_data_dir", return_value=self.root / "data"):
            first = screenshots.save_screenshot(image)
            before = first.stat().st_mtime_ns
            with image.copy() as copied:
                second = screenshots.save_screenshot(copied)
        self.assertEqual(first, second)
        self.assertEqual(first.parent, self.root / "data" / "screenshots")
        self.assertEqual(first.stat().st_mtime_ns, before)
        with Image.open(first) as saved:
            self.assertEqual(saved.size, image.size)
            self.assertEqual(saved.tobytes(), image.tobytes())
        self.assertEqual(len(list(first.parent.iterdir())), 1)

    def test_identity_includes_pixels_and_dimensions(self):
        first = screenshots.save_screenshot(self.image(size=(3, 4)), self.root)
        second = screenshots.save_screenshot(self.image(size=(4, 3)), self.root)
        third = screenshots.save_screenshot(self.image(size=(3, 4), color="black"), self.root)
        self.assertEqual(len({first, second, third}), 3)

    def test_transparency_white_background_and_caller_unchanged(self):
        image = self.image("RGBA", (3, 1), (0, 0, 0, 0))
        image.putpixel((1, 0), (255, 0, 0, 128))
        image.putpixel((2, 0), (5, 6, 7, 255))
        original = image.tobytes()
        saved_path = screenshots.save_screenshot(image, self.root)
        with Image.open(saved_path) as saved:
            self.assertEqual(saved.mode, "RGB")
            self.assertEqual([saved.getpixel((x, 0)) for x in range(3)],
                             [(255, 255, 255), (255, 127, 127), (5, 6, 7)])
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.tobytes(), original)

    def test_palette_transparency_and_equivalent_pixels_reuse(self):
        image = self.image("P", (2, 1), 0)
        image.putpalette([0, 0, 0, 9, 8, 7] + [0] * 762)
        image.putpixel((1, 0), 1)
        image.info["transparency"] = 0
        first = screenshots.save_screenshot(image, self.root)
        equivalent = self.image(size=(2, 1))
        equivalent.putpixel((1, 0), (9, 8, 7))
        self.assertEqual(first, screenshots.save_screenshot(equivalent, self.root))
        self.assertEqual(image.mode, "P")
        self.assertEqual(image.info["transparency"], 0)

    def test_exif_orientation_is_applied_once_without_mutating_input(self):
        image = self.image(size=(3, 2))
        image.putpixel((0, 0), (1, 2, 3))
        image.getexif()[0x0112] = 6
        before = image.tobytes()
        first = screenshots.save_screenshot(image, self.root)
        with Image.open(first) as saved:
            self.assertEqual(saved.size, (2, 3))
            self.assertEqual(saved.getpixel((1, 0)), (1, 2, 3))
            self.assertNotIn(0x0112, saved.getexif())
            self.assertEqual(first, screenshots.save_screenshot(saved, self.root))
        self.assertEqual(image.size, (3, 2))
        self.assertEqual(image.getexif()[0x0112], 6)
        self.assertEqual(image.tobytes(), before)

    def test_corrupt_or_tampered_file_is_preserved_and_recovery_reused(self):
        image = self.image()
        first = screenshots.save_screenshot(image, self.root)
        first.write_bytes(b"damaged original")
        second = screenshots.save_screenshot(image, self.root)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), b"damaged original")
        self.assertEqual(second, screenshots.save_screenshot(image, self.root))
        # 전체 파일 검증: 픽셀이 같아도 뒤에 데이터가 붙었으면 별도 원본으로 보존한다.
        changed = second.read_bytes() + b"unexpected appended data"
        second.write_bytes(changed)
        third = screenshots.save_screenshot(image, self.root)
        self.assertNotIn(third, (first, second))
        self.assertEqual(second.read_bytes(), changed)
        self.assertEqual(third, screenshots.save_screenshot(image, self.root))

    def test_valid_png_with_changed_pixels_is_not_reused(self):
        image = self.image()
        first = screenshots.save_screenshot(image, self.root)
        self.image(color="black").save(first)
        changed = first.read_bytes()
        second = screenshots.save_screenshot(image, self.root)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), changed)
        with Image.open(second) as saved:
            self.assertEqual(saved.getpixel((0, 0)), (255, 255, 255))

    def test_same_size_corruption_cannot_reuse_original_records(self):
        image = self.image()
        first = screenshots.save_screenshot(image, self.root)
        original = first.read_bytes()
        changed = bytearray(original)
        changed[len(changed) // 2] ^= 1
        first.write_bytes(changed)
        second = screenshots.save_screenshot(image, self.root)
        self.assertNotEqual(first, second)
        self.assertEqual(first.stat().st_size, second.stat().st_size)
        self.assertEqual(first.read_bytes(), changed)
        self.assertEqual(second.read_bytes(), original)

    def test_occupied_directory_does_not_get_overwritten(self):
        image = self.image()
        first = screenshots.save_screenshot(image, self.root)
        first.unlink()
        first.mkdir()
        second = screenshots.save_screenshot(image, self.root)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_file())
        self.assertNotEqual(first, second)

    def test_failed_write_leaves_no_partial_or_temporary_file(self):
        with mock.patch.object(screenshots.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                screenshots.save_screenshot(self.image(), self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_atomic_publication_race_reuses_complete_file(self):
        publish = screenshots._publish

        def other_writer_first(temporary, destination):
            # 동시에 같은 이미지가 들어왔을 때 먼저 완성된 파일이 있어도 덮어쓰지 않는다.
            destination.write_bytes(temporary.read_bytes())
            publish(temporary, destination)

        with mock.patch.object(screenshots, "_publish", side_effect=other_writer_first):
            saved = screenshots.save_screenshot(self.image(), self.root)
        self.assertEqual(list(self.root.iterdir()), [saved])
        with Image.open(saved) as image:
            image.verify()

    def test_clipboard_helper_passes_results_without_reading_actual_clipboard(self):
        for result in (self.image(), ["example.png"], None):
            with self.subTest(result=type(result).__name__):
                with mock.patch("PIL.ImageGrab.grabclipboard", return_value=result) as grab:
                    self.assertIs(screenshots.read_clipboard(), result)
                    grab.assert_called_once_with()

    def test_invalid_input_is_rejected_without_creating_directory(self):
        destination = self.root / "not-created"
        with self.assertRaises(TypeError):
            screenshots.save_screenshot(None, destination)
        with self.assertRaises(ValueError):
            screenshots.save_screenshot(self.image(size=(0, 1)), destination)
        self.assertFalse(destination.exists())

    def test_saved_screenshot_runs_through_existing_ocr_and_hwpx_pipeline(self):
        from yeone_ocr.cli import CANCEL, main
        from yeone_ocr.reader_mock import MockReader
        from yeone_ocr.schema import PageReading, Region, Paragraph, Column
        from yeone_ocr.verify import check_hwpx_file

        image = self.image(size=(300, 240))
        image.putpixel((0, 0), (3, 5, 7))
        source = screenshots.save_screenshot(image, self.root / "screenshots")
        recognized = PageReading(pdf_page=1, regions=[Region(id="body", kind="본문", paragraphs=[
            Paragraph(columns=[Column(col_from_right=1, text="天地玄黃 스크린샷")])])])
        seen = []

        def fake_read(reader, actual_image, file_name, pdf_page, total, extra=""):
            with actual_image.convert("RGB") as rgb:
                seen.append((actual_image.size, rgb.getpixel((0, 0))))
            return recognized.model_copy(deep=True)

        CANCEL.clear()
        work = self.root / "result"
        output = io.StringIO()
        with mock.patch.object(MockReader, "read_page", fake_read), contextlib.redirect_stdout(output):
            code = main(["run", str(source), "--work", str(work), "--reader", "mock",
                         "--no-adjudicate", "--no-second-pass", "--no-context", "--checkpoint", "0", "--upscale", "1"])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(seen, [((300, 240), (3, 5, 7))])
        hwpx = work / "out" / f"{source.stem}.hwpx"
        self.assertEqual(check_hwpx_file(str(hwpx)), [])
        with ZipFile(hwpx) as archive:
            self.assertIn("天地玄黃 스크린샷", archive.read("Contents/section0.xml").decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
