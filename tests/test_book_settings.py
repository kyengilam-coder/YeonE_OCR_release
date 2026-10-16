"""책별 설정(결과 폴더의 book_settings.json): 창 둘·책 둘이 서로 덮지 않고, 고르기만으로는 폴더가 생기지 않으며, 원본을 바꿔 보관해도 남는다(화면·API 불필요)."""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ops import gui_module                                    # noqa: E402

from yeone_ocr import __version__, state                           # noqa: E402
from yeone_ocr.state import BOOK_SETTINGS, Work, load_book_settings, save_book_settings   # noqa: E402

BOOK = {"layout": "flow", "template": "D:/양식/본초학.hwpx", "preset": "kaibao", "bold_split": False,
        "pages": "3-9", "print_offset": "29", "mode": "vertical"}


def snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() if p.is_file() else None for p in root.rglob("*")}


class Var:
    def __init__(self, value):
        self.value = value; self.callbacks = []

    def get(self):
        return self.value

    def set(self, value):
        self.value = value
        for callback in self.callbacks:
            callback("", "", "write")

    def trace_add(self, _mode, callback):
        self.callbacks.append(callback)


class BookFile(unittest.TestCase):
    def setUp(self):
        self.td = Path(self.enterContext(tempfile.TemporaryDirectory())); self.dir = self.td / "책_YeonE_OCR"

    def disk(self) -> dict:
        return json.loads((self.dir / BOOK_SETTINGS).read_text(encoding="utf-8"))

    def test_two_windows_merge_on_save(self):
        """창 A 가 양식을, 그 전에 읽은 창 B 가 쪽 범위를 바꿔 저장해도 둘 다 남고, B 가 손대지 않은 양식을 다시 저장해도 A 의 것이 남는다."""
        self.assertEqual(save_book_settings(self.dir, dict(BOOK, template="T0.hwpx"), create=True), dict(BOOK, template="T0.hwpx"))
        a_loaded = load_book_settings(self.dir); b_loaded = load_book_settings(self.dir)
        a_new = dict(a_loaded, template="TA.hwpx"); save_book_settings(self.dir, a_new, a_loaded)
        b_new = dict(b_loaded, pages="10-20"); save_book_settings(self.dir, b_new, b_loaded)
        self.assertEqual((self.disk()["template"], self.disk()["pages"]), ("TA.hwpx", "10-20"))
        save_book_settings(self.dir, b_new, b_new)                                       # B 가 다시 저장(양식은 B 가 읽은 T0 그대로) → A 의 TA 를 되돌리지 않는다
        save_book_settings(self.dir, a_new, a_new)                                       # A 도 쪽 범위를 되돌리지 않는다
        self.assertEqual((self.disk()["template"], self.disk()["pages"]), ("TA.hwpx", "10-20"))
        save_book_settings(self.dir, dict(b_new, template=""), b_new)                    # 비우는 것도 바꾼 것이다
        self.assertEqual(self.disk()["template"], "")
        d = self.disk()
        self.assertEqual((d["schema_version"], d["saved_by"]), (1, __version__)); self.assertRegex(d["updated"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual(list(self.dir.glob(".*.tmp")) + list(self.dir.glob("*.tmp")), [])   # 원자적 쓰기의 임시 파일이 남지 않는다
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [BOOK_SETTINGS])

    def test_unknown_keys_kept_and_invalid_values_dropped_on_load(self):
        self.dir.mkdir()
        raw = {"schema_version": 1, "layout": "x", "bold_split": "yes", "pages": 3, "print_offset": True, "mode": "vertical",
               "template": None, "preset": "kaibao", "future_key": {"a": 1}}
        (self.dir / BOOK_SETTINGS).write_text(json.dumps(raw), encoding="utf-8")
        before = (self.dir / BOOK_SETTINGS).read_bytes()
        self.assertEqual(load_book_settings(self.dir), {"mode": "vertical", "preset": "kaibao"})
        self.assertEqual((self.dir / BOOK_SETTINGS).read_bytes(), before)               # 읽기만 해서는 파일을 바꾸지 않는다
        loaded = load_book_settings(self.dir)
        got = save_book_settings(self.dir, dict(BOOK, mode="vertical", preset="kaibao"), loaded)
        d = self.disk()
        self.assertEqual(d["future_key"], {"a": 1})                                       # 모르는 열쇠(새 판)는 그대로
        self.assertEqual(got, BOOK); self.assertEqual({k: d[k] for k in BOOK}, BOOK)       # 꼴이 틀린 값은 창의 값으로 바뀐다
        self.assertEqual(load_book_settings(self.dir), BOOK)

    def test_broken_json_is_moved_aside(self):
        self.dir.mkdir(); warned = []
        for k, text in enumerate(('{"layout": "flow", ', '["flow"]')):                     # 쓰다 만 JSON · 맨 위가 {…} 가 아님
            (self.dir / BOOK_SETTINGS).write_text(text, encoding="utf-8")
            with mock.patch.object(state, "WARN", warned.append):
                self.assertEqual(load_book_settings(self.dir), {})
            self.assertFalse((self.dir / BOOK_SETTINGS).exists())
            broken = sorted(p.name for p in self.dir.glob("book_settings.json*broken*"))
            self.assertEqual(len(broken), k + 1)                                          # 앞서 치운 것은 덮지 않는다
            self.assertEqual(len(warned), k + 1); self.assertIn("깨져 있어", warned[-1]); self.assertTrue(warned[-1].startswith("주의:"))
        self.assertEqual((self.dir / "book_settings.json.broken").read_text(encoding="utf-8"), '{"layout": "flow", ')
        (self.dir / BOOK_SETTINGS).write_text("{", encoding="utf-8")                        # 저장할 때 깨진 것을 만나도 치우고 새로 쓴다
        with mock.patch.object(state, "WARN", warned.append):
            self.assertEqual(save_book_settings(self.dir, BOOK), BOOK)
        self.assertEqual(load_book_settings(self.dir), BOOK); self.assertEqual(len(warned), 3)
        (self.dir / BOOK_SETTINGS).write_bytes("\ufeff".encode() + json.dumps(BOOK).encode())   # 메모장이 붙인 BOM 은 깨진 것이 아니다
        self.assertEqual(load_book_settings(self.dir), BOOK)

    def test_missing_folder_is_created_only_with_create(self):
        before = snapshot(self.td)
        self.assertEqual(load_book_settings(self.dir), {})
        self.assertIsNone(save_book_settings(self.dir, BOOK))
        self.assertEqual(snapshot(self.td), before); self.assertFalse(self.dir.exists())
        self.assertEqual(save_book_settings(self.dir / "속", BOOK, create=True), BOOK)    # 실행 직전(create): 폴더째 만든다
        self.assertEqual(load_book_settings(self.dir / "속"), BOOK)

    def test_unchanged_save_does_not_rewrite(self):
        save_book_settings(self.dir, BOOK, create=True)
        path = self.dir / BOOK_SETTINGS; os.utime(path, (1, 1)); before = path.read_bytes()
        self.assertEqual(save_book_settings(self.dir, BOOK, BOOK), BOOK)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime), (before, 1))
        self.assertEqual(save_book_settings(self.dir, {"pages": "1", "force": True, "mode": "sideways"}, {}), dict(BOOK, pages="1"))   # 모르는·틀린 값은 쓰지 않는다
        self.assertNotIn("force", self.disk())

    def test_read_error_is_raised_not_overwritten(self):
        save_book_settings(self.dir, BOOK, create=True)
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("읽기 거부")):
            with self.assertRaises(PermissionError):
                save_book_settings(self.dir, dict(BOOK, template="X"), BOOK)
        self.assertEqual(load_book_settings(self.dir), BOOK)


