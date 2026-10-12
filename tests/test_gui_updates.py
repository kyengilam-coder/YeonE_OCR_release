"""업데이트 GUI 경계: 네트워크·설치·Tk 창 없이 작업 보호와 오류 경로를 검사한다."""
import queue
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yeone_ocr import gui


class ImmediateThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


def app_state():
    app = SimpleNamespace(cfg={"auto_update": True, "api_key": "saved-secret"}, running=False,
                          _update_checking=False, _update_installing=False, _update_pending=None,
                          _update_prompt_open=False, _update_manual=False, q=queue.Queue(),
                          _append=Mock(), save_settings=Mock(), destroy=Mock(), wait_window=Mock(),
                          check_packages=Mock(),
                          v_status=SimpleNamespace(set=Mock()), v_pkg=SimpleNamespace(set=Mock()))

    def set_running(on, command=""):
        app.running = on
    app._set_running = Mock(side_effect=set_running)
    for name in ("check_updates", "_update_checked", "_offer_update", "_start_update", "_update_downloaded"):
        setattr(app, name, getattr(gui.App, name).__get__(app))
    return app


class GuiUpdates(unittest.TestCase):
    def setUp(self):
        self.app = app_state()
        self.info = SimpleNamespace(version="99.0.0", url="https://github.com/kyengilam-coder/YeonE_OCR/releases/tag/v99.0.0", notes="새 버전")
        self.backend = ModuleType("yeone_ocr.updater")
        self.backend.check_update = Mock(return_value=self.info)
        self.backend.download_update = Mock(return_value=Path("verified.exe"))
        self.backend.install_update = Mock()
        for ctx in (patch.dict(sys.modules, {"yeone_ocr.updater": self.backend}),
                    patch.object(gui.threading, "Thread", ImmediateThread),
                    patch.object(gui.messagebox, "showinfo"), patch.object(gui.messagebox, "showwarning"),
                    patch.object(gui.messagebox, "showerror"), patch.object(gui.sys, "frozen", True, create=True),
                    patch.object(gui.sys, "platform", "win32")):
            ctx.start(); self.addCleanup(ctx.stop)

    def deliver(self):
        kind, value, error = self.app.q.get_nowait()
        if kind == "update_check":
            self.app._update_checked(value, error)
        elif kind == "update_download":
            self.app._update_downloaded(value, error)
        else:
            self.fail(kind)

    def test_automatic_check_respects_preference_and_does_not_install(self):
        self.assertTrue(gui.DEFAULTS["auto_update"])
        self.app.cfg["auto_update"] = False
        self.app.check_updates(manual=False)
        self.backend.check_update.assert_not_called()
        self.app.check_updates(manual=True)
        self.deliver()
        self.assertIs(self.app._update_pending, self.info)
        self.backend.download_update.assert_not_called()
        self.backend.install_update.assert_not_called()

    def test_automatic_network_failure_is_quiet_manual_failure_is_visible(self):
        self.backend.check_update.side_effect = RuntimeError("offline")
        self.app.check_updates(manual=False)
        self.deliver()
        gui.messagebox.showwarning.assert_not_called()
        self.app._append.assert_called()
        self.app.check_updates(manual=True)
        self.deliver()
        gui.messagebox.showwarning.assert_called_once()
        self.assertFalse(self.app._update_checking)

    def test_offer_waits_for_work_to_finish_and_postpone_does_not_download(self):
        self.app.running = True
        self.app.check_updates(manual=False)
        self.deliver()
        with patch.object(gui, "UpdateDialog", return_value=SimpleNamespace(install=False)) as dialog:
            self.app._offer_update()
            dialog.assert_not_called()
            self.app.running = False
            self.app._worker = SimpleNamespace(is_alive=lambda: True)
            self.app._offer_update()
            dialog.assert_not_called()
            self.app._worker = None
            self.app._offer_update()
            dialog.assert_called_once_with(self.app, self.info, True)
        self.backend.download_update.assert_not_called()
        self.assertIsNone(self.app._update_pending)

    def test_source_mode_has_no_installer_and_does_not_download(self):
        self.app._update_pending = self.info
        with patch.object(gui.sys, "frozen", False), patch.object(gui, "UpdateDialog", return_value=SimpleNamespace(install=False)) as dialog:
            self.app._offer_update()
            dialog.assert_called_once_with(self.app, self.info, False)
            self.app._start_update(self.info)
        self.backend.download_update.assert_not_called()
        self.backend.install_update.assert_not_called()

    def test_install_rechecks_busy_state_after_confirmation(self):
        self.app.running = True
        self.app._start_update(self.info)
        self.assertIs(self.app._update_pending, self.info)
        self.backend.download_update.assert_not_called()
        self.app.destroy.assert_not_called()

    def test_verified_download_starts_helper_before_closing_and_preserves_settings(self):
        self.app._start_update(self.info)
        self.assertTrue(self.app.running)
        self.assertTrue(self.app._update_installing)
        self.app._set_running.assert_called_once_with(True, "update")
        self.app.destroy.assert_not_called()
        self.backend.install_update.side_effect = lambda *args: self.app.destroy.assert_not_called()
        self.deliver()
        self.backend.install_update.assert_called_once_with(Path("verified.exe"), Path(sys.executable), gui.os.getpid())
        self.app.destroy.assert_called_once()
        self.assertTrue(self.app._closing)
        self.assertEqual(self.app.cfg["api_key"], "saved-secret")
        self.assertEqual(self.app.save_settings.call_count, 2)

    def test_poll_stops_using_widgets_after_successful_update_shutdown(self):
        self.app._start_update(self.info)
        self.app._fx_fetched = Mock()
        self.app.after = Mock()
        self.app.q.put(("fx", 1400, "2026-10-12"))
        gui.App._poll(self.app)
        self.app.destroy.assert_called_once()
        self.app._fx_fetched.assert_not_called()
        self.app.after.assert_not_called()

    def test_failed_verification_or_helper_launch_keeps_current_app_running(self):
        for phase in ("download", "install"):
            with self.subTest(phase=phase):
                self.app = app_state()
                self.backend.download_update.side_effect = RuntimeError("checksum mismatch") if phase == "download" else None
                self.backend.install_update.side_effect = RuntimeError("cannot start helper") if phase == "install" else None
                self.backend.install_update.reset_mock()
                self.app._start_update(self.info)
                self.deliver()
                self.app.destroy.assert_not_called()
                self.assertFalse(self.app._update_installing)
                self.assertFalse(self.app.running)
                if phase == "download":
                    self.backend.install_update.assert_not_called()
        self.assertEqual(gui.messagebox.showerror.call_count, 2)

    def test_close_during_download_never_starts_installation(self):
        self.app._start_update(self.info)
        with patch.object(gui.messagebox, "askyesno", return_value=True):
            gui.App.on_close(self.app)
        self.deliver()
        self.backend.install_update.assert_not_called()
        self.app.destroy.assert_called_once()

    def test_setting_save_failure_aborts_before_download(self):
        self.app.save_settings.side_effect = OSError("read only")
        self.app._start_update(self.info)
        self.backend.download_update.assert_not_called()
        self.app.destroy.assert_not_called()
        self.assertFalse(self.app.running)

    def test_frozen_package_action_never_launches_pip_or_prompts_to_install(self):
        with patch.object(gui.subprocess, "Popen") as popen, patch.object(gui.messagebox, "askyesno") as ask:
            gui.App.install_packages(self.app)
            gui.App._pkg_result(self.app, ["pymupdf"])
            popen.assert_not_called()
            ask.assert_not_called()
        self.app.check_packages.assert_called_once_with()
        self.assertIn("설치 구성요소 오류", self.app.v_pkg.set.call_args.args[0])

    def test_frozen_component_check_becomes_available_when_other_work_finishes(self):
        package = Mock()
        app = SimpleNamespace(buttons=[package], b_packages=package, b_stop=Mock(), _reader_changed=Mock())
        gui.App._set_running(app, False)
        package.configure.assert_called_with(state="normal")

    def test_update_preference_does_not_replace_other_settings(self):
        self.app.v_auto_update = SimpleNamespace(get=lambda: False)
        with patch.object(gui, "save_config") as save:
            gui.App._save_update_preference(self.app)
            save.assert_called_once_with(self.app.cfg)
        self.assertFalse(self.app.cfg["auto_update"])
        self.assertEqual(self.app.cfg["api_key"], "saved-secret")


if __name__ == "__main__":
    unittest.main()
