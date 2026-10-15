"""PDF change request: GUI dispatch and output options, with no real API or clipboard."""
import contextlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image

from test_ops import gui_module
from test_gui_settings_merge import Var, fake_app


class RequestGui(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.gui = self.stack.enter_context(gui_module())
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    def test_pdf_button_reads_selected_pages_and_ignores_document_settings(self):
        app = SimpleNamespace(_need_pdf=lambda: True, _prepare_env=lambda: True,
                              pdf=lambda: str(self.root / "source.png"), work=lambda: self.root / "work",
                              nb=mock.Mock(), _save_book=mock.Mock(), start=mock.Mock(),
                              _pages=lambda: ["--pages", "2-4"],
                              _read_opts=lambda: ["--reader", "mock", "--mode", "horizontal_zh"],
                              _glyph_opts=lambda **kw: ["--no-suspects"],
                              _answered_opts=lambda: ["--skip-answered", "any-model"],
                              _build_opts=mock.Mock(side_effect=AssertionError("PDF does not use HWPX settings")))
        plans = []
        def plan(kind, go):
            plans.append(kind); go(["--no-adjudicate"])
        app._with_plan = plan
        self.gui.App.do_run_pdf(app)
        argv = app.start.call_args.args[0]
        self.assertEqual(plans, ["run-pdf"])
        self.assertEqual(argv[:2], ["run", str(self.root / "source.png")])
        for flag in ("--pdf-only", "--pages", "2-4", "--no-adjudicate", "--no-suspects", "horizontal_zh"):
            self.assertIn(flag, argv)
        self.assertEqual(app.start.call_args.kwargs["open_after"], self.root / "work/out/source_글자층.pdf")
        app._save_book.assert_called_once_with(create=True)
        app._build_opts.assert_not_called()

    def test_document_switches_and_rule_path_are_forwarded_and_saved(self):
        app = fake_app(self.gui, dict(self.gui.DEFAULTS), traditional=False, yun=False, punctuation=True,
                       punctuation_rules="C:/rules/custom.json")
        args = self.gui.App._build_opts(app)
        for flag in ("--no-traditional", "--no-yun", "--no-context-model", "--punctuation-rules"):
            self.assertIn(flag, args)
        self.assertNotIn("--no-punctuation", args)
        with mock.patch.object(self.gui, "write_config"):
            self.gui.App.save_settings(app, quiet=True)
        self.assertFalse(app.cfg["normalize_traditional"])
        self.assertFalse(app.cfg["normalize_yun"])
        self.assertTrue(app.cfg["normalize_punctuation"])
        self.assertEqual(app.cfg["punctuation_rules"], "C:/rules/custom.json")

    def test_invalid_enabled_rule_file_blocks_before_api_preparation(self):
        app = SimpleNamespace(_need_pdf=lambda: True, v_punctuation=Var(True),
                              v_punctuation_rules=Var(str(self.root / "missing.json")),
                              _prepare_env=mock.Mock(), v_ctxmodel=Var(False))
        with mock.patch.object(self.gui.messagebox, "showerror") as error:
            self.gui.App.do_run(app)
            self.gui.App.do_build(app)
        self.assertEqual(error.call_count, 2)
        app._prepare_env.assert_not_called()
        self.assertFalse(hasattr(app, "_env_need_key"))
        app.v_punctuation.set(False)
        self.assertTrue(self.gui.App._corrections_ok(app))

    def test_custom_rules_validation_accepts_editor_file_format(self):
        path = self.root / "punctuation.json"
        path.write_text(json.dumps({"rules": [{"find": "。", "replace": ". "}]}), encoding="utf-8")
        app = SimpleNamespace(v_punctuation=Var(True), v_punctuation_rules=Var(str(path)))
        self.assertTrue(self.gui.App._corrections_ok(app))


class PreviewImages(unittest.TestCase):
    def test_preview_honors_orientation_and_leaves_original_unchanged(self):
        from yeone_ocr.gui_preview import preview_image
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rotated.jpg"
            image = Image.new("RGB", (100, 50), "white")
            exif = image.getexif(); exif[274] = 6
            image.save(path, exif=exif); image.close()
            before = path.read_bytes()
            with preview_image(str(path), (40, 40)) as preview:
                self.assertEqual(preview.size, (20, 40))
                self.assertEqual(preview.mode, "RGB")
            self.assertEqual(path.read_bytes(), before)

    def test_preview_of_bundle_uses_first_page_and_fits_box(self):
        import pymupdf
        from yeone_ocr.gui_preview import preview_image
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bundle.pdf"
            with pymupdf.open() as doc:
                doc.new_page(width=100, height=200)
                doc.new_page(width=200, height=100)
                doc.save(path)
            with preview_image(str(path), (50, 50)) as preview:
                self.assertEqual(preview.size, (25, 50))


if __name__ == "__main__":
    unittest.main()
