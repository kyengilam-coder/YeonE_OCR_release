"""클립보드 붙여넣기의 GUI 경계를 실제 화면·클립보드·API·사용자 설정 없이 검사한다."""
import contextlib
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image, ImageGrab

from test_ops import gui_module


class Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class ScreenshotGui(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.gui = self.stack.enter_context(gui_module())
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        self.original = str(self.root / "previous.pdf")
        self.saved = self.root / "screenshots" / "capture.png"
        self.persisted = []
        self.persist = self.stack.enter_context(mock.patch.object(
            self.gui, "save_config", side_effect=lambda cfg: self.persisted.append(copy.deepcopy(cfg))))
        self.info = self.stack.enter_context(mock.patch.object(self.gui.messagebox, "showinfo"))
        self.error = self.stack.enter_context(mock.patch.object(self.gui.messagebox, "showerror"))

    def app(self):
        cfg = {"last_pdf": self.original, "pages": "12-20", "print_offset": "29", "force": True,
               "reader": "anthropic", "model": "claude-test", "api_key": "synthetic-key",
               "provider_settings": {"anthropic": {"model": "claude-test", "api_key": "synthetic-key"}},
               "mode": "horizontal_zh"}
        app = SimpleNamespace(running=False, _closing=False, _update_installing=False, cfg=cfg,
                              v_pdf=Var(self.original), v_pages=Var("12-20"), v_off=Var("29"), v_force=Var(True),
                              v_reader=Var("anthropic"), v_model=Var("claude-test"), v_key=Var("synthetic-key"),
                              select_tab=mock.Mock(), refresh_table=mock.Mock(), _append=mock.Mock(),
                              _remember_inputs=mock.Mock(),
                              _bundle_images=mock.Mock(), do_run=mock.Mock(), start=mock.Mock(),
                              _set_running=mock.Mock(), v_status=Var("working"), v_prog=Var(""), progress=mock.Mock(),
                              mode_key=lambda: "horizontal_zh")
        app.work = lambda: self.gui.work_dir_for(app.v_pdf.get())
        app._reset_screenshot_pages = self.gui.App._reset_screenshot_pages.__get__(app)
        app._use_pdf = mock.Mock(wraps=self.gui.App._use_pdf.__get__(app))
        return app

    @staticmethod
    def snapshot(app):
        return (app.v_pdf.get(), app.v_pages.get(), app.v_off.get(), app.v_force.get(), copy.deepcopy(app.cfg))

    def assert_reset(self, app):
        self.assertEqual((app.v_pages.get(), app.v_off.get(), app.v_force.get()), ("", "", False))
        self.assertEqual((app.cfg["pages"], app.cfg["print_offset"], app.cfg["force"]), ("", "", False))

    def test_image_is_saved_before_selection_and_closed_without_starting_ocr(self):
        app = self.app()
        image = Image.new("RGB", (80, 60), "white")

        def save(captured):
            self.assertIs(captured, image)
            self.assertEqual(app.v_pdf.get(), self.original)
            self.saved.parent.mkdir()
            captured.save(self.saved)
            return self.saved

        with mock.patch.object(ImageGrab, "grabclipboard", return_value=image), \
             mock.patch("yeone_ocr.screenshots.save_screenshot", side_effect=save) as save_image, \
             mock.patch.object(image, "close", wraps=image.close) as close:
            self.assertEqual(self.gui.App.paste_screenshot(app), "break")
        save_image.assert_called_once_with(image)
        close.assert_called_once_with()
        self.assertTrue(self.saved.is_file())
        app._use_pdf.assert_called_once_with(str(self.saved))
        self.assertEqual((app.v_pdf.get(), app.cfg["last_pdf"]), (str(self.saved), str(self.saved)))
        self.assert_reset(app)
        app.select_tab.assert_called_once_with(2)
        app.refresh_table.assert_called_once_with()
        self.assertEqual(self.persisted[-1]["pages"], "")
        self.assertEqual((app.v_reader.get(), app.v_model.get(), app.v_key.get()),
                         ("anthropic", "claude-test", "synthetic-key"))
        self.assertEqual(self.persisted[-1]["provider_settings"],
                         {"anthropic": {"model": "claude-test", "api_key": "synthetic-key"}})
        self.assertEqual(app.cfg["mode"], "horizontal_zh")
        app.do_run.assert_not_called()
        app.start.assert_not_called()
        self.error.assert_not_called()

    def test_empty_or_text_clipboard_preserves_selected_document_and_settings(self):
        for captured in (None, "ordinary text", []):
            with self.subTest(captured=captured):
                app = self.app(); before = self.snapshot(app)
                with mock.patch.object(ImageGrab, "grabclipboard", return_value=captured), \
                     mock.patch("yeone_ocr.screenshots.save_screenshot") as save:
                    self.assertEqual(self.gui.App.paste_screenshot(app), "break")
                save.assert_not_called()
                app._use_pdf.assert_not_called()
                app._bundle_images.assert_not_called()
                self.assertEqual(self.snapshot(app), before)
        self.assertEqual(self.info.call_count, 3)
        self.persist.assert_not_called()

    def test_clipboard_access_failure_preserves_input(self):
        app = self.app(); before = self.snapshot(app)
        with mock.patch.object(ImageGrab, "grabclipboard", side_effect=OSError("clipboard busy")), \
             mock.patch("yeone_ocr.screenshots.save_screenshot") as save:
            self.assertEqual(self.gui.App.paste_screenshot(app), "break")
        save.assert_not_called()
        self.assertEqual(self.snapshot(app), before)
        self.error.assert_called_once()
        self.persist.assert_not_called()

    def test_image_save_failure_preserves_input_and_closes_capture(self):
        app = self.app(); before = self.snapshot(app)
        image = Image.new("RGB", (20, 30), "white")
        with mock.patch.object(ImageGrab, "grabclipboard", return_value=image), \
             mock.patch("yeone_ocr.screenshots.save_screenshot", side_effect=OSError("disk full")), \
             mock.patch.object(image, "close", wraps=image.close) as close:
            self.assertEqual(self.gui.App.paste_screenshot(app), "break")
        close.assert_called_once_with()
        self.assertEqual(self.snapshot(app), before)
        app.select_tab.assert_not_called()
        app._use_pdf.assert_not_called()
        self.persist.assert_not_called()
        self.error.assert_called_once()

    def images(self):
        sources = [self.root / "page2.PNG", self.root / "page1.png"]
        for source in sources:
            with Image.new("RGB", (10, 20), "white") as image:
                image.save(source)
        return sources

    def test_copied_single_image_uses_original_file_and_resets_page_settings(self):
        app = self.app(); source = self.images()[0]
        with mock.patch.object(ImageGrab, "grabclipboard", return_value=[str(source)]), \
             mock.patch("yeone_ocr.screenshots.save_screenshot") as save:
            self.assertEqual(self.gui.App.paste_screenshot(app), "break")
        save.assert_not_called()
        app._use_pdf.assert_called_once_with(str(source))
        app._bundle_images.assert_not_called()
        app.select_tab.assert_called_once_with(2)
        self.assert_reset(app)

    def test_copied_multiple_images_use_existing_bundle_workflow(self):
        app = self.app(); sources = self.images(); before = self.snapshot(app)
        with mock.patch.object(ImageGrab, "grabclipboard", return_value=sources), \
             mock.patch("yeone_ocr.screenshots.save_screenshot") as save:
            self.assertEqual(self.gui.App.paste_screenshot(app), "break")
        save.assert_not_called()
        app._bundle_images.assert_called_once_with([str(source) for source in sources], reset_pages=True)
        app._use_pdf.assert_not_called()
        self.assertEqual(self.snapshot(app), before)  # 실제 묶기 완료 콜백에서만 원본·쪽 설정을 바꾼다.
        app.select_tab.assert_called_once_with(2)

    def test_bundle_success_resets_page_settings_when_new_input_is_ready(self):
        app = self.app(); app._bundle_reset_pages = True
        output = self.root / "combined.pdf"
        self.gui.App._bundle_msg(app, "done", output, None, "images combined")
        self.assert_reset(app)
        self.assertEqual(app.v_pdf.get(), str(output))
        app._use_pdf.assert_called_once_with(str(output))
        self.assertFalse(app._bundle_reset_pages)
        self.error.assert_not_called()

    def test_bundle_failure_retains_previous_input_and_page_settings(self):
        app = self.app(); app._bundle_reset_pages = True
        before = self.snapshot(app)
        self.gui.App._bundle_msg(app, "done", None, "cannot create PDF", "")
        self.assertEqual(self.snapshot(app), before)
        app._use_pdf.assert_not_called()
        self.persist.assert_not_called()
        self.assertFalse(app._bundle_reset_pages)
        self.error.assert_called_once()

    def test_copied_nonimage_missing_file_or_directory_does_not_change_selection(self):
        valid = self.images()[0]
        nonimage = self.root / "notes.txt"; nonimage.write_text("not an image", encoding="utf-8")
        directory = self.root / "folder.png"; directory.mkdir()
        for invalid in (nonimage, self.root / "missing.png", directory):
            with self.subTest(invalid=invalid.name):
                app = self.app(); before = self.snapshot(app)
                with mock.patch.object(ImageGrab, "grabclipboard", return_value=[str(valid), str(invalid)]):
                    self.assertEqual(self.gui.App.paste_screenshot(app), "break")
                app._use_pdf.assert_not_called()
                app._bundle_images.assert_not_called()
                self.assertEqual(self.snapshot(app), before)
        self.assertEqual(self.info.call_count, 3)
        self.persist.assert_not_called()

    def test_busy_closing_or_updating_does_not_read_clipboard(self):
        for flag in ("running", "_closing", "_update_installing"):
            with self.subTest(flag=flag):
                app = self.app(); setattr(app, flag, True)
                before = self.snapshot(app)
                with mock.patch.object(ImageGrab, "grabclipboard") as read:
                    self.assertEqual(self.gui.App.paste_screenshot(app, SimpleNamespace()), "break")
                read.assert_not_called()
                self.assertEqual(self.snapshot(app), before)
        self.info.assert_not_called()
        self.error.assert_not_called()

    def test_startup_registers_only_explicit_screenshot_shortcut_without_reading_clipboard(self):
        # __init__의 실제 binding 호출을 검사하되 Tk/설정/패키지/업데이트 작업은 실행하지 않는다.
        app = self.gui.App.__new__(self.gui.App)
        app.scale = 1
        for name in ("title", "_style", "geometry", "minsize", "_build", "bind", "after", "protocol"):
            setattr(app, name, mock.Mock())
        app.winfo_screenwidth = mock.Mock(return_value=1920)
        app.winfo_screenheight = mock.Mock(return_value=1080)
        with mock.patch.object(self.gui.tk.Tk, "__init__", return_value=None), \
             mock.patch.object(self.gui, "load_config", return_value={"last_pdf": ""}), \
             mock.patch.object(self.gui, "fx_from_config", return_value=((1300, "test"), False)), \
             mock.patch("yeone_ocr.app_icon.set_windows_app_id"), \
             mock.patch("yeone_ocr.app_icon.apply_window_icon"), \
             mock.patch.object(ImageGrab, "grabclipboard") as read:
            self.gui.App.__init__(app)
        self.assertEqual({call.args[0] for call in app.bind.call_args_list},
                         {"<Control-Shift-KeyPress-V>", "<Control-Shift-KeyPress-v>"})
        for call in app.bind.call_args_list:
            self.assertEqual(call.args[1].__func__, self.gui.App.paste_screenshot)
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
