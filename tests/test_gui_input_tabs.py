"""PDF·이미지 입력은 따로 보관하고 판독 설정은 공유한다(화면·개인 설정·API 불필요)."""
import contextlib
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image, ImageGrab

from test_ops import gui_module


class Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Frame:
    """Tk pack의 기존 순서 유지 규칙을 모델링해 입력/공통 카드 순서도 검사한다."""
    def __init__(self, layout):
        self.layout = layout

    def pack(self, **options):
        if "before" in options or "after" in options:
            self.pack_forget()
            neighbor = options.get("before", options.get("after"))
            self.layout.insert(self.layout.index(neighbor) + ("after" in options), self)
        elif self not in self.layout:
            self.layout.append(self)

    def pack_forget(self):
        if self in self.layout:
            self.layout.remove(self)


class InputTabs(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.gui = self.stack.enter_context(gui_module())
        self.saved = []
        self.save = self.stack.enter_context(mock.patch.object(
            self.gui, "save_config", side_effect=lambda cfg: self.saved.append(copy.deepcopy(cfg))))
        self.info = self.stack.enter_context(mock.patch.object(self.gui.messagebox, "showinfo"))
        self.error = self.stack.enter_context(mock.patch.object(self.gui.messagebox, "showerror"))

    def app(self):
        states = {0: {"pdf": "book.pdf", "pages": "10-20", "print_offset": "29"},
                  2: {"pdf": "image.png", "pages": "2", "print_offset": "-1"}}
        variables = {tab: {key: Var(value) for key, value in state.items()} for tab, state in states.items()}
        layout = []; actions = []
        frames = [Frame(layout) for _ in range(4)]
        common = frames[3]; cards = [Frame(actions) for _ in range(3)]
        frames[0].pack(); cards[0].pack()
        app = SimpleNamespace(running=False, _closing=False, _update_installing=False,
            _selected_tab=0, _active_input=0, _last_ocr_tab=0, _inputs_ready=True, _input_vars=variables, _input_force={0: True, 2: False},
            v_pdf=variables[0]["pdf"], v_pages=variables[0]["pages"], v_off=variables[0]["print_offset"], v_force=Var(True),
            v_tab=Var(0), v_tabhint=Var(""), v_pdf2=Var(""),
            v_reader=Var("anthropic"), v_model=Var("claude-test"), v_key=Var("synthetic-test-key"),
            cfg={"last_pdf": "book.pdf", "reader": "anthropic", "model": "claude-test", "api_key": "synthetic-test-key"},
            tab_frames=frames, _common_frame=common, act_cards=cards, layout=layout, actions=actions,
            _sync_output_location=mock.Mock(), _sync_settings_summary=mock.Mock(), refresh_table=mock.Mock(), _append=mock.Mock(),
            _bundle_images=mock.Mock(), mode_key=lambda: "horizontal_zh")
        for name in ("select_tab", "return_to_ocr", "_remember_inputs", "_reset_screenshot_pages", "pdf", "pdf2"):
            setattr(app, name, getattr(self.gui.App, name).__get__(app))
        app.work = lambda: self.gui.work_dir_for(app.pdf())
        app._use_pdf = mock.Mock(wraps=self.gui.App._use_pdf.__get__(app))
        return app

    @staticmethod
    def input_values(app, tab):
        return {key: var.get() for key, var in app._input_vars[tab].items()}

    def test_legacy_input_migrates_to_matching_tab_with_page_settings(self):
        for path, expected in (("book.PDF", 0), ("capture.PNG", 2), ("photo.jpg", 2), ("", 0)):
            with self.subTest(path=path):
                cfg = {"last_pdf": path, "pages": "3-5", "print_offset": "8", "force": True}
                before = copy.deepcopy(cfg)
                states, initial = self.gui.ocr_input_config(cfg)
                self.assertEqual(initial, expected)
                self.assertEqual(states[expected], {"pdf": path, "pages": "3-5", "print_offset": "8", "force": True})
                self.assertEqual(states[2 if expected == 0 else 0], {"pdf": "", "pages": "", "print_offset": "", "force": False})
                self.assertEqual(cfg, before)

    def test_separate_saved_inputs_override_legacy_values_and_restore_active_tab(self):
        saved = {"0": {"pdf": "saved.pdf", "pages": "5-9", "print_offset": "4", "force": True},
                 "2": {"pdf": "saved-images.pdf", "pages": "1-2", "print_offset": "", "force": False}}
        # 이미지 묶음도 PDF 확장자이므로 저장한 탭 번호가 우선한다.
        cfg = {"last_pdf": "legacy.pdf", "pages": "99", "input_tab": 2, "ocr_inputs": copy.deepcopy(saved)}
        states, initial = self.gui.ocr_input_config(cfg)
        self.assertEqual(initial, 2)
        self.assertEqual(states, {int(key): value for key, value in saved.items()})
        states[0]["pdf"] = "edited.pdf"
        self.assertEqual(cfg["ocr_inputs"], saved)

    def test_invalid_saved_shape_falls_back_to_legacy_image_without_crashing(self):
        for saved in (None, "invalid", {"0": [], "2": "invalid"}):
            with self.subTest(saved=saved):
                states, initial = self.gui.ocr_input_config({"last_pdf": "capture.png", "input_tab": 1, "ocr_inputs": saved})
                self.assertEqual((initial, states[2]["pdf"], states[0]["pdf"]), (2, "capture.png", ""))

    def test_switch_keeps_pdf_and_image_aliases_pages_offsets_and_force_separate(self):
        app = self.app()
        shared = (app.v_reader, app.v_model, app.v_key)
        app.select_tab(2)
        self.assertIs(app.v_pdf, app._input_vars[2]["pdf"])
        self.assertIs(app.v_pages, app._input_vars[2]["pages"])
        self.assertIs(app.v_off, app._input_vars[2]["print_offset"])
        self.assertEqual((app.pdf(), app.v_pages.get(), app.v_off.get(), app.v_force.get()), ("image.png", "2", "-1", False))
        app.v_pdf.set("new-image.png"); app.v_pages.set("4"); app.v_off.set("7"); app.v_force.set(True)
        app.select_tab(0)
        self.assertEqual((app.pdf(), app.v_pages.get(), app.v_off.get(), app.v_force.get()), ("book.pdf", "10-20", "29", True))
        app.v_force.set(False)
        app.select_tab(2)
        self.assertEqual((app.pdf(), app.v_pages.get(), app.v_off.get(), app.v_force.get()), ("new-image.png", "4", "7", True))
        self.assertEqual(app._input_force, {0: False, 2: True})
        self.assertTrue(all(old is new for old, new in zip(shared, (app.v_reader, app.v_model, app.v_key))))
        self.assertEqual(tuple(var.get() for var in shared), ("anthropic", "claude-test", "synthetic-test-key"))

    def test_input_tabs_only_show_input_cards_and_ocr_actions(self):
        app = self.app()
        for tab in (2, 0, 2):
            app.select_tab(tab)
            self.assertEqual(app.layout, [app.tab_frames[tab]])
            self.assertEqual(app.actions, [app.act_cards[0]])

    def test_bookmark_tab_hides_shared_controls_and_does_not_change_active_input(self):
        app = self.app(); app.select_tab(2)
        aliases = (app.v_pdf, app.v_pages, app.v_off)
        app.select_tab(1)
        self.assertEqual(app._active_input, 2)
        self.assertEqual((app.v_pdf, app.v_pages, app.v_off), aliases)
        self.assertEqual(app.layout, [app.tab_frames[1]])
        self.assertEqual(app.actions, [app.act_cards[1]])
        app.select_tab(2)
        self.assertEqual(app.layout, [app.tab_frames[2]])
        self.assertEqual(app.actions, [app.act_cards[0]])

    def test_running_rejects_tab_switch_and_restores_segment_selection(self):
        app = self.app(); app.running = True; app.v_tab.set(2)
        app.select_tab(2)
        self.assertEqual((app.v_tab.get(), app._selected_tab, app._active_input), (0, 0, 0))
        self.assertIs(app.v_pdf, app._input_vars[0]["pdf"])
        self.assertEqual(app.layout, [app.tab_frames[0]])
        app.refresh_table.assert_not_called()

    def test_settings_preserves_aliases_and_returns_to_last_pdf_or_image_work(self):
        for tab in (0, 2):
            with self.subTest(tab=tab):
                app = self.app(); app.select_tab(tab)
                app.v_pages.set("3-5"); app.v_off.set("6"); app.v_force.set(True)
                aliases = (app.v_pdf, app.v_pages, app.v_off)
                shared = (app.v_reader, app.v_model, app.v_key)
                app.select_tab(3)
                self.assertEqual(app.layout, [app._common_frame])
                self.assertEqual(app.actions, [app.act_cards[2]])
                self.assertEqual((app._last_ocr_tab, app._active_input), (tab, tab))
                self.assertEqual((app.v_pdf, app.v_pages, app.v_off), aliases)
                self.assertTrue(app.v_force.get())
                # 설정 탭에서 고친 공통 모델과 현재 작업의 force는 돌아가도 유지된다.
                app.v_model.set("claude-updated"); app.v_force.set(False)
                app.select_tab(3)  # 다시 선택해도 복귀 대상을 설정 탭으로 덮어쓰지 않는다.
                app.return_to_ocr()
                self.assertEqual((app.v_tab.get(), app._selected_tab, app._active_input), (tab, tab, tab))
                self.assertEqual((app.v_pdf, app.v_pages, app.v_off), aliases)
                self.assertEqual((app.v_pages.get(), app.v_off.get(), app.v_force.get()), ("3-5", "6", False))
                self.assertTrue(all(old is new for old, new in zip(shared, (app.v_reader, app.v_model, app.v_key))))
                self.assertEqual(app.v_model.get(), "claude-updated")
                self.assertEqual(app.actions, [app.act_cards[0]])

    def test_settings_returns_to_bookmark_tab_without_changing_general_or_image_inputs(self):
        app = self.app(); app.select_tab(2); app.select_tab(1)
        before = {tab: self.input_values(app, tab) for tab in (0, 2)}
        app.select_tab(3)
        self.assertEqual((app._last_ocr_tab, app._active_input), (1, 2))
        app.return_to_ocr()
        self.assertEqual(app._selected_tab, 1)
        self.assertEqual(app.layout, [app.tab_frames[1]])
        self.assertEqual(app.actions, [app.act_cards[1]])
        self.assertEqual({tab: self.input_values(app, tab) for tab in (0, 2)}, before)

    def test_settings_cannot_be_left_while_work_is_running(self):
        app = self.app(); app.select_tab(2); app.select_tab(3); app.running = True
        app.return_to_ocr()
        self.assertEqual((app._selected_tab, app.v_tab.get(), app._last_ocr_tab), (3, 3, 2))
        self.assertEqual(app.actions, [app.act_cards[2]])

    def test_settings_stop_is_enabled_for_advanced_work_and_disabled_afterwards(self):
        app = self.app(); app.select_tab(3)
        app.buttons = [mock.Mock()]; app._tab_buttons = [mock.Mock() for _ in range(4)]
        app._reader_changed = mock.Mock()
        app.b_stop, app.b_stop2, app.b_stop3 = mock.Mock(), mock.Mock(), mock.Mock()
        app.progress = mock.Mock(); app.v_prog = Var("")
        self.gui.App._set_running(app, True, "read")
        app.b_stop3.configure.assert_called_with(state="normal")
        self.assertEqual(app.actions, [app.act_cards[2]])
        for button in app.buttons + app._tab_buttons:
            button.configure.assert_called_with(state="disabled")
        self.gui.App._set_running(app, False)
        app.b_stop3.configure.assert_called_with(state="disabled")
        self.gui.App._set_running(app, True, "update")
        app.b_stop3.configure.assert_called_with(state="disabled")

    def test_advanced_build_from_settings_uses_last_active_ocr_input(self):
        for tab, source in ((0, "book.pdf"), (2, "image.png")):
            with self.subTest(tab=tab):
                app = self.app(); app.select_tab(tab); app.select_tab(3)
                app._need_pdf = lambda: True; app._prepare_env = lambda: True
                app._pages = self.gui.App._pages.__get__(app)
                app._build_opts = lambda: []; app._cap_opts = lambda: []
                app.start = mock.Mock()
                self.gui.App.do_build(app)
                argv = app.start.call_args.args[0]
                self.assertEqual(argv[:2], ["build", source])
                self.assertEqual(argv[argv.index("--pages") + 1], app.v_pages.get())
                self.assertEqual(argv[argv.index("--reader") + 1], "anthropic")

    def test_save_settings_persists_shared_options_collation_policy_and_both_inputs(self):
        app = self.app(); app.select_tab(2); app.select_tab(3)
        for name, value in {"v_model2": "claude-second", "v_up": "2", "v_second": True,
                            "v_docx": True, "v_join": True, "v_hl": True, "v_pb": False,
                            "v_ctx": True, "v_workers": "4", "v_ckpt": "3", "v_cache": False,
                            "v_think2": False, "v_blind": True, "v_chunk": "6", "v_maxcalls": "15",
                            "v_suspects": True}.items():
            setattr(app, name, Var(value))
        app.check_list_mode = lambda: "extra"; app.memo_mode = lambda: "all"
        app.collate_policy = lambda: "memo"; app.collate_memo_mode = lambda: "inline"
        app.media_res_key = lambda: "default"; app.page_think_key = lambda: "auto"; app.page_mres_key = lambda: "auto"
        self.gui.App.save_settings(app, quiet=True)
        saved = self.saved[-1]
        self.assertEqual((saved["collate_policy"], saved["collate_memo"], saved["memo_mode"]), ("memo", "inline", "all"))
        self.assertEqual((saved["reader"], saved["model"], saved["second_model"], saved["docx"], saved["max_calls"]),
                         ("anthropic", "claude-test", "claude-second", True, 15))
        self.assertEqual(saved["provider_settings"]["anthropic"]["api_key"], "synthetic-test-key")
        self.assertEqual((saved["ocr_inputs"]["0"]["pdf"], saved["ocr_inputs"]["2"]["pdf"], saved["input_tab"]),
                         ("book.pdf", "image.png", 2))
        self.assertEqual(saved["ocr_inputs"]["0"]["pages"], "10-20")
        self.assertEqual(saved["ocr_inputs"]["2"]["pages"], "2")

    def test_settings_summary_omits_key_and_identifies_active_input_force_scope(self):
        app = self.app()
        app.v_docx = Var(True); app.v_provider = Var("Claude"); app.v_mode = Var("가로쓰기 한문")
        app.v_settings_summary = Var(""); app.v_force_scope = Var("")
        for tab, scope in ((0, "일반 OCR"), (2, "이미지 OCR")):
            app.select_tab(tab)
            self.gui.App._sync_settings_summary(app)
            self.assertEqual(app.v_settings_summary.get(), "Claude · claude-test · 가로쓰기 한문 · HWPX + DOCX")
            self.assertNotIn(app.v_key.get(), app.v_settings_summary.get())
            self.assertIn(scope, app.v_force_scope.get())

    def test_remember_inputs_serializes_both_tabs_and_current_force(self):
        app = self.app(); app.select_tab(2)
        app.v_force.set(True); app._remember_inputs()
        self.assertEqual(app.cfg["ocr_inputs"], {
            "0": {"pdf": "book.pdf", "pages": "10-20", "print_offset": "29", "force": True},
            "2": {"pdf": "image.png", "pages": "2", "print_offset": "-1", "force": True}})
        self.assertEqual(app.cfg["input_tab"], 2)
        restored, initial = self.gui.ocr_input_config(app.cfg)
        self.assertEqual((restored[0]["pdf"], restored[2]["pdf"], initial), ("book.pdf", "image.png", 2))
        self.assertEqual((app.cfg["reader"], app.cfg["model"], app.cfg["api_key"]),
                         ("anthropic", "claude-test", "synthetic-test-key"))

    def test_paste_switches_to_image_tab_before_resetting_and_preserves_pdf_work(self):
        app = self.app(); original = self.input_values(app, 0)
        captured = Image.new("RGB", (40, 50), "white")
        with mock.patch.object(ImageGrab, "grabclipboard", return_value=captured), \
             mock.patch("yeone_ocr.screenshots.save_screenshot", return_value=Path("saved-capture.png")):
            self.assertEqual(self.gui.App.paste_screenshot(app), "break")
        self.assertEqual((app._selected_tab, app._active_input), (2, 2))
        self.assertEqual(self.input_values(app, 0), original)
        self.assertEqual(self.input_values(app, 2), {"pdf": "saved-capture.png", "pages": "", "print_offset": ""})
        self.assertEqual(app._input_force, {0: True, 2: False})
        self.assertEqual(self.saved[-1]["ocr_inputs"]["0"], {**original, "force": True})
        app.select_tab(0)
        self.assertEqual((app.pdf(), app.v_pages.get(), app.v_off.get(), app.v_force.get()), ("book.pdf", "10-20", "29", True))
        self.error.assert_not_called()

    def test_bookmark_pdf_fallback_uses_general_pdf_even_while_image_tab_is_active(self):
        app = self.app(); app.select_tab(2)
        self.assertEqual(app.pdf2(), "book.pdf")
        app.v_pdf2.set("explicit-collation.pdf")
        self.assertEqual(app.pdf2(), "explicit-collation.pdf")
        app.v_pdf2.set(""); app._input_vars[0]["pdf"].set("")
        self.assertEqual(app.pdf2(), "")

    def test_image_picker_single_selects_image_tab_and_preserves_pdf_settings(self):
        app = self.app(); original = self.input_values(app, 0)
        with mock.patch.object(self.gui.filedialog, "askopenfilenames", return_value=("picked.PNG",)) as pick:
            self.gui.App.choose_images(app)
        self.assertEqual((app._active_input, app.pdf(), app.v_pages.get(), app.v_off.get(), app.v_force.get()), (2, "picked.PNG", "", "", False))
        self.assertEqual(self.input_values(app, 0), original)
        app._use_pdf.assert_called_once_with("picked.PNG")
        self.assertNotIn("*.pdf", str(pick.call_args.kwargs["filetypes"]))

    def test_image_picker_multiple_defers_input_change_until_bundle_success(self):
        app = self.app(); previous = self.input_values(app, 2)
        with mock.patch.object(self.gui.filedialog, "askopenfilenames", return_value=("second.png", "first.jpg")):
            self.gui.App.choose_images(app)
        self.assertEqual(app._active_input, 2)
        self.assertEqual(self.input_values(app, 2), previous)
        app._bundle_images.assert_called_once_with(["second.png", "first.jpg"], reset_pages=True)
        app._use_pdf.assert_not_called()

    def test_image_picker_cancel_invalid_or_busy_does_not_change_current_input(self):
        for result in ((), ("wrong.pdf",), None):
            with self.subTest(result=result):
                app = self.app()
                with mock.patch.object(self.gui.filedialog, "askopenfilenames", return_value=result):
                    self.gui.App.choose_images(app)
                self.assertEqual((app._active_input, app.pdf(), app.v_pages.get()), (0, "book.pdf", "10-20"))
                app._use_pdf.assert_not_called(); app._bundle_images.assert_not_called()
        app = self.app(); app.running = True
        with mock.patch.object(self.gui.filedialog, "askopenfilenames") as pick:
            self.gui.App.choose_images(app)
        pick.assert_not_called()

    def test_pdf_picker_uses_general_tab_without_overwriting_image_input(self):
        app = self.app(); app.select_tab(2)
        previous = self.input_values(app, 2)
        with mock.patch.object(self.gui.filedialog, "askopenfilename", return_value="new-book.pdf") as pick:
            self.gui.App.choose_pdf(app)
        self.assertEqual((app._active_input, app.pdf()), (0, "new-book.pdf"))
        self.assertEqual(self.input_values(app, 2), previous)
        self.assertEqual(pick.call_args.kwargs["filetypes"], [("PDF", "*.pdf")])


if __name__ == "__main__":
    unittest.main()
