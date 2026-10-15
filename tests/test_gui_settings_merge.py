"""창 설정 저장은 이 창이 바꾼 값만 파일에 얹고, 모델 단계 어림이 실패하면 묻고, 양식 파일이 없으면 모델을 부르기 전에 막는다(화면·API 불필요)."""
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
             template=cfg.get("template", ""), bold=True, ctxmodel=True, maxcalls="0", status="")
    v.update(over)
    app = types.SimpleNamespace(cfg=cfg, _cfg_loaded=copy.deepcopy(cfg), **{"v_" + k: Var(x) for k, x in v.items()})
    app.check_list_mode = lambda: "none"; app.memo_mode = lambda: "notice"; app.collate_policy = lambda: "sure"; app.collate_memo_mode = lambda: "notice"
    app.media_res_key = lambda: "high"; app.mode_key = lambda i: "auto"; app.page_think_key = lambda: "auto"; app.page_mres_key = lambda: "auto"
    app.pdf = lambda: "book.pdf"; app._remember_inputs = lambda: None
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


def _plan_app(gui, pdf, work, ctxmodel=True):
    app = types.SimpleNamespace(v_reader=Var("openai"), v_model=Var("gpt-4.1"), v_force=Var(False), v_pages=Var(""), v_blind=Var(False),
                                v_status=mock.Mock(), update_idletasks=lambda: None, log=[])
    if ctxmodel is not None:
        app.v_ctxmodel = Var(ctxmodel); app.v_layout = Var(gui.LAYOUT_LABELS["page"])
    app.pdf = lambda: str(pdf); app.work = lambda: Path(work.dir)
    app._append = app.log.append
    for name in ("_model_step_plan", "_glyph_plan", "_glyph_model", "_confirm_model_steps", "layout_key"):
        setattr(app, name, getattr(gui.App, name).__get__(app))
    return app


class ModelStepEstimateFailure(unittest.TestCase):
    """어림을 하지 못했으면 묻지 않고 모델을 부르는 일이 없다: 까닭을 보이고 [예]=그대로 · [아니요]=모델 없이(기본) · [취소]."""
    def setUp(self):
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.gui = self.stack.enter_context(gui_module())
        self.td = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    def ask(self, app, answer, adjudicate=True):
        shown = []
        def fake(title, msg, **kw):
            shown.append((msg, kw)); return answer
        with mock.patch.object(self.gui.messagebox, "askyesnocancel", fake):
            got = app._confirm_model_steps(adjudicate=adjudicate)
        return got, shown

    def test_normalization_estimate_exception_asks_with_reason_and_logs(self):
        from yeone_ocr import cli
        pdf, work = _records_work(self.gui, self.td)
        app = _plan_app(self.gui, pdf, work)
        with mock.patch.object(cli, "normalization_plan", side_effect=RuntimeError("boom")):
            for answer, want in ((True, []), (False, ["--no-adjudicate", "--no-context-model"]), (None, None)):
                got, shown = self.ask(app, answer)
                self.assertEqual(got, want)
                msg, kw = shown[0]
                self.assertIn("모델 호출 수를 어림하지 못했습니다", msg); self.assertIn("문맥 교정 어림 실패: RuntimeError: boom", msg)
                self.assertIn("[아니요] 모델 없이 실행(--no-adjudicate --no-context-model)", msg)
                self.assertIn("default", kw)                                           # 기본 단추는 [아니요]
            self.assertTrue(app.log and app.log[0].startswith("어림 실패: 문맥 교정 어림 실패: RuntimeError: boom"))
            got, shown = self.ask(app, False, adjudicate=False)                         # [④ 문서 만들기]는 3차 판별 선택지가 없다
            self.assertEqual(got, ["--no-context-model"])

    def test_glyph_estimate_exception_and_unreadable_records_ask_too(self):
        import yeone_ocr.adjudicate as adj
        from yeone_ocr import state
        pdf, work = _records_work(self.gui, self.td)
        app = _plan_app(self.gui, pdf, work, ctxmodel=None)                              # 문맥 교정 칸이 없는 옛 설정
        with mock.patch.object(adj, "pending_sites", side_effect=ValueError("깨진 기록")):
            got, shown = self.ask(app, False)
        self.assertEqual(got, ["--no-adjudicate"])
        self.assertIn("3차 판별 어림 실패: ValueError: 깨진 기록", shown[0][0])
        app = _plan_app(self.gui, pdf, work)
        with mock.patch.object(state, "Work", side_effect=OSError("권한 없음")):
            got, shown = self.ask(app, False)
        self.assertEqual(got, ["--no-adjudicate", "--no-context-model"])
        self.assertIn("판독 기록을 읽지 못함: OSError: 권한 없음", shown[0][0])

    def test_suspect_sites_are_counted_and_shown(self):
        """비용 검토에서 잡힌 것: 〔a/b〕 와 문맥 교정이 0 이어도 합의 의심(入/人 자형 쌍) 호출은 나가므로 창에 세어 보이고, [아니요] 면 3차 판별째로 끈다."""
        import yeone_ocr.adjudicate as adj
        from yeone_ocr import cli
        pdf, work = _records_work(self.gui, self.td)
        app = _plan_app(self.gui, pdf, work); app.v_suspects = Var(True)
        none = {"total": 0, "reused": 0, "remembered": 0, "ask": 0, "calls": 0}
        with mock.patch.object(cli, "normalization_plan", return_value=none), mock.patch.object(cli, "_load_layer", return_value={"plain": "天地"}), \
             mock.patch.object(adj, "suspects_pending", return_value=11):
            got, shown = self.ask(app, False)
            self.assertEqual(got, ["--no-adjudicate", "--no-context-model"])
            self.assertIn("합의 판독 의심", shown[0][0]); self.assertIn("11곳", shown[0][0]); self.assertIn("약 2회", shown[0][0])
            self.assertEqual(self.ask(app, True)[0], [])
        with mock.patch.object(cli, "normalization_plan", return_value=none), mock.patch.object(cli, "_load_layer", side_effect=OSError("글자층 없음")):
            got, shown = self.ask(app, False)                                            # 합의 의심을 세지 못해도 묻는다
            self.assertIn("합의 의심 어림 실패: OSError: 글자층 없음", shown[0][0])
        app.v_suspects = Var(False)
        with mock.patch.object(cli, "normalization_plan", return_value=none), mock.patch.object(adj, "suspects_pending", side_effect=AssertionError("끄면 세지 않는다")), \
             mock.patch.object(self.gui.messagebox, "askyesnocancel", lambda *a, **k: self.fail("물을 것이 없으면 묻지 않는다")):
            self.assertEqual(app._confirm_model_steps(adjudicate=True), [])

    def test_no_records_or_nothing_to_ask_does_not_ask(self):
        from yeone_ocr import cli
        pdf, work = _records_work(self.gui, self.td)
        app = _plan_app(self.gui, pdf, work)
        with mock.patch.object(cli, "normalization_plan", return_value={"total": 0, "reused": 0, "remembered": 0, "ask": 0, "calls": 0}), \
             mock.patch.object(self.gui.messagebox, "askyesnocancel", lambda *a, **k: self.fail("물을 것이 없으면 묻지 않는다")):
            self.assertEqual(app._confirm_model_steps(adjudicate=True), [])
        for p in work.readings_dir.glob("p*.json"):
            p.unlink()                                                                  # 기록이 아직 없음 → 판독 비용 창이 대신한다
        with mock.patch.object(cli, "normalization_plan", side_effect=AssertionError("부르면 안 됨")), \
             mock.patch.object(self.gui.messagebox, "askyesnocancel", lambda *a, **k: self.fail("기록이 없으면 묻지 않는다")):
            self.assertEqual(app._confirm_model_steps(adjudicate=True), [])
        self.assertEqual(app.log, [])


