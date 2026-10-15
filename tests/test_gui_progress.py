"""작업 탭의 진행 카드(서재 모양): 단계 띠(한 번에 인식 PLANS['run'] · 문자 PDF ['run-pdf'])의 동그라미·영수증·꼬리표·비용 줄, 상태 알약,
큰 숫자 줄, 쪽 띠(칸 나누기·눈금·칸 모양)와 실행 전 칸 상태(state.page_marks), 그리고 진짜 창에서의 배치(위·아래 칸 없이 기록 카드가 남은 높이를
채움 · 고급 도구 카드 · 실행 중 잠금 · 열기 단추는 잠그지 않음). 진짜 창이 필요한 시험은 화면(xvfb)이 없으면 건너뛴다."""
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ops import _real_tk, gui_module

ROWS = [{"step": "read1", "calls": [45, 45], "usd": [2.82, 4.94]}, {"step": "read2", "calls": [90, 90], "usd": [2.61, 3.74]},
        {"step": "glyph", "calls": [0, 0], "usd": [0.0, 0.0]}, {"step": "suspect", "calls": [45, 180], "usd": [0.27, 1.41]},
        {"step": "choice", "calls": [15, 15], "usd": [0.48, 0.96]}]
COUNTS = {"target": 46, "records": 1, "to_read": 45}


def text(line):
    return "".join(t for t, _ in line) if line != "TAG" else "TAG"


