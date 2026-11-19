"""설정 탭(서재 모양 — 디자인 명세 §6.3·§5.9·§6.8): 섹션 두 열 ↔ 한 열, 모든 설정이 같은 변수에 묶인 채 남아 있음, 영수증·꼬리표는 돈 드는 줄에만,
자동 교정의 흐린 줄, '이 책' 고급(인쇄 쪽수 보정)은 값이 있으면 펼침, 쪽 범위·인쇄 쪽수 칸은 지금 입력 탭에 다시 묶임, 바닥 알림 줄, 중문부호 규칙 창의
공백 보이기와 Enter. 진짜 창이 필요한 시험은 화면(xvfb)이 없으면 건너뛴다."""
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ops import _real_tk, gui_module


class NoticeText(unittest.TestCase):
    def test_short_paths_keeps_last_two_parts(self):
        with gui_module() as gui:
            sp = gui.short_paths
            self.assertEqual(sp("설정 저장됨 → C:\\Users\\me\\.yeone_ocr\\config.json · 「책.pdf」의 책별 설정 → D:\\本草\\책_YeonE_OCR\\book_settings.json"),
                             "설정 저장됨 → …\\.yeone_ocr\\config.json · 「책.pdf」의 책별 설정 → …\\책_YeonE_OCR\\book_settings.json")
            self.assertEqual(sp("저장 → /home/me/a b/.yeone_ocr/config.json"), "저장 → …/.yeone_ocr/config.json")
            for keep in ("판독 12/45쪽 · 〔a/b〕 남음", "out/x.hwpx", ""):                   # 쪽 수·표시·짧은 경로는 그대로
                self.assertEqual(sp(keep), keep)


class RulesText(unittest.TestCase):
    def test_spaces_and_empty_values_are_visible(self):
        try:
            from yeone_ocr.gui_corrections import shown
        except ImportError:
            self.skipTest("tkinter 없음")
        self.assertEqual((shown(", "), shown(" : "), shown(""), shown("。")), (",␣", "␣:␣", "(지움)", "。"))


