"""Preview bundled image pages without OCR, network or clipboard access."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from PIL import Image

from test_ops import _real_tk


def make_pages(path, colors=("red", "blue")):
    images = [Image.new("RGB", (40, 60), color) for color in colors]
    try:
        images[0].save(path, save_all=True, append_images=images[1:])
    finally:
        for image in images:
            image.close()


class PreviewPages(unittest.TestCase):
    def test_pdf_and_tiff_show_selected_page_and_release_source(self):
        from yeone_ocr.gui_preview import preview_image, preview_page_count
        with tempfile.TemporaryDirectory() as temp:
            for suffix in ("pdf", "tiff"):
                with self.subTest(suffix=suffix):
                    path = Path(temp) / f"pages.{suffix}"
                    make_pages(path)
                    self.assertEqual(preview_page_count(str(path)), 2)
                    for index, channel in ((0, 0), (1, 2)):
                        with preview_image(str(path), (20, 30), index) as image:
                            pixel = image.getpixel((10, 15))
                            self.assertGreater(pixel[channel], 240)
                            self.assertLess(pixel[2 if channel == 0 else 0], 15)
                            self.assertEqual(image.size, (20, 30))
                    for index in (-1, 2):
                        with self.assertRaises(IndexError):
                            preview_image(str(path), (20, 30), index)
                    path.unlink()  # Windows also rejects this if the source is still open.

    def test_single_image_count_and_default_page_remain_compatible(self):
        from yeone_ocr.gui_preview import preview_image, preview_page_count
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "image.png"
            with Image.new("RGB", (40, 60), "green") as image:
                image.save(path)
            self.assertEqual(preview_page_count(str(path)), 1)
            with preview_image(str(path), (20, 30)) as image:
                self.assertEqual(image.getpixel((10, 15)), (0, 128, 0))

    def test_mpo_extra_frames_are_not_document_pages(self):
        from yeone_ocr.gui_preview import preview_image, preview_page_count
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "photo.mpo"
            make_pages(path)
            with Image.open(path) as source:
                self.assertEqual(source.format, "MPO")
                self.assertEqual(source.n_frames, 2)
            self.assertEqual(preview_page_count(str(path)), 1)
            with preview_image(str(path), (20, 30)) as image:
                self.assertGreater(image.getpixel((10, 15))[0], 240)
            with self.assertRaises(IndexError):
                preview_image(str(path), (20, 30), 1)


class PreviewNavigation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _real_tk()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        import tkinter as tk
        from yeone_ocr.gui_preview import ImageInputPreview
        self.path = Path(self.temp.name) / "pages.tiff"
        make_pages(self.path)
        self.variable = tk.StringVar(self.root, value=str(self.path))
        self.preview = ImageInputPreview(self.root, variable=self.variable,
                                         paste=mock.Mock(), open_source=mock.Mock())
        self.addCleanup(self.preview.destroy)
        self.preview.pack()
        self.draw()

    def draw(self):
        if self.preview._pending is not None:
            self.preview.after_cancel(self.preview._pending)
        self.preview._draw()

    def test_buttons_change_visible_page_and_resize_reuses_current_thumbnail(self):
        from yeone_ocr import gui_preview
        self.assertEqual(self.preview.page_label.cget("text"), "1 / 2쪽")
        self.assertTrue(self.preview.previous_button.instate(["disabled"]))
        with mock.patch.object(gui_preview, "preview_image", wraps=gui_preview.preview_image) as render:
            self.preview.next_button.invoke()
            self.draw()
            self.assertEqual(self.preview.page_label.cget("text"), "2 / 2쪽")
            self.assertEqual(self.preview._thumbnail.getpixel((10, 15)), (0, 0, 255))
            self.assertTrue(self.preview.next_button.instate(["disabled"]))
            self.draw()
            self.assertEqual(render.call_count, 1)
            self.preview.previous_button.invoke()
            self.draw()
            self.assertEqual(self.preview.page_label.cget("text"), "1 / 2쪽")
            self.assertEqual(self.preview._thumbnail.getpixel((10, 15)), (255, 0, 0))
            self.assertEqual(render.call_count, 2)

    def test_appended_source_can_show_last_page_then_replacement_resets(self):
        appended = Path(self.temp.name) / "appended.pdf"
        make_pages(appended, ("red", "blue", "green"))
        self.variable.set(str(appended))
        self.preview.show_last_page()
        self.draw()
        self.assertEqual(self.preview.page_label.cget("text"), "3 / 3쪽")
        self.assertTrue(self.preview.next_button.instate(["disabled"]))
        self.variable.set(str(self.path))
        self.draw()
        self.assertEqual(self.preview.page_label.cget("text"), "1 / 2쪽")
        self.assertEqual(self.preview._thumbnail.getpixel((10, 15)), (255, 0, 0))

    def test_same_path_changed_to_single_page_resets_navigation_and_image(self):
        self.preview.next_button.invoke()
        self.draw()
        make_pages(self.path, ("green",))
        self.variable.set(str(self.path))
        self.draw()
        self.assertEqual(self.preview.page_label.cget("text"), "1 / 1쪽")
        self.assertEqual(self.preview._thumbnail.getpixel((10, 15)), (0, 128, 0))
        self.assertTrue(self.preview.previous_button.instate(["disabled"]))
        self.assertTrue(self.preview.next_button.instate(["disabled"]))

    def test_empty_and_invalid_sources_clear_count_and_preview(self):
        invalid = Path(self.temp.name) / "invalid.tiff"
        invalid.write_bytes(b"invalid image")
        for path in (str(invalid), "", str(invalid.with_name("missing.tiff"))):
            with self.subTest(path=path):
                self.variable.set(path)
                self.draw()
                self.assertIsNone(self.preview._thumbnail)
                self.assertEqual(self.preview.page_label.cget("text"), "0쪽")
                self.assertTrue(self.preview.previous_button.instate(["disabled"]))
                self.assertTrue(self.preview.next_button.instate(["disabled"]))


if __name__ == "__main__":
    unittest.main()
