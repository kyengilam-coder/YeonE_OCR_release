"""결과 폴더 짜임: 맨 위에는 out(문서) · review.html(검수 화면) · report.md(보고)와 작업자료/ 만 둔다.
작업 폴더(--work)의 이름이 '작업자료'면 그 부모가 결과 폴더이고, 이전 판처럼 작업 자료가 맨 위에 펼쳐진 폴더는 그대로 쓰다가
[결과 폴더 정리](state.tidy_result_dir)로 한 폴더에 옮긴다 — 같은 폴더 안의 이름 바꾸기이며, 판독 기록은 그대로 이어 쓴다."""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ops import gui_module  # noqa: E402

from yeone_ocr import cli  # noqa: E402
from yeone_ocr.state import (INTERNAL_DIR, PUBLIC_NAMES, Work, has_flat_work, public_root,  # noqa: E402
                             tidy_result_dir, work_dir_in)


def _pdf(path: Path, pages: int = 2) -> Path:
    with pymupdf.open() as doc:
        for _ in range(pages):
            doc.new_page(width=300, height=400)
        doc.save(path)
    return path


def _run(pdf: Path, work: Path) -> int:
    with contextlib.redirect_stdout(io.StringIO()):
        return cli.main(["run", str(pdf), "--work", str(work), "--reader", "mock", "--ocr-strategy", "ai-full",
                         "--no-adjudicate", "--no-second-pass", "--no-context", "--checkpoint", "0"])


