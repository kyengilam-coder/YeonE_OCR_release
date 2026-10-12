"""Cost previews use current input identity without creating, archiving or repairing files."""
import contextlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image
from yeone_ocr import cli, estimate
from yeone_ocr.state import Work
try:
    from yeone_ocr import gui
except ImportError:                           # tkinter 없는 CI 에서는 명령줄 미리보기를 계속 검증한다
    gui = None


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() if p.is_file() else None for p in root.rglob("*")}


def cli_preview(source, folder, dpi=300):
    captured = []
    with mock.patch.object(estimate, "format_estimate", side_effect=lambda e: captured.append(e) or "preview"), contextlib.redirect_stdout(io.StringIO()):
        result = cli.main(["estimate", str(source), "--work", str(folder), "--dpi", str(dpi),
                           "--model", "gemini-2.5-pro", "--no-second-pass"])
    assert result == 0
    return captured[0]


def gui_preview(source, folder):
    value = lambda x: types.SimpleNamespace(get=lambda: x)
    app = types.SimpleNamespace(pdf=lambda: str(source), work=lambda: folder, fx_setting=lambda: (1300, "fixture"), cfg={},
                                v_pages=value(""), v_up=value("1"), v_second=value(False), v_model=value("gemini-2.5-pro"),
                                v_model2=value(""), v_cache=value(False), v_force=value(False), v_reader=value("gemini"))
    return gui.App.estimate_data(app)


def previews(source, folder):
    yield cli_preview(source, folder)
    if gui is not None:
        yield gui_preview(source, folder)


class PreviewSource(unittest.TestCase):
    def test_current_stale_legacy_and_broken_records_have_correct_pending_cost(self):
        for case in ("same", "changed", "dpi", "legacy", "broken"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                root = Path(td); source = root / "page.png"
                Image.new("RGB", (30, 40), "white").save(source)
                work = Work(str(source), str(root / "result")); work.ensure_source(300)
                work.save_record(1, {"pdf_page": 1, "pass1": {}, "merged": {}})
                if case == "changed":
                    Image.new("RGB", (30, 40), "red").save(source)
                elif case == "dpi":
                    manifest = json.loads((work.dir / "source.json").read_text(encoding="utf-8"))
                    manifest["dpi"] = 144
                    (work.dir / "source.json").write_text(json.dumps(manifest), encoding="utf-8")
                elif case == "legacy":
                    (work.dir / "source.json").unlink()
                elif case == "broken":
                    work.record_path(1).write_text("{", encoding="utf-8")
                before = snapshot(root)
                for preview in previews(source, work.dir):
                    self.assertEqual(preview["already_read"], 1 if case == "same" else 0)
                    self.assertEqual(preview["calls"], 0 if case == "same" else 1)
                    if case in ("changed", "dpi", "legacy"):
                        self.assertTrue(any("모두 새로 판독" in note for note in preview["notes"]))
                self.assertEqual(snapshot(root), before)

    def test_preview_does_not_create_a_new_work_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = root / "page.png"; folder = root / "not-created"
            Image.new("RGB", (30, 40), "white").save(source)
            before = snapshot(root)
            for preview in previews(source, folder):
                self.assertEqual(preview["calls"], 1)
            self.assertFalse(folder.exists())
            self.assertEqual(snapshot(root), before)

    def test_cli_uses_requested_dpi_for_preview(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = root / "page.png"
            Image.new("RGB", (30, 40), "white").save(source)
            work = Work(str(source), str(root / "result")); work.ensure_source(144)
            work.save_record(1, {"pdf_page": 1, "pass1": {}, "merged": {}})
            before = snapshot(root)
            self.assertEqual(cli_preview(source, work.dir, dpi=144)["calls"], 0)
            self.assertEqual(cli_preview(source, work.dir, dpi=300)["calls"], 1)
            self.assertEqual(snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
