"""창 설정 저장은 이 창이 바꾼 값만 파일에 얹고, 비용 어림은 작업 스레드에서 세며 실패하면 닫힌 쪽으로 묻고, 양식 파일이 없으면 모델을 부르기 전에 막는다(화면·API 불필요)."""
import contextlib
import copy
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ops import gui_module


class Var:
    def __init__(self, value): self.value = value
    def get(self): return self.value
    def set(self, value): self.value = value


def fake_app(gui, cfg: dict, **over):
    """save_settings 가 읽는 칸을 모두 가진 가짜 창. cfg 는 창이 읽은 설정(_cfg_loaded 도 그 사본)이고 칸은 실제 창처럼 그 값으로 시작한다."""
    v = dict(reader=cfg.get("reader", "gemini"), model=cfg.get("model", ""), key=cfg.get("api_key", ""), model2="auto", pages="", off="", up="2",
             second=True, docx=cfg.get("docx", False), join=True, force=False, hl=True, pb=True, ctx=True, workers="8", ckpt="20", cache=True,
             think2=True, blind=False, chunk="8", suspects=True, tlpdf=False, layout=gui.LAYOUT_LABELS[cfg.get("layout", "page")],
             template=cfg.get("template", ""), bold=True, ctxmodel=True, maxcalls="0", status="", skipany=cfg.get("skip_any_model", True))
    v.update(over)
    app = types.SimpleNamespace(cfg=cfg, _cfg_loaded=copy.deepcopy(cfg), _preset_ref=over.get("preset", cfg.get("preset", gui.DEFAULT_PRESET)),
                                **{"v_" + k: Var(x) for k, x in v.items() if k != "preset"})
    app.check_list_mode = lambda: "none"; app.memo_mode = lambda: "notice"; app.collate_policy = lambda: "sure"; app.collate_memo_mode = lambda: "notice"
    app.media_res_key = lambda: "high"; app.mode_key = lambda i: "auto"; app.page_think_key = lambda: "auto"; app.page_mres_key = lambda: "auto"
    app.pdf = lambda: "book.pdf"; app._remember_inputs = lambda: None; app._save_book = mock.Mock()   # 책별 설정은 test_book_settings
    app.layout_key = lambda: next((k for k, lab in gui.LAYOUT_LABELS.items() if lab == app.v_layout.get()), "page")
    return app