class BandView(unittest.TestCase):
    def setUp(self):
        from yeone_ocr import gui_progress
        self.G = gui_progress

    def test_hwpx_and_pdf_runs_use_their_own_plan(self):
        hw = self.G.band_view("run", phase="ready", counts=COUNTS, rows=ROWS, est="ok")
        pdf = self.G.band_view("run-pdf", phase="ready", counts=COUNTS, rows=ROWS, est="ok")
        self.assertEqual([v["name"] for v in hw], ["준비", "판독", "3차 판별", "문서 만들기", "검수 화면", "보고"])
        self.assertEqual([v["name"] for v in pdf], ["준비", "판독", "3차 판별", "글자층 넣기", "검수 화면", "보고"])
        self.assertEqual([v["receipt"] for v in hw], [False, True, True, True, False, False])
        self.assertEqual([v["receipt"] for v in pdf], [False, True, True, False, False, False])     # 글자층 넣기는 모델을 부르지 않음
        self.assertEqual([text(ln) for ln in pdf[3]["lines"]], ["문자 PDF", "모델 안 씀"])
        self.assertNotIn("TAG", pdf[3]["lines"])

    def test_tag_only_on_model_steps(self):
        for cmd in ("run", "run-pdf"):
            view = self.G.band_view(cmd, phase="ready", counts=COUNTS, rows=ROWS, est="ok")
            tagged = [v["key"] for v in view if v["tag_top"] or "TAG" in v["lines"]]
            self.assertEqual(tagged, ["adjudicate", "build"] if cmd == "run" else ["adjudicate"])
        off = self.G.band_view("run", phase="ready", counts=COUNTS, rows=ROWS, est="ok", ctx_model=False)
        self.assertEqual([text(ln) for ln in off[3]["lines"]], ["HWPX", "문맥 교정 모델 꺼짐(설정)"])
        self.assertFalse(off[3]["receipt"])

    def test_ready_lines_and_cost_from_ledger(self):
        view = self.G.band_view("run", phase="ready", counts=COUNTS, rows=ROWS, est="ok")
        self.assertEqual([text(ln) for ln in view[0]["lines"]], ["기록 있는 1쪽", "나머지 45쪽은", "판독하며 준비", "모델 안 씀"])
        self.assertEqual([text(ln) for ln in view[1]["lines"]], ["새로 읽을 45쪽", "1차 예상 $2.82~4.94", "2차 예상 $2.61~3.74"])
        self.assertIn(("$2.82~4.94", "cap_b"), view[1]["lines"][1])                                       # 돈 드는 금액은 굵게
        self.assertEqual([text(ln) for ln in view[2]["lines"]], ["글자 판별·합의 의심", "예상 $0.27~1.41", "호출 45~180회"])
        self.assertEqual([text(ln) for ln in view[3]["lines"]], ["HWPX", "문맥 교정(번체)", "TAG", "예상 $0.48~0.96", "호출 15회"])
        none = self.G.band_view("run", phase="ready", counts=COUNTS, rows=None, est="none")
        self.assertEqual(text(none[1]["lines"][-1]), "어림 전")
        stale = self.G.band_view("run", phase="ready", counts=COUNTS, rows=ROWS, est="stale")
        self.assertEqual(stale[1]["lines"][-1], [("어림 다시 필요", "warn")])
        unpriced = [dict(r, usd=None) if r["step"] == "suspect" else r for r in ROWS]
        self.assertIn(("단가 미입력", "warn"), self.G.band_view("run", phase="ready", counts=COUNTS, rows=unpriced, est="ok")[2]["lines"][1])
        empty = self.G.band_view("run", phase="empty")
        self.assertEqual([text(v["lines"][0]) for v in empty],
                         ["쪽 이미지", "1차·2차", "글자 판별·합의 의심", "HWPX", "review.html", "report.md"])

    def test_running_circles_and_status(self):
        view = self.G.band_view("run", phase="running", idx=1, done=12, total=45, seen={"prepare": (1, 1), "read": (12, 45)},
                                counts=COUNTS, rows=ROWS, est="ok")
        self.assertEqual([v["circle"] for v in view], ["done", "now", "wait", "wait", "wait", "wait"])
        self.assertEqual([v["connector"] for v in view], ["done", "plain", "plain", "plain", "plain", None])
        self.assertEqual(view[0]["lines"][0], [("끝 · 1쪽", "cap")])
        self.assertEqual(view[1]["lines"][0], [("12 / 45쪽", "cap_b")])
        self.assertEqual(text(view[2]["lines"][0]), "기다림")
        self.assertTrue(view[1]["bold"] and not view[2]["bold"])
        pdf = self.G.band_view("run-pdf", phase="running", idx=3, done=20, total=46, counts=COUNTS, rows=ROWS, est="ok")
        self.assertEqual((pdf[3]["circle"], text(pdf[3]["lines"][0])), ("now", "20 / 46쪽"))
        self.assertEqual([text(ln) for ln in pdf[3]["lines"]], ["20 / 46쪽", "문자 PDF", "모델 안 씀"])      # 실행 중에도 무엇을 만드는지(F_work_pdf)
        wait = self.G.band_view("run-pdf", phase="running", idx=1, done=12, total=45, counts=COUNTS, rows=ROWS, est="ok")
        self.assertEqual([text(ln) for ln in wait[3]["lines"]], ["기다림", "문자 PDF", "모델 안 씀"])
        fin = self.G.band_view("run-pdf", phase="done", idx=5, result={"kind": "ok", "rc": 0})
        self.assertEqual([text(ln) for ln in fin[3]["lines"]], ["끝 · 문자 PDF", "모델 안 씀"])                # 끝 줄이 이미 말함 — 겹치지 않게

    def test_finished_states(self):
        seen = {"prepare": (1, 1), "read": (45, 45), "adjudicate": (45, 45), "build": (46, 46), "review": (1, 1), "report": (1, 1)}
        failed = self.G.band_view("run", phase="done", idx=5, seen=seen, counts=COUNTS, rows=ROWS, est="ok",
                                  result={"kind": "partial", "rc": 3, "failed": True, "failed_n": 1})
        self.assertEqual([v["circle"] for v in failed], ["done", "fail", "done", "done", "done", "done"])
        self.assertEqual(failed[1]["lines"][0], [("끝 44쪽 · ", "cap"), ("실패 1쪽", "red_b")])
        self.assertEqual(text(failed[3]["lines"][0]), "끝 · HWPX")
        rc4 = self.G.band_view("run", phase="done", idx=5, seen=seen, result={"kind": "partial", "rc": 4})
        self.assertEqual((rc4[2]["circle"], text(rc4[2]["lines"][0])), ("fail", "못 마침 · 〔a/b〕 남음"))
        rc5 = self.G.band_view("run", phase="done", idx=5, seen=seen, result={"kind": "partial", "rc": 5})
        self.assertEqual(rc5[3]["lines"][0], [("끝 · 교정 기록 확인", "warn")])
        stop = self.G.band_view("run", phase="done", idx=1, done=12, total=45, seen=seen, result={"kind": "stopped", "rc": 130})
        self.assertEqual([v["circle"] for v in stop], ["done", "stop", "wait", "wait", "wait", "wait"])
        self.assertEqual(text(stop[1]["lines"][0]), "중지됨 · 남은 33쪽")
        err = self.G.band_view("run-pdf", phase="done", idx=3, done=3, total=46, seen=seen, result={"kind": "error", "rc": 1})
        self.assertEqual((err[3]["circle"], text(err[3]["lines"][0])), ("fail", "오류 — 진행 기록"))
        # 모든 쪽 판독이 실패한 실행(rc 2): 뒤 단계(문서 만들기·검수 화면)는 기록이 없어 아무것도 만들지 않았다 — 끝남(✓)으로 그리지 않는다
        none = self.G.band_view("run", phase="done", idx=5, seen=seen, counts=COUNTS,
                                result={"kind": "error", "rc": 2, "failed_n": 2, "failed_pages": [2, 3]})
        self.assertEqual([v["circle"] for v in none], ["done", "fail", "wait", "wait", "wait", "wait"])
        self.assertEqual((text(none[1]["lines"][0]), text(none[3]["lines"][0]), text(none[4]["lines"][0])), ("오류 — 진행 기록", "기다림", "기다림"))

    def test_without_choice_skips_model_steps(self):
        view = self.G.band_view("run", phase="running", idx=1, done=3, total=45, without=True, counts=COUNTS, rows=ROWS, est="ok")
        self.assertEqual((view[2]["circle"], text(view[2]["lines"][0])), ("skip", "이번엔 뺌"))
        self.assertEqual([text(ln) for ln in view[3]["lines"]], ["기다림", "문맥 교정(번체)", "TAG", "이번엔 뺌"])

    def test_pill_big_and_task(self):
        G = self.G
        self.assertEqual(G.pill_view(phase="empty"), ("원본 없음", "mute"))
        self.assertEqual(G.pill_view(phase="ready"), ("준비됨", "idle"))
        self.assertEqual(G.pill_view(phase="running", stage="read"), ("● 판독 중", "run"))
        self.assertEqual(G.pill_view(phase="running", stage="textlayer"), ("● 글자층 넣기 중", "run"))
        self.assertEqual(G.pill_view(phase="done", result={"kind": "ok", "rc": 0}), ("✓ 끝", "green"))
        self.assertEqual(G.pill_view(phase="done", result={"kind": "partial", "rc": 3, "failed": True}), ("! 끝 · 실패한 쪽 있음", "fail"))
        self.assertEqual(G.pill_view(phase="done", result={"kind": "stopped", "rc": 130}), ("■ 중지됨", "mute"))
        ready = G.big_view("run", phase="ready", counts=COUNTS)
        self.assertEqual(ready[:3], [("새로 읽을", "m"), ("45", "big"), ("쪽", "unit")])
        self.assertIn("판독 기록 있는 1쪽은 건너뜀", ready[3][0])
        shot = G.big_view("run", phase="ready", counts={"target": 1, "records": 0, "to_read": 1}, screenshot=True)
        self.assertEqual(shot[-1][0], " · 스크린샷 한 장 · 판독 기록 없음")
        run = G.big_view("run", phase="running", stage="read", done=12, total=45)
        self.assertEqual(run, [("판독", "m"), ("12", "big"), ("/ 45쪽", "unit")])
        done = G.big_view("run", phase="done", seen={"read": (45, 45)}, result={"kind": "partial", "rc": 3, "failed": True, "failed_n": 1, "failed_pages": [9]})
        self.assertEqual(done[1], ("44", "big")); self.assertEqual(done[3], (" · 실패 1쪽(9쪽)", "red_b"))
        self.assertIn("원본을 고르면", G.big_view("run", phase="empty")[0][0])
        self.assertEqual(G.task_view("read", "③ 판독", phase="running"), {"circle": "now", "title": "이 작업: ③ 판독", "sub": "한 번에 인식의 한 단계만 따로 합니다"})
        self.assertEqual(G.task_view("textlayer", "PDF 글자층 넣기", phase="done", result={"kind": "ok"})["sub"], "")

    def test_failed_pages_from_problems(self):
        probs = ["주의: 무엇", "오류: 판독에 실패한 쪽 3개: 9, 12, 40 — ③ 판독을 다시 누르면 이 쪽들만 다시 시도합니다"]
        self.assertEqual((self.G.failed_pages(probs), self.G.failed_count(probs)), ([9, 12, 40], 3))
        self.assertEqual((self.G.failed_pages(None), self.G.failed_count([])), ([], 0))


