"""Host routing and the offline command share projects without old CLI globals."""
from argparse import Namespace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image


class ComparisonHostTests(unittest.TestCase):
    def app(self, tab=0, source="book.pdf"):
        return SimpleNamespace(running=False, _closing=False, _selected_tab=tab,
            pdf=lambda: source, pdf2=lambda: "bookmark.pdf", mode_key=lambda _tab=None: "vertical")

    def test_pdf_image_and_bookmark_route_without_changing_old_inputs(self):
        from yeone_ocr.gui import App
        for tab, source in ((0, "book.pdf"), (1, "bookmark.pdf"), (2, "image.png")):
            with self.subTest(tab=tab):
                app = self.app(tab, "image.png" if tab == 2 else "book.pdf")
                with patch("yeone_ocr.comparison_editor.ComparisonEditor") as editor:
                    App.open_local_comparison(app)
                self.assertEqual(editor.call_args.kwargs["source_path"], source)
                self.assertEqual(editor.call_args.kwargs["direction"], "vertical")
                self.assertEqual(app.pdf(), "image.png" if tab == 2 else "book.pdf")

    def test_reuses_same_source_window(self):
        from yeone_ocr.gui import App
        app = self.app()
        window = Mock()
        window.project.source_path = Path("book.pdf")
        window.winfo_exists.return_value = True
        app._comparison_windows = [window]
        with patch("yeone_ocr.comparison_editor.ComparisonEditor") as editor:
            App.open_local_comparison(app)
        editor.assert_not_called()
        window.lift.assert_called_once()

    def test_host_remembers_explicit_engine_folder(self):
        from yeone_ocr.gui import App
        app = self.app(); app.cfg = {}; app.save_settings = Mock()
        App._remember_local_engine_root(app, Path("chosen-engines"))
        self.assertEqual(app.cfg["local_engine_root"], str(Path("chosen-engines").resolve()))
        app.save_settings.assert_called_once_with(quiet=True)

    def test_ai_settings_are_captured_from_current_selection_without_other_profiles(self):
        from yeone_ocr.gui import App
        app = self.app()
        app.v_reader = SimpleNamespace(get=lambda: "openai")
        app.v_model = SimpleNamespace(get=lambda: " chosen-vision-model ")
        app.v_key = SimpleNamespace(get=lambda: " fixture-key ")
        app.cfg = {"provider_settings": {"anthropic": {"api_key": "other-profile"}}}
        self.assertEqual(App._comparison_ai_settings(app), {
            "provider": "openai", "model": "chosen-vision-model", "api_key": "fixture-key"})
        with patch("yeone_ocr.comparison_editor.ComparisonEditor") as editor:
            App.open_local_comparison(app)
        self.assertEqual(editor.call_args.kwargs["on_ai_settings"](), App._comparison_ai_settings(app))

    def test_ai_settings_reject_incomplete_provider_selection(self):
        from yeone_ocr.gui import App
        app = self.app()
        app.v_reader = SimpleNamespace(get=lambda: "unknown")
        app.v_model = SimpleNamespace(get=lambda: "")
        app.v_key = SimpleNamespace(get=lambda: "")
        with self.assertRaises(ValueError):
            App._comparison_ai_settings(app)

    def test_parent_close_waits_for_comparison_checkpoint(self):
        from yeone_ocr.gui import App
        app = self.app()
        app.after = Mock()
        app.on_close = Mock()
        window = Mock()
        window.winfo_exists.return_value = True
        window.request_close.return_value = False
        window.busy = True
        app._comparison_windows = [window]
        app.destroy = Mock()
        App.on_close(app)
        app.destroy.assert_not_called()
        app.after.assert_called_once_with(200, app.on_close)

    def test_parent_close_stops_polling_if_child_save_failed(self):
        from yeone_ocr.gui import App
        app = self.app(); app.after = Mock(); app.v_status = Mock()
        window = Mock(); window.winfo_exists.return_value = True
        window.request_close.return_value = False; window.busy = False
        app._comparison_windows = [window]
        App.on_close(app)
        app.after.assert_not_called()
        app.v_status.set.assert_called_once()


class ComparisonCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "image.png"
        Image.new("RGB", (120, 180), "white").save(self.source)

    def args(self):
        return Namespace(pdf=str(self.source), project=None, work=str(self.root / "job"),
            pages="1", profile="mixed", direction="vertical", dpi=300, engine_root=None, force=False)

    def test_command_reopens_checkpoint_and_preserves_partial_results(self):
        from yeone_ocr.comparison_cli import command
        from yeone_ocr.comparison import ComparisonProject
        from yeone_ocr.comparison_bridge import default_project_path
        def run(project, number, **kwargs):
            raw = {"kind": "recognition", "schema_version": 1, "engine": "tesseract",
                "coordinate_system": "rendered-image pixels, xyxy", "text": "漢樂一\ufe00。",
                "words": [], "source_path": str(self.source), "source_sha256": project.source_sha256,
                "page_number": 1, "image_width": 120, "image_height": 180,
                "engine_version": "fixture", "languages": "kor+chi_tra", "psm": 5,
                "duration_seconds": 0, "engine_metadata": {}, "preprocessing": {}}
            project.add_result(raw)
            kwargs["on_result"](raw)
            return {"completed": ["tesseract"], "errors": {"easyocr": "fixture failure"}, "cancelled": False}
        with patch("yeone_ocr.comparison.run_page", side_effect=run):
            self.assertEqual(command(self.args()), 3)
        project = ComparisonProject.load(default_project_path(self.source, self.root / "job"))
        self.assertEqual(project.text(1), "漢樂一\ufe00。")
        self.assertEqual(len(project.page(1)["runs"]), 1)

    def test_cli_dispatch_does_not_initialize_cloud_call_globals(self):
        from yeone_ocr import cli
        with patch("yeone_ocr.comparison_cli.command", return_value=0) as command:
            with patch("yeone_ocr.reader_gemini.set_call_cap") as cap:
                self.assertEqual(cli.main(["compare-engines", str(self.source), "--pages", "1"]), 0)
        command.assert_called_once()
        cap.assert_not_called()

    def test_invalid_scope_is_an_error(self):
        from yeone_ocr.comparison_cli import parse_pages
        for value in ("999", "0", "2-1", "1-999", "bad", "1,"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_pages(value, 1)
        self.assertEqual(parse_pages("1-2,2,4", 4), [1, 2, 4])

    def test_invalid_scope_does_not_create_a_project(self):
        from yeone_ocr.comparison_cli import command
        args = self.args(); args.pages = "99"
        with patch("yeone_ocr.comparison_bridge.open_project") as opened:
            self.assertEqual(command(args), 2)
        opened.assert_not_called()
        self.assertFalse((self.root / "job").exists())

    def test_missing_source_is_a_clean_cli_error(self):
        from yeone_ocr.comparison_cli import command
        args = self.args(); args.pdf = str(self.root / "missing.pdf")
        self.assertEqual(command(args), 2)
        self.assertFalse((self.root / "job").exists())


if __name__ == "__main__":
    unittest.main()