class Layout(unittest.TestCase):
    def setUp(self):
        self.td = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.pdf = _pdf(self.td / "책.pdf")
        self.root = self.td / "책_YeonE_OCR"
        cli.CANCEL.clear()

    def test_new_result_folder_keeps_only_documents_review_and_report_on_top(self):
        work = work_dir_in(self.root)
        self.assertEqual(work, self.root / INTERNAL_DIR)
        self.assertEqual(_run(self.pdf, work), 0)
        top = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(top, sorted(["out", "review.html", "report.md", INTERNAL_DIR]))
        self.assertTrue((self.root / "out" / "책.hwpx").is_file())
        inner = {p.name for p in work.iterdir()}
        self.assertTrue({"readings", "pages", "state.json", "source.json"} <= inner)
        self.assertFalse(inner & set(PUBLIC_NAMES))                                    # 작업자료 안에는 문서·검수 화면·보고가 없다
        html = (self.root / "review.html").read_text(encoding="utf-8")
        self.assertIn(f"{INTERNAL_DIR}/pages/", html)                                  # 검수 화면의 쪽 그림은 작업자료 안을 가리킨다
        self.assertEqual(_run(self.pdf, work), 0)                                      # 다시 실행: 같은 자리를 이어 쓴다
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), top)

    def test_explicit_work_folder_with_another_name_keeps_the_flat_layout(self):
        work = self.td / "work" / "책"
        self.assertEqual(public_root(work), work)
        self.assertEqual(_run(self.pdf, work), 0)
        self.assertTrue((work / "out" / "책.hwpx").is_file()); self.assertTrue((work / "review.html").is_file())
        self.assertTrue((work / "readings").is_dir())

    def test_older_flat_folder_is_used_in_place_until_tidied_then_resumes_without_rereading(self):
        self.assertEqual(_run(self.pdf, self.root), 0)                                 # 이전 판의 짜임(작업 자료가 맨 위)
        self.assertTrue(has_flat_work(self.root))
        self.assertEqual(work_dir_in(self.root), self.root)                             # 저절로 옮기지 않는다
        (self.root / "book_settings.json").write_text('{"pages": "1"}', encoding="utf-8")
        state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        state["local_first_project"] = str(self.root / "comparison" / "x.yeone-compare.json")
        (self.root / "comparison").mkdir(); (self.root / "comparison" / "x.yeone-compare.json").write_text("{}", encoding="utf-8")
        (self.root / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        records = sorted(p.name for p in (self.root / "readings").glob("p*.json"))
        hwpx = (self.root / "out" / "책.hwpx").read_bytes()

        done = tidy_result_dir(self.root)
        inner = self.root / INTERNAL_DIR
        self.assertEqual(done["work"], inner)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), sorted(["out", "review.html", "report.md", INTERNAL_DIR]))
        self.assertIn("readings", done["moved"]); self.assertNotIn("out", done["moved"])
        self.assertEqual(sorted(p.name for p in (inner / "readings").glob("p*.json")), records)
        self.assertEqual((self.root / "out" / "책.hwpx").read_bytes(), hwpx)          # 문서는 손대지 않는다
        self.assertTrue((inner / "book_settings.json").is_file())
        moved_state = json.loads((inner / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(moved_state["local_first_project"], str(inner / "comparison" / "x.yeone-compare.json"))
        self.assertEqual(work_dir_in(self.root), inner)
        self.assertEqual(tidy_result_dir(self.root)["moved"], [])                       # 두 번째는 할 일이 없다

        with mock.patch("yeone_ocr.reader_mock.MockReader.read_page", side_effect=AssertionError("기록 있는 쪽을 다시 읽음")):
            self.assertEqual(_run(self.pdf, inner), 0)                                  # 옮긴 기록을 그대로 이어 쓴다(다시 판독하지 않음)
        self.assertEqual(Work(str(self.pdf), str(inner)).out_dir, self.root / "out")

    def test_failed_move_puts_everything_back(self):
        self.assertEqual(_run(self.pdf, self.root), 0)
        before = sorted(p.name for p in self.root.iterdir())
        real = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(src)
            if Path(src).name == "readings":
                raise PermissionError(32, "다른 프로그램이 사용 중")
            return real(src, dst)
        with mock.patch("yeone_ocr.state.os.replace", side_effect=flaky):
            with self.assertRaises(PermissionError):
                tidy_result_dir(self.root)
        self.assertGreater(len(calls), 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)          # 반쯤 옮긴 폴더를 남기지 않는다
        self.assertEqual(work_dir_in(self.root), self.root)

    def test_empty_or_missing_folder_is_new_layout(self):
        self.assertEqual(work_dir_in(self.root), self.root / INTERNAL_DIR)
        self.assertEqual(tidy_result_dir(self.root)["moved"], [])
        self.assertFalse(self.root.exists())


class GuiTidy(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.gui = self.stack.enter_context(gui_module())
        self.td = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.pdf = _pdf(self.td / "책.pdf")
        cli.CANCEL.clear()

    def app(self):
        gui = self.gui
        app = types.SimpleNamespace(running=False, _planning=False, v_pdf=types.SimpleNamespace(get=lambda: str(self.pdf)), log=[],
                                    v_status=mock.Mock(), _book={"dir": None}, start=mock.Mock(),
                                    _save_book=mock.Mock(), _load_book=mock.Mock(), _sync_output_location=mock.Mock(), refresh_table=mock.Mock(),
                                    _pages=lambda: [], _n_records=gui.App._n_records)
        app._append = app.log.append
        for name in ("pdf", "work", "result_dir", "_need_pdf"):
            setattr(app, name, getattr(gui.App, name).__get__(app))
        return app

    def test_result_and_work_folders_for_new_and_older_books(self):
        gui = self.gui
        root = gui.result_dir_for(str(self.pdf))
        self.assertEqual(root.name, "책_YeonE_OCR")
        self.assertEqual(gui.work_dir_for(str(self.pdf)), root / INTERNAL_DIR)
        self.assertEqual(gui.book_place(root / INTERNAL_DIR), f"책_YeonE_OCR/{INTERNAL_DIR}/book_settings.json")
        self.assertEqual(_run(self.pdf, root), 0)                                       # 이전 판이 만든 결과 폴더
        self.assertEqual(gui.work_dir_for(str(self.pdf)), root)
        self.assertEqual(gui.book_place(root), "책_YeonE_OCR/book_settings.json")

    def test_tidy_asks_moves_and_rebuilds_the_review(self):
        gui = self.gui
        root = gui.result_dir_for(str(self.pdf))
        self.assertEqual(_run(self.pdf, root), 0)
        app = self.app()
        with mock.patch.object(gui.messagebox, "askyesno", return_value=False) as ask, mock.patch.object(gui, "App", gui.App):
            gui.App.tidy_result_folder(app)
        ask.assert_called_once(); self.assertTrue((root / "readings").is_dir())          # 아니요: 아무것도 옮기지 않음
        with mock.patch.object(gui.messagebox, "askyesno", return_value=True), mock.patch.object(gui.App, "_schedule_cards"):
            gui.App.tidy_result_folder(app)
        self.assertFalse((root / "readings").exists()); self.assertTrue((root / INTERNAL_DIR / "readings").is_dir())
        app._save_book.assert_called_once(); app._load_book.assert_called_once()
        argv = app.start.call_args.args[0]                                              # 검수 화면의 그림 경로가 바뀌어 다시 만든다(모델 호출 없음)
        self.assertEqual(argv[:4], ["review", str(self.pdf), "--work", str(root / INTERNAL_DIR)])
        self.assertTrue(any(INTERNAL_DIR in m for m in app.log))
        with mock.patch.object(gui.messagebox, "showinfo") as info:                     # 이미 정리됨
            gui.App.tidy_result_folder(app)
        info.assert_called_once()

    def test_tidy_failure_reports_and_keeps_the_folder(self):
        gui = self.gui
        root = gui.result_dir_for(str(self.pdf))
        self.assertEqual(_run(self.pdf, root), 0)
        before = sorted(p.name for p in root.iterdir())
        app = self.app()
        with mock.patch.object(gui.messagebox, "askyesno", return_value=True), mock.patch.object(gui.messagebox, "showerror") as err, \
                mock.patch.object(gui, "tidy_result_dir", side_effect=PermissionError(32, "사용 중")):
            gui.App.tidy_result_folder(app)
        err.assert_called_once(); self.assertIn("되돌렸습니다", err.call_args.args[1])
        self.assertEqual(sorted(p.name for p in root.iterdir()), before)
        app.start.assert_not_called()

    def test_running_blocks_tidy(self):
        gui = self.gui
        root = gui.result_dir_for(str(self.pdf))
        self.assertEqual(_run(self.pdf, root), 0)
        app = self.app(); app.running = True
        with mock.patch.object(gui.messagebox, "askyesno") as ask:
            gui.App.tidy_result_folder(app)
        ask.assert_not_called(); self.assertTrue((root / "readings").is_dir())


if __name__ == "__main__":
    unittest.main()