class PageStrip(unittest.TestCase):
    def setUp(self):
        from yeone_ocr import gui_progress
        self.G = gui_progress

    def test_cells_follow_min_width(self):
        per, cells = self.G.strip_layout(46, 760, 2, 12)
        self.assertEqual((per, len(cells)), (1, 46))
        per, cells = self.G.strip_layout(462, 760, 2, 12)                                           # 명세 §5.4: 54칸까지 → 9쪽 한 칸 52칸
        self.assertEqual((per, len(cells)), (9, 52))
        self.assertGreaterEqual(min(x1 - x0 for x0, x1, _, _ in cells), 12)
        self.assertEqual((cells[0][2], cells[-1][3]), (0, 462))
        self.assertEqual(self.G.strip_layout(0, 760, 2, 12), (1, []))

    def test_ticks_are_round_numbers(self):
        per, cells = self.G.strip_layout(46, 760, 2, 12)
        self.assertEqual([t for _, t in self.G.strip_ticks(list(range(1, 47)), per, cells)], ["1", "10", "20", "30", "40", "46"])
        per, cells = self.G.strip_layout(462, 760, 2, 12)
        ticks = [t for _, t in self.G.strip_ticks(list(range(1, 463)), per, cells)]
        self.assertEqual((ticks[0], ticks[1], ticks[-1]), ("1", "50", "462"))

    def test_cell_kind_for_groups(self):
        k = self.G.cell_kind
        self.assertEqual(k(["done", "fail", "remain"]), ("fail", 0.0))
        self.assertEqual(k(["done", "now", "remain", "remain"]), ("now", 0.25))
        self.assertEqual(k(["record", "record"]), ("record", 0.0))
        self.assertEqual(k(["done"]), ("done", 1.0))
        self.assertEqual(k(["done", "record", "remain", "remain"]), ("remain", 0.5))
        self.assertEqual(k(["remain"]), ("remain", 0.0))

    def test_page_marks_from_records_and_status(self):
        from yeone_ocr.state import page_marks
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "책_YeonE_OCR"
            self.assertEqual(page_marks(work, [1, 2]), {1: "remain", 2: "remain"})                  # 결과 폴더가 없어도
            self.assertFalse(work.exists())
            (work / "readings").mkdir(parents=True)
            (work / "readings" / "p001.json").write_text("{}", encoding="utf-8")
            (work / "state.json").write_text(json.dumps({"pages": {"2": {"statuses": ["판독 확인 필요"]}, "1": {"statuses": ["판독 확인 필요"]}}}),
                                             encoding="utf-8")
            self.assertEqual(page_marks(work, [1, 2, 3]), {1: "record", 2: "fail", 3: "remain"})   # 기록이 있으면 확인 필요여도 기록 있음
            self.assertEqual(page_marks(work, [1, 2, 3], force=True), {1: "remain", 2: "fail", 3: "remain"})


