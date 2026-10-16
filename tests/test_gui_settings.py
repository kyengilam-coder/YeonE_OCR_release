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

    def test_1_sections_two_columns_and_one_column(self):
        app = self.app; gui = self.gui; S = app._set_sections
        def column(name):
            col = app._set_cols[name]
            return [k for k in ("read", "book", "collate", "docs", "auto", "model") if S[k]["card"].grid_info().get("in") == col]
        try:
            gui.App._layout_settings(app, int(1300 * app.scale))
            self.assertEqual((column("left"), column("right")), (["read", "book", "collate"], ["docs", "auto", "model"]))
            gui.App._layout_settings(app, int(1000 * app.scale))                      # 논리 1100 미만: 한 열(판독 → 이 책 → 책갈피 → 문서 → 자동 교정 → 모델 단계)
            self.assertEqual(column("left"), ["read", "book", "collate", "docs", "auto", "model"])
            rows = sorted(column("left"), key=lambda k: int(S[k]["card"].grid_info()["row"]))
            self.assertEqual(rows, ["read", "book", "collate", "docs", "auto", "model"])
            self.assertEqual(app._set_cols["right"].winfo_manager(), "")
        finally:
            gui.App._layout_settings(app, int(1300 * app.scale))

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
                 "v_cforce", "v_auto_update", "v_mode", "v_pages", "v_off")
        missing = [n for n in names if str(getattr(app, n)) not in bound]
        self.assertEqual(missing, [])
        texts = {str(w.cget("text")).strip() for w in self.walk(app._common_frame) if w.winfo_class() == "TButton"}
        for t in ("키 확인·모델 고르기", "보기", "전체", "고르기…", "비우기", "규칙 추가·수정…", "파일에서 고르기…", "기본 규칙", "세부 설정 ▾", "고급 ▸", "책갈피 OCR 교정 ▸"):
            self.assertIn(t, texts)
        foot = [str(w.cget("text")).strip() for w in self.walk(app._footer) if w.winfo_class() in ("TButton", "TCheckbutton")]
        self.assertEqual(foot, ["설정 저장", "OCR로 돌아가기", "시작할 때 업데이트 확인"])       # 바닥 줄 — 중지(b_stop3)는 없앰
        self.assertFalse(hasattr(app, "b_stop3"))

    def test_3_receipts_and_tags_only_on_rows_that_change_calls(self):
        R = self.app._set_rows
        self.assertEqual({k for k, r in R.items() if r["receipt"] is not None}, {"force", "ctxmodel", "suspects", "blind", "skipany", "cforce"})
        self.assertEqual({k for k, r in R.items() if r["tag"] is not None}, {"ctxmodel"})
        self.assertEqual(str(self.app._set_sections["model"]["tag"].cget("text")), "[아니요]면 뺌")      # 모델 단계 제목의 꼬리표
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
        self.assertIn(app.e_pages_set, app._lock_entries); self.assertIn(app.e_print_offset, app._lock_entries)
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
        finally:
            app.v_pkg.set(old)

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
