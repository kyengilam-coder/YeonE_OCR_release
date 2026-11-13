"""엔진 관리 창(진짜 Tk — 화면이 없으면 건너뜀): 설정 탭의 단추 · 점검 결과에 따른 선택 · 설치 진행·결과 · 다시 시도 · 취소.

GPU 점검과 설치는 가짜로 바꾼다(프로세스·네트워크 없음)."""
import contextlib
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ops import _real_tk, gui_module


class EngineManagerRealTk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _real_tk().destroy()
        cls.stack = contextlib.ExitStack()
        cls.gui = gui = cls.stack.enter_context(gui_module())
        from yeone_ocr import engine_manager, gui_engines
        from ab_ocr import runtime
        cls.em, cls.dialogs = engine_manager, gui_engines
        cls.td = td = Path(cls.stack.enter_context(tempfile.TemporaryDirectory()))
        cfg = td / "cfg" / "config.json"; cfg.parent.mkdir()
        cfg.write_text(json.dumps({"auto_update": False, "reader": "gemini", "model": "gemini-2.5-flash", "fx_auto": 1400,
                                   "fx_auto_date": "2099-01-01", "input_tab": 0}), encoding="utf-8")
        (td / "app").mkdir()
        for target, name, value in ((gui, "CONFIG_PATH", cfg), (gui, "missing_packages", lambda: []), (gui.App, "_fetch_fx", lambda self: None),
                                    (gui, "write_config", lambda app: None),
                                    (runtime, "application_root", lambda: td / "app"),
                                    (runtime, "user_engine_root", lambda: td / "user" / "local-engines")):
            cls.stack.enter_context(mock.patch.object(target, name, value))
        cls.stack.enter_context(mock.patch.dict("os.environ", {"YEONE_OCR_ENGINE_ROOT": "", "AB_OCR_ENGINE_ROOT": ""}))
        cls.app = gui.App(); cls.app.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.destroy()
        finally:
            cls.stack.close()

    def setUp(self):
        self.gpu = self.em.Gpu("NVIDIA GeForce GTX 1660", "610.88", 6144, 1400, "7.5")
        self.probe = {"gpus": [self.gpu], "message": self.gpu.describe()}
        patcher = mock.patch.object(self.em, "probe_gpus", side_effect=lambda: dict(self.probe))
        patcher.start(); self.addCleanup(patcher.stop)
        for name in ("askokcancel", "askyesno"):
            patcher = mock.patch.object(self.dialogs.messagebox, name, return_value=True)
            setattr(self, name, patcher.start()); self.addCleanup(patcher.stop)

    def open(self):
        self.app.open_engine_manager()
        window = self.app._engine_manager
        self.addCleanup(lambda: window.winfo_exists() and window._finish_close())
        self.wait(lambda: str(window.b_probe.cget("state")) == "normal")
        return window

    def wait(self, done, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.update()
            if done():
                return
            time.sleep(0.02)
        self.fail("시간 안에 끝나지 않음")

    def install(self, keys):
        folder = self.td / "user" / "local-engines"
        extras = {"paddle_server_gpu": {"execution_provider": "cuda"}, "surya_cuda": {"backend_device": "cuda"}}
        for key in keys:
            option = self.em.OPTION_BY_KEY[key]
            target = folder / "tools" / option.engine
            target.mkdir(parents=True, exist_ok=True)
            path = target / "installation.json"
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"engine": option.engine}
            path.write_text(json.dumps({**data, **extras.get(key, {})}), encoding="utf-8")

    def test_1_settings_row_opens_one_window_with_recommended_engines_checked(self):
        row = self.app._set_rows["engines"]
        self.assertEqual(row["name"].cget("text"), "OCR 엔진")
        self.assertEqual(row["ctrl"].cget("text"), "엔진 관리…")
        row["ctrl"].invoke()
        window = self.app._engine_manager
        self.addCleanup(lambda: window.winfo_exists() and window._finish_close())
        self.wait(lambda: str(window.b_probe.cget("state")) == "normal")
        self.assertIn("GTX 1660", window.v_gpu.get())
        self.assertEqual(window.selected(), [option.key for option in self.em.OPTIONS])
        self.assertEqual({row["state"].cget("text") for row in window.rows.values()}, {"추천"})
        self.assertIn("9.6 GB", window.b_install.cget("text"))
        self.assertEqual(str(window.b_install.cget("state")), "normal")
        self.assertIn(str(self.td / "user" / "local-engines"), window.v_root.get())
        self.app.open_engine_manager()                               # 두 번 열어도 같은 창
        self.assertIs(self.app._engine_manager, window)
        window.close()
        self.assertFalse(window.winfo_exists())

    def test_1b_gpu_engine_setting_row_is_saved_only_once_changed(self):
        row = self.app._set_rows["local_gpu_engines"]
        self.assertEqual(row["name"].cget("text"), "GPU 엔진")
        self.assertIs(self.app.cfg.get("local_gpu_engines"), None)
        self.app.save_settings(quiet=True)
        self.assertIs(self.app.cfg.get("local_gpu_engines"), None)          # 바꾸지 않으면 환경 변수를 따름
        row["ctrl"].invoke()
        self.app.save_settings(quiet=True)
        self.assertIs(self.app.cfg["local_gpu_engines"], bool(self.app.v_local_gpu_engines.get()))
        row["ctrl"].invoke()

    def test_2_without_nvidia_gpu_only_cpu_engines_can_be_picked_and_none_is_preselected(self):
        self.probe = {"gpus": [], "message": "NVIDIA GPU를 찾지 못했습니다(nvidia-smi 없음)."}
        window = self.open()
        self.assertEqual(window.selected(), [])
        self.assertEqual(str(window.b_install.cget("state")), "disabled")
        states = {key: str(row["check"].cget("state")) for key, row in window.rows.items()}
        self.assertEqual(states, {"paddle_server": "normal", "paddle_server_gpu": "disabled", "surya": "normal",
                                  "surya_cuda": "disabled", "glmocr": "disabled"})
        self.assertEqual(window.rows["glmocr"]["state"].cget("text"), "NVIDIA GPU 필요")

    def test_3_gpu_backend_and_its_base_engine_are_picked_together(self):
        window = self.open()
        for row in window.rows.values():
            row["var"].set(False)
        window.rows["surya_cuda"]["check"].invoke()
        self.assertEqual(window.selected(), ["surya", "surya_cuda"])
        window.rows["surya"]["check"].invoke()                       # 바탕 엔진을 빼면 GPU 가속도 빠진다
        self.assertEqual(window.selected(), [])

    def test_4_install_shows_progress_results_and_keeps_failures_checked_for_retry(self):
        window = self.open()
        calls = []

        def run_install(keys, *, selected_root, cancel, gpus, log, progress):
            calls.append(list(keys))
            progress(0, len(keys), "PaddleOCR server (CPU)", "install")
            log("pulling manifest 40%", True)
            log("verified ch_PP-OCRv5_det_server.onnx")
            failed = {"surya", "surya_cuda"} if len(calls) == 1 else set()
            self.install([key for key in keys if key not in failed])
            progress(len(keys), len(keys), "", "done")
            return {"results": [{"key": key, "label": self.em.OPTION_BY_KEY[key].label,
                                 "status": "failed" if key == "surya" else "skipped" if key in failed else "ok",
                                 "message": "설치기가 오류로 끝났습니다(종료 코드 1)" if key == "surya" else ""} for key in keys],
                    "log": self.td / "install.log", "roots": [self.td / "user" / "local-engines"]}

        with mock.patch.object(self.em, "run_install", side_effect=run_install), mock.patch.object(self.app, "_append") as noted:
            window.b_install.invoke()
            self.wait(lambda: not window.busy and "끝" in window.v_stage.get())
            self.assertIn("엔진 설치: PaddleOCR server (CPU)", noted.call_args.args[0])  # 작업 탭 기록에도 남긴다
            self.assertIn("GLM-OCR", self.askokcancel.call_args.args[1])
            self.assertIn("Open RAIL-M", self.askokcancel.call_args.args[1])          # Surya 가중치 라이선스 알림
            self.assertEqual(calls[0], [option.key for option in self.em.OPTIONS])
            text = window.log.get("1.0", "end")
            self.assertIn("verified ch_PP-OCRv5_det_server.onnx", text)
            self.assertNotIn("pulling manifest", text)               # 진행 표시는 로그에 쌓지 않는다
            self.assertIn("Surya OCR 2 (CPU): 실패 — 설치기가 오류로 끝났습니다", text)
            self.assertIn("3개 설치", window.v_stage.get())
            self.assertEqual(window.selected(), ["surya", "surya_cuda"])              # 끝나지 않은 것만 체크된 채
            self.assertEqual(window.rows["glmocr"]["state"].cget("text"), "설치됨")
            self.assertEqual(str(window.rows["glmocr"]["check"].cget("state")), "disabled")
            self.assertEqual(str(window.b_log.cget("state")), "normal")
            self.assertEqual(window.b_close.cget("text"), "닫기")
            window.b_install.invoke()                                # 다시 시도
            self.wait(lambda: not window.busy and len(calls) == 2 and window.selected() == [])
            self.assertEqual(calls[1], ["surya", "surya_cuda"])
            self.assertEqual({row["state"].cget("text") for row in window.rows.values()}, {"설치됨"})
            self.assertEqual(str(window.b_install.cget("state")), "disabled")

    def test_5_closing_while_installing_cancels_first_and_main_window_waits(self):
        for folder in (self.td / "user").glob("local-engines/tools/*"):
            for path in folder.glob("installation.json"):
                path.unlink()
        window = self.open()
        started, seen = threading.Event(), {}

        def run_install(keys, *, selected_root, cancel, gpus, log, progress):
            started.set()
            seen["cancelled"] = cancel.wait(10)
            return {"results": [{"key": key, "label": self.em.OPTION_BY_KEY[key].label, "status": "cancelled", "message": ""}
                                for key in keys], "log": None, "roots": []}

        with mock.patch.object(self.em, "run_install", side_effect=run_install):
            window.b_install.invoke()
            self.wait(started.is_set)
            self.assertEqual(window.b_close.cget("text"), "취소")
            self.app.on_close()                                      # 앱을 닫으려 해도 설치 취소부터 묻는다
            self.assertTrue(self.app.winfo_exists() and window.winfo_exists())
            self.assertIn("설치를 취소할까요", self.askyesno.call_args.args[1])
            self.wait(lambda: not window.busy and "끝" in window.v_stage.get())
        self.assertTrue(seen["cancelled"])
        self.assertEqual(len(window.selected()), len(self.em.OPTIONS))               # 그대로 다시 시도할 수 있다
        window.close()
        self.assertFalse(window.winfo_exists())


if __name__ == "__main__":
    unittest.main()