class ProgressCardRealTk(unittest.TestCase):
    """진짜 창(xvfb): 위·아래 칸(PanedWindow) 없이 기록 카드가 보이는 탭의 왼쪽 열 맨 아래에 들고, 실행 중에는 진행 카드가 단계 띠를 움직이며
    고급 도구는 잠기되 열기 단추는 살아 있다."""
    @classmethod
    def setUpClass(cls):
        _real_tk().destroy()
        cls.stack = contextlib.ExitStack()
        cls.gui = gui = cls.stack.enter_context(gui_module())
        cls.td = td = Path(cls.stack.enter_context(tempfile.TemporaryDirectory()))
        cfg = td / "cfg" / "config.json"; cfg.parent.mkdir()
        cfg.write_text(json.dumps({"auto_update": False, "reader": "gemini", "model": "gemini-2.5-flash", "fx_auto": 1400, "fx_auto_date": "2099-01-01",
                                   "input_tab": 0, "advanced_open": False}), encoding="utf-8")
        for target, name, value in ((gui, "CONFIG_PATH", cfg), (gui, "missing_packages", lambda: []), (gui.App, "_fetch_fx", lambda self: None)):
            cls.stack.enter_context(mock.patch.object(target, name, value))
        import pymupdf
        cls.pdf = td / "책.pdf"
        with pymupdf.open() as doc:
            for _ in range(5):
                doc.new_page(width=300, height=400)
            doc.save(cls.pdf)
        cls.app = gui.App(); cls.app.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.destroy()
        finally:
            cls.stack.close()

    def settle(self):
        for _ in range(4):
            self.app.update()
            if getattr(self.app, "_cards_job", None):
                self.app._sync_cards()
            self.app._poll()

    def wait_strip(self):
        import time
        for _ in range(100):
            self.settle()
            if self.app._strip_pages:
                return
            time.sleep(0.05)

    def test_1_no_paned_window_and_log_card_follows_tab(self):
        app = self.app
        self.assertFalse(hasattr(app, "pane")); self.assertFalse(hasattr(app, "_bottom"))
        app.select_tab(0); self.settle()
        self.assertEqual(app._log_card.grid_info().get("in"), app._work_left)
        app.select_tab(1); self.settle()
        self.assertEqual(app._log_card.grid_info().get("in"), app._collate_cols["left"])
        self.assertEqual(app.nb.tab(1, "state"), "hidden")                                              # 책갈피 OCR 은 진행 기록만
        app.select_tab(3); self.settle()
        self.assertEqual(app._log_card.winfo_manager(), "")
        self.assertEqual(app._footer.winfo_manager(), "pack")                                           # 설정 탭의 바닥 줄
        app.select_tab(0); self.settle()
        self.assertEqual(app.nb.tab(1, "state"), "normal"); self.assertEqual(app._footer.winfo_manager(), "")

    def test_2_tools_card_toggles_between_progress_and_log(self):
        app = self.app; app.select_tab(0); self.settle()
        with mock.patch.object(self.gui, "write_config"):
            app.toggle_advanced(); self.settle()
            try:
                info = app._adv_card.grid_info()
                self.assertEqual(info.get("in"), app._work_left)
                self.assertEqual(int(info["row"]), int(app._prog_card.grid_info()["row"]) + 1)
                self.assertEqual(int(app._log_card.grid_info()["row"]), int(info["row"]) + 1)
                self.assertEqual(app.b_adv.cget("text"), "고급 도구 ▴")
                names = [b.cget("text").strip() for b in app._tool_rows[0][1] + app._tool_rows[1][1]]
                self.assertEqual(names[:6], ["① 구조 보기", "② 준비", "③ 판독", "④ 문서 만들기", "⑤ 검수 화면", "⑥ 보고"])
                self.assertIn(str(app.icons["receipt"]), str(app._tool_rows[0][1][2].cget("image")))      # 돈 드는 단추에만 영수증
                self.assertEqual(str(app._tool_rows[0][1][0].cget("image")), "")
            finally:
                app.toggle_advanced(); self.settle()
        self.assertEqual(app._adv_card.winfo_manager(), "")

    def test_3_running_moves_band_and_locks_tools_not_open_buttons(self):
        app = self.app; app.select_tab(0); app._use_pdf(str(self.pdf)); self.wait_strip()
        self.assertEqual(app._strip_pages, [1, 2, 3, 4, 5])
        self.assertEqual(app.l_pill.cget("text"), "준비됨")
        self.assertEqual([c["name"].cget("text").strip() for c in app._band_cols][3], "문서 만들기")
        app._run_label = "한 번에 인식 (전체 → HWPX)"
        app._set_running(True, "run"); app._on_progress("prepare", 0, 0); app._on_progress("read", 2, 5); self.settle()
        try:
            self.assertEqual(app.l_pill.cget("text"), "● 판독 중")
            self.assertEqual([c["head"][0] for c in app._band_cols], ["done", "now", "wait", "wait", "wait", "wait"])
            self.assertEqual(app.l_pct.cget("text"), f"전체 {(0.03 + 0.75 * 2 / 5) * 100:.0f}%")
            self.assertEqual(str(app.b_adv.cget("state")), "disabled")
            self.assertEqual(app.l_adv_cap.cget("text"), "실행 중에는 고급 도구를 쓸 수 없습니다")
            for b in (app.b_open_hwpx, app.b_open_pdf, app.b_open_review, app.b_open_work, app._book_cards[0]["source"]):
                self.assertEqual(str(b.cget("state")), "normal")
            self.assertTrue(all(str(b.cget("state")) == "disabled" for b in app._tool_rows[0][1]))
            self.assertEqual([str(x) for x in app.tk.splitlist(app._tool_rows[0][1][2].cget("image"))],     # 잠긴 영수증 단추는 흐린 영수증(점무늬 없이)
                             [str(app.icons["receipt"]), "disabled", str(app.icons["receipt_dis"])])
            self.assertEqual(str(app._tab_buttons[1].cget("state")), "disabled")
            self.assertEqual(app._lockline.winfo_manager(), "pack")                                     # 잠금 줄(한 줄 안내)
        finally:
            app._set_running(False); self.settle()
        self.assertEqual(str(app.b_adv.cget("state")), "normal")

    def test_4_pdf_run_band_and_strip_click_opens_page_row(self):
        app = self.app; app.select_tab(0); app._use_pdf(str(self.pdf)); self.wait_strip()
        app._run_label = "한 번에 인식 (문자 PDF 만들기)"
        app._set_running(True, "run-pdf"); app._on_progress("textlayer", 1, 5); self.settle()
        try:
            self.assertEqual(app._band_cols[3]["name"].cget("text").strip(), "글자층 넣기")
            self.assertEqual(str(app._band_cols[3]["name"].cget("image")), "")
            self.assertEqual(app.l_pill.cget("text"), "● 글자층 넣기 중")
        finally:
            app._set_running(False); self.settle()
        app.update()
        per, cells = app._strip_cells
        self.assertEqual(len(cells), 5)
        with mock.patch.object(app, "refresh_table"):
            self.gui.App._strip_click(app, (cells[2][0] + cells[2][1]) / 2)
            self.assertEqual((app.v_nb.get(), app.nb.index("current")), (1, 1))
            self.assertEqual(app._want_row, 3)
        app.v_nb.set(0); app.nb.select(0); app.__dict__.pop("_want_row", None)

    def test_6_page_events_drive_strip_big_line_and_calls(self):
        """큐의 쪽 알림('page', 쪽, 상태)과 호출 수를 실은 진행 알림이 쪽 띠·범례·큰 숫자 줄·단계 몫을 움직이고, 멈추면 남은 '지금'은 '남음'."""
        app = self.app; gui = self.gui; app.select_tab(0); app._use_pdf(str(self.pdf)); self.wait_strip()
        app._run_label = "한 번에 인식 (전체 → HWPX)"
        app._set_running(True, "run"); app._calls_live = True
        items = [("progress", "prepare", 0, 0, 0), ("page", 1, "record")] + [("page", n, "remain") for n in (2, 3, 4, 5)]
        items += [("progress", "read", 0, 4, 0), ("page", 2, "now"), ("page", 3, "now"), ("page", 2, "done"), ("progress", "read", 1, 4, 3),
                  ("page", 4, "now"), ("page", 3, "fail"), ("progress", "read", 2, 4, 5), ("page", 5, "now"), ("page", 9, "bogus")]
        for it in items:
            app.q.put(it)
        try:
            with mock.patch.object(gui.App, "_call_count", return_value=5):
                self.settle()
                self.assertEqual(app._strip_marks, {1: "record", 2: "done", 3: "fail", 4: "now", 5: "now"})
                self.assertEqual((app._calls_by_stage, app._calls_used), ({"read": 5}, 5))
                big = "".join(w.cget("text") for w in app._big_left.winfo_children())
                self.assertIn("지금 4~5쪽 판독 중 (동시 2쪽)", big)
                legend = [app.c_legend.itemcget(i, "text") for i in app.c_legend.find_all()
                          if app.c_legend.type(i) == "text" and app.c_legend.itemcget(i, "text") != "!"]     # '!' 은 실패 견본 속 글
                self.assertEqual(legend, ["끝남 1", "지금 2", "실패 1", "기록 있음 · 건너뜀 1", "남음 0"])
                app.q.put(("page", 4, "done")); app.q.put(("done", 130, app._run_label))
                self.settle()
        finally:
            if app.running:
                app._set_running(False)
            self.settle()
        self.assertEqual(app._run_result["calls"], 5)
        self.assertEqual(app._strip_marks[4], "done"); self.assertEqual(app._strip_marks[5], "remain")      # 멈춘 쪽은 남음
        gui.App._on_page(app, 5, "now")                                                                     # 끝난 뒤 늦게 온 알림은 버림
        self.assertEqual(app._strip_marks[5], "remain")
        app._run_result = None; app._strip_marks = {}; app.__dict__.pop("_page_live", None)

    def test_7_page_events_only_for_this_tab_read(self):
        """책갈피 교정(다른 PDF의 쪽을 판독)·문서 만들기 같은 명령의 쪽 알림은 이 탭의 쪽 띠를 바꾸지 않는다."""
        app = self.app; app.select_tab(0); app._use_pdf(str(self.pdf)); self.wait_strip()
        for cmd in ("collate", "build"):
            app._set_running(True, cmd)
            before = dict(app._strip_marks)
            try:
                self.gui.App._on_page(app, 2, "now")
                self.assertEqual(app._strip_marks, before, cmd)
            finally:
                app._set_running(False)
        self.assertIn("run", self.gui.PAGE_CMDS); self.assertIn("read", self.gui.PAGE_CMDS); self.assertNotIn("collate", self.gui.PAGE_CMDS)

    def test_5_collate_open_buttons_are_not_locked(self):
        app = self.app
        texts = {}
        def walk(w):
            for c in w.winfo_children():
                if c.winfo_class() == "TButton":
                    texts[str(c.cget("text")).strip()] = c
                walk(c)
        walk(app.act_cards[1])
        self.assertNotIn(texts["보고 열기"], app.buttons); self.assertNotIn(texts["교정본 열기"], app.buttons)
        self.assertIn(texts["▶  HWPX 전체 교정"], app.buttons)

    def test_9_legend_fail_glyph_uses_the_cell_font(self):
        """범례의 실패 견본 '!'는 칸과 같은 F_CAP_B(9pt) — 앱에서 9pt 아래 글은 없다."""
        app = self.app; app.select_tab(0); app._use_pdf(str(self.pdf)); self.wait_strip()
        self.gui.App._draw_legend(app)
        cv = app.c_legend
        bang = [i for i in cv.find_all() if cv.type(i) == "text" and cv.itemcget(i, "text") == "!"]
        self.assertEqual(len(bang), 1)
        size = abs(int(app.tk.call("font", "actual", cv.itemcget(bang[0], "font"), "-size")))
        self.assertEqual(size, abs(int(app.tk.call("font", "actual", app.fonts["CAP_B"], "-size"))))
        self.assertGreaterEqual(size, 9)

    def test_8_collate_section_buttons_need_a_picked_row(self):
        """[이 구획 대조만]·[이 구획 교정]은 표에서 구획을 고른 때만 켜진다(F_collate) — 실행 중에는 잠기고, 끝나면 고른 줄이 있을 때만 다시 켜진다."""
        app = self.app
        state = lambda: [str(b.cget("state")) for b in app._csec_btns]
        old = list(app.collate_rows)
        app.collate_rows = [{"no": 1, "lv": 1, "name": "大薊", "read": "대계", "para": 3, "pdf": "大薊", "page": 12, "how": "책갈피"},
                            {"no": 2, "lv": 1, "name": "小薊", "read": "소계", "para": 2, "pdf": "小薊", "page": 13, "how": "책갈피"}]
        try:
            app.refresh_collate_table(); self.settle()
            self.assertEqual(state(), ["disabled", "disabled"])                                     # 표는 찼지만 고른 줄이 없음
            app.ctable.selection_set("2"); self.settle()
            self.assertEqual(state(), ["normal", "normal"])
            app._set_running(True, "collate"); self.settle()
            try:
                self.assertEqual(state(), ["disabled", "disabled"])
                app.ctable.selection_set("1"); self.settle()
                self.assertEqual(state(), ["disabled", "disabled"])                                 # 실행 중에는 골라도 잠김
            finally:
                app._set_running(False); self.settle()
            self.assertEqual(state(), ["normal", "normal"])
            app.ctable.selection_remove(app.ctable.selection()); self.settle()
            self.assertEqual(state(), ["disabled", "disabled"])
            app.ctable.selection_set("1"); app._set_running(True, "read"); app._set_running(False); app.refresh_collate_table(); self.settle()
            self.assertEqual(state(), ["disabled", "disabled"])                                     # 표를 다시 채우면 고른 줄이 없어진다
        finally:
            app.collate_rows = old; app.refresh_collate_table(); self.settle()


if __name__ == "__main__":
    unittest.main()