class WorkFolder(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.td = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.src = self.td / "page.png"; Image.new("RGB", (30, 40), "white").save(self.src)
        self.Image = Image

    def test_folder_with_only_book_settings_is_new_work(self):
        d = self.td / "result"
        save_book_settings(d, BOOK, create=True)                                          # 창이 실행 직전에 남긴 것만 있는 폴더
        w = Work(str(self.src), str(d))
        self.assertTrue((d / "source.json").is_file())
        self.assertTrue(w.source_matches()); self.assertEqual(load_book_settings(d), BOOK)

    def test_changed_source_archives_results_but_keeps_book_settings(self):
        d = self.td / "result"
        w = Work(str(self.src), str(d)); w.ensure_source(300)
        w.save_record(1, {"pdf_page": 1, "merged": {}}); w.ensure_pages([1])
        save_book_settings(d, BOOK)
        self.Image.new("RGB", (30, 40), "red").save(self.src)                            # 원본을 바꿔 넣음
        warned = []
        with mock.patch.object(state, "WARN", warned.append):
            w.ensure_source(300)
        archives = list((d / "archives").iterdir())
        self.assertEqual(len(archives), 1)
        self.assertTrue((archives[0] / "state.json").is_file()); self.assertFalse((d / "state.json").exists())
        self.assertTrue((archives[0] / "readings" / "p001.json").is_file())
        self.assertFalse((archives[0] / BOOK_SETTINGS).exists()); self.assertEqual(load_book_settings(d), BOOK)
        self.assertEqual(len(warned), 1); self.assertIn("책별 설정(book_settings.json)은 그대로 둡니다.", warned[0])
        (d / BOOK_SETTINGS).unlink(); self.Image.new("RGB", (30, 40), "blue").save(self.src)
        w.save_record(1, {"pdf_page": 1, "merged": {}})
        with mock.patch.object(state, "WARN", warned.append):
            w.ensure_source(300)
        self.assertNotIn("책별 설정", warned[-1])                                          # 파일이 없으면 말하지 않는다


class Window(unittest.TestCase):
    """가짜 창(실제 메서드를 붙임): 원본 고르기·탭 바꾸기·설정 저장·실행 직전에 책별 설정을 읽고 쓴다."""
    def setUp(self):
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.gui = self.stack.enter_context(gui_module())
        self.td = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        here = os.getcwd(); os.chdir(self.td); self.stack.callback(os.chdir, here)
        self.saved = []
        self.stack.enter_context(mock.patch.object(self.gui, "save_config", side_effect=lambda cfg: self.saved.append(copy.deepcopy(cfg))))
        self.stack.enter_context(mock.patch.object(self.gui.messagebox, "showerror"))
        self.a = str(self.td / "개보본초.pdf"); self.b = str(self.td / "다른책.pdf"); self.img = str(self.td / "캡처.png")

    def book_dir(self, pdf) -> Path:
        return self.gui.work_dir_for(pdf)

    def app(self, tab0=("", "", ""), tab2=("", "", ""), *, ready=True):
        gui = self.gui; L = gui.LAYOUT_LABELS
        variables = {tab: {k: Var(v) for k, v in zip(("pdf", "pages", "print_offset"), vals)} for tab, vals in ((0, tab0), (2, tab2))}
        modes = {0: Var(gui.MODE_LABELS["auto"]), 2: Var(gui.MODE_LABELS["auto"])}
        app = SimpleNamespace(running=False, _closing=False, _update_installing=False, _selected_tab=0, _active_input=0, _last_ocr_tab=0,
                              _inputs_ready=False, _input_vars=variables, _input_force={0: False, 2: False}, _book={"dir": None, "loaded": {}},
                              v_pdf=variables[0]["pdf"], v_pages=variables[0]["pages"], v_off=variables[0]["print_offset"], v_force=Var(False),
                              v_tab=Var(0), v_tabhint=Var(""), _mode_vars=modes, v_mode=modes[0], cb_mode=mock.Mock(),
                              v_layout=Var(L["page"]), v_template=Var(""), v_bold=Var(True), _preset_ref="regions", v_preset=Var(gui.preset_label("regions")),
                              cb_preset=mock.Mock(), cb_bold=mock.Mock(), cb_pb=None, te_template=None,
                              cfg={}, tab_frames=[mock.Mock() for _ in range(4)], act_cards=[], log=[],
                              _sync_output_location=mock.Mock(), _sync_settings_summary=mock.Mock(), refresh_table=mock.Mock())
        app._append = app.log.append; app.work = lambda: gui.work_dir_for(app.pdf())
        for name in ("select_tab", "_remember_inputs", "_reset_screenshot_pages", "pdf", "mode_key", "layout_key", "preset_ref", "_preset_values",
                     "_layout_changed", "_book_dir", "_book_values", "_apply_book", "_load_book", "_save_book", "_use_pdf", "_doc_summary", "_pending",
                     "_sync_book_label"):
            setattr(app, name, getattr(gui.App, name).__get__(app))
        app.v_layout.trace_add("write", app._layout_changed)
        if ready:                                                                     # 창을 다 만든 뒤 처음 탭을 고르는 것처럼(_build 끝)
            app._inputs_ready = True; app.select_tab(0)
        return app

    def values(self, app):
        return {"layout": app.layout_key(), "template": app.v_template.get(), "preset": app.preset_ref(), "bold_split": app.v_bold.get(),
                "pages": app.v_pages.get(), "print_offset": app.v_off.get(), "mode": app.mode_key(app._active_input)}

    def test_choosing_a_book_loads_its_settings_and_a_new_book_clears_pages_without_creating_folders(self):
        gui = self.gui
        save_book_settings(self.book_dir(self.a), BOOK, create=True)
        app = self.app(("", "1-2", "5"))
        app.v_force.set(True)
        app._use_pdf(self.a)                                                           # 처음 고름(앞 원본 없음): 그 책의 설정
        self.assertEqual(self.values(app), BOOK)
        self.assertEqual(app.v_preset.get(), gui.preset_label("kaibao"))
        self.assertEqual(app.cb_preset.configure.call_args.kwargs["values"][-2:], [gui.PRESET_PICK, gui.PRESET_EXPORT])
        app.cb_bold.state.assert_called_with(["!disabled"])                              # 本草 조문은 굵은 글씨 해석이 있다 → 획 굵기 칸을 켬
        self.assertIn(f"책별 설정을 불러옴({self.book_dir(self.a).name}/book_settings.json): 짜임 {gui.LAYOUT_LABELS['flow']} · 양식 본초학.hwpx"
                      f" · 짜임 규칙 {gui.preset_label('kaibao')} · 획 굵기 안 잼 · 쪽 범위 3-9 · 인쇄 쪽수 보정 29", app.log[-1])
        self.assertEqual(self.saved[-1]["ocr_inputs"]["0"]["pages"], "3-9")
        before = snapshot(self.td)
        app.v_force.set(True)
        app._use_pdf(self.b)                                                           # 새 책(설정 파일·결과 폴더 없음)
        self.assertEqual((app.v_pages.get(), app.v_off.get(), app.v_force.get()), ("", "", False))
        self.assertEqual({k: self.values(app)[k] for k in ("layout", "template", "preset", "bold_split", "mode")},
                         {k: BOOK[k] for k in ("layout", "template", "preset", "bold_split", "mode")})   # 문서 설정은 지난 값을 이어 쓴다
        self.assertIn("새 책: 쪽 범위·인쇄 쪽수 보정은 비우고", app.log[-1])
        self.assertIn(f"문서 설정은 지난 설정(짜임 {gui.LAYOUT_LABELS['flow']} · 양식 본초학.hwpx · 짜임 규칙 {gui.preset_label('kaibao')}", app.log[-1])
        self.assertEqual(snapshot(self.td), before); self.assertFalse(self.book_dir(self.b).exists())   # 고르기만으로는 아무것도 쓰지 않는다
        app.v_pages.set("7"); app.v_template.set("새양식.hwpx")
        app._use_pdf(self.a)                                                           # 되돌아오면 그 책의 값
        self.assertEqual(self.values(app), BOOK)
        self.assertFalse(self.book_dir(self.b).exists())
        app.v_template.set("고친양식.hwpx"); app.v_pages.set("4-5"); app.v_force.set(True)
        app._use_pdf(self.b)                                                           # 다른 책으로 바꿀 때 앞 책(폴더 있음)에 저장
        self.assertEqual(load_book_settings(self.book_dir(self.a)), dict(BOOK, template="고친양식.hwpx", pages="4-5"))
        self.assertFalse(app.v_force.get())
        self.assertEqual((app.v_template.get(), app.v_pages.get()), ("새양식.hwpx", "7"))     # 폴더가 없는 B 에서 앞서 고친 값이 돌아온다(다른 책의 양식·빈 쪽 범위 = 책 전체로 읽지 않게)
        self.assertIn("아직 실행하지 않은 책: 이 창에서 앞서 고른 설정을 되살림", app.log[-1]); self.assertIn("쪽 범위 7", app.log[-1])
        self.assertFalse(self.book_dir(self.b).exists())
        app._use_pdf(self.b)                                                           # 같은 책을 다시 고름 — 비우지 않는다
        app.v_pages.set("8"); app._use_pdf(self.b)
        self.assertEqual(app.v_pages.get(), "8")

    def test_unsaved_new_book_values_survive_browsing_restart_and_land_in_the_file_on_run(self):
        """결과 폴더가 없는 새 책 C 에서 고른 양식·짜임 규칙·쪽 범위: 다른 책을 보고 와도, 창을 다시 열어도(전체 설정의 book_pending) 그대로 — 실행하면 파일로 옮기고 지운다.
        보기만 한 새 책은 기억하지 않는다(지난 설정을 그대로 이어 씀)."""
        gui = self.gui
        c = str(self.td / "새책.pdf")
        save_book_settings(self.book_dir(self.a), BOOK, create=True)
        app = self.app((self.a, "", ""))
        app._use_pdf(self.b); app._use_pdf(self.a)                                     # B 는 보기만 함
        self.assertEqual(app.cfg.get("book_pending", {}), {})
        app._use_pdf(c)
        app.v_layout.set(gui.LAYOUT_LABELS["flow"]); app.v_template.set("C양식.hwpx"); app._preset_ref = "regions"; app.v_pages.set("1-5")
        app._use_pdf(self.a)
        self.assertEqual(app.v_template.get(), BOOK["template"])
        pend = app.cfg["book_pending"][str(self.book_dir(c))]
        self.assertEqual((pend["template"], pend["preset"], pend["pages"]), ("C양식.hwpx", "regions", "1-5"))
        w2 = self.app((self.a, "", "")); w2.cfg = copy.deepcopy(app.cfg)                 # 창을 다시 연 셈(같은 전체 설정)
        w2._use_pdf(c)
        self.assertEqual({k: self.values(w2)[k] for k in ("layout", "template", "preset", "pages")},
                         {"layout": "flow", "template": "C양식.hwpx", "preset": "regions", "pages": "1-5"})
        w2._save_book(create=True)                                                       # 실행 직전: 폴더를 만들어 파일로 — 기억은 지운다
        self.assertEqual(load_book_settings(self.book_dir(c))["template"], "C양식.hwpx")
        self.assertNotIn(str(self.book_dir(c)), w2.cfg["book_pending"])
        self.assertFalse(self.book_dir(self.b).exists())

    def test_tab_switch_saves_before_swapping_and_loads_the_other_tabs_book(self):
        gui = self.gui
        save_book_settings(self.book_dir(self.a), BOOK, create=True)
        image_book = {"layout": "page", "pages": "1", "print_offset": "", "mode": "horizontal_zh"}
        save_book_settings(self.book_dir(self.img), image_book, create=True)
        app = self.app((self.a, "3-9", "29"), (self.img, "1", ""))                    # 시작: 처음 탭의 책을 읽음
        self.assertEqual(self.values(app), BOOK)
        app.v_pages.set("5-9")
        app.select_tab(2)
        self.assertEqual(load_book_settings(self.book_dir(self.a))["pages"], "5-9")      # 바꾸기 전에 앞 탭의 값으로 앞 책에 저장
        self.assertEqual((app.layout_key(), app.v_pages.get(), app.mode_key(2), app.mode_key(0)), ("page", "1", "horizontal_zh", "vertical"))
        self.assertEqual(load_book_settings(self.book_dir(self.img)), dict(image_book))  # 이미지 책은 앞 탭의 쪽 범위로 더럽혀지지 않는다
        app.select_tab(3); app.v_layout.set(gui.LAYOUT_LABELS["flow"]); app.select_tab(2)   # 설정 탭에서 바꾸고 돌아와도 다시 읽어 되돌리지 않는다
        self.assertEqual(app.layout_key(), "flow")
        app.select_tab(0)
        self.assertEqual(load_book_settings(self.book_dir(self.img))["layout"], "flow")
        self.assertEqual(self.values(app), dict(BOOK, pages="5-9"))
        self.assertEqual(sorted(p.name for p in self.td.iterdir()), sorted([self.book_dir(self.a).name, self.book_dir(self.img).name]))

    def test_startup_without_book_file_keeps_remembered_pages(self):
        app = self.app((self.a, "10-20", "29"))
        self.assertEqual((app.v_pages.get(), app.v_off.get()), ("10-20", "29"))         # 옮겨 온 ocr_inputs 의 쪽 범위를 비우지 않는다
        self.assertEqual(app._book["dir"], self.book_dir(self.a)); self.assertFalse(self.book_dir(self.a).exists())
        app.select_tab(2); app.select_tab(0)
        self.assertEqual((app.v_pages.get(), app.v_off.get()), ("10-20", "29"))
        self.assertEqual(list(self.td.iterdir()), [])

    def test_screenshot_reset_does_not_store_empty_pages_into_the_previous_book(self):
        save_book_settings(self.book_dir(self.img), {"pages": "2", "print_offset": "-1"}, create=True)
        app = self.app(tab2=(self.img, "2", "-1")); app.select_tab(2)
        app._reset_screenshot_pages(); app._use_pdf(str(self.td / "새캡처.png"))
        self.assertEqual(load_book_settings(self.book_dir(self.img))["pages"], "2")
        self.assertEqual(app.v_pages.get(), "")

    def test_broken_or_unreadable_book_file_only_warns(self):
        d = self.book_dir(self.a); d.mkdir(); (d / BOOK_SETTINGS).write_text("{", encoding="utf-8")
        app = self.app(); app._use_pdf(self.a)
        self.assertTrue(any(m.startswith("주의: 책별 설정") and "깨져 있어" in m for m in app.log))
        self.assertTrue(list(d.glob("book_settings.json*broken*")))
        with mock.patch.object(self.gui, "save_book_settings", side_effect=PermissionError("읽기 전용")):
            app._save_book(create=True)
        self.assertIn("주의: 책별 설정을 저장하지 못했습니다", app.log[-1]); self.assertIn("PermissionError: 읽기 전용", app.log[-1])
        with mock.patch.object(self.gui, "load_book_settings", side_effect=OSError("네트워크")):
            app._use_pdf(self.b)
        self.assertIn("주의: 책별 설정을 읽지 못했습니다", app.log[-1]); self.assertIn("OSError: 네트워크", app.log[-1])

    def test_window_never_writes_a_book_it_did_not_load(self):
        save_book_settings(self.book_dir(self.a), BOOK, create=True)
        before = snapshot(self.td)
        app = self.app((self.b, "", ""), ready=False)                                  # 다 만들어지기 전(_build 가운데의 첫 select_tab)
        app.select_tab(0); app._save_book(create=True)
        self.assertIsNone(app._book["dir"])
        app._inputs_ready = True; app.select_tab(0)                                    # 이제 B 를 불러옴(폴더 없음)
        self.assertEqual(app._book["dir"], self.book_dir(self.b))
        app.v_template.set("B양식.hwpx"); app.v_pdf.set(self.a)                         # 불러오지 않은 A 로 칸만 바뀜: 칸 값이 A 의 것이 아니다
        app._save_book(create=False); app._save_book(create=True)
        self.assertEqual(snapshot(self.td), before)

    def test_showing_a_book_does_not_count_as_this_windows_change_to_new_book_defaults(self):
        """창 W2 가 책 A(원본 쪽대로·양식 없음)를 보여 주기만 하고 닫혀도, 창 W1 이 저장한 새 책 기본값(내용별·본초학 양식)을 덮지 않는다."""
        from test_gui_settings_merge import fake_app
        gui = self.gui
        path = self.td / "cfg" / "config.json"; path.parent.mkdir()
        self.stack.enter_context(mock.patch.object(gui, "CONFIG_PATH", path))
        disk = lambda: json.loads(path.read_text(encoding="utf-8"))
        put = lambda d: path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")   # save_config 는 setUp 에서 self.saved 로 가로챔
        put(dict(gui.DEFAULTS, layout="flow", template="옛양식.hwpx", preset="kaibao"))
        save_book_settings(self.book_dir(self.a), {"layout": "page", "template": "", "preset": "regions"}, create=True)

        def window():
            w = fake_app(gui, gui.load_config(), layout=gui.LAYOUT_LABELS["flow"], template="옛양식.hwpx")
            book = self.app((self.a, "", ""), ready=False); book.cfg = w.cfg; vars(book)["_cfg_loaded"] = w._cfg_loaded
            book.v_layout.set(gui.LAYOUT_LABELS["flow"]); book.v_template.set("옛양식.hwpx"); book._preset_ref = "kaibao"   # 전체 설정으로 뜬 창
            book._inputs_ready = True; book.select_tab(0)
            for k in ("v_layout", "v_template", "v_bold", "v_pages", "v_off", "_preset_ref"):
                setattr(w, k, getattr(book, k))
            w._book, w._active_input, w._mode_vars = book._book, 0, book._mode_vars
            for name in ("_save_book", "_book_dir", "_book_values", "preset_ref", "_pending", "layout_key", "mode_key"):
                setattr(w, name, getattr(gui.App, name).__get__(w))
            w.pdf = book.pdf
            return book, w
        b2, w2 = window()                                                                # W2: 책 A 를 불러옴(page·''·regions)
        self.assertEqual((b2.layout_key(), b2.v_template.get(), b2.preset_ref()), ("page", "", "regions"))
        put(dict(disk(), layout="flow", template="새양식.hwpx", preset="kaibao"))         # 그 사이 W1 이 새 책 기본값을 저장
        gui.App.save_settings(w2, quiet=True)                                            # W2 닫기 — 사용자는 아무것도 고치지 않음
        got = self.saved[-1]
        self.assertEqual((got["layout"], got["template"], got["preset"]), ("flow", "새양식.hwpx", "kaibao"))
        put(got); b2.v_template.set("W2가고침.hwpx"); gui.App.save_settings(w2, quiet=True)   # 불러온 뒤 고친 것은 이 창의 변경
        self.assertEqual(self.saved[-1]["template"], "W2가고침.hwpx")

    def test_document_card_names_the_book_its_fields_belong_to(self):
        save_book_settings(self.book_dir(self.a), BOOK, create=True)
        app = self.app((self.a, "", ""), (self.img, "", "")); app.v_docbook = Var("")
        app.select_tab(0)
        self.assertIn("「개보본초.pdf」(일반 OCR 탭)의 책별 설정입니다 — 개보본초_YeonE_OCR/book_settings.json", app.v_docbook.get())
        app.select_tab(2); app.select_tab(3)                                           # 설정 탭: 마지막에 쓴 입력 탭(이미지)의 책
        self.assertIn("「캡처.png」(이미지 OCR 탭)", app.v_docbook.get()); self.assertIn("결과 폴더가 아직 없어 이 창이 기억했다가 처음 실행할 때 남깁니다", app.v_docbook.get())

    def test_two_windows_on_one_book(self):
        save_book_settings(self.book_dir(self.a), BOOK, create=True)
        w1 = self.app(); w1._use_pdf(self.a); w2 = self.app(); w2._use_pdf(self.a)
        w1.v_template.set("A창.hwpx"); w1._save_book()
        w2.v_pages.set("1-3"); w2._save_book()
        w2._save_book(); w1._save_book()
        self.assertEqual(load_book_settings(self.book_dir(self.a)), dict(BOOK, template="A창.hwpx", pages="1-3"))

    def test_save_settings_writes_only_into_an_existing_folder(self):
        from test_gui_settings_merge import fake_app
        gui = self.gui
        self.stack.enter_context(mock.patch.object(gui, "CONFIG_PATH", self.td / "cfg" / "config.json"))
        app = fake_app(gui, dict(gui.DEFAULTS), layout=gui.LAYOUT_LABELS["flow"], template="T.hwpx", pages="2-4")
        app.pdf = lambda: self.a; app._active_input = 0; app._append = mock.Mock(); app._book = {"dir": self.book_dir(self.a), "loaded": {}}
        for name in ("_save_book", "_book_dir", "_book_values", "preset_ref", "_pending"):
            setattr(app, name, getattr(gui.App, name).__get__(app))
        gui.App.save_settings(app, quiet=True)
        self.assertFalse(self.book_dir(self.a).exists())                                 # 아직 실행하지 않은 책 — 폴더를 만들지 않는다
        self.book_dir(self.a).mkdir()
        gui.App.save_settings(app, quiet=True)
        self.assertEqual(load_book_settings(self.book_dir(self.a)),
                         {"layout": "flow", "template": "T.hwpx", "preset": "basic", "bold_split": True, "pages": "2-4", "print_offset": "", "mode": "auto"})
        self.assertEqual((self.saved[0]["layout"], self.saved[0]["template"]), ("flow", "T.hwpx"))   # 전체 설정 = 새 책의 기본값(지난 값)
        self.assertNotIn("book_settings", json.dumps(self.saved[0], ensure_ascii=False))

    def test_run_start_writes_book_settings_with_create_after_confirmations(self):
        gui = self.gui
        app = self.app(); app._use_pdf(self.a)
        app.v_template.set("양식.hwpx"); app.v_pages.set("3-9")
        seen = []
        app._need_pdf = app._prepare_env = lambda: True
        app.v_reader = Var("openai"); app.v_model = Var("gpt-4.1"); app.work = lambda: self.book_dir(self.a)
        app._pages = lambda: []; app._build_opts = lambda: []; app._cap_opts = lambda: []; app._answered_opts = lambda: ["--skip-answered", "any-model"]
        app.start = lambda argv, label: seen.append((self.book_dir(self.a) / BOOK_SETTINGS).is_file())
        app.v_ctxmodel = Var(True)
        app._with_plan = lambda kind, go: None                                          # 비용 창에서 [취소] — go(실행·저장)를 부르지 않는다
        gui.App.do_build(app)
        self.assertEqual(seen, []); self.assertFalse(self.book_dir(self.a).exists())
        app._with_plan = lambda kind, go: go([])
        gui.App.do_build(app)
        self.assertEqual(seen, [True])                                                   # 실행 직전에 폴더째 만들어 남김
        self.assertEqual(load_book_settings(self.book_dir(self.a)), dict(self.values(app)))
        self.assertEqual(load_book_settings(self.book_dir(self.a))["pages"], "3-9")


class LastBuild(unittest.TestCase):
    def test_build_records_what_it_used(self):
        import pymupdf
        from yeone_ocr import cli, merge, prompts
        from _hwpx_fixtures import make_template
        mode, fold = prompts.MODE, merge.FOLD_ZH                                          # 명령이 읽기 방향 전역을 바꾼다 — 뒤 시험에 남기지 않게
        self.addCleanup(lambda: (setattr(prompts, "MODE", mode), setattr(merge, "FOLD_ZH", fold)))
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td); tpl = tmp / "양식.hwpx"; make_template(tpl)
            src = tmp / "책.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page(width=420, height=300)
                page.insert_text((30, 80), "香薷　味辛微温主霍乱腹痛", fontname="china-s", fontsize=18)
                doc.save(src)
            w = tmp / "w"; log = io.StringIO()
            with contextlib.redirect_stdout(log):
                rc = cli.main(["run", str(src), "--work", str(w), "--reader", "mock", "--checkpoint", "0", "--mode", "horizontal_zh", "--no-normalize"])
            self.assertEqual(rc, 0, log.getvalue()[-600:])
            st = json.loads((w / "state.json").read_text(encoding="utf-8"))
            self.assertEqual({k: st["last_build"][k] for k in ("layout", "template", "preset", "outputs")},
                             {"layout": "page", "template": "", "preset": "", "outputs": ["책.hwpx"]})
            with contextlib.redirect_stdout(log):
                rc = cli.main(["build", str(src), "--work", str(w), "--layout", "flow", "--template", str(tpl), "--preset", "kaibao",
                               "--no-bold", "--no-normalize", "--out", "내용별"])
            self.assertEqual(rc, 0, log.getvalue()[-600:])
            st = json.loads((w / "state.json").read_text(encoding="utf-8"))
            self.assertEqual({k: st["last_build"][k] for k in ("layout", "template", "preset", "outputs")},
                             {"layout": "flow", "template": str(tpl), "preset": "kaibao", "outputs": ["내용별.hwpx"]})
            self.assertRegex(st["last_build"]["at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
            self.assertIn("1", st["pages"])                                               # 쪽 상태는 그대로
            self.assertFalse((w / BOOK_SETTINGS).exists())                                # 명령줄은 책별 설정을 쓰지 않는다(창이 선택지를 넘김)


if __name__ == "__main__":
    unittest.main()