class TemplateGuard(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.gui = self.stack.enter_context(gui_module())
        self.td = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    def app(self, template: str):
        gui = self.gui
        app = types.SimpleNamespace(_need_pdf=lambda: True, _prepare_env=lambda: True, _confirm_cost=lambda: self.fail("양식이 없으면 비용 창 전에 막는다"),
                                    v_reader=Var("anthropic"), v_model=Var("claude-custom"), v_pages=Var(""), pdf=lambda: "book.pdf", work=lambda: Path("work"),
                                    _pages=lambda: [], _build_opts=lambda: [], _cap_opts=lambda: [], _read_opts=lambda: [], _glyph_opts=lambda suspects=False: [],
                                    nb=mock.Mock(), v_layout=Var(gui.LAYOUT_LABELS["flow"]), v_template=Var(template),
                                    _confirm_model_steps=lambda adjudicate=False: self.fail("양식이 없으면 어림 전에 막는다"), start=mock.Mock())
        app.layout_key = gui.App.layout_key.__get__(app)
        return app

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
        app = self.app(str(present)); app._confirm_model_steps = lambda adjudicate=False: []
        with mock.patch.object(gui.messagebox, "showerror") as err:
            gui.App.do_build(app)
        err.assert_not_called(); app.start.assert_called_once()
        self.assertEqual(gui.missing_template(types.SimpleNamespace(cfg={})), "")           # 양식 칸이 없는 가짜 창

    def test_settings_summary_names_template_or_says_none(self):
        gui = self.gui
        app = types.SimpleNamespace(v_docx=Var(False), v_layout=Var(gui.LAYOUT_LABELS["flow"]), v_template=Var(str(self.td / "양식" / "본초.hwpx")),
                                    v_provider=Var("Gemini"), v_model=Var("gemini-x"), v_mode=Var("세로"), v_settings_summary=Var(""), v_force_scope=Var(""),
                                    _active_input=0)
        app.layout_key = gui.App.layout_key.__get__(app)
        gui.App._sync_settings_summary(app)
        self.assertTrue(app.v_settings_summary.get().endswith("HWPX · 내용별 · 양식 본초.hwpx"), app.v_settings_summary.get())
        app.v_template.set("  ")
        gui.App._sync_settings_summary(app)
        self.assertTrue(app.v_settings_summary.get().endswith("HWPX · 내용별 · 양식 없음(기본 모양)"), app.v_settings_summary.get())
        app.v_layout.set(gui.LAYOUT_LABELS["page"])
        gui.App._sync_settings_summary(app)
        self.assertTrue(app.v_settings_summary.get().endswith(" · HWPX"))


if __name__ == "__main__":
    unittest.main()
