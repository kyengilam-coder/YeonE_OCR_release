"""Local-first launch settings, scoped API authorization, and review routing."""
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


class LocalFirstGuiTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.gui = self.stack.enter_context(gui_module())
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    def app(self, **changes):
        gui = self.gui
        app = SimpleNamespace(cfg=dict(gui.DEFAULTS, local_engine_root="chosen-engines"),
            v_reader=Var("openai"), v_key=Var(""), v_maxcalls=Var("0"),
            v_ocr_strategy=Var(gui.OCR_STRATEGY_LABELS["local-first"]),
            v_local_profile=Var(gui.LOCAL_PROFILE_LABELS["traditional"]),
            v_local_ai_review=Var(True), v_local_ai_max_calls=Var("20"),
            save_settings=mock.Mock(), _append=mock.Mock())
        for key, value in changes.items():
            getattr(app, key).set(value)
        return app

    def test_strategy_flags_preserve_scope_and_explicit_no_review(self):
        app = self.app(v_local_profile=self.gui.LOCAL_PROFILE_LABELS["mixed"], v_local_ai_review=False,
                       v_local_ai_max_calls="7")
        flags = self.gui.App._strategy_opts(app)
        self.assertEqual(flags, ["--ocr-strategy", "local-first", "--local-profile", "mixed",
            "--local-ai-max-calls", "7", "--no-local-ai-review", "--local-engine-root", "chosen-engines"])
        app.v_ocr_strategy.set(self.gui.OCR_STRATEGY_LABELS["ai-full"])
        self.assertEqual(self.gui.App._strategy_opts(app), ["--ocr-strategy", "ai-full"])

    def test_two_column_order_is_on_by_default_and_only_switching_it_off_adds_a_flag(self):
        app = self.app()
        self.assertNotIn("--no-local-columns", self.gui.App._strategy_opts(app))
        app.v_local_columns = Var(False)
        self.assertIn("--no-local-columns", self.gui.App._strategy_opts(app))
        self.assertIs(self.gui.DEFAULTS["local_columns"], True)

    def test_line_verification_is_off_by_default_and_switching_it_on_adds_a_flag(self):
        app = self.app()
        self.assertNotIn("--local-line-verification", self.gui.App._strategy_opts(app))
        app.v_local_line_verification = Var(True)
        self.assertIn("--local-line-verification", self.gui.App._strategy_opts(app))
        self.assertIs(self.gui.DEFAULTS["local_line_verification"], False)

    def test_easyocr_is_off_for_horizontal_by_default_and_checking_it_adds_a_flag(self):
        app = self.app()
        self.assertNotIn("--local-easyocr", self.gui.App._strategy_opts(app))
        app.v_local_easyocr = Var(True)
        self.assertIn("--local-easyocr", self.gui.App._strategy_opts(app))
        self.assertIs(self.gui.DEFAULTS["local_easyocr"], False)

    def test_subscription_plan_choice_adds_a_flag_only_when_not_automatic(self):
        gui = self.gui
        app = self.app()
        self.assertNotIn("--local-ai-plan", gui.App._strategy_opts(app))               # 자동: 플래그 없음
        app.v_local_ai_subscription = Var(True)
        app.v_local_ai_plan = Var(gui.AI_PLAN_LABELS["chatgpt"])
        flags = gui.App._strategy_opts(app)
        self.assertEqual(flags[flags.index("--local-ai-plan") + 1], "chatgpt")
        app.v_local_ai_plan = Var(gui.AI_PLAN_LABELS["gemini"])
        self.assertIn("gemini", gui.App._strategy_opts(app))
        app.v_local_ai_subscription = Var(False)                                        # 구독 끔: 계정 선택은 보내지 않음
        flags = gui.App._strategy_opts(app)
        self.assertNotIn("--local-ai-plan", flags)
        self.assertIn("--local-ai-auth", flags)
        self.assertEqual(gui.DEFAULTS["local_ai_plan"], "auto")
        self.assertEqual(set(gui.AI_PLAN_LABELS), {"auto", "claude", "chatgpt", "gemini"})

    def test_ai_link_row_lists_targets_and_the_button_switches_to_the_choice_or_the_order(self):
        import queue
        gui = self.gui
        row = mock.Mock(); label = mock.Mock(); box = mock.Mock(); button = mock.Mock()
        app = SimpleNamespace(_ai_link_row=row, l_ai_link=label, cb_ai_next=box, b_ai_switch=button,
                              v_ai_next=Var(gui.AI_NEXT_AUTO), px=lambda v: v, q=queue.Queue())
        state = {"plan": "claude", "label": "Claude 구독", "available": True, "targets": ["chatgpt", "gemini", "api"],
                 "next": "chatgpt"}
        gui.App._show_ai_link(app, state)
        row.grid.assert_called_once()
        label.configure.assert_called_with(text="Claude 구독")
        values = box.configure.call_args.kwargs["values"]
        self.assertEqual(values, ["자동(순서대로) (다음: ChatGPT 구독)", "ChatGPT 구독", "Gemini 구독", "API 키"])
        self.assertEqual(app.v_ai_next.get(), values[0])
        with mock.patch("yeone_ocr.subscriptions.switch", return_value="넘김") as switch, \
                mock.patch("yeone_ocr.subscriptions.state", return_value=None):
            gui.App.switch_subscription(app)                                # 고르지 않음: 정해진 순서
            self.assertEqual(app.q.get(timeout=5), ("log", "  넘김\n"))
            app.v_ai_next.set("Gemini 구독")
            gui.App.switch_subscription(app)
            app.q.get(timeout=5); app.q.get(timeout=5)
        self.assertEqual([c.args for c in switch.call_args_list], [(None,), ("gemini",)])
        gui.App._show_ai_link(app, None)
        row.grid_remove.assert_called_once()

    def test_ai_link_row_names_every_connection_and_the_resting_plans_and_the_test_button_reports(self):
        import queue
        gui = self.gui
        label, tip = mock.Mock(), SimpleNamespace(text="")
        app = SimpleNamespace(_ai_link_row=mock.Mock(), l_ai_link=label, cb_ai_next=mock.Mock(), b_ai_switch=mock.Mock(),
                              _ai_link_tip=tip, v_ai_next=Var(gui.AI_NEXT_AUTO), px=lambda v: v, q=queue.Queue(),
                              b_plan_test=mock.Mock(), l_plans=mock.Mock())
        state = {"plan": "claude", "label": "Claude 구독 · Gemini 구독", "available": True, "targets": ["chatgpt", "gemini"], "next": "gemini",
                 "resting": [{"plan": "chatgpt", "reason": "Codex 사용량 한도(원문: usage limit)", "minutes": 42}]}
        gui.App._show_ai_link(app, state)
        label.configure.assert_called_with(text="Claude 구독 · Gemini 구독  |  쉼: ChatGPT(42분 뒤 다시)")
        self.assertIn("ChatGPT 구독 — 약 42분 뒤 다시 씁니다: Codex 사용량 한도(원문: usage limit)", tip.text)
        with mock.patch("yeone_ocr.cli.plan_test_lines", return_value=["Claude: 응답함 · 모델 opus · 9초"]):
            gui.App.test_plans(app)
            self.assertEqual(app.q.get(timeout=5), ("plan_test", ["Claude: 응답함 · 모델 opus · 9초"]))
        gui.App.test_plans(app)                                                         # 시험 중에는 다시 시작하지 않는다
        self.assertTrue(app.q.empty())

    def _partial_app(self, **extra):
        seen = {}

        def prepare():
            seen["need_key"] = vars(app).pop("_env_need_key", True)
            return True
        app = SimpleNamespace(running=False, _need_records=lambda what: True, pdf=lambda: "book.pdf", work=lambda: "work-folder",
                              _prepare_env=prepare, _pages=lambda: ["--pages", "1-200"], _build_opts=lambda: ["--layout", "page"],
                              _answered_opts=lambda: ["--skip-answered", "same-model"], v_reader=Var("gemini"),
                              nb=mock.Mock(), _save_book=mock.Mock(), start=mock.Mock(), **extra)
        return app, seen

    def test_partial_document_is_built_from_the_pages_read_so_far_without_calling_a_model(self):
        gui = self.gui
        app, seen = self._partial_app()
        with mock.patch.object(gui.App, "_result_editors_ready", return_value=True), \
                mock.patch.object(gui.App, "_corrections_ok", return_value=True), \
                mock.patch.object(gui, "template_ok", return_value=True), mock.patch.object(gui, "preset_ok", return_value=True):
            gui.App.do_partial(app)
            argv, label = app.start.call_args.args
            self.assertEqual(argv, ["build", "book.pdf", "--work", "work-folder", "--pages", "1-200", "--layout", "page",
                                    "--reader", "gemini", "--no-context-model", "--skip-answered", "same-model"])
            self.assertEqual(label, gui.PARTIAL_LABEL)
            self.assertIs(seen["need_key"], False)                                      # 모델을 부르지 않으니 키를 묻지 않는다
            app.start.reset_mock(); app.running = True
            gui.App.do_partial(app)                                                     # 실행 중에는 시작하지 않는다
            app.start.assert_not_called()

    def test_partial_button_shows_only_for_a_book_read_part_way(self):
        gui = self.gui
        app = SimpleNamespace(pdf=lambda: "book.pdf", _counts={"target": 462, "records": 193, "to_read": 269})
        with mock.patch.object(gui.App, "_n_records", return_value=193):
            self.assertTrue(gui.App._partial_wanted(app))
            app._counts = {"target": 462, "records": 462, "to_read": 0}
            self.assertFalse(gui.App._partial_wanted(app))                              # 다 읽은 책
            self.assertTrue(gui.App._partial_wanted(app, {"kind": "stopped"}))          # 방금 중지: 엔진만 읽고 AI를 기다리던 쪽이 있을 수 있다
            app._counts = None
            self.assertTrue(gui.App._partial_wanted(app))                               # 아직 세는 중
        with mock.patch.object(gui.App, "_n_records", return_value=0):
            self.assertFalse(gui.App._partial_wanted(app, {"kind": "stopped"}))         # 판독 기록이 없으면 만들 것이 없다

    def test_stopping_a_run_offers_a_document_of_the_pages_read_so_far(self):
        import queue
        gui = self.gui
        var = lambda: SimpleNamespace(set=lambda v: None, get=lambda: "")

        def finish(rc, label, answer, records=193):
            later = []
            app = SimpleNamespace(_set_running=lambda on, command="": None, _closing=False, v_status=var(), v_prog=var(), _append=lambda s: None,
                                  tracker=SimpleNamespace(t0=0.0, frac=0.4, finish=lambda: None, command="run"),
                                  progress=SimpleNamespace(configure=lambda **k: None), refresh_table=lambda: None, open_after=None,
                                  _last_writer=gui.QueueWriter(queue.Queue()), pdf=lambda: "book.pdf", open_hwpx=mock.Mock(),
                                  after=lambda _ms, call: later.append(call))
            with mock.patch.object(gui, "messagebox") as box, mock.patch.object(gui.App, "_n_records", return_value=records), \
                    mock.patch.object(gui.App, "_request_counts"), mock.patch.object(gui.App, "do_partial") as partial:
                box.askyesno.return_value = answer
                gui.App._finish(app, rc, label)
                for call in later:
                    call()
            return box, partial, app
        box, partial, _ = finish(130, "한 번에 인식 (전체 → HWPX)", True)
        self.assertIn("판독 기록이 있는 193쪽으로 HWPX를 만들까요", box.askyesno.call_args.args[1])
        partial.assert_called_once()
        box, partial, _ = finish(130, "한 번에 인식 (전체 → HWPX)", False)
        partial.assert_not_called()                                                     # 아니요: 나중에 단추로
        box, partial, _ = finish(130, "한 번에 인식 (전체 → HWPX)", True, records=0)
        box.askyesno.assert_not_called()                                                # 읽은 쪽이 없으면 묻지 않는다
        box, partial, _ = finish(130, "한 번에 인식 (문자 PDF 만들기)", True)
        box.askyesno.assert_not_called()                                                # 문자 PDF 실행은 묻지 않는다
        box, partial, app = finish(0, gui.PARTIAL_LABEL, True)
        self.assertIn("지금까지 읽은 쪽으로 HWPX를 만들었습니다", box.askyesno.call_args.args[1])
        app.open_hwpx.assert_called_once()

    def test_resume_choice_adds_a_flag_only_when_not_the_default(self):
        gui = self.gui
        app = self.app()
        self.assertNotIn("--local-resume", gui.App._strategy_opts(app))
        self.assertEqual(gui.DEFAULTS["local_resume"], "unfinished")
        for key in ("continue", "all"):
            app.v_local_resume = Var(gui.RESUME_LABELS[key])
            flags = gui.App._strategy_opts(app)
            self.assertEqual(flags[flags.index("--local-resume") + 1], key)
        app.v_local_resume = Var(gui.RESUME_LABELS["unfinished"])
        self.assertNotIn("--local-resume", gui.App._strategy_opts(app))

    def test_parallel_requests_add_a_flag_only_when_not_the_default(self):
        gui = self.gui
        app = self.app()
        self.assertNotIn("--local-ai-parallel", gui.App._strategy_opts(app))
        self.assertEqual(gui.DEFAULTS["local_ai_parallel"], 3)
        app.v_local_ai_parallel = Var("1")
        flags = gui.App._strategy_opts(app)
        self.assertEqual(flags[flags.index("--local-ai-parallel") + 1], "1")
        app.v_local_ai_parallel = Var("9")                                              # out of range: the default
        self.assertNotIn("--local-ai-parallel", gui.App._strategy_opts(app))

    def test_pages_a_plan_takes_together_add_a_flag_only_when_not_the_default(self):
        gui = self.gui
        app = self.app()
        self.assertNotIn("--local-ai-pages", gui.App._strategy_opts(app))
        self.assertEqual(gui.DEFAULTS["local_ai_pages"], 2)
        app.v_local_ai_pages = Var("3")
        flags = gui.App._strategy_opts(app)
        self.assertEqual(flags[flags.index("--local-ai-pages") + 1], "3")
        app.v_local_ai_pages = Var("9")                                                 # out of range: the default
        self.assertNotIn("--local-ai-pages", gui.App._strategy_opts(app))

    def test_split_is_on_by_default_and_unchecking_it_adds_a_flag(self):
        gui = self.gui
        app = self.app()
        self.assertNotIn("--no-local-ai-split", gui.App._strategy_opts(app))
        self.assertIs(gui.DEFAULTS["local_ai_split"], True)
        app.v_local_ai_split = Var(False)
        self.assertIn("--no-local-ai-split", gui.App._strategy_opts(app))

    def test_split_saved_off_by_1_2_8_is_turned_on_once_and_a_later_choice_is_kept(self):
        gui = self.gui
        old = gui.upgrade_config(dict(gui.DEFAULTS, local_ai_split=False), {"local_ai_split": False})
        self.assertIs(old["local_ai_split"], True)                                    # 1.2.8 의 기본값 '끔'
        self.assertEqual(old["local_ai_split_version"], 1)
        kept = gui.upgrade_config(dict(gui.DEFAULTS, local_ai_split=False),
                                  {"local_ai_split": False, "local_ai_split_version": 1})
        self.assertIs(kept["local_ai_split"], False)                                  # 1.2.9 에서 직접 끈 값

    def test_install_button_asks_first_runs_the_official_command_and_guides_without_node(self):
        import queue
        gui = self.gui
        app = SimpleNamespace(v_setup_plan=Var(gui.SETUP_LABELS["gemini"]), q=queue.Queue(), v_status=Var(""))
        with mock.patch("yeone_ocr.plan_setup.find_npm", return_value=None), \
                mock.patch("yeone_ocr.plan_setup.find_winget", return_value=None), \
                mock.patch.object(gui.messagebox, "askyesno", return_value=True) as ask, \
                mock.patch("webbrowser.open") as browser:
            gui.App.install_plan(app)
        self.assertIn("Node.js", ask.call_args.args[0]); browser.assert_called_once_with("https://nodejs.org/")
        ran = []
        with mock.patch("yeone_ocr.plan_setup.find_npm", return_value="npm"), \
                mock.patch("yeone_ocr.plan_setup.run", side_effect=lambda command, log: ran.append(command) or 0), \
                mock.patch.object(gui.messagebox, "askyesno", return_value=True) as ask:
            gui.App.install_plan(app)
            self.assertIn("npm install -g @google/gemini-cli", ask.call_args.args[1])
            self.assertEqual(app.q.get(timeout=5)[0], "log")
            self.assertEqual(app.q.get(timeout=5), ("setup_done", "Gemini (Gemini CLI) 설치", 0))
        self.assertEqual(ran, [["npm", "install", "-g", "@google/gemini-cli"]])
        app._setup_busy = False
        with mock.patch("yeone_ocr.plan_setup.find_npm", return_value="npm"), \
                mock.patch("yeone_ocr.plan_setup.run") as run, \
                mock.patch.object(gui.messagebox, "askyesno", return_value=False):
            gui.App.install_plan(app)                                                  # 아니요: 아무것도 하지 않음
        run.assert_not_called()

    def test_login_button_needs_the_program_and_opens_its_window(self):
        gui = self.gui
        app = SimpleNamespace(v_setup_plan=Var(gui.SETUP_LABELS["chatgpt"]))
        with mock.patch("yeone_ocr.chatgpt_subscription.find_cli", return_value=None), \
                mock.patch.object(gui.messagebox, "showinfo") as info:
            gui.App.open_plan_login(app)
        self.assertIn("먼저 [설치]", info.call_args.args[1])
        with mock.patch("yeone_ocr.chatgpt_subscription.find_cli", return_value="codex"), \
                mock.patch("yeone_ocr.plan_setup.open_login", return_value=None) as opened, \
                mock.patch.object(gui.messagebox, "showinfo") as info:
            gui.App.open_plan_login(app)
        opened.assert_called_once_with("chatgpt", "codex")
        self.assertIn("Sign in with ChatGPT", info.call_args.args[1])

    def test_local_run_allows_no_key_but_legacy_requires_it(self):
        app = self.app()
        app._env_optional_key = True
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(self.gui.messagebox, "showwarning") as warning:
            self.assertTrue(self.gui.App._prepare_env(app))
            warning.assert_not_called()
            app.save_settings.assert_called_once_with(quiet=True)
            self.assertFalse(self.gui.App._prepare_env(app))
            warning.assert_called_once()

    def test_configured_selected_key_is_forwarded_without_probe(self):
        app = self.app(v_key="sk-synthetic-key")
        app._env_optional_key = True
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(self.gui.messagebox, "askyesno", return_value=True):
            self.assertTrue(self.gui.App._prepare_env(app))
            import os
            self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-synthetic-key")

    def test_local_preflight_never_counts_full_page_ai_and_clears_stale_pending_ledger(self):
        app = self.app()
        app._ledger_pending = {"stale": True}
        go = mock.Mock()
        with mock.patch("yeone_ocr.costplan.count_plan", side_effect=AssertionError("must not count full-page AI")):
            self.gui.App._with_plan(app, "run", go)
        go.assert_called_once_with([])
        self.assertNotIn("_ledger_pending", vars(app))
        self.assertIsNone(app._ledger)
        self.assertIn("20회", app._append.call_args.args[0])

    def test_local_preview_states_cap_without_creating_output_or_sending(self):
        app = self.app(v_local_ai_review=False)
        with mock.patch.object(self.gui.messagebox, "showinfo") as info:
            self.gui.App._with_plan(app, "run-pdf", None)
        self.assertIn("외부 전송 없이", info.call_args.args[1])
        self.assertFalse(any(self.root.iterdir()))

    def test_invalid_local_cap_blocks_execution(self):
        for cap in ("", "0", "101", "oops"):
            app = self.app(v_local_ai_max_calls=cap)
            go = mock.Mock()
            with mock.patch.object(self.gui.messagebox, "showwarning"):
                self.gui.App._with_plan(app, "run", go)
            go.assert_not_called()

    def test_settings_roundtrip_uses_user_selected_mode_profile_and_budget(self):
        gui = self.gui
        app = fake_app(gui, dict(gui.DEFAULTS))
        app.v_ocr_strategy = Var(gui.OCR_STRATEGY_LABELS["ai-full"])
        app.v_local_profile = Var(gui.LOCAL_PROFILE_LABELS["simplified"])
        app.v_local_ai_review = Var(False)
        app.v_local_ai_max_calls = Var("11")
        with mock.patch.object(gui, "write_config"):
            gui.App.save_settings(app, quiet=True)
        self.assertEqual({key: app.cfg[key] for key in ("ocr_strategy", "local_profile", "local_ai_review", "local_ai_max_calls")},
                         dict(ocr_strategy="ai-full", local_profile="simplified", local_ai_review=False, local_ai_max_calls=11))

    def test_document_and_pdf_launch_share_local_first_options(self):
        for pdf_only in (False, True):
            with self.subTest(pdf_only=pdf_only):
                gui = self.gui
                app = self.app(v_local_profile=gui.LOCAL_PROFILE_LABELS["simplified"], v_local_ai_max_calls="9")
                app._need_pdf = lambda: True
                app._prepare_env = mock.Mock(return_value=True)
                app._save_book = mock.Mock()
                app.nb = mock.Mock()
                app.v_pages = Var("2-3")
                app.work = lambda: self.root / "work"
                app.pdf = lambda: str(self.root / "fixture.pdf")
                app._pages = lambda: ["--pages", "2-3"]
                app._read_opts = lambda: ["--reader", "openai"]
                app._glyph_opts = lambda **_: []
                app._answered_opts = lambda: []
                app._build_opts = lambda: []
                app.start = mock.Mock()
                app._with_plan = lambda kind, go: gui.App._with_plan(app, kind, go)
                with mock.patch.object(gui.App, "_corrections_ok", return_value=True), \
                     mock.patch.object(gui, "template_ok", return_value=True), mock.patch.object(gui, "preset_ok", return_value=True):
                    (gui.App.do_run_pdf if pdf_only else gui.App.do_run)(app)
                argv = app.start.call_args.args[0]
                for flag in ("--ocr-strategy", "local-first", "--local-profile", "simplified", "--local-ai-max-calls", "9"):
                    self.assertIn(flag, argv)
                self.assertEqual("--pdf-only" in argv, pdf_only)
                self.assertTrue(app._env_optional_key)

    def test_main_pipeline_project_opens_for_proposal_adoption(self):
        from yeone_ocr.comparison import ComparisonProject
        source = self.root / "fixture.png"
        Image.new("RGB", (50, 50), "white").save(source)
        work = self.root / "work"
        work.mkdir()
        project = ComparisonProject.open(source)
        path = work / "local-first.json"
        project.save(path)
        (work / "state.json").write_text(json.dumps({"local_first_project": str(path)}), encoding="utf-8")
        app = SimpleNamespace(running=False, _closing=False, _selected_tab=0, pdf=lambda: str(source),
            mode_key=lambda _tab=None: "auto", cfg={}, work=lambda: work)
        with mock.patch("yeone_ocr.comparison_editor.ComparisonEditor") as editor:
            self.gui.App.open_local_comparison(app)
        self.assertEqual(Path(editor.call_args.kwargs["project_path"]), path)

    def folder_project(self):
        from yeone_ocr.comparison import ComparisonProject
        from yeone_ocr.pdfio import open_pdf
        from yeone_ocr.state import Work
        source = self.root / "image-bundle"
        source.mkdir()
        Image.new("RGB", (50, 50), "white").save(source / "1.png")
        work = Work(str(source), str(self.root / "bundle-work"))
        snapshot = work.dir / "snapshot.pdf"
        with open_pdf(str(source)) as document:
            document.save(snapshot)
        project = ComparisonProject.open(snapshot)
        path = work.dir / "local-first.json"
        project.save(path)
        state = work.load_state()
        state["local_first_project"] = str(path)
        work.save_state(state)
        app = SimpleNamespace(running=False, _closing=False, _selected_tab=2, pdf=lambda: str(source),
            mode_key=lambda _tab=None: "vertical", cfg={}, work=lambda: work.dir, v_status=Var(""),
            v_ocr_strategy=Var(self.gui.OCR_STRATEGY_LABELS["local-first"]),
            v_local_profile=Var(self.gui.LOCAL_PROFILE_LABELS["traditional"]))
        return app, source, work, snapshot, path

    def test_image_folder_snapshot_project_reopens_without_directory_source_override(self):
        app, source, work, snapshot, path = self.folder_project()
        with mock.patch("yeone_ocr.comparison_editor.ComparisonEditor") as editor:
            self.gui.App.open_local_comparison(app)
        values = editor.call_args.kwargs
        self.assertEqual(Path(values["project_path"]), path)
        self.assertEqual(Path(values["source_path"]), snapshot)
        self.assertEqual(Path(values["work_dir"]), work.dir)
        self.assertIsNone(values["profile"])
        self.assertIsNone(values["direction"])

    def test_modified_image_folder_never_opens_old_snapshot_project(self):
        app, source, work, snapshot, path = self.folder_project()
        Image.new("RGB", (50, 50), "black").save(source / "1.png")
        with mock.patch("yeone_ocr.comparison_editor.ComparisonEditor") as editor:
            self.gui.App.open_local_comparison(app)
        editor.assert_not_called()
        self.assertIn("판독 당시와 다릅니다", app.v_status.get())

    def test_changed_snapshot_is_rejected_even_if_original_folder_matches(self):
        app, source, work, snapshot, path = self.folder_project()
        with snapshot.open("ab") as stream:
            stream.write(b"changed")
        with mock.patch("yeone_ocr.comparison_editor.ComparisonEditor") as editor:
            self.gui.App.open_local_comparison(app)
        editor.assert_not_called()
        self.assertIn("저장된 원본", app.v_status.get())

    def test_manual_local_comparison_keeps_traditional_vertical_profile(self):
        app = self.app()
        app.running = False
        app._selected_tab = 0
        app.pdf = lambda: str(self.root / "book.pdf")
        app.mode_key = lambda _tab=None: "vertical"
        app.work = lambda: self.root / "not-run"
        with mock.patch("yeone_ocr.comparison_editor.ComparisonEditor") as editor:
            self.gui.App.open_local_comparison(app)
        self.assertEqual(editor.call_args.kwargs["profile"], "traditional")
        self.assertEqual(editor.call_args.kwargs["direction"], "vertical")

    def test_manual_comparison_uses_one_selected_provider_key(self):
        first, second = "AIza" + "A" * 35, "AIza" + "B" * 35
        app = self.app(v_reader="gemini", v_key=first + "," + second)
        app.v_model = Var("synthetic-vision")
        self.assertEqual(self.gui.App._comparison_ai_settings(app)["api_key"], first)

    def test_local_progress_and_pending_finish_never_claim_third_full_ai_pass(self):
        from yeone_ocr.gui_progress import band_view
        view = band_view("run", phase="done", result={"kind": "partial", "rc": 4}, local_first=True)
        names = {row["key"]: row for row in view}
        self.assertEqual(names["read"]["name"], "로컬 판독·구간 검증")
        self.assertEqual(names["adjudicate"]["name"], "검증 기록")
        self.assertFalse(names["build"]["receipt"])
        self.assertNotIn("3차", repr(view))
        self.assertIn("AI 검증 미완료", self.gui.finish_words(4, local_first=True)[1])
        alert = self.gui.finish_alert(4, "로컬 인식", ["주의: 키 없음"], local_first=True)
        self.assertIn("로컬 결과와 제안", alert[1])

    def test_call_count_includes_selective_transport_without_importing_reader(self):
        import sys
        with mock.patch.dict(sys.modules, {"yeone_ocr.cli": SimpleNamespace(LOCAL_API_CALLS=3),
                                          "yeone_ocr.reader_gemini": None}):
            self.assertEqual(self.gui.App._call_count(), 3)

    def test_editor_unlocalized_only_plan_reports_unresolved_instead_of_clean(self):
        import threading
        from yeone_ocr.comparison_editor import ComparisonEditor
        project = object()
        editor = SimpleNamespace(_closing=False, _closed=False, project=project,
                                 _stop=threading.Event(), status=Var(""))
        plan = SimpleNamespace(summary={"calls": 0, "unlocalized_targets": {"1": ["large-region"]}})
        ComparisonEditor._confirm_ai_plan(editor, plan, "unused", project)
        self.assertIn("특정하지 못해", editor.status.get())


if __name__ == "__main__":
    unittest.main()