class SettingsTabRealTk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _real_tk().destroy()
        cls.stack = contextlib.ExitStack()
        cls.gui = gui = cls.stack.enter_context(gui_module())
        cls.td = td = Path(cls.stack.enter_context(tempfile.TemporaryDirectory()))
        cfg = td / "cfg" / "config.json"; cfg.parent.mkdir()
        cfg.write_text(json.dumps({"auto_update": False, "reader": "gemini", "model": "gemini-2.5-flash", "fx_auto": 1400, "fx_auto_date": "2099-01-01",
                                   "input_tab": 0, "layout": "flow"}), encoding="utf-8")
        for target, name, value in ((gui, "CONFIG_PATH", cfg), (gui, "missing_packages", lambda: []), (gui.App, "_fetch_fx", lambda self: None),
                                    (gui, "write_config", lambda app: None)):
            cls.stack.enter_context(mock.patch.object(target, name, value))
        import pymupdf
        cls.pdf = td / "개보본초.ocr.pdf"
        with pymupdf.open() as doc:
            for _ in range(3):
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
        for _ in range(3):
            self.app.update()
            if getattr(self.app, "_cards_job", None):
                self.app._sync_cards()

    def walk(self, w):
        for c in w.winfo_children():
            yield c
            yield from self.walk(c)

    def test_template_selection_reaches_output_command_from_all_book_controls(self):
        from yeone_ocr.gui_rule_generation import BookRuleDialog
        from tests._hwpx_fixtures import make_template
        app = self.app
        previous = app.v_template.get(), app.v_layout.get()
        template = self.td / 'selected-book-template.hwpx'
        make_template(template)
        dialog = BookRuleDialog(app, app._input_vars[0]['pdf'].get(), self.td)
        try:
            for select in (app.pick_template, dialog.pick_template,
                           lambda: dialog.template_entry.insert('end', str(template))):
                app.v_template.set('')
                app.v_layout.set(self.gui.LAYOUT_LABELS['page'])
                with mock.patch.object(self.gui.filedialog, 'askopenfilename', return_value=str(template)):
                    select()
                argv = app._build_opts()
                self.assertEqual(argv[argv.index('--template') + 1], str(template))
                self.assertEqual(argv[argv.index('--layout') + 1], 'flow')
            app.v_layout.set(self.gui.LAYOUT_LABELS['page'])
            self.assertNotIn('--template', app._build_opts())
        finally:
            dialog.close()
            app.v_template.set(previous[0]); app.v_layout.set(previous[1])

    def test_book_style_editor_saves_template_mapping_and_reopens(self):
        from yeone_ocr.gui_book_styles import BookStylesDialog
        from test_template_headings import template
        from yeone_ocr.presets import DATA, load_preset
        app=self.app;old=(app._preset_ref,app.v_template.get(),app.v_layout.get());dialog=again=None
        path=self.td/'style-template.hwpx';template(path);target=self.td/'my-book-styles.json'
        try:
            app._preset_ref=str(DATA/'book_rule_yaolun.json');app.v_template.set(str(path))
            dialog=BookStylesDialog(app);dialog.withdraw()
            self.assertIn('book_styles',app._set_rows)
            self.assertEqual(dialog.variables['subsection'].get(),'제목-본초명(소)')
            self.assertIn('14pt',dialog.details['subsection'].get())
            # Different valid choices persist; recognition data is not opened.
            dialog.variables['body'].set('제목-본초명(소)')
            with mock.patch('yeone_ocr.gui_book_styles.filedialog.asksaveasfilename',return_value=str(target)):
                dialog.save()
            rule=load_preset(target)
            self.assertEqual(rule.template,str(path.resolve()))
            self.assertEqual(rule.para_styles['body'],('제목-본초명(소)',))
            self.assertEqual(app.preset_ref(),str(target.resolve()))
            again=BookStylesDialog(app);again.withdraw()
            self.assertEqual(again.variables['body'].get(),'제목-본초명(소)')
        finally:
            for win in (again,dialog):
                if win is not None and win.winfo_exists():win.destroy()
            app._preset_ref=old[0];app.v_template.set(old[1]);app.v_layout.set(old[2])
            app.v_preset.set(self.gui.preset_label(old[0]))

    def test_hanja_editor_add_edit_delete_save_reload_and_command(self):
        from yeone_ocr.gui_hanja_rules import HanjaRulesDialog
        from yeone_ocr.hanja_rules import MODES, load_hanja_rules
        app = self.app; previous = app.v_hanja_rules.get(); dialog = again = None
        path = self.td / 'editable-hanja.json'
        try:
            app.v_hanja_rules.set(''); dialog = HanjaRulesDialog(app); dialog.withdraw()
            self.assertIn('hanja', app._set_rows)
            index = next(i for i,r in enumerate(dialog.rules) if r['find']=='產')
            dialog.tree.selection_set(str(index)); dialog.remove()
            self.assertFalse(any(r['find']=='產' for r in dialog.rules))
            dialog.find.set('測試字'); dialog.replace.set('試驗字'); dialog.mode.set(MODES['auto']); dialog.add()
            dialog.replace.set('試用字'); dialog.update_rule()
            self.assertEqual(dialog.rules[-1]['replace'],'試用字')
            with mock.patch('yeone_ocr.gui_hanja_rules.filedialog.asksaveasfilename',return_value=str(path)), \
                    mock.patch.object(self.gui,'write_config',side_effect=lambda a:self.gui.save_config(a.cfg)):
                dialog.save()
            self.assertEqual(app.v_hanja_rules.get(),str(path.resolve()))
            saved = json.loads(self.gui.CONFIG_PATH.read_text(encoding='utf-8'))
            self.assertEqual(saved['hanja_rules'],str(path.resolve()))
            again = HanjaRulesDialog(app); again.withdraw()
            self.assertFalse(any(r['find']=='產' for r in again.rules))
            self.assertEqual(again.rules[-1]['replace'],'試用字')
            self.assertIn('--hanja-rules', self.gui.App._normalization_opts(app))
        finally:
            for win in (again,dialog):
                if win is not None and win.winfo_exists():win.destroy()
            app.v_hanja_rules.set(previous)

    def test_gemini_quota_dialog_selects_available_model_and_blocks_exhausted_model(self):
        import time
        from yeone_ocr import gemini_quota
        from test_gemini_quota import REPORT
        quotas = gemini_quota.parse_quotas(REPORT)
        rows = [dict(id=slug, title=slug, quotas=gemini_quota.model_quotas(slug, '', quotas), blocked=blocked)
                for slug, blocked in [('gemini-4-argon-high', True), ('gemini-4-hydrogen-high', False)]]
        app = self.app
        old = dict(app.cfg)
        win = None
        try:
            with mock.patch.object(gemini_quota, 'inventory', return_value=dict(models=rows, current=rows[0]['id'])), \
                    mock.patch.object(self.gui, 'write_config') as save, mock.patch.object(self.gui.messagebox, 'showinfo') as info:
                app.show_gemini_models()
                win = next(w for w in app.winfo_children() if w.winfo_class() == 'Toplevel' and w.title() == 'Antigravity 모델·잔여 한도')
                widgets = list(self.walk(win))
                self.assertTrue(any('다른 Google 계정' in w.cget('text') and '프로필 메뉴' in w.cget('text')
                                    for w in widgets if w.winfo_class() == 'TLabel'))
                open_app = next(w for w in widgets if w.winfo_class() == 'TButton' and w.cget('text') == 'Gemini 앱 열기')
                with mock.patch.object(self.gui.webbrowser, 'open') as browser:
                    open_app.invoke()
                browser.assert_called_once_with('https://gemini.google.com/app')
                self.assertEqual(app.cfg, old)
                tree = next(w for w in self.walk(win) if w.winfo_class() == 'Treeview')
                deadline = time.monotonic() + 5
                while not tree.get_children() and time.monotonic() < deadline:
                    app.update(); time.sleep(0.01)
                self.assertEqual(len(tree.get_children()), 2)
                apply = next(w for w in self.walk(win) if w.winfo_class() == 'TButton' and w.cget('text') == '선택 적용')
                tree.selection_set(rows[0]['id']); apply.invoke()
                info.assert_called_once(); save.assert_not_called()
                tree.selection_set(rows[1]['id']); apply.invoke()
                save.assert_called_once()
                self.assertEqual(app.cfg['gemini_subscription_model'], rows[1]['id'])
                options = app._strategy_opts()
                self.assertIn('--no-local-ai-api-fallback', options)
                self.assertEqual(options[options.index('--local-gemini-model') + 1], rows[1]['id'])
                self.assertEqual(options[options.index('--local-ai-auth') + 1], 'subscription')
        finally:
            if win is not None:
                win.destroy()
            app.cfg.clear(); app.cfg.update(old)

    def test_1_sections_two_columns_and_one_column(self):
        app = self.app; gui = self.gui; S = app._set_sections
        def column(name):
            col = app._set_cols[name]
            return [k for k in ("read", "ai", "book", "docs", "auto", "collate") if S[k]["card"].grid_info().get("in") == col]
        simple = app.v_simple.get()
        app.set_simple_view(False, save=False)                                        # 두 열·한 열은 자세히 보기의 배치(간단히 보기는 갈래 하나씩 — test_1b)
        try:
            gui.App._layout_settings(app, int(1300 * app.scale))
            self.assertEqual((column("left"), column("right")), (["read", "ai", "book"], ["docs", "auto", "collate"]))   # 읽는 쪽 | 만드는 쪽
            gui.App._layout_settings(app, int(1000 * app.scale))                      # 논리 1100 미만: 한 열(작업 차례)
            self.assertEqual(column("left"), ["read", "ai", "book", "docs", "auto", "collate"])
            rows = sorted(column("left"), key=lambda k: int(S[k]["card"].grid_info()["row"]))
            self.assertEqual(rows, ["read", "ai", "book", "docs", "auto", "collate"])
            self.assertEqual(app._set_cols["right"].winfo_manager(), "")
        finally:
            gui.App._layout_settings(app, int(1300 * app.scale))
            app.set_simple_view(simple, save=False)

    def test_1b_simple_view_shows_one_section_at_a_time(self):
        """간단히 보기(기본): 왼쪽 갈래 줄에서 고른 섹션 하나만 보이고, 자세히 보기로 바꾸면 여섯 섹션이 모두 돌아온다. 값(변수)은 그대로다."""
        app = self.app; gui = self.gui; S = app._set_sections
        shown = lambda: sorted(k for k, sec in S.items() if sec["card"].winfo_manager() == "grid")
        simple = app.v_simple.get()
        try:
            app.set_simple_view(True, save=False); app.select_tab(3); self.settle()
            self.assertTrue(gui.DEFAULTS["simple_view"])                               # 새 설치의 기본은 간단히 보기
            self.assertEqual(app._set_rail.winfo_manager(), "grid")
            self.assertEqual([text for _key, text in gui.App.SET_RAIL], ["인식", "AI 판독기", "이 책", "문서 만들기", "자동 교정", "책갈피 교정"])
            self.assertEqual(sorted(app._rail_buttons), sorted(S))                     # 갈래마다 섹션이 하나씩
            for key, _text in gui.App.SET_RAIL:
                app._rail_buttons[key].invoke(); self.settle()
                self.assertEqual(shown(), [key])
                self.assertEqual(S[key]["card"].grid_info().get("in"), app._set_cols["left"])
            self.assertTrue(app.v_collate_set.get())                                   # 책갈피 교정 갈래는 접힌 몸통을 펼쳐 둔다
            self.assertEqual(S["collate"]["body"].winfo_manager(), "pack")
            model = app.v_model.get()
            app.set_simple_view(False, save=False); self.settle()
            self.assertEqual(app._set_rail.winfo_manager(), "")
            self.assertEqual(shown(), sorted(S)); self.assertEqual(app.v_model.get(), model)
            self.assertFalse(app.v_simple.get()); self.assertTrue(app.v_detail.get())
            gui.App._goto_print_offset(app); self.settle()                             # 자세히 보기에서는 갈래를 바꿔도 섹션이 모두 남는다
            self.assertEqual(shown(), sorted(S))
            app.set_simple_view(True, save=False); self.settle()
            self.assertEqual(shown(), ["book"])                                        # 인쇄 쪽수 칸으로 갈 때 고른 '이 책' 갈래
        finally:
            app.v_set_rail.set("read")
            if app.v_collate_set.get():
                app.toggle_collate_settings()
            if app.v_book_adv.get():
                app.toggle_book_adv()
            app.set_simple_view(simple, save=False); app.select_tab(0); self.settle()

    def test_2_every_setting_stays_wired_to_its_variable(self):
        """자리만 옮겼다: 설정 탭(접힌 세부 설정·고급·책갈피 섹션 포함)과 바닥 줄에 지난 판의 설정 변수가 모두 칸으로 남아 있다."""
        app = self.app; app.select_tab(0); self.settle()
        bound = set()
        for w in list(self.walk(app._common_frame)) + list(self.walk(app._footer)):
            for opt in ("variable", "textvariable"):
                try:
                    v = str(w.cget(opt))
                except Exception:
                    continue
                if v:
                    bound.add(v)
        names = ("v_provider", "v_model", "v_key", "v_force", "v_layout", "v_preset", "v_bold", "v_hl", "v_ctx", "v_pb", "v_join", "v_docx", "v_tlpdf",
                 "v_memo", "v_chk", "v_traditional", "v_yun", "v_punctuation", "v_ctxmodel", "v_suspects", "v_blind", "v_skipany", "v_model2", "v_workers",
                 "v_up", "v_ckpt", "v_chunk", "v_mres", "v_second", "v_cache", "v_think2", "v_think1", "v_mres1", "v_maxcalls", "v_policy", "v_cmemo",
                 "v_cforce", "v_auto_update", "v_mode", "v_pages", "v_off", "v_local_ai_subscription", "v_local_ai_plan",
                 "v_local_ai_review", "v_local_ai_max_calls", "v_local_resume", "v_local_ai_parallel", "v_local_ai_split", "v_local_ai_pages")
        missing = [n for n in names if str(getattr(app, n)) not in bound]
        self.assertEqual(missing, [])
        texts = {str(w.cget("text")).strip() for w in self.walk(app._common_frame) if w.winfo_class() == "TButton"}
        for t in ("키 확인·모델 고르기", "보기", "전체", "고르기…", "비우기", "규칙 추가·수정…", "파일에서 고르기…", "기본 규칙", "세부 설정 ▾", "고급 ▸", "책갈피 OCR 교정 ▸"):
            self.assertIn(t, texts)
        foot = [str(w.cget("text")).strip() for w in self.walk(app._footer) if w.winfo_class() in ("TButton", "TCheckbutton")]
        self.assertEqual(foot, ["설정 저장", "OCR로 돌아가기", "업데이트 확인", "시작할 때 업데이트 확인"])   # 바닥 줄 — 중지(b_stop3)는 없앰
        self.assertFalse(hasattr(app, "b_stop3"))

    def test_3_receipts_and_tags_only_on_rows_that_change_calls(self):
        R = self.app._set_rows
        self.assertEqual({k for k, r in R.items() if r["receipt"] is not None}, {"force", "ctxmodel", "suspects", "blind", "skipany", "cforce"})
        self.assertEqual({k for k, r in R.items() if r["tag"] is not None}, {"ctxmodel"})
        self.assertEqual(str(self.app._model_stage_tag.cget("text")), "[아니요]면 뺌")               # AI 판독기 안 '판독 뒤 모델 단계' 머리의 꼬리표
        for k in ("rules", "punc"):                                                                # 규칙 셋은 모델 호출 없음
            self.assertIsNone(R[k]["receipt"])

    def test_4_auto_correction_greys_out_unused_rows_and_keeps_values(self):
        app = self.app; gui = self.gui; R = app._set_rows
        ctx = R["ctxmodel"]["ctrl"]
        try:
            app.v_ctxmodel.set(True)
            app.v_traditional.set(False); app.v_yun.set(False); self.settle()
            self.assertTrue(ctx.instate(["disabled"])); self.assertTrue(R["ctxmodel"]["muted"])
            self.assertTrue(app.v_ctxmodel.get())                                                 # 값은 그대로(다시 켜면 돌아옴)
            self.assertIn("--no-context-model", gui.App._build_opts(app))                         # 흐린 줄의 값은 실행에 쓰이지 않는다(_context_model_enabled)
            self.assertEqual(str(R["ctxmodel"]["receipt"].cget("image")), str(app.icons["receipt_dis"]))
            app.v_yun.set(True); self.settle()
            self.assertFalse(ctx.instate(["disabled"])); self.assertFalse(R["ctxmodel"]["muted"])
            app.v_punctuation.set(False); self.settle()
            self.assertEqual({str(b.cget("state")) for b in app._punc_btns}, {"disabled"})
            app._set_running(True, "read"); app._set_running(False); self.settle()                # 실행이 끝나 단추를 다시 켜도 꺼진 채
            self.assertEqual({str(b.cget("state")) for b in app._punc_btns}, {"disabled"})
            app.v_punctuation.set(True); self.settle()
            self.assertEqual({str(b.cget("state")) for b in app._punc_btns}, {"normal"})
            self.assertEqual(app.v_punctuation_label.get(), "지금: 내장 기본 규칙 · 위에서 아래 순서로 바꿈")
            self.assertNotIn("--no-context-model", gui.App._build_opts(app))
        finally:
            app.v_traditional.set(True); app.v_yun.set(True); app.v_punctuation.set(True); self.settle()

    def test_5_print_offset_advanced_opens_when_it_has_a_value(self):
        app = self.app; app.select_tab(0); self.settle()
        try:
            app.v_book_adv.set(False); app._apply_book_adv(); app.v_off.set("")
            app.select_tab(3); self.settle()
            self.assertFalse(app.v_book_adv.get()); self.assertEqual(app._book_adv.winfo_manager(), "")    # 평소 접힘
            app.select_tab(0); app.v_off.set("29"); app.select_tab(3); self.settle()
            self.assertTrue(app.v_book_adv.get()); self.assertEqual(app._book_adv.winfo_manager(), "pack")  # 값이 있으면 펼친 채(숨은 보정이 없게)
            self.assertEqual(app.b_book_adv.cget("text"), "고급 ▴")
            app.toggle_book_adv(); self.settle(); self.assertEqual(app._book_adv.winfo_manager(), "")
            app.select_tab(0); app._goto_print_offset(); self.settle()                             # 판의 '[설정] → 이 책 → 고급에서 고침'
            self.assertEqual(app._selected_tab, 3); self.assertTrue(app.v_book_adv.get())
        finally:
            app.v_off.set(""); app.select_tab(0); self.settle()

    def test_6_book_entries_follow_the_active_input_tab(self):
        app = self.app
        for tab in (2, 0):
            app.select_tab(tab); app.select_tab(3)
            self.assertEqual(str(app.e_pages_set.cget("textvariable")), str(app._input_vars[tab]["pages"]))
            self.assertEqual(str(app.e_print_offset.cget("textvariable")), str(app._input_vars[tab]["print_offset"]))
            self.assertEqual(str(app.cb_mode.cget("textvariable")), str(app._mode_vars[2 if tab == 2 else 0]))
        self.assertIn(app.e_pages_set, app._lock_entries)
        self.assertNotIn(app.e_print_offset, app._lock_entries)
        app.select_tab(0)

    def test_7_notice_line_shows_last_news_only_on_settings_tab(self):
        app = self.app; gui = self.gui
        app.select_tab(3); self.settle()
        self.assertEqual(app.v_notice.get(), gui.SETTINGS_NOTE)
        app.v_status.set("준비됨"); self.assertEqual(app.v_notice.get(), gui.SETTINGS_NOTE)       # 알릴 것 없음
        app.v_status.set("설정 저장됨 → /home/me/x/.yeone_ocr/config.json")
        self.assertEqual(app.v_notice.get(), "설정 저장됨 → …/.yeone_ocr/config.json")
        self.assertEqual(app._notice_tip.text, "설정 저장됨 → /home/me/x/.yeone_ocr/config.json")
        app._append("키 확인: 쓸 수 있음\n")
        self.assertEqual(app.v_notice.get(), "키 확인: 쓸 수 있음")
        app.select_tab(0); self.assertEqual(app.v_notice.get(), gui.SETTINGS_NOTE)               # 탭을 옮기면 평소 글
        app._append("작업 탭의 기록\n"); self.assertEqual(app.v_notice.get(), gui.SETTINGS_NOTE)
        app.v_status.set("준비됨")

    def test_8_layout_greys_out_rows_it_does_not_use(self):
        app = self.app; gui = self.gui; R = app._set_rows
        try:
            app.v_template.set(str(self.td / "본초학 양식.hwpx")); app._preset_ref = "kaibao"
            app.v_layout.set(gui.LAYOUT_LABELS["page"]); self.settle()
            self.assertFalse(R["pb"]["muted"]); self.assertTrue(R["template"]["muted"]); self.assertTrue(R["preset"]["muted"]); self.assertTrue(R["bold"]["muted"])
            self.assertTrue(R["preset"]["desc"].cget("text").endswith(" · 원본 쪽대로라 쓰지 않음"))
            self.assertIn("지금은 재지 않음 — 원본 쪽대로", R["bold"]["desc"].cget("text"))
            self.assertTrue(app.te_template.instate(["disabled"]))
            app.v_layout.set(gui.LAYOUT_LABELS["flow"]); self.settle()
            self.assertTrue(R["pb"]["muted"]); self.assertFalse(R["template"]["muted"]); self.assertFalse(R["bold"]["muted"])
            self.assertEqual(R["pb"]["desc"].cget("text"), gui.PB_DESC[True])
            self.assertEqual(str(app.te_template.cget("text")).strip(), "본초학 양식.hwpx")
            self.assertEqual(app._tpl_tip.text, str(self.td / "본초학 양식.hwpx"))                   # 전체 경로는 풍선
            app.v_template.set(""); self.settle()
            self.assertEqual(str(app.te_template.cget("text")), "없음 — 기본 모양")
            self.assertIn("지금은 재지 않음 — 양식이 없음", R["bold"]["desc"].cget("text"))
        finally:
            app.v_template.set(""); app.v_layout.set(gui.LAYOUT_LABELS["flow"]); self.settle()

    def test_9_book_section_names_the_book_and_its_save_state(self):
        app = self.app; bk = app._set_book
        app.select_tab(0); app.v_pdf.set(""); app.select_tab(3); self.settle()
        self.assertEqual(bk["title"].cget("text"), "이 책 — 원본 없음")
        self.assertEqual(bk["save_state"].cget("text"), "원본이 없어 아직 저장할 곳이 없습니다")
        app.select_tab(0); app._use_pdf(str(self.pdf)); app.select_tab(3); self.settle()
        self.assertEqual(bk["title"].cget("text"), "이 책 — 개보본초.ocr.pdf")
        self.assertEqual(bk["sub"].cget("text"), "아래 7가지(고급 포함)는 이 책에만 적용됩니다 · 일반 OCR 탭에서 고른 책")
        self.assertEqual(bk.get("seal_chars"), "개보")
        self.assertEqual(bk["save_state"].cget("text"), "결과 폴더가 생기면(첫 실행) 저장")
        self.assertIn("「개보본초.ocr.pdf」(일반 OCR 탭)의 책별 설정입니다", bk["title_tip"].text)
        self.assertTrue(bk["title_tip"].text.startswith("쪽 범위·인쇄 쪽수·읽기 방향·문서 짜임·양식·짜임 규칙·획 굵기는"))
        app.select_tab(0)

    def test_10_collapsed_sections_open_and_close(self):
        app = self.app; app.select_tab(3); self.settle()
        s = app._set_sections["collate"]
        self.assertEqual(s["body"].winfo_manager(), "")
        app.toggle_collate_settings(); self.settle()
        self.assertEqual(s["body"].winfo_manager(), "pack"); self.assertEqual(app.b_collate_set.cget("text"), "책갈피 OCR 교정 ▴")
        app.toggle_collate_settings(); self.settle(); self.assertEqual(s["body"].winfo_manager(), "")
        details = app.v_details.get()
        app.toggle_details(); self.settle()
        self.assertEqual(app.details.winfo_manager(), "" if details else "pack")
        app.toggle_details(); self.settle()
        self.assertEqual(app.details.winfo_manager(), "pack" if details else "")
        app.select_tab(0)

    def test_12_bare_checkboxes_draw_focus_around_the_box_and_header_keeps_its_front(self):
        """글 없는 체크 상자(설정 줄의 조작)는 초점 점선이 상자를 감싼다 — 켜진 상자(먹 채움)에서도 Tab 초점이 보이게.
        머리띠의 패키지 상태는 자리가 모자라면 ' → ' 앞('설치 구성요소 오류 2개')만, 그래도 넘치면 앞을 줄여 '…오류 2개' — 전체 글은 풍선 도움말로."""
        app = self.app; R = app._set_rows
        bare = [R[k]["ctrl"] for k in ("bold", "cforce", "suspects", "blind")]
        self.assertEqual({str(c.cget("style")) for c in bare}, {"Bare.TCheckbutton"})
        def find(layout, name):
            for el, opts in layout:
                if el == name:
                    return opts
                got = find(opts.get("children", []), name)
                if got is not None:
                    return got
            return None
        focus = find(self.gui.ttk.Style(app).layout("Bare.TCheckbutton"), "Checkbutton.focus")
        self.assertEqual([el for el, _ in focus["children"]], ["BareCheck.indicator"])
        old = app.v_pkg.get(); lab = app._pkg_label; f = app._font("CAP")
        full = "설치 구성요소 오류 2개 → 설치파일로 다시 설치"
        try:
            for room, shown in ((f.measure(full) + 4, full), (f.measure("설치 구성요소 오류 2개") + 2, "설치 구성요소 오류 2개")):
                with mock.patch.object(lab, "winfo_width", return_value=room):
                    app.v_pkg.set(full)
                    self.assertEqual(lab.cget("text"), shown)
                    self.assertEqual(app._pkg_tip.text, full)                                     # 풍선 도움말은 늘 전체 글
            with mock.patch.object(lab, "winfo_width", return_value=f.measure("…오류 2개") + 1):
                app._fit_pkg()
                self.assertEqual(lab.cget("text"), "…오류 2개")                                   # 상태와 수가 남는다(안내 꼬리 '…다시 설치'가 아니라)
            for ok in self.gui.PKG_OK:                                                             # 정상 상태는 명령 띠에 적지 않는다(오류·점검 중만 보인다)
                app.v_pkg.set(ok)
                self.assertEqual((lab.cget("text"), app._pkg_tip.text), ("", ""))
        finally:
            app.v_pkg.set(old)

    def test_13_confusion_ledger_dialog_lists_pairs_and_manages_ledger_and_samples(self):
        app = self.app
        from yeone_ocr import confusion_memory as memory, glyph_samples
        from yeone_ocr.gui_confusions import ConfusionLedgerDialog
        memory.store_path().write_text(json.dumps({"format": memory.FORMAT, "version": memory.VERSION, "enabled": True,
            "suppressed": [], "documents": {"d" * 32: {"at": "2026-11-15T00:00:00+0000", "source": "a.pdf", "pages": 1,
                "engines": {"ppocrv6": {"versions": [], "seen": {"己": 20}, "coverage_basis": memory.COVERAGE_BASIS,
                                        "pairs": [["己", "已", {"human": 3}, ["甲「己」丙"]]]}}}}}), encoding="utf-8")
        dlg = ConfusionLedgerDialog(app)
        try:
            dlg.update()
            self.assertEqual([str(v) for v in dlg.tree.item("0")["values"]],
                             ["ppocrv6", "己", "→", "已", "3", "3·0·0", "15.0%", "예"])
            self.assertIn("의심 글자 1개", dlg.intro.get("1.0", "end"))
            self.assertIn("고친 줄 0개", dlg.v_samples_note.get())
            dlg.tree.selection_set("0"); dlg.update()
            self.assertIn("甲「己」丙", dlg.v_pick.get())
            dlg.exclude(); dlg.update()                                                         # 잘못 쌓인 쌍 빼기
            self.assertEqual(dlg.tree.get_children(), ())
            self.assertFalse(memory.LearnedConfusions.load().active())
            dlg.restore(); dlg.update()
            self.assertEqual(len(dlg.tree.get_children()), 1)
            dlg.v_enabled.set(False); dlg.toggle()
            self.assertFalse(memory.LearnedConfusions.load().enabled)
            dlg.v_samples.set(False); dlg.toggle_samples()
            self.assertFalse(glyph_samples.is_enabled())
            with mock.patch("yeone_ocr.gui_confusions.messagebox.askyesno", return_value=True):
                dlg.clear()
            self.assertEqual(memory.LearnedConfusions.load().rows, ())
            out = self.td / "samples-export"; out.mkdir()
            with mock.patch("yeone_ocr.gui_confusions.filedialog.askdirectory", return_value=str(out)), \
                    mock.patch("yeone_ocr.gui_confusions.messagebox.showinfo") as info:
                dlg.export_samples()
            info.assert_called_once()
            self.assertTrue(any(out.glob("연어OCR_글자조각_*/rec_gt.txt")))
            self.assertEqual(str(dlg.b_close.cget("style")), "DlgInkDef.TButton")
            dlg.b_close.invoke()
            self.assertFalse(bool(dlg.winfo_exists()))
        finally:
            try:
                dlg.destroy()
            except Exception:
                pass

    def test_11_rules_dialog_enter_never_saves_from_the_fields(self):
        app = self.app
        from yeone_ocr.gui_corrections import PunctuationRulesDialog
        app.v_punctuation_rules.set("")
        dlg = PunctuationRulesDialog(app)
        try:
            dlg.update()
            first = dlg.tree.item("0")["values"]
            self.assertEqual([str(v) for v in first], ["1", "，", "→", ",␣"])
            self.assertIn("지금: 내장 기본 규칙 15개", dlg.intro.get("1.0", "end"))
            dlg.tree.selection_set("2"); dlg.update()
            self.assertEqual(dlg.v_pick.get(), "고른 3번 줄 · 빈 바꿀 부호는 삭제 · 공백도 그대로")
            with mock.patch("yeone_ocr.gui_corrections.filedialog.asksaveasfilename", return_value="") as ask:
                dlg.entries[0].focus_force(); dlg.update()
                with mock.patch.object(dlg, "focus_get", return_value=dlg.entries[0]):
                    dlg._on_return()                                                              # 찾을 부호 칸의 Enter: 아무것도 하지 않음
                ask.assert_not_called()
                with mock.patch.object(dlg, "focus_get", return_value=dlg):
                    dlg._on_return()                                                              # 창 자체: 기본 단추(파일로 저장하고 적용…)
                ask.assert_called_once()
            self.assertTrue(dlg.winfo_exists())
            self.assertEqual(str(dlg.b_save.cget("style")), "DlgInkDef.TButton")                  # 먹 채움 + 고리 — 돈이 들지 않으므로 인주가 아님
            with mock.patch.object(dlg, "focus_get", return_value=dlg.b_cancel):
                dlg._on_return()                                                                  # 초점이 [취소]면 취소
            self.assertFalse(bool(dlg.winfo_exists()))
        finally:
            try:
                dlg.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
