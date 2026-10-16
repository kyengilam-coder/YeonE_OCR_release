"""비용 창(서재 모양): 표 줄 묶기·단계/대상 가르기·0원 줄, 기본 단추(고리·'Enter' 캡션·인주 채움/테두리)가 costplan.default_choice 를 따르는지,
Enter 가 창 하나에서만 처리되어 단가 칸·펼침 단추에서 돈 드는 실행을 누르지 않는지, 문자 PDF 실행의 글, 책 제목·도장 규칙.
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


def _row(step, label, scope, model, calls, tin=(0, 0), tout=(0, 0)):
    return {"step": step, "label": label, "scope": scope, "model": model, "basis": "토큰 기본값(실측 없음)", "calls": list(calls), "in": list(tin),
            "cached": [0, 0], "out": list(tout), "detail": "", "items": None, "model_step": step not in ("read1", "read2")}


def _plan(model="gemini-2.5-flash", kind="run", ctx=True, read=True, model_steps=True, glyph_model=None):
    """한 번에 인식 계획(costplan.count_plan 모양): 판독 두 줄 + 3차 판별(기록 있는 쪽 0원 · 새로 읽을 쪽) + 문맥 교정."""
    gm = glyph_model or model
    rows = []
    if read:
        rows += [_row("read1", "1차 · 쪽 전체 판독 — 새로 읽을 3쪽(어림)", "new", model, (3, 3), (30000, 40000), (3000, 6000)),
                 _row("read2", "2차 · 확대 조각 확인 — 새로 읽을 3쪽(어림)", "new", model + " (auto)", (3, 3), (20000, 30000), (2000, 3000))]
    if model_steps:
        rows += [_row("glyph", "3차 · 글자 판별 — 기록 있는 1쪽", "have", gm, (0, 0)),
                 _row("glyph", "3차 · 글자 판별 — 새로 읽을 3쪽(어림)", "new", gm, (2, 6), (4000, 9000), (300, 900))]
        if ctx:
            rows += [_row("choice", "문맥 교정(번체) — 새로 읽을 3쪽(어림)", "new", gm, (1, 1), (3000, 5000), (100, 300))]
    return {"kind": kind, "model": model, "context_model": ctx, "skip_any_model": True, "force": False, "pages": 4, "records": 1, "to_read": 3,
            "new_pages": 3, "same_source": True, "rows": rows, "notes": [], "skipped": {}, "errors": []}


class CostTablePure(unittest.TestCase):
    """창 없이: 표 줄 만들기(_cells)와 책 제목·도장 규칙."""
    def setUp(self):
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.gui = self.stack.enter_context(gui_module())

    def cells(self, plan, kind="run", flags=("--no-adjudicate",)):
        from yeone_ocr.costplan import price_plan
        ns = types.SimpleNamespace(kind=kind, no_flags=list(flags), GROUPS=self.gui.CostDialog.GROUPS)
        return self.gui.CostDialog._cells(ns, price_plan(plan, {}, None))

    def test_groups_split_step_and_scope_and_dim_zero_rows(self):
        out = self.cells(_plan())
        groups = [(c[1], c[3]) for c in out if c[0] == "group"]
        self.assertEqual(groups, [("판독", False), ("모델 단계", True)])           # 꼬리표는 모델 단계 묶음에만
        rows = [c for c in out if c[0] == "row"]
        steps = [r[1]["step"] for r in rows]
        self.assertEqual(steps, ["1차 · 쪽 전체 판독", "2차 · 확대 조각 확인", "3차 · 글자 판별", None, "문맥 교정(번체)"])   # 같은 단계의 둘째 줄은 칸을 합침
        glyph_have, glyph_new = rows[2], rows[3]
        self.assertEqual((glyph_have[1]["scope"], glyph_have[2]["span"], glyph_have[2]["last"]), ("기록 있는 1쪽", 2, False))
        self.assertEqual((glyph_new[1]["scope"], glyph_new[2]["span"], glyph_new[2]["last"]), ("새로 읽을 3쪽(어림)", 0, True))
        self.assertEqual((glyph_have[1]["cost"], glyph_have[2]["zero"], glyph_have[2]["cost"]), ("$0.00", True, "zero"))   # '$0.00~0.00' 이 아니라 하나로
        self.assertEqual(glyph_new[2]["cost"], "money"); self.assertTrue(glyph_new[1]["cost"].startswith("$"))
        self.assertEqual(rows[0][1]["calls"], "3"); self.assertEqual(glyph_new[1]["calls"], "2~6")

    def test_unknown_price_rows_and_kind_groups(self):
        rows = [c for c in self.cells(_plan(model="nobody-model")) if c[0] == "row"]
        self.assertEqual([(r[1]["cost"], r[2]["cost"]) for r in rows if not r[2]["zero"]][0], ("단가 미입력", "warn"))
        self.assertEqual([r[1]["cost"] for r in rows if r[2]["zero"]], ["$0.00"])        # 호출 0 인 줄은 단가가 없어도 0원
        read = self.cells(_plan(kind="read", model_steps=False), kind="read", flags=())
        self.assertEqual([(c[1], c[3]) for c in read if c[0] == "group"], [("판독", False)])
        build = self.cells(_plan(kind="build", read=False), kind="build", flags=())
        self.assertEqual([(c[1], c[3]) for c in build if c[0] == "group"], [("문서 만들기", False)])   # [아니요]가 뺄 것이 없으면 꼬리표 없음
        build = self.cells(_plan(kind="build", read=False), kind="build", flags=("--no-context-model",))
        self.assertEqual([(c[1], c[3]) for c in build if c[0] == "group"], [("문서 만들기", True)])

    def test_book_title_and_seal(self):
        bt = self.gui.book_title
        self.assertEqual(bt(str(Path("本草") / "개보본초.ocr.pdf")), ("개보본초", "개보", "개보본초.ocr.pdf"))
        self.assertEqual(bt("/a/證類本草.PDF"), ("證類本草", "證類", "證類本草.PDF"))
        self.assertEqual(bt("/a/abc report.pdf"), ("abc report", "AB", "abc report.pdf"))
        self.assertEqual(bt("/a/x7 개.pdf"), ("x7 개", "X7", "x7 개.pdf"))                 # 한글·한자가 둘이 안 되면 앞 두 글자
        self.assertEqual(bt("/s/screenshot-3f9a2c71deadbeef.png"), ("스크린샷", "화면", "screenshot-3f9a2c71….png"))
        self.assertEqual(bt("/s/screenshot-3f9a2c71deadbeef_2.png")[:2], ("스크린샷", "화면"))
        self.assertEqual(bt(""), ("", "", ""))
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "사진 묶음.ocr"; d.mkdir()
            self.assertEqual(bt(str(d)), ("사진 묶음.ocr", "사진", "사진 묶음.ocr"))     # 이미지 폴더는 폴더 이름이 제목

    def test_pdf_run_window_gets_flag_and_no_adjudicate_only(self):
        """[▶ 한 번에 인식 (문자 PDF 만들기)]: 어림은 run 으로 세되 창에는 문자 PDF 임을 알리고, [아니요]는 --no-adjudicate 만 넘긴다(문맥 교정 모델은 이미 꺼짐)."""
        from test_gui_settings_merge import ImmediateThread, _plan_app, _records_work
        gui = self.gui
        td = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(mock.patch.object(gui, "CONFIG_PATH", td / "cfg" / "config.json"))
        pdf, work = _records_work(gui, td)
        app = _plan_app(gui, pdf, work)
        shown = []
        def make(app_, plan, kind, preview=False, pdf_only=False):
            shown.append({"kind": kind, "pdf_only": pdf_only, "ctx": plan.get("context_model")})
            return types.SimpleNamespace(choice="without")
        plan = gui.App._plan_input.__get__(app)("run")
        from dataclasses import replace
        from yeone_ocr import costplan
        p = costplan.count_plan(replace(plan, context_model=False, normalize=False, layout="page", preset=gui.DEFAULT_PRESET, punctuation_rules=""))
        p["errors"].append({"step": "glyph", "reason": "x"})                                # 물을 것을 만든다(창이 뜨게)
        go = mock.Mock()
        with mock.patch.object(gui, "CostDialog", make):
            app._plan_seq = 3; app._plan_done(3, "run-pdf", ("ok", p), go)
        self.assertEqual(shown, [{"kind": "run", "pdf_only": True, "ctx": False}])
        go.assert_called_once_with(["--no-adjudicate"])
        ImmediateThread.made.clear(); seen = []
        with mock.patch.object(gui.threading, "Thread", ImmediateThread), mock.patch.object(gui, "PlanWait"):
            app._with_plan("run-pdf", go)
        while not app.q.empty():
            seen.append(app.q.get_nowait())
        self.assertEqual([item[2] for item in seen if item[0] == "plan"], ["run-pdf"])      # 큐에는 문자 PDF 실행임이 남는다(계획은 run)


class CostDialogRealTk(unittest.TestCase):
    """진짜 창(xvfb): 기본 단추 모양·캡션, Enter 규칙, 문자 PDF 글, 세 격자의 열 너비."""
    @classmethod
    def setUpClass(cls):
        _real_tk().destroy()
        cls.stack = contextlib.ExitStack()
        cls.gui = gui = cls.stack.enter_context(gui_module())
        td = Path(cls.stack.enter_context(tempfile.TemporaryDirectory()))
        cfg = td / "cfg" / "config.json"; cfg.parent.mkdir()
        cfg.write_text(json.dumps({"auto_update": False, "reader": "gemini", "model": "gemini-2.5-flash", "fx_auto": 1400, "fx_auto_date": "2099-01-01"}),
                       encoding="utf-8")
        for target, name, value in ((gui, "CONFIG_PATH", cfg), (gui, "missing_packages", lambda: []), (gui.App, "_fetch_fx", lambda self: None)):
            cls.stack.enter_context(mock.patch.object(target, name, value))
        cls.app = gui.App(); cls.app.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.destroy()
        finally:
            cls.stack.close()

    def dialog(self, plan, kind="run", **kw):
        d = self.gui.CostDialog(self.app, plan, kind, **kw)
        self.addCleanup(lambda: d.winfo_exists() and d.destroy())
        d.update_idletasks()
        return d

    def look(self, d):
        return {ch: (str(b.cget("style")), d.caps[ch].cget("text")) for ch, b in d.btns.items()}

    def test_known_price_spend_button_is_default_with_ring(self):
        d = self.dialog(_plan())
        self.assertEqual(d.default, "all")
        self.assertEqual(self.look(d), {"all": ("DlgSealDef.TButton", "Enter · 모든 단계"), "without": ("Dlg.TButton", "판독·문서 만들기만"),
                                        None: ("Dlg.TButton", "Esc · 하지 않음")})
        self.assertFalse(d.adj_shown); self.assertEqual(d.pill, "한 번에 인식 · HWPX")
        self.assertIn("판독·문서 만들기만", d.why.get("1.0", "end"))

    def test_unknown_price_closes_default_and_outlines_spend_button(self):
        d = self.dialog(_plan(model="nobody-model"))                                     # 판독 단가가 없음 → [취소]
        self.assertIsNone(d.default)
        self.assertEqual(self.look(d), {"all": ("DlgSealOut.TButton", "모든 단계"), "without": ("Dlg.TButton", "판독·문서 만들기만"),
                                        None: ("DlgDef.TButton", "Enter · Esc · 하지 않음")})
        self.assertTrue(d.adj_shown); self.assertEqual(d.v_head.get(), "금액을 다 낼 수 없음"); self.assertEqual(d.v_missing.get(), "nobody-model")
        d = self.dialog(_plan(glyph_model="nobody-model"))                               # 모델 단계만 단가 없음 → [아니요]
        self.assertEqual(d.default, "without")
        self.assertEqual(self.look(d)["without"], ("DlgDef.TButton", "Enter · 판독·문서 만들기만"))
        self.assertEqual(self.look(d)["all"][0], "DlgSealOut.TButton")

    def test_enter_only_runs_from_window_or_focused_button(self):
        """§10-4: 단가 칸 Enter = 다시 계산만 · 펼침 단추 Enter = 펼치기만 · [다시 계산] Enter = 다시 계산만 · [취소] Enter = 닫기 · 창 자체 = 기본 단추."""
        for b in (d := self.dialog(_plan())).btns.values():
            self.assertEqual(b.bind("<Return>"), "")                                    # 단추마다 묶지 않는다(창에서 한 번만)
        self.assertTrue(d.bind("<Return>"))
        with mock.patch.object(d, "render", wraps=d.render) as render:
            d.focus_get = lambda: d.e_in
            self.assertEqual(d._on_return(), "break")
            self.assertTrue(d.winfo_exists()); self.assertIsNone(d.choice); render.assert_called()
            self.assertTrue(d.v_recalc.get().startswith("다시 계산했습니다"))
        d.focus_get = lambda: d.b_more
        d._on_return(); self.assertTrue(d.more_shown); self.assertTrue(d.winfo_exists()); self.assertIsNone(d.choice)
        with mock.patch.object(d, "render", wraps=d.render) as render:
            d.focus_get = lambda: d.b_recalc
            d._on_return(); render.assert_called(); self.assertTrue(d.winfo_exists()); self.assertIsNone(d.choice)
        d.focus_get = lambda: d.cb_model
        d._on_return(); self.assertTrue(d.winfo_exists())
        d.focus_get = lambda: d.btns[None]
        d._on_return(); self.assertFalse(d.winfo_exists()); self.assertIsNone(d.choice)
        d = self.dialog(_plan())
        d.focus_get = lambda: d
        d._on_return(); self.assertEqual(d.choice, "all")
        d = self.dialog(_plan())
        d.focus_get = lambda: d.btns[d.default]
        d._on_return(); self.assertEqual(d.choice, "all")

    def test_enter_in_price_field_never_starts_reading_when_default_is_without(self):
        d = self.dialog(_plan(glyph_model="nobody-model"))
        self.assertEqual(d.default, "without"); self.assertTrue(d.adj_shown)              # 단가 칸이 펼쳐진 채로 뜬다
        d.focus_get = lambda: d.e_out
        d._on_return(); d._on_return()                                                    # 두 번 쳐도
        self.assertTrue(d.winfo_exists()); self.assertIsNone(d.choice)

    def test_pdf_run_texts(self):
        d = self.dialog(_plan(ctx=False), pdf_only=True)
        self.assertEqual(d.pill, "한 번에 인식 · 문자 PDF")
        self.assertEqual(self.look(d)["without"], ("Dlg.TButton", "판독·문자 PDF만"))
        self.assertIn("판독·문자 PDF 만들기만", d.why.get("1.0", "end")); self.assertNotIn("문서 만들기만", d.why.get("1.0", "end"))
        self.assertEqual(d.v_skip.get(), self.gui.CostDialog.NOTE_PDF)

    def test_build_without_anything_to_drop_hides_no_and_preview_has_close_only(self):
        d = self.dialog(_plan(kind="build", read=False, ctx=False), kind="build")
        self.assertNotIn("without", d.btns); self.assertIsNone(d.why)
        d = self.dialog(_plan(), preview=True)
        self.assertEqual(self.look(d), {None: ("DlgDef.TButton", "Enter · Esc 닫기")}); self.assertEqual(d.pill, "HWPX 만들기 어림")
        d.focus_get = lambda: d
        d._on_return(); self.assertIsNone(d.choice); self.assertFalse(d.winfo_exists())

    def test_three_grids_share_column_widths_and_long_values_fit(self):
        plan = _plan(model="claude-sonnet-4-5-20250929")
        plan["rows"][0]["calls"] = [1500, 2850]; plan["rows"][0]["in"] = [21_600_000, 34_400_000]; plan["rows"][0]["label"] = "1차 · 쪽 전체 판독 — 새로 읽을 461쪽(어림)"
        d = self.dialog(plan)
        d.update_idletasks()
        widths = [[g.grid_bbox(i, 0, i, int(g.grid_size()[1]) - 1)[2] for i in range(8)] for g in d.grids]
        self.assertEqual(widths[0], widths[1]); self.assertEqual(widths[1], widths[2])
        self.assertLessEqual(d.col_w["model"], self.app.px(170))
        if self.gui.work_area(d)[0] >= self.app.px(1200):
            self.assertGreaterEqual(d.col_w["basis"], self.app.px(150))
        self.assertGreaterEqual(d.content_w, self.app.px(852))

    def test_table_spans_the_widest_row_and_gives_back_the_unused_gutter(self):
        """표가 단가 칸 줄·금액 줄·바닥 단추 줄보다 좁게 끝나지 않고, 스크롤 막대가 없으면 막대 자리(gutter)를 근거 열이 쓴다."""
        P = self.app.px
        with mock.patch.object(self.gui, "work_area", lambda w: (P(1920), P(1040))):            # 넓은 화면(가상 화면 크기와 상관없이)
            d = self.dialog(_plan(model="claude-sonnet-4-5-20250929"))
            narrow = self.dialog(_plan(model="gpt-4.1"))
        d.update_idletasks()
        self.assertGreaterEqual(d.content_w, d._adj_row.winfo_reqwidth() + 2 * P(14))   # 단가 칸은 접혀 있어도 잰다(펼칠 때 너비가 바뀌지 않게)
        self.assertGreaterEqual(d.content_w, d._amt.winfo_reqwidth())
        self.assertGreaterEqual(d.content_w, d._foot.winfo_reqwidth())
        self.assertFalse(d.sb.winfo_ismapped())
        basis = d.COLS.index("basis")
        for g in d.grids:
            self.assertEqual(int(g.grid_columnconfigure(basis)["minsize"]), d.col_w["basis"] + d.gutter)
        self.assertEqual(int(d.cv.cget("width")), d.content_w)
        self.assertLessEqual(int(d.cb_model.cget("width")), 24)
        self.assertEqual(int(narrow.cb_model.cget("width")), 12)                                # 짧은 모델 이름이면 시안처럼 좁게

    def test_more_panel_sits_below_buttons_and_dialog_fits_small_work_area(self):
        """근거·가정을 펼쳐도 단추 줄이 밀려 내려가지 않는다(바닥 줄 아래에 펼침) — 작은 작업 영역에서는 표 몸통과 근거 글이 줄고 스크롤된다."""
        P = self.app.px
        plan = _plan()
        for r in plan["rows"]:
            r["detail"] = "자세한 근거 " * 30
        plan["notes"] = [f"가정 {i} " * 20 for i in range(8)]
        with mock.patch.object(self.gui, "work_area", lambda w: (P(1366), P(600))):
            d = self.dialog(plan)
            d.toggle_more(); d.update_idletasks()
            self.assertGreater(int(d.more.grid_info()["row"]), int(d._foot.grid_info()["row"]))   # 바닥 단추 줄 아래
            self.assertLessEqual(d.winfo_reqheight(), P(600) - P(40) + 2 * d._more_lh)
            self.assertLess(int(d.t_more.cget("height")), d._more_lines())                       # 넘치는 근거 글은 그 칸만 스크롤
            self.assertEqual(d.sb_more.winfo_manager(), "grid")
            d.toggle_more(); d.update_idletasks()
            self.assertEqual(d.more.winfo_manager(), "")
            self.assertLessEqual(d.winfo_reqheight(), P(600) - P(40))

    def test_wheel_and_page_keys_scroll_the_body_from_any_cell(self):
        """휠은 창 하나에 묶는다(칸 Label 위에서 굴려도 몸통이 움직임) — PgDn·PgUp 도 몸통을 한 화면씩."""
        P = self.app.px
        with mock.patch.object(self.gui, "work_area", lambda w: (P(1366), P(560))):
            d = self.dialog(_plan())
        cell = [w for w in d.inner.winfo_children() if w.winfo_class() == "Label" and str(w.cget("text")).strip()][3]
        self.assertIn(str(d), cell.bindtags()); self.assertEqual(cell.bind("<MouseWheel>"), "")
        self.assertTrue(d.bind("<MouseWheel>") and d.bind("<Button-5>") and d.bind("<Next>"))
        with mock.patch.object(d.sb, "winfo_ismapped", lambda: True):
            for ev in (types.SimpleNamespace(widget=cell, num=5, delta=0), types.SimpleNamespace(widget=cell, num=0, delta=-120)):
                # Each wheel format starts at the top. At high DPI one unit can
                # reach the bottom; the following format must still be tested.
                d.cv.yview_moveto(0)
                before = d.cv.yview()[0]
                self.assertEqual(d._wheel(ev), "break")
                self.assertGreater(d.cv.yview()[0], before)
            before = d.cv.yview()[0]
            d._page(-1); self.assertLess(d.cv.yview()[0], before)
        self.assertIsNone(d._wheel(types.SimpleNamespace(widget=d.t_more, num=5, delta=0)))   # 근거 글 위면 그 글만(제 Text 묶음)


if __name__ == "__main__":
    unittest.main()
