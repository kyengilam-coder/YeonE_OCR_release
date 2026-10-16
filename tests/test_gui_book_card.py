"""작업 탭(서재 모양)의 책 카드·실행 카드·장부: 책 수치(state.book_counts), 경로 가운데 줄임, 끝난 뒤 캡션, 장부(마지막 비용 어림 —
미리 보기는 아무것도 쓰지 않고 실행할 때만 결과 폴더의 last_cost.json 에, 칸을 바꾸면 '어림 다시 필요'), 실행 중 단추 잠금(도는 실행의 자리만 잠긴 모양,
열기 단추는 사본으로 열림, 원본이 없으면 실행 단추가 꺼짐), 이 책의 설정 판(인쇄 쪽수는 값이 있을 때만 · 저장 상태).
진짜 창이 필요한 시험은 화면(xvfb)이 없으면 건너뛴다."""
import contextlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ops import _real_tk, gui_module


def _pdf(path: Path, pages: int) -> Path:
    import pymupdf
    with pymupdf.open() as doc:
        for _ in range(pages):
            doc.new_page(width=300, height=400)
        doc.save(path)
    return path


class Measure:
    """tkfont.Font 대신: 글자 하나 = 1."""
    def measure(self, text):
        return len(text)


class BookCountsAndText(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.gui = self.stack.enter_context(gui_module())
        self.td = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    def test_book_counts_reads_page_count_and_record_files_only(self):
        from yeone_ocr.state import book_counts
        pdf = _pdf(self.td / "책.pdf", 5)
        work = self.td / "책_YeonE_OCR"
        self.assertEqual(book_counts(str(pdf), work), {"target": 5, "records": 0, "to_read": 5})
        self.assertFalse(work.exists())                                                   # 세기만 — 결과 폴더를 만들지 않는다
        (work / "readings").mkdir(parents=True)
        for n in (1, 3, 9):
            (work / "readings" / f"p{n:03d}.json").write_text("{}", encoding="utf-8")
        (work / "readings" / "pxyz.json").write_text("{}", encoding="utf-8")              # 쪽 번호가 아닌 이름은 세지 않는다
        self.assertEqual(book_counts(str(pdf), work), {"target": 5, "records": 2, "to_read": 3})
        self.assertEqual(book_counts(str(pdf), work, "2-4"), {"target": 3, "records": 1, "to_read": 2})
        self.assertEqual(book_counts(str(pdf), work, "", force=True), {"target": 5, "records": 2, "to_read": 5})   # 다시 판독이면 모두 새로
        with self.assertRaises(ValueError):
            book_counts(str(pdf), work, "가-나")

    def test_book_counts_for_image_file_and_folder(self):
        from PIL import Image
        from yeone_ocr.state import book_counts
        folder = self.td / "사진"; folder.mkdir()
        for i in (1, 2, 10):
            Image.new("RGB", (20, 30), "white").save(folder / f"p{i}.png")
        self.assertEqual(book_counts(str(folder / "p1.png"), self.td / "w1")["target"], 1)
        self.assertEqual(book_counts(str(folder), self.td / "w2")["target"], 3)
        empty = self.td / "빈"; empty.mkdir()
        with self.assertRaises(ValueError):
            book_counts(str(empty), self.td / "w3")

    def test_last_cost_file_does_not_make_a_folder_look_used(self):
        """실행 직전에 남긴 last_cost.json·book_settings.json 만 있는 결과 폴더는 새 작업이다 — 원본 지문을 등록한다(이전 결과로 보지 않음)."""
        from yeone_ocr.state import BOOK_SETTINGS, LAST_COST, Work
        pdf = _pdf(self.td / "b.pdf", 1)
        work = self.td / "b_YeonE_OCR"; work.mkdir()
        (work / LAST_COST).write_text("{}", encoding="utf-8"); (work / BOOK_SETTINGS).write_text("{}", encoding="utf-8")
        Work(str(pdf), str(work))
        self.assertTrue((work / "source.json").is_file())

    def test_elide_middle_keeps_both_ends(self):
        f = Measure()
        self.assertEqual(self.gui.elide_middle("짧은 경로", f, 20), "짧은 경로")
        path = "D:\\本草\\아주 긴 폴더 이름\\개보본초.ocr.pdf"
        out = self.gui.elide_middle(path, f, 16)
        self.assertLessEqual(len(out), 16); self.assertIn("…", out)
        self.assertTrue(out.startswith("D:\\")); self.assertTrue(out.endswith(".ocr.pdf"))   # 앞은 드라이브, 뒤는 파일 이름
        self.assertEqual(self.gui.elide_middle(path, f, 1), "…")

    def test_finish_caption_names_the_run_to_press_again(self):
        fc = self.gui.finish_caption
        self.assertIn("HWPX와 검수 화면", fc(0, "run", False))
        self.assertIn("문자 PDF를 열었습니다", fc(0, "run-pdf", False))
        self.assertIn("[HWPX 만들기]를 다시 누르면 실패한 쪽만", fc(3, "run", True))
        self.assertIn("[문자 PDF 만들기]를 다시 누르면 실패한 쪽만", fc(3, "run-pdf", True))
        self.assertIn("[HWPX 만들기]를 다시 누르면 실패한 9쪽만 다시 판독하고", fc(3, "run", True, (9,)))     # F_done — 쪽 번호까지
        self.assertIn("실패한 1, 2, 3, 4, 5…쪽만", fc(3, "run", True, (1, 2, 3, 4, 5, 6)))
        self.assertIn("주의 줄", fc(3, "run", False))
        self.assertIn("3차 판별을 이어서", fc(4, "run", False))
        self.assertIn("[결과 폴더]", fc(5, "run", False))
        self.assertIn("이전 완성 PDF를 그대로", fc(130, "run-pdf", False))
        self.assertIn("오류: 줄", fc(1, "run", False))
        self.assertEqual(fc(0, "read", False), self.gui.READY_CAP)                          # 한 번에 인식이 아닌 명령은 평소 캡션


class Ledger(unittest.TestCase):
    """장부(마지막 비용 어림): 미리 보기는 메모리에만, 실행은 go() 안에서만 결과 폴더(last_cost.json)에 — [취소]는 아무것도 쓰지 않는다."""
    def setUp(self):
        from test_gui_settings_merge import _records_work
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.gui = self.stack.enter_context(gui_module())
        self.td = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(mock.patch.object(self.gui, "CONFIG_PATH", self.td / "cfg" / "config.json"))
        self.pdf, self.work = _records_work(self.gui, self.td)

    def app(self, choice):
        from test_gui_settings_merge import _plan_app
        app = _plan_app(self.gui, self.pdf, self.work)
        self.stack.enter_context(mock.patch.object(self.gui, "CostDialog", lambda *a, **k: types.SimpleNamespace(choice=choice)))
        return app

    def plan(self, app, kind="run", go=None, pdf_only=False):
        from test_gui_settings_merge import ImmediateThread
        with mock.patch.object(self.gui.threading, "Thread", ImmediateThread), mock.patch.object(self.gui, "PlanWait"):
            app._with_plan("run-pdf" if pdf_only else kind, go)
            app._poll()

    def last_cost(self) -> Path:
        return Path(self.work.dir) / self.gui.LAST_COST

    def test_preview_fills_ledger_in_memory_without_writing(self):
        app = self.app(None)
        before = sorted(p.name for p in Path(self.work.dir).iterdir())
        self.plan(app)                                                                     # [비용 미리 보기]
        self.assertEqual(sorted(p.name for p in Path(self.work.dir).iterdir()), before)
        led = app._ledger
        self.assertFalse(led["saved"]); self.assertEqual((led["action"], led["kind"]), ("hwpx", "run"))
        self.assertEqual(led["pages"], 1); self.assertTrue(led["rows"]); self.assertIn("sig", led)
        self.assertFalse(self.gui.App._ledger_stale(app, led))
        app.v_pages.set("1")                                                               # 칸을 바꾸면 '어림 다시 필요'
        self.assertTrue(self.gui.App._ledger_stale(app, led))
        app.v_pages.set(""); app.v_model.set("gpt-4.1-mini")
        self.assertTrue(self.gui.App._ledger_stale(app, led))

    def test_run_writes_last_cost_only_inside_go_and_cancel_writes_nothing(self):
        from yeone_ocr import costplan
        app = self.app(None)
        go = mock.Mock(side_effect=lambda extra: self.gui.App._save_ledger(app))
        with mock.patch.object(costplan, "needs_confirm", return_value=True):
            self.plan(app, go=go)                                                          # 비용 창에서 [취소]
        go.assert_not_called(); self.assertFalse(self.last_cost().exists())
        self.assertIsNone(getattr(app, "_ledger_pending", None))
        app = self.app("all")
        go = mock.Mock(side_effect=lambda extra: self.gui.App._save_ledger(app))
        with mock.patch.object(costplan, "needs_confirm", return_value=True):
            self.plan(app, go=go)                                                          # [예 · 모두 실행]
        go.assert_called_once_with([])
        saved = json.loads(self.last_cost().read_text(encoding="utf-8"))
        self.assertEqual((saved["choice"], saved["action"], saved["saved"]), ("all", "hwpx", True))
        self.assertEqual(app._ledger_for, "run"); self.assertTrue(app._ledger["saved"])
        for k in ("rows", "totals", "krw", "models", "fx", "sig"):
            self.assertIn(k, saved)
        self.assertEqual({tuple(sorted(r)) for r in saved["rows"]}, {("calls", "scope", "step", "usd")})   # 요약만(근거·토큰은 남기지 않음)

    def test_pdf_run_ledger_compares_with_pdf_inputs(self):
        from yeone_ocr import costplan
        app = self.app("without")
        go = mock.Mock(side_effect=lambda extra: self.gui.App._save_ledger(app))
        with mock.patch.object(costplan, "needs_confirm", return_value=True):
            self.plan(app, go=go, pdf_only=True)
        go.assert_called_once_with(["--no-adjudicate"])
        led = app._ledger
        self.assertEqual((led["action"], led["choice"], app._ledger_for), ("pdf", "without", "run-pdf"))
        self.assertFalse(led["sig"]["context_model"])
        self.assertFalse(self.gui.App._ledger_stale(app, led))                            # 문자 PDF 의 입력(문맥 교정 없음)으로 견준다
        app.v_ctxmodel.set(False)                                                          # 문자 PDF 는 문맥 교정 모델을 쓰지 않으므로 바꿔도 낡지 않는다
        self.assertFalse(self.gui.App._ledger_stale(app, led))
        app.v_pages.set("1")
        self.assertTrue(self.gui.App._ledger_stale(app, led))

    def test_ledger_rows_group_steps_and_without_choice(self):
        app = self.app(None)
        self.plan(app)
        app._calls_by_stage = {}
        view = self.gui.App._ledger_rows(app, app._ledger, False, False, False, False, None)
        names = [g[0] for g in view[:-1]]
        self.assertEqual(names, ["판독", "모델 단계"])
        self.assertTrue(view[1][2])                                                        # 모델 단계에만 [아니요]면 뺌
        self.assertFalse(view[0][2])
        self.assertTrue(view[0][3].startswith("예상 "))
        self.assertEqual(view[-1][1], "합계")
        app._calls_by_stage = {"prepare": 0, "read": 2}; app._calls_tail = ("read", 1)    # 이번 실행: 판독 몫 3회(알림 사이에 읽은 1회 포함)
        led = dict(app._ledger, choice="all")
        view = self.gui.App._ledger_rows(app, led, True, True, False, False, 3)
        hi = sum(r["calls"][1] for r in led["rows"] if r["step"] in ("read1", "read2"))
        self.assertTrue(view[0][3].startswith("호출 3회 / 예상 "), view[0][3])
        self.assertEqual(view[0][5], "over" if 3 > hi else "plain")
        self.assertTrue(view[1][3].startswith("아직 · 예상 "))                            # 모델 단계는 아직
        app._calls_by_stage = {"read": hi + 4}; app._calls_tail = None
        self.assertEqual(self.gui.App._ledger_rows(app, led, True, True, False, False, hi + 4)[0][5], "over")   # 어림보다 많으면 주의 색
        led = dict(app._ledger, choice="without")
        view = self.gui.App._ledger_rows(app, led, True, True, False, False, 3)
        self.assertEqual(view[1][3], "이번엔 뺌")
        self.assertTrue(all(r[1] == "이번엔 뺌" and r[2] == "muted" for r in view[1][4]))   # 모델 단계 줄은 지우지 않고 흐리게
        self.assertEqual(view[-1][1], "합계(이번 실행)")

    def test_load_ledger_reads_saved_file_and_ignores_broken(self):
        app = self.app(None)
        self.last_cost().write_text("{", encoding="utf-8")
        self.gui.App._load_ledger(app); self.assertIsNone(app._ledger)
        self.last_cost().write_text(json.dumps({"kind": "run", "rows": [], "totals": {}, "action": "pdf"}), encoding="utf-8")
        self.gui.App._load_ledger(app); self.assertTrue(app._ledger["saved"]); self.assertEqual(app._ledger["action"], "pdf")


class WorkTabRealTk(unittest.TestCase):
    """진짜 창(xvfb): 원본이 없으면 실행 단추가 꺼지고, 실행 중에는 도는 실행의 자리만 잠긴 모양·[■ 중지]·열기 단추는 살고, 판의 쪽 범위 칸은 잠긴다."""
    @classmethod
    def setUpClass(cls):
        _real_tk().destroy()
        cls.stack = contextlib.ExitStack()
        cls.gui = gui = cls.stack.enter_context(gui_module())
        cls.td = td = Path(cls.stack.enter_context(tempfile.TemporaryDirectory()))
        cfg = td / "cfg" / "config.json"; cfg.parent.mkdir()
        cfg.write_text(json.dumps({"auto_update": False, "reader": "gemini", "model": "gemini-2.5-flash", "fx_auto": 1400, "fx_auto_date": "2099-01-01",
                                   "input_tab": 0}), encoding="utf-8")
        for target, name, value in ((gui, "CONFIG_PATH", cfg), (gui, "missing_packages", lambda: []), (gui.App, "_fetch_fx", lambda self: None)):
            cls.stack.enter_context(mock.patch.object(target, name, value))
        cls.app = gui.App(); cls.app.withdraw()
        cls.pdf = _pdf(td / "개보본초.ocr.pdf", 3)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.destroy()
        finally:
            cls.stack.close()

    def settle(self):
        for _ in range(3):
            self.app.update()
            if getattr(self.app, "_cards_job", None):
                self.app._sync_cards()

    def state(self, b):
        return str(b.cget("state"))

    def test_1_no_source_disables_run_and_open_buttons(self):
        app = self.app; app.select_tab(0); app.v_pdf.set(""); self.settle()
        for b in (app.b_run, app.b_run_pdf, app.b_estimate, app.b_open_hwpx, app.b_open_pdf, app.b_open_review, app.b_open_work):
            self.assertEqual(self.state(b), "disabled", b.cget("text"))
        w = app._book_cards[0]
        self.assertEqual(w["title"].cget("text"), "원본을 고르십시오")
        self.assertEqual(str(w["pick"].cget("style")), "Ink.TButton")                       # 빈 상태의 주 단추는 먹 채움
        self.assertEqual(app.v_output_dir.get(), "")
        self.assertEqual(w["save_state"].cget("text"), "원본이 없어 아직 저장할 곳이 없습니다")

    def test_2_source_enables_and_running_locks_only_its_own_slot(self):
        app = self.app; gui = self.gui
        app.select_tab(0); app._use_pdf(str(self.pdf)); self.settle()
        w = app._book_cards[0]
        self.assertEqual((w["title"].cget("text"), w["fname"].cget("text")), ("개보본초", "개보본초.ocr.pdf"))
        for b in (app.b_run, app.b_run_pdf, app.b_estimate, app.b_open_hwpx):
            self.assertEqual(self.state(b), "normal")
        self.assertEqual(w["save_state"].cget("text"), "결과 폴더가 생기면(첫 실행) 저장")
        for b in (app.b_open_hwpx, app.b_open_pdf, app.b_open_review, app.b_open_work):
            self.assertNotIn(b, app.buttons)                                                # 열기만 하는 단추는 잠그지 않는다
        entry = w["pages_parts"][0]
        app._run_label = "한 번에 인식 (전체 → HWPX)"
        app._set_running(True, "run"); app._on_progress("read", 1, 3); self.settle()
        try:
            self.assertEqual(str(app.b_run.cget("style")), "SealBusy.TButton")
            self.assertIn("한 번에 인식 (HWPX) · 판독 중", app.b_run.cget("text"))
            lock = str(app.icons["lock_seal"])                                              # 잠긴 단추에도 자물쇠 그림을 따로 — 점무늬(stipple)가 덮지 않게
            self.assertEqual([str(x) for x in app.tk.splitlist(app.b_run.cget("image"))], [lock, "disabled", lock])
            self.assertEqual(self.state(app.b_run_pdf), "disabled"); self.assertEqual(str(app.b_run_pdf.cget("style")), "SealOut.TButton")
            self.assertEqual(self.state(app.b_stop), "normal")
            self.assertEqual(self.state(app.b_open_hwpx), "normal")
            self.assertIn("disabled", entry.state())
            self.assertTrue(w["collapsed"])                                                 # 실행 중에는 책 카드를 한 줄로 접는다
            gui.App._toggle_locked_book(app, 0); self.settle()
            self.assertFalse(w["collapsed"]); self.assertEqual(w["panel_cap"].cget("text"), "실행 중에는 잠김 · 끝나면 고칠 수 있음")
            from yeone_ocr import cli
            try:
                app.do_stop()
            finally:
                cli.CANCEL.clear()                                                          # 다른 시험의 명령이 멈추지 않게
            self.assertEqual((app.b_stop.cget("text"), self.state(app.b_stop)), ("멈추는 중…", "disabled"))
        finally:
            app._set_running(False); self.settle()
        self.assertEqual((str(app.b_run.cget("style")), app.b_run.cget("text")), ("Seal.TButton", gui.RUN_TEXT))
        self.assertEqual(app.b_stop.cget("text"), "■  중지")
        self.assertNotIn("disabled", entry.state())
        self.assertFalse(w["collapsed"])
        app._set_running(True, "run-pdf"); self.settle()
        try:
            self.assertEqual(str(app.b_run_pdf.cget("style")), "SealOutBusy.TButton")
            self.assertEqual(str(app.b_run.cget("style")), "Seal.TButton"); self.assertEqual(self.state(app.b_run), "disabled")
        finally:
            app._set_running(False); self.settle()

    def test_3_print_offset_row_only_when_set_and_save_state(self):
        app = self.app
        app.select_tab(0); app._use_pdf(str(self.pdf)); self.settle()
        w = app._book_cards[0]
        app.v_off.set(""); self.settle()
        self.assertFalse(w["off_row"].winfo_manager())
        app.v_off.set("29"); self.settle()
        self.assertEqual(w["off_row"].winfo_manager(), "grid"); self.assertEqual(w["off_val"].cget("text"), "+ 29")
        app.v_off.set("-3"); self.settle()
        self.assertEqual(w["off_val"].cget("text"), "− 3")
        app.v_off.set(""); self.settle()
        app._save_book(create=True); self.settle()
        self.assertEqual(w["save_state"].cget("text"), "결과 폴더에 저장됨")
        app.v_pages.set("1-2"); self.settle()
        self.assertTrue(w["save_state"].cget("text").startswith("바뀜"))
        app.v_pages.set(""); self.settle()

    def test_4_open_result_while_running_opens_a_copy(self):
        app = self.app; gui = self.gui
        app.select_tab(0); app._use_pdf(str(self.pdf)); self.settle()
        out = app.work() / "out"; out.mkdir(parents=True, exist_ok=True)
        hwpx = out / "개보본초.ocr.hwpx"; hwpx.write_bytes(b"result")
        with mock.patch.object(gui, "open_path") as op:
            app.open_hwpx()
            op.assert_called_once_with(hwpx)
            app.running = True
            try:
                app.open_hwpx()
            finally:
                app.running = False
        copy = op.call_args.args[0]
        self.assertNotEqual(copy, hwpx); self.assertIn("_보기_", copy.name); self.assertEqual(copy.read_bytes(), b"result")
        copy.unlink()

    def test_5_image_tab_has_its_own_card_and_paste_button(self):
        app = self.app
        app.select_tab(2); self.settle()
        w = app._book_cards[2]
        self.assertIsNotNone(app.image_preview.empty_widget)
        if not app._input_vars[2]["pdf"].get():
            self.assertEqual(self.state(w["clear"]), "disabled")
            self.assertEqual(w["title"].cget("text"), "원본을 고르십시오")
        app.select_tab(0); self.settle()


if __name__ == "__main__":
    unittest.main()