class SettingsMerge(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.gui = self.stack.enter_context(gui_module())
        td = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(td) / "cfg" / "config.json"
        self.stack.enter_context(mock.patch.object(self.gui, "CONFIG_PATH", self.path))

    def disk(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_merge_config_rules(self):
        disk = {"a": 1, "b": 2, "provider_settings": {"gemini": {"api_key": "g"}, "openai": {"api_key": "o2"}}}
        loaded = {"a": 1, "b": 1, "provider_settings": {"gemini": {"api_key": "g"}, "openai": {"api_key": "o1"}}}
        new = {"a": 1, "b": 1, "c": 3, "provider_settings": {"gemini": {"api_key": "g9"}, "openai": {"api_key": "o1"}}}
        self.assertEqual(self.gui.merge_config(disk, loaded, new),
                         {"a": 1, "b": 2, "c": 3, "provider_settings": {"gemini": {"api_key": "g9"}, "openai": {"api_key": "o2"}}})

    def test_other_windows_changes_survive_and_own_changes_win(self):
        """창 A 가 양식 "" 로 읽음 → 창 B 가 양식 X 저장 → A 가 양식을 건드리지 않고 저장 → 파일은 X. A 가 Y 로 바꾸면 Y."""
        gui = self.gui
        gui.save_config(dict(gui.DEFAULTS, layout="flow"))
        a = fake_app(gui, gui.load_config(), key="key-a"); b = fake_app(gui, gui.load_config())
        b.v_template.set("X.hwpx"); b.v_reader.set("openai"); b.v_model.set("gpt-custom"); b.v_key.set("key-b")
        gui.App.save_settings(b, quiet=True)
        self.assertEqual(self.disk()["template"], "X.hwpx")
        gui.App.save_settings(a, quiet=True)                                            # A 는 양식·제공사를 건드리지 않았다
        d = self.disk()
        self.assertEqual((d["template"], d["reader"], d["model"]), ("X.hwpx", "openai", "gpt-custom"))
        self.assertEqual(d["provider_settings"]["openai"]["api_key"], "key-b")
        self.assertEqual(d["provider_settings"]["gemini"]["api_key"], "key-a")
        a.v_template.set("Y.hwpx"); a.v_docx.set(True)
        gui.App.save_settings(a, quiet=True)                                            # A 가 바꾼 값은 A 가 이긴다
        d = self.disk()
        self.assertEqual((d["template"], d["docx"], d["reader"]), ("Y.hwpx", True, "openai"))
        gui.App.save_settings(b, quiet=True)                                            # B 는 docx 를 건드리지 않았다 → A 의 True 가 남는다
        d = self.disk()
        self.assertEqual((d["template"], d["docx"]), ("Y.hwpx", True))
        a.v_template.set("")
        gui.App.save_settings(a, quiet=True)                                            # 비우는 것도 바꾼 것이다
        self.assertEqual(self.disk()["template"], "")

    def test_single_window_writes_everything_as_before(self):
        gui = self.gui
        a = fake_app(gui, gui.load_config(), model="gemini-custom", key="key-a", layout=gui.LAYOUT_LABELS["flow"])   # 파일 없음 → 기본값에서 시작해 셋을 바꿈
        gui.App.save_settings(a, quiet=True)
        d = self.disk()
        self.assertEqual((d["reader"], d["model"], d["layout"], d["max_calls"], d["provider_settings"]["gemini"]["api_key"]),
                         ("gemini", "gemini-custom", "flow", 0, "key-a"))
        self.assertEqual(a._cfg_loaded["model"], "gemini-custom")
        a.v_model.set("gemini-other"); gui.App.save_settings(a, quiet=True)
        self.assertEqual(self.disk()["model"], "gemini-other")

    def test_old_structure_setting_becomes_preset_once_and_survives_saves(self):
        """0.7.x 설정(짜임 규칙 structure, preset 없음): 내용별로 쓰던 사람은 本草 조문(kaibao) — 같은 문서. 저장해도·다시 읽어도 같고, 옛 열쇠는 그대로 둔다."""
        gui = self.gui
        up = lambda raw: gui.upgrade_config(dict(gui.DEFAULTS, **raw), raw)["preset"]
        self.assertEqual([up(r) for r in ({"layout": "flow"}, {"layout": "flow", "structure": "auto"}, {"layout": "flow", "structure": "none"},
                                           {"layout": "flow", "structure": "bencao"}, {"layout": "page", "structure": "auto"}, {"layout": "page"}, {},
                                           {"preset": "D:/a.json"}, {"layout": "flow", "preset": "regions"}, {"layout": "flow", "structure": ["x"]},
                                           {"layout": "page", "structure": "auto", "template": "D:/본초학.hwpx"}, {"layout": "page", "structure": "bencao"},
                                           {"layout": "page", "template": "  "})],
                         ["kaibao", "kaibao", "regions", "bencao", "basic", "basic", "basic", "D:/a.json", "regions", "kaibao",
                          "kaibao", "bencao", "basic"])                              # 검수용으로 잠시 원본 쪽대로 둔 사람(양식이 있음)도 本草 조문 그대로
        self.assertEqual(gui.upgrade_config(dict(gui.DEFAULTS, preset=None), {"preset": None})["preset"], "basic")   # 망가진 값
        for selected in ("regions", "kaibao", "bencao", "auto", "none", "D:/a.json"):
            self.assertEqual(up({"layout": "flow", "preset": selected}), selected)       # 명시적으로 고른 규칙은 새 기본값으로 바꾸지 않는다
        gui.save_config({"layout": "flow", "structure": "auto", "template": "T.hwpx", "workers": 8})
        cfg = gui.load_config()
        self.assertEqual((cfg["preset"], cfg["structure"], cfg["template"]), ("kaibao", "auto", "T.hwpx"))
        self.assertNotIn("preset", self.disk())                                          # 읽기만 해서는 파일을 바꾸지 않는다
        a = fake_app(gui, cfg)
        gui.App.save_settings(a, quiet=True)
        d = self.disk()
        self.assertEqual((d["preset"], d["structure"], d["layout"]), ("kaibao", "auto", "flow"))   # 옛 판이 읽는 열쇠는 그대로
        self.assertEqual(gui.load_config()["preset"], "kaibao")
        b = fake_app(gui, gui.load_config(), preset="regions"); gui.App.save_settings(b, quiet=True)
        self.assertEqual(self.disk()["preset"], "regions")                               # 고른 것은 다시 옮기지 않는다
        gui.App.save_settings(a, quiet=True)                                              # A 는 짜임 규칙을 건드리지 않았다 → B 의 값이 남는다
        self.assertEqual(self.disk()["preset"], "regions")

    def test_skip_answered_option_defaults_on_is_passed_and_saved(self):
        """다른 모델이 이미 물은 자리·후보 건너뛰기: 기본 켬(돈이 덜 드는 쪽), 창은 늘 --skip-answered 를 넘기고, 끈 것도 저장한다."""
        gui = self.gui
        from yeone_ocr.cli import build_parser
        self.assertIs(gui.DEFAULTS["skip_any_model"], True)
        a = fake_app(gui, gui.load_config())
        self.assertEqual(gui.App._answered_opts(a), ["--skip-answered", "any-model"])
        a.v_skipany.set(False)
        self.assertEqual(gui.App._answered_opts(a), ["--skip-answered", "same-model"])
        for cmd in ("run", "adjudicate", "build"):
            self.assertEqual(build_parser().parse_args([cmd, "x.pdf"] + gui.App._answered_opts(a)).skip_answered, "same-model")
        gui.App.save_settings(a, quiet=True)
        self.assertIs(self.disk()["skip_any_model"], False)
        self.assertIs(gui.load_config()["skip_any_model"], False)
        b = fake_app(gui, dict(gui.DEFAULTS))                                             # 옛 설정(열쇠 없음)은 켬
        self.assertEqual(gui.App._answered_opts(b), ["--skip-answered", "any-model"])

    def test_fake_window_without_snapshot_writes_whole_config(self):
        gui = self.gui
        app = types.SimpleNamespace(cfg={"workers": 3, "template": "T.hwpx"})
        gui.save_config({"workers": 8, "template": "", "docx": True})
        gui.write_config(app)
        self.assertEqual(self.disk(), {"workers": 3, "template": "T.hwpx"})


def _records_work(gui, td: Path, text: str = "天地玄黃宇宙洪荒"):
    """판독 기록이 한 쪽 있는 결과 폴더(〔a/b〕 없음)."""
    import pymupdf
    from yeone_ocr.schema import Column, PageReading, Paragraph, Region
    from yeone_ocr.state import Work
    pdf = td / "v.pdf"
    with pymupdf.open() as doc:
        doc.new_page(width=500, height=800); doc.save(pdf)
    work = Work(str(pdf), str(td / "w"))
    pr = PageReading(pdf_page=1, regions=[Region(id="b1", kind="본문", bbox=[60, 80, 380, 960], paragraphs=[Paragraph(columns=[Column(col_from_right=1, text=text)])])])
    work.save_record(1, {"file": "v.pdf", "pdf_page": 1, "pass1": pr.model_dump(), "merged": pr.model_dump()})
    return pdf, work


class ImmediateThread:
    made = []

    def __init__(self, target=None, daemon=None, name=None, **kw):
        self.target, self.daemon, self.name = target, daemon, name; ImmediateThread.made.append(self)

    def start(self):
        self.target()


def _plan_app(gui, pdf, work, ctxmodel=True, reader="openai"):
    """비용 어림(_with_plan → 작업 스레드 → _poll → _plan_done)에 쓰는 칸을 가진 가짜 창. 기록 칸 대신 log, 진행 창·비용 창은 시험이 바꿔 끼운다."""
    app = fake_app(gui, dict(gui.DEFAULTS), reader=reader, model="gpt-4.1", ctxmodel=ctxmodel)
    app.pdf = lambda: str(pdf); app.work = lambda: Path(work.dir); app.log = []; app._append = app.log.append
    app.q = __import__("queue").Queue(); app.running = False; app.buttons = [mock.Mock()]; app._fx = None
    app._planning = False; app._plan_seq = 0; app._plan_wait = None; app.after = mock.Mock(); app._offer_update = lambda: None
    app.wait_window = lambda d: None
    for name in ("_plan_input", "_with_plan", "_plan_done", "_lock_for_plan", "_poll", "fx_setting"):
        setattr(app, name, getattr(gui.App, name).__get__(app))
    app.preset_ref = lambda: app._preset_ref
    return app


class CostPlanWindow(unittest.TestCase):
    """비용 한 창: 어림은 작업 스레드에서(창 스레드는 큐로만 받음), 어림이 실패하면 물을 것이 0 이어도 창을 띄우고 닫힌 쪽(모델 단계 없이·취소)을 기본으로,
    [취소]면 아무것도 실행하지도 쓰지도 않는다."""
    def setUp(self):
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.gui = self.stack.enter_context(gui_module())
        self.td = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.pdf, self.work = _records_work(self.gui, self.td)
        self.shown = []

    def dialog(self, choice):
        """CostDialog 대신: 받은 계획과 그 기본 단추를 적어 두고 choice 를 고른 창."""
        from yeone_ocr import costplan
        def make(app, plan, kind, preview=False):
            priced = costplan.price_plan(plan, {}, None)
            self.shown.append({"plan": plan, "kind": kind, "preview": preview, "default": costplan.default_choice(priced)})
            return types.SimpleNamespace(choice=choice)
        return mock.patch.object(self.gui, "CostDialog", make)

    def test_worker_thread_counts_and_poll_runs_when_nothing_to_confirm(self):
        gui = self.gui
        app = _plan_app(gui, self.pdf, self.work, ctxmodel=False)                          # 문맥 교정 모델 끔 → 문서 만들기는 물을 것이 없다
        go = mock.Mock(); ImmediateThread.made.clear()
        with mock.patch.object(gui.threading, "Thread", ImmediateThread), mock.patch.object(gui, "PlanWait") as wait, self.dialog("all"):
            app._with_plan("build", go)
            self.assertTrue(app._planning); go.assert_not_called()                          # 결과는 큐로만 — 창 스레드(_poll)가 받는다
            app.buttons[0].configure.assert_called_with(state="disabled"); wait.assert_called_once()
            app._with_plan("build", go)                                                     # 어림 중에 다시 눌러도 두 번 세지 않는다
            self.assertEqual(len(ImmediateThread.made), 1)
            app._poll()
        self.assertEqual((ImmediateThread.made[0].daemon, ImmediateThread.made[0].name), (True, "cost-plan"))
        go.assert_called_once_with([]); self.assertEqual(self.shown, [])
        self.assertFalse(app._planning); app.buttons[0].configure.assert_called_with(state="normal"); wait.return_value.destroy.assert_called_once()
        self.assertTrue(app.log[-1].startswith("비용 어림: 호출 0회"))
        app.after.assert_called_with(100, app._poll)

    def test_mock_reader_skips_planning(self):
        app = _plan_app(self.gui, self.pdf, self.work, reader="mock")
        go = mock.Mock()
        with mock.patch.object(self.gui.threading, "Thread", side_effect=AssertionError("세지 않는다")):
            app._with_plan("run", go)
        go.assert_called_once_with([])

    def test_failed_estimate_opens_window_with_closed_default(self):
        from yeone_ocr import cli, costplan
        app = _plan_app(self.gui, self.pdf, self.work)
        for kind, want in (("run", "cancel"), ("read", "cancel"), ("build", "cancel"), ("adjudicate", "cancel")):
            go = mock.Mock(); self.shown.clear()
            with self.dialog("all"):
                app._plan_seq = 7; app._plan_done(7, kind, ("error", "RuntimeError: boom"), go)       # 어림 스레드가 통째로 실패
            self.assertEqual(self.shown[0]["default"], want, kind)
            self.assertEqual(self.shown[0]["plan"]["errors"][0]["reason"], "RuntimeError: boom")
            go.assert_called_once_with([])                                                  # 사용자가 [예]를 골랐을 때만
        plan = costplan.count_plan(app._plan_input("run"))
        self.assertEqual(plan["errors"], [])
        with mock.patch.object(cli, "normalization_plan", side_effect=RuntimeError("boom")):
            plan = costplan.count_plan(app._plan_input("run"))                              # 한 단계만 실패: 물을 것이 0 이어도 창을 띄운다
        for choice, want in (("without", ["--no-adjudicate", "--no-context-model"]), ("all", []), (None, None)):
            go = mock.Mock(); self.shown.clear()
            with self.dialog(choice):
                app._plan_seq = 8; app._plan_done(8, "run", ("ok", plan), go)
            self.assertEqual(self.shown[0]["default"], "without")
            if want is None:
                go.assert_not_called()                                                     # [취소]: 실행도 저장도 없음(책별 설정은 go 안에서만)
            else:
                go.assert_called_once_with(want)
        self.assertTrue(any("어림하지 못한 단계 1곳" in line for line in app.log))

    def test_cancel_stale_and_preview(self):
        from yeone_ocr import costplan
        app = _plan_app(self.gui, self.pdf, self.work)
        go = mock.Mock(); wait = mock.Mock(); app._plan_wait = wait; app._planning = True; app._plan_seq = 3
        with self.dialog("all"):
            app._plan_done(2, "run", ("ok", {}), go)                                        # 지난 어림의 늦은 결과는 버린다
            self.assertTrue(app._planning); wait.destroy.assert_not_called()
            app._plan_done(3, "run", ("cancelled", None), go)
        go.assert_not_called(); wait.destroy.assert_called_once(); self.assertEqual(app.v_status.get(), "비용 어림을 취소했습니다")
        plan = costplan.count_plan(app._plan_input("run"))
        with self.dialog("all"):
            app._plan_seq = 4; app._plan_done(4, "run", ("ok", plan), None)                  # [비용 미리 보기]: 물을 것이 없어도 창을 보이기만
        self.assertTrue(self.shown[0]["preview"])

    def test_window_failures_and_late_cancel_run_nothing(self):
        """비용 창을 보이지 못하거나(예외 — _poll 을 끊지 않음) 어림을 시작하지 못하거나, 결과가 온 뒤 [취소]를 눌렀으면 실행하지 않는다."""
        import threading
        from yeone_ocr import costplan
        app = _plan_app(self.gui, self.pdf, self.work)
        plan = costplan.count_plan(app._plan_input("run"))
        plan["errors"].append({"step": "choice", "reason": "x"})
        go = mock.Mock()
        with mock.patch.object(self.gui, "CostDialog", side_effect=RuntimeError("창 없음")), mock.patch.object(self.gui.messagebox, "showerror") as err:
            app._plan_seq = 5; app._plan_done(5, "run", ("ok", plan), go)
        go.assert_not_called(); self.assertIn("비용 창을 보이지 못해 실행하지 않습니다: RuntimeError: 창 없음", err.call_args.args[1])
        with mock.patch.object(self.gui, "PlanWait", side_effect=RuntimeError("Tk")), mock.patch.object(self.gui.messagebox, "showerror") as err, \
             mock.patch.object(self.gui.threading, "Thread", side_effect=AssertionError("세지 않는다")):
            app._with_plan("run", go)
        self.assertFalse(app._planning); go.assert_not_called(); self.assertIn("비용 어림을 시작하지 못해 실행하지 않습니다", err.call_args.args[1])
        cancel = threading.Event(); cancel.set()
        app._plan_wait = types.SimpleNamespace(cancel=cancel, destroy=mock.Mock()); app._plan_seq = 6
        with self.dialog("all"):
            app._plan_done(6, "run", ("ok", plan), go)                                      # 결과가 큐에 든 뒤 누른 [취소]
        go.assert_not_called(); self.assertEqual(self.shown, []); self.assertEqual(app.v_status.get(), "비용 어림을 취소했습니다")

    def test_plan_input_reads_window_fields(self):
        app = _plan_app(self.gui, self.pdf, self.work)
        app.v_pages.set("2-3"); app.v_force.set(True); app.v_skipany.set(False); app.v_up.set("엉뚱"); app._preset_ref = "kaibao"
        app.v_layout.set(self.gui.LAYOUT_LABELS["flow"])
        p = app._plan_input("run")
        self.assertEqual((p.kind, p.pdf, p.work_dir, p.pages, p.reader, p.model, p.force, p.skip_any_model, p.upscale, p.layout, p.preset),
                         ("run", str(self.pdf), str(self.work.dir), "2-3", "openai", "gpt-4.1", True, False, 2.0, "flow", "kaibao"))


class TemplateGuard(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.gui = self.stack.enter_context(gui_module())
        self.td = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    def app(self, template: str):
        gui = self.gui
        app = types.SimpleNamespace(_need_pdf=lambda: True, _prepare_env=lambda: True, v_ctxmodel=Var(True),
                                    v_reader=Var("anthropic"), v_model=Var("claude-custom"), v_pages=Var(""), pdf=lambda: "book.pdf", work=lambda: Path("work"),
                                    _pages=lambda: [], _build_opts=lambda: [], _cap_opts=lambda: [], _read_opts=lambda: [], _glyph_opts=lambda suspects=False: [],
                                    nb=mock.Mock(), v_layout=Var(gui.LAYOUT_LABELS["flow"]), v_template=Var(template),
                                    _with_plan=lambda kind, go: self.fail("양식이 없으면 비용 어림 전에 막는다"), start=mock.Mock(),
                                    _preset_ref="kaibao", _save_book=mock.Mock(), _answered_opts=lambda: ["--skip-answered", "any-model"])
        app.layout_key = gui.App.layout_key.__get__(app)
        return app

    def test_unusable_preset_blocks_build_and_run_before_model_work(self):
        gui = self.gui
        present = self.td / "양식.hwpx"; present.write_bytes(b"PK")
        missing = str(self.td / "없는_규칙.json")
        app = self.app(str(present)); app._preset_ref = missing
        with mock.patch.object(gui.messagebox, "showerror") as err:
            gui.App.do_build(app); gui.App.do_run(app)
        self.assertEqual(err.call_count, 2); app.start.assert_not_called()
        self.assertIn(f"짜임 규칙을 쓸 수 없습니다: 짜임 규칙 파일이 없습니다: {missing}", err.call_args.args[1])
        self.assertIn("[설정] → 이 책 → 짜임 규칙을 다시 고르십시오", err.call_args.args[1])
        app.v_layout.set(gui.LAYOUT_LABELS["page"]); app.v_template.set("")              # 원본 쪽대로면 짜임 규칙을 보지 않는다
        self.assertTrue(gui.preset_ok(app))
        self.assertTrue(gui.preset_ok(types.SimpleNamespace(cfg={})))                    # 짜임 칸이 없는 가짜 창
        app.v_layout.set(gui.LAYOUT_LABELS["flow"]); app._preset_ref = "bencao"
        self.assertTrue(gui.preset_ok(app))

    def test_preset_combo_picks_builtin_or_checked_file(self):
        gui = self.gui
        good = self.td / "내책.json"; good.write_text('{"schema": 1, "label": "내 책"}', encoding="utf-8")
        bad = self.td / "틀림.json"; bad.write_text('{"schema": 1, "entry": {"opener": ["味"]}}', encoding="utf-8")
        app = types.SimpleNamespace(_preset_ref="kaibao", v_preset=Var(gui.preset_label("kaibao")), cb_preset=mock.Mock(),
                                    _layout_changed=mock.Mock(), _sync_settings_summary=mock.Mock())
        app._preset_values = gui.App._preset_values.__get__(app)
        pick = lambda: gui.App._preset_selected(app)
        app.v_preset.set(gui.PRESET_PICK)
        with mock.patch.object(gui.filedialog, "askopenfilename", return_value=str(bad)), mock.patch.object(gui.messagebox, "showerror") as err:
            pick()
        self.assertIn("이 짜임 규칙 파일은 쓸 수 없습니다.\n짜임 규칙 파일 ", err.call_args.args[1]); self.assertIn("모르는 칸 'entry.opener'", err.call_args.args[1])
        self.assertEqual((app._preset_ref, app.v_preset.get()), ("kaibao", gui.preset_label("kaibao")))   # 앞의 것 그대로
        app.v_preset.set(gui.PRESET_PICK)
        with mock.patch.object(gui.filedialog, "askopenfilename", return_value=""):
            pick()                                                                         # 고르기를 그만둠
        self.assertEqual(app._preset_ref, "kaibao")
        app.v_preset.set(gui.PRESET_PICK)
        with mock.patch.object(gui.filedialog, "askopenfilename", return_value=str(good)):
            pick()
        self.assertEqual((app._preset_ref, app.v_preset.get()), (str(good), "파일: 내책.json"))
        self.assertEqual(app.cb_preset.configure.call_args.kwargs["values"][-3:], ["파일: 내책.json", gui.PRESET_PICK, gui.PRESET_EXPORT])
        app.v_preset.set(gui.preset_label("regions")); pick()
        self.assertEqual(app._preset_ref, "regions")
        self.assertEqual(app._layout_changed.call_count, 4); self.assertEqual(app._sync_settings_summary.call_count, 4)

    def test_missing_template_blocks_build_and_run_before_model_work(self):
        gui = self.gui
        missing = str(self.td / "없는_양식.hwpx")
        app = self.app(missing)
        with mock.patch.object(gui.messagebox, "showerror") as err:
            gui.App.do_build(app); gui.App.do_run(app)
        self.assertEqual(err.call_count, 2); self.assertIn(f"양식 파일이 없습니다: {missing}", err.call_args.args[1])
        app.start.assert_not_called()
        self.assertEqual(gui.missing_template(app), missing)
        app.v_layout.set(gui.LAYOUT_LABELS["page"])                                     # 원본 쪽대로면 양식 칸은 쓰지 않는다
        self.assertEqual(gui.missing_template(app), "")
        present = self.td / "양식.hwpx"; present.write_bytes(b"PK")
        app = self.app(str(present)); app._with_plan = lambda kind, go: go([])
        with mock.patch.object(gui.messagebox, "showerror") as err:
            gui.App.do_build(app)
        err.assert_not_called(); app.start.assert_called_once()
        self.assertEqual(gui.missing_template(types.SimpleNamespace(cfg={})), "")           # 양식 칸이 없는 가짜 창

    def test_settings_summary_names_template_or_says_none(self):
        gui = self.gui
        app = types.SimpleNamespace(v_docx=Var(False), v_layout=Var(gui.LAYOUT_LABELS["flow"]), v_template=Var(str(self.td / "양식" / "본초.hwpx")),
                                    v_provider=Var("Gemini"), v_model=Var("gemini-x"), v_mode=Var("세로"), v_settings_summary=Var(""), v_force_scope=Var(""),
                                    _active_input=0, _preset_ref="kaibao")
        app.layout_key = gui.App.layout_key.__get__(app)
        gui.App._sync_settings_summary(app)
        self.assertTrue(app.v_settings_summary.get().endswith("HWPX · 내용별 · 양식 본초.hwpx · 짜임 本草 조문"), app.v_settings_summary.get())
        app.v_template.set("  "); app._preset_ref = "regions"
        gui.App._sync_settings_summary(app)
        self.assertTrue(app.v_settings_summary.get().endswith("HWPX · 내용별 · 양식 없음(기본 모양) · 짜임 영역 종류만"), app.v_settings_summary.get())
        app.v_layout.set(gui.LAYOUT_LABELS["page"])
        gui.App._sync_settings_summary(app)
        self.assertTrue(app.v_settings_summary.get().endswith(" · HWPX"))
        app.v_layout.set(gui.LAYOUT_LABELS["flow"]); app._preset_ref = str(self.td / "옮겨진 (v2).json")   # 없어진 규칙 파일 — 실행 전에 보이게
        gui.App._sync_settings_summary(app)
        self.assertTrue(app.v_settings_summary.get().endswith("짜임 파일: 옮겨진 (v2).json (읽지 못함)"), app.v_settings_summary.get())
        app._preset_ref = "bencao"
        gui.App._sync_settings_summary(app)
        self.assertTrue(app.v_settings_summary.get().endswith("짜임 本草 조문 · 약재명 늘 떼기(예전 설정 bencao)"), app.v_settings_summary.get())

    def test_template_with_bencao_styles_offers_the_kaibao_preset(self):
        """설정이 덮여 영역 종류만으로 옮겨진 사람이 本草 양식을 다시 고르면 묻는다 — 예: 本草 조문으로, 아니요: 그대로 두고 진행 기록에 한 줄."""
        import zipfile
        gui = self.gui
        tpl = self.td / "본초학.hwpx"
        with zipfile.ZipFile(tpl, "w") as z:
            z.writestr("Contents/header.xml", '<hh:head xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head"><hh:styles>'
                       '<hh:style id="0" name="바탕글"/><hh:style id="1" name="제목-본초명+생약명"/><hh:style id="2" name="본경 원문"/></hh:styles></hh:head>')
        plain = self.td / "보통.hwpx"
        with zipfile.ZipFile(plain, "w") as z:
            z.writestr("Contents/header.xml", '<hh:head xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head"><hh:style id="0" name="본문"/></hh:head>')
        self.assertEqual(gui.template_suggests(tpl, "regions"), ["본경 원문", "제목-본초명+생약명"])
        self.assertEqual((gui.template_suggests(tpl, "kaibao"), gui.template_suggests(plain, "regions"), gui.template_suggests(self.td / "없음.hwpx", "regions")), ([], [], []))
        for yes, want in ((False, "regions"), (True, "kaibao")):
            log = []
            app = types.SimpleNamespace(_preset_ref="regions", v_preset=Var(""), cb_preset=mock.Mock(), _layout_changed=mock.Mock(), _sync_settings_summary=mock.Mock(),
                                        _append=log.append, v_template=Var(""), v_layout=Var(gui.LAYOUT_LABELS["page"]))
            app._preset_values = gui.App._preset_values.__get__(app); app._suggest_preset = gui.App._suggest_preset.__get__(app)
            app.layout_key = gui.App.layout_key.__get__(app)
            with mock.patch.object(gui.filedialog, "askopenfilename", return_value=str(tpl)), mock.patch.object(gui.messagebox, "askyesno", return_value=yes) as ask:
                gui.App.pick_template(app)
            self.assertIn("짜임 규칙을 '本草 조문'으로 바꿀까요?", ask.call_args.args[1])
            self.assertEqual((app._preset_ref, app.layout_key()), (want, "flow"))
            self.assertIn("짜임 규칙을 '本草 조문" if yes else "주의: 이 양식에는 本草 조문 규칙(kaibao)의 스타일", log[-1])
        with mock.patch.object(gui.filedialog, "askopenfilename", return_value=str(tpl)), mock.patch.object(gui.messagebox, "askyesno") as ask:
            gui.App.pick_template(app)                                                    # 이미 本草 조문이면 묻지 않는다
        ask.assert_not_called()

    def test_export_builtin_preset_copy(self):
        gui = self.gui
        log = []
        app = types.SimpleNamespace(_preset_ref="regions", v_preset=Var(gui.PRESET_EXPORT), cb_preset=mock.Mock(), _layout_changed=mock.Mock(),
                                    _sync_settings_summary=mock.Mock(), _append=log.append)
        app._preset_values = gui.App._preset_values.__get__(app)
        dest = self.td / "내책_규칙.json"
        with mock.patch.object(gui.filedialog, "asksaveasfilename", return_value=str(dest)):
            gui.App._preset_selected(app)
        from yeone_ocr.presets import DATA, load_preset
        self.assertEqual(dest.read_bytes(), (DATA / "preset_kaibao.json").read_bytes())
        self.assertEqual(load_preset(dest).id, "kaibao")
        self.assertEqual((app._preset_ref, app.v_preset.get()), ("regions", gui.preset_label("regions")))   # 고르던 규칙은 그대로
        self.assertIn(f"本草 조문 규칙의 사본을 저장했습니다: {dest}", log[-1])
        app.v_preset.set(gui.PRESET_EXPORT)
        with mock.patch.object(gui.filedialog, "asksaveasfilename", return_value=str(self.td / "없는폴더" / "x.json")), \
             mock.patch.object(gui.messagebox, "showerror") as err:
            gui.App._preset_selected(app)
        self.assertIn("규칙 파일을 저장하지 못했습니다", err.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
