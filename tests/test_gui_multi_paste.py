"""Real image files and bundle workers exercise repeated paste without Tk, clipboard or API."""
import contextlib
import copy
import io
from pathlib import Path
import queue
import unittest
from unittest import mock

from PIL import Image, ImageGrab
import pymupdf

import test_gui_input_tabs as input_tabs
from yeone_ocr import pdfio, screenshots


class MultipleImagePaste(unittest.TestCase):
    def setUp(self):
        input_tabs.InputTabs.setUp(self)
        self.root = Path.cwd()
        self.stack.enter_context(mock.patch.object(screenshots.runtime, "user_data_dir", return_value=self.root))

    def app(self):
        app = input_tabs.InputTabs.app(self)
        for variable in app._input_vars[2].values():
            variable.set("")
        app.q = queue.Queue()
        app.b_stop = mock.Mock()
        app.v_status = input_tabs.Var("")
        app.v_prog = input_tabs.Var("")
        app.progress = mock.Mock()
        app.image_preview = mock.Mock()
        app._set_running = lambda running: setattr(app, "running", running)
        app._bar_value = lambda fraction: round(fraction * 1000)
        app._bundle_path = self.gui.App._bundle_path
        for name in ("_bundle_images", "_bundle_msg", "paste_screenshot", "clear_image_input", "on_close", "_close_when_idle"):
            setattr(app, name, getattr(self.gui.App, name).__get__(app))
        return app

    def image(self, name, color):
        path = self.root / name
        with Image.new("RGB", (60, 40), color) as image:
            image.save(path)
        return path

    def paste(self, app, captured):
        with mock.patch.object(ImageGrab, "grabclipboard", return_value=captured):
            self.assertEqual(app.paste_screenshot(), "break")
        if app.running:
            self.drain_bundle(app)

    def drain_bundle(self, app):
        # The actual background worker writes the PDF, then queues the GUI completion.
        while True:
            item = app.q.get(timeout=10)
            self.assertEqual(item[0], "bundle")
            app._bundle_msg(*item[1:])
            if item[1] == "done":
                break
        self.assertFalse(app.running)
        self.assertTrue(app.q.empty())

    def colors(self, path):
        with pdfio.open_pdf(str(path)) as doc:
            result = []
            for page in doc:
                pixmap = page.get_pixmap(alpha=False)
                result.append(pixmap.pixel(pixmap.width // 2, pixmap.height // 2))
            return result

    def test_sequential_captures_follow_paste_order_despite_hash_filenames(self):
        app = self.app()
        general = input_tabs.InputTabs.input_values(app, 0)
        samples = []
        for color in ((255, 0, 0), (0, 255, 0), (0, 0, 255)):
            with Image.new("RGB", (60, 40), color) as capture:
                source = screenshots.save_screenshot(capture)
            samples.append((source, color))
        # A descending fingerprint order catches accidental re-sorting of captures.
        samples.sort(key=lambda sample: sample[0].name, reverse=True)
        expected = []
        for source, color in samples:
            with Image.open(source) as stored:
                self.paste(app, stored.copy())
            expected.append(color)
            self.assertEqual(self.colors(app.pdf()), expected)
        self.assertEqual(input_tabs.InputTabs.input_values(app, 0), general)
        self.assertTrue(app._input_force[0])
        self.assertEqual((app.v_pages.get(), app.v_off.get(), app.v_force.get()), ("", "", False))
        self.assertEqual(app.image_preview.show_last_page.call_count, 2)
        self.assertEqual(self.saved[-1]["ocr_inputs"]["0"]["pdf"], "book.pdf")
        self.error.assert_not_called()

    def test_same_screenshot_pasted_three_times_keeps_three_pages(self):
        app = self.app()
        color = (40, 120, 210)
        for count in (1, 2, 3):
            self.paste(app, Image.new("RGB", (60, 40), color))
            self.assertEqual(self.colors(app.pdf()), [color] * count)
        # Content-addressed source reuse must not deduplicate intentional page copies.
        self.assertEqual(len(list((self.root / "screenshots").glob("*.png"))), 1)
        self.error.assert_not_called()

    def test_existing_multipage_bundle_then_copied_files_keeps_old_pages_first(self):
        app = self.app()
        old = self.root / "existing.pdf"
        with pymupdf.open() as doc:
            doc.new_page(width=60, height=40).draw_rect((0, 0, 60, 40), color=None, fill=(1, 0, 0))
            doc.new_page(width=60, height=40).draw_rect((0, 0, 60, 40), color=None, fill=(0, 1, 0))
            doc.save(old)
        before = old.read_bytes()
        app.select_tab(2)
        app._use_pdf(str(old))
        page10 = self.image("page10.png", (255, 255, 0))
        page2 = self.image("page2.png", (0, 0, 255))
        self.paste(app, [str(page10), str(page2)])
        self.assertEqual(self.colors(app.pdf()), [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)])
        self.assertEqual(old.read_bytes(), before)
        self.assertLess(len(Path(app.pdf()).name), 60)
        self.error.assert_not_called()

    def test_clear_then_paste_starts_a_single_page_and_preserves_previous_bundle(self):
        app = self.app()
        self.paste(app, Image.new("RGB", (60, 40), (255, 0, 0)))
        self.paste(app, Image.new("RGB", (60, 40), (0, 255, 0)))
        previous = Path(app.pdf())
        before = previous.read_bytes()
        app.clear_image_input()
        self.assertEqual(app.pdf(), "")
        self.paste(app, Image.new("RGB", (60, 40), (0, 0, 255)))
        self.assertEqual(Path(app.pdf()).suffix, ".png")
        self.assertEqual(self.colors(app.pdf()), [(0, 0, 255)])
        self.assertEqual(previous.read_bytes(), before)
        self.assertEqual((app.v_pages.get(), app.v_off.get(), app.v_force.get()), ("", "", False))
        self.error.assert_not_called()

    def test_failed_append_preserves_selection_page_options_and_all_sources(self):
        app = self.app()
        self.paste(app, Image.new("RGB", (60, 40), (255, 0, 0)))
        self.paste(app, Image.new("RGB", (60, 40), (0, 255, 0)))
        app.v_pages.set("2")
        app.v_off.set("29")
        app.v_force.set(True)
        incoming = self.image("incoming.png", (0, 0, 255))
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        values = input_tabs.InputTabs.input_values(app, 2)
        config = copy.deepcopy(app.cfg)

        def fail_after_partial_write(_sources, destination, progress=None):
            Path(destination).write_bytes(b"incomplete PDF")
            raise OSError("synthetic disk full")

        with mock.patch("yeone_ocr.image_bundle.save_sources_as_pdf", side_effect=fail_after_partial_write):
            self.paste(app, [str(incoming)])
        self.assertEqual(input_tabs.InputTabs.input_values(app, 2), values)
        self.assertTrue(app.v_force.get())
        self.assertEqual(app.cfg, config)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
        self.assertEqual(self.colors(app.pdf()), [(255, 0, 0), (0, 255, 0)])
        self.error.assert_called_once()

    def test_saved_bundle_can_be_restored_and_appended_without_original_captures(self):
        app = self.app()
        self.paste(app, Image.new("RGB", (60, 40), (255, 0, 0)))
        self.paste(app, Image.new("RGB", (60, 40), (0, 255, 0)))
        old_bundle = Path(app.pdf())

        def persist_inputs(**_options):
            app._remember_inputs()
            self.gui.write_config(app)

        app.save_settings = mock.Mock(side_effect=persist_inputs)
        app.destroy = mock.Mock()
        app.on_close()
        app.destroy.assert_called_once()
        restored, selected = self.gui.ocr_input_config(self.saved[-1])
        self.assertEqual(selected, 2)
        self.assertEqual(restored[2]["pdf"], str(old_bundle))
        for capture in (self.root / "screenshots").glob("*.png"):
            capture.unlink()

        reopened = self.app()
        reopened.cfg = copy.deepcopy(self.saved[-1])
        for tab, state in restored.items():
            for key, variable in reopened._input_vars[tab].items():
                variable.set(state[key])
            reopened._input_force[tab] = state["force"]
        reopened.select_tab(selected)
        self.paste(reopened, Image.new("RGB", (60, 40), (0, 0, 255)))
        self.assertEqual(self.colors(reopened.pdf()), [(255, 0, 0), (0, 255, 0), (0, 0, 255)])
        self.assertTrue(old_bundle.is_file())
        self.assertEqual(reopened._input_vars[0]["pdf"].get(), "book.pdf")
        self.error.assert_not_called()

    def test_close_waits_for_completed_worker_callback_before_saving_selection(self):
        from yeone_ocr import cli

        app = self.app()
        self.paste(app, Image.new("RGB", (60, 40), (255, 0, 0)))
        first = app.pdf()
        with mock.patch.object(ImageGrab, "grabclipboard", return_value=Image.new("RGB", (60, 40), (0, 255, 0))):
            app.paste_screenshot()
        app._worker.join(timeout=10)
        self.assertFalse(app._worker.is_alive())
        self.assertTrue(app._bundle_in_progress)
        self.assertEqual(app.pdf(), first)
        app.after = mock.Mock()
        app.destroy = mock.Mock()

        def persist_inputs(**_options):
            app._remember_inputs()
            self.gui.write_config(app)

        app.save_settings = mock.Mock(side_effect=persist_inputs)
        self.addCleanup(cli.CANCEL.clear)
        with mock.patch.object(self.gui.messagebox, "askyesno", return_value=True):
            app.on_close()
        self.assertTrue(app._closing)
        app.after.assert_called_once_with(200, app._close_when_idle)
        app.destroy.assert_not_called()
        app.save_settings.assert_not_called()
        self.drain_bundle(app)
        self.assertFalse(app._bundle_in_progress)
        app._close_when_idle()
        app.destroy.assert_called_once()
        app.save_settings.assert_called_once_with(quiet=True)
        self.assertEqual(self.saved[-1]["ocr_inputs"]["2"]["pdf"], app.pdf())
        self.assertEqual(self.colors(app.pdf()), [(255, 0, 0), (0, 255, 0)])
        self.error.assert_not_called()

    def test_three_pasted_scans_complete_offline_ocr_and_searchable_pdf_in_order(self):
        from test_textlayer import scan
        from yeone_ocr import cli, textlayer
        from yeone_ocr.reader_mock import MockReader

        app = self.app()
        expected = ["天地玄黃", "宇宙洪荒", "日月盈昃"]
        layers = {}
        for number, text in enumerate(expected, 1):
            _source, truth = scan(self.root, [text], vertical=True, name=f"scan{number}")
            boxes = [box for _character, box in truth[0]]
            layers[number] = pdfio.TextLayer(page_width=360, page_height=480, vertical_columns=[{
                "text": text, "x": (boxes[0][0] + boxes[0][2]) / 2,
                "y0": boxes[0][1], "y1": boxes[-1][3]}])
            with Image.open(self.root / f"scan{number}.png") as stored:
                self.paste(app, stored.copy())
        source = Path(app.pdf())
        before = source.read_bytes()
        reader = MockReader(layers)
        work = self.root / "ocr-work"
        log = io.StringIO()
        cli.CANCEL.clear()
        self.addCleanup(cli.CANCEL.clear)
        with mock.patch.object(cli, "make_reader", return_value=reader), contextlib.redirect_stdout(log):
            result = cli.main(["run", str(source), "--work", str(work), "--reader", "mock", "--mode", "vertical",
                               "--workers", "1", "--pdf-only"])
        self.assertEqual(result, 0, log.getvalue())
        self.assertEqual(reader.calls["read_page"], 3)
        self.assertEqual(sorted(path.name for path in (work / "readings").glob("p*.json")),
                         ["p001.json", "p002.json", "p003.json"])
        with pymupdf.open(textlayer.out_path(work, source)) as finished, pymupdf.open(source) as original:
            self.assertEqual(len(finished), 3)
            for number, text in enumerate(expected, 1):
                self.assertEqual([column["text"] for column in pdfio.text_layer(finished, number, own=True).vertical_columns], [text])
                self.assertEqual(finished[number - 1].get_pixmap().samples, original[number - 1].get_pixmap().samples)
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse(list(work.rglob("*.hwpx")))
        self.error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
