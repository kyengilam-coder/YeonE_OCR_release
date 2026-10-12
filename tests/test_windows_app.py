"""Fast entry-point regressions; the executable smoke test is explicit."""
import tempfile
import json
import os
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

from yeone_ocr import __version__
from yeone_ocr.windows_app import SafeLog, _acknowledge_update_ready, _normal_gui, main, redact_log


class WindowsEntrypointTests(unittest.TestCase):
    @staticmethod
    def ready_job(directory, token):
        job = Path(directory) / ("install-" + "1" * 32)
        job.mkdir()
        (job / "install.json").write_text(json.dumps({"schema_version": 1,
            "ready_token": token, "target": sys.executable}), encoding="utf-8")
        return job / "application-ready"

    def test_update_ready_is_atomic_and_requires_a_valid_job(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.ready_job(directory, "a" * 64)
            self.assertFalse(_acknowledge_update_ready(str(path), "invalid"))
            self.assertFalse(path.exists())
            self.assertTrue(_acknowledge_update_ready(str(path), "a" * 64))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["token"], "a" * 64)
            self.assertEqual({p.name for p in path.parent.iterdir()}, {"application-ready", "install.json"})

    def test_update_ready_refuses_other_files_mismatched_token_and_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.ready_job(directory, "c" * 64)
            existing = path.parent / "important.txt"
            existing.write_text("preserve", encoding="utf-8")
            self.assertFalse(_acknowledge_update_ready(str(existing), "c" * 64))
            self.assertEqual(existing.read_text(encoding="utf-8"), "preserve")
            self.assertFalse(_acknowledge_update_ready(str(path), "d" * 64))
            config = path.parent / "install.json"
            data = json.loads(config.read_text(encoding="utf-8"))
            data["target"] = str(path.parent / "some-other.exe")
            config.write_text(json.dumps(data), encoding="utf-8")
            self.assertFalse(_acknowledge_update_ready(str(path), "c" * 64))
            self.assertFalse(path.exists())

    def test_update_acknowledgement_waits_for_event_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            token = "b" * 64
            path = self.ready_job(directory, token)
            owner = self

            class FakeApp:
                def __init__(self):
                    owner.assertFalse(path.exists())
                    owner.assertNotIn("YEONE_UPDATE_READY_FILE", os.environ)
                    owner.assertNotIn("YEONE_UPDATE_READY_TOKEN", os.environ)

                def after_idle(self, callback):
                    self.callback = callback
                    owner.assertFalse(path.exists())

                def mainloop(self):
                    owner.assertFalse(path.exists())
                    self.callback()
                    owner.assertEqual(json.loads(path.read_text(encoding="utf-8"))["token"], token)

            with patch.dict(os.environ, {"YEONE_UPDATE_READY_FILE": str(path), "YEONE_UPDATE_READY_TOKEN": token}), \
                 patch("yeone_ocr.gui.App", FakeApp):
                self.assertEqual(_normal_gui(), 0)

    def test_version_does_not_launch_gui_or_self_test(self):
        with patch("yeone_ocr.windows_app._console_message") as message, \
             patch("yeone_ocr.windows_app._normal_gui") as gui, \
             patch("yeone_ocr.windows_app._self_test") as smoke:
            self.assertEqual(main(["--version"]), 0)
        message.assert_called_once_with("YeonE_OCR " + __version__)
        gui.assert_not_called()
        smoke.assert_not_called()

    def test_output_and_self_test_are_required_together(self):
        for args in (["--self-test"], ["--output", "unused"], ["--version", "--output", "unused"]):
            with self.subTest(args=args), self.assertRaises(SystemExit) as raised:
                main(args)
            self.assertEqual(raised.exception.code, 2)

    def test_redacts_environment_google_and_header_credentials(self):
        key = "AIza" + "A" * 35
        text = f"failure {key} api_key=hidden-key Authorization: Bearer hidden-token ?key=query-secret custom-secret"
        clean = redact_log(text, ["custom-secret"])
        for secret in (key, "hidden-key", "hidden-token", "query-secret", "custom-secret"):
            self.assertNotIn(secret, clean)

    def test_split_writes_do_not_leak_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.txt"
            stream = SafeLog(path)
            try:
                key = "AIza" + "B" * 35
                stream.write(key[:15])
                stream.flush()
                self.assertEqual(path.read_text(encoding="utf-8"), "")
                stream.write(key[15:] + "\n")
                stream.finish()
                self.assertNotIn(key, path.read_text(encoding="utf-8"))
                self.assertIn("[REDACTED]", path.read_text(encoding="utf-8"))
            finally:
                stream.close()


if __name__ == "__main__":
    unittest.main()
