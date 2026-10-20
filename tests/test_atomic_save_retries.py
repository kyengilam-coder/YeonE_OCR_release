"""Windows sharing retries must recheck source, competing saves and protected files."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from ab_ocr import ocr
from yeone_ocr import comparison
from yeone_ocr.comparison import ComparisonError, ComparisonProject, SourceChangedError


@unittest.skipUnless(os.name == "nt", "Windows sharing-error retry policy")
class AtomicSaveRetries(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="atomic-sharing-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.source = self.root / "synthetic.png"
        Image.new("RGB", (80, 60), "white").save(self.source)
        self.project = ComparisonProject.open(self.source)
        self.project.add_result(ocr.RecognitionResult(text="原文神𠮷한글。", words=[],
            source_path=str(self.source), source_sha256=self.project.source_sha256,
            page_number=1, image_width=80, image_height=60, engine_version="synthetic",
            languages="kor+chi_tra", psm=6, duration_seconds=0, engine="tesseract"))
        self.target = self.root / "project.json"
        self.project.save(self.target)
        self.old = self.target.read_bytes()
        self.project.set_text(1, "人校神𠮷한글。")
        self.replace = os.replace

    @staticmethod
    def lock(code=5):
        error = PermissionError("synthetic Windows sharing error")
        error.winerror = code
        return error

    def assert_no_pending_file(self):
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_transient_sharing_errors_retry_then_reopen_exact_text_and_history(self):
        errors = iter((5, 32, 33))
        def replace(source, target):
            code = next(errors, None)
            if code is not None:
                raise self.lock(code)
            return self.replace(source, target)
        with patch.object(comparison.os, "replace", side_effect=replace) as calls, patch.object(comparison.time, "sleep"):
            self.project.save(self.target)
        self.assertEqual(calls.call_count, 4)
        reopened = ComparisonProject.load(self.target)
        self.assertEqual(reopened.text(1), "人校神𠮷한글。")
        self.assertEqual(reopened.page(1)["runs"], self.project.page(1)["runs"])
        self.assertEqual(reopened.page(1)["edits"], self.project.page(1)["edits"])
        self.assertFalse(self.project.dirty)
        self.assert_no_pending_file()

    def test_competing_window_save_during_wait_is_not_overwritten(self):
        other = ComparisonProject.load(self.target)
        attempts = [0]
        other_bytes = []
        def replace(source, target):
            attempts[0] += 1
            if attempts[0] == 1:
                raise self.lock(32)
            return self.replace(source, target)
        def competing_save(_delay):
            other.set_text(1, "另一窗的校訂")
            other.save(self.target)
            other_bytes.append(self.target.read_bytes())
        with patch.object(comparison.os, "replace", side_effect=replace), patch.object(comparison.time, "sleep", side_effect=competing_save):
            with self.assertRaises(ComparisonError):
                self.project.save(self.target)
        self.assertEqual(self.target.read_bytes(), other_bytes[0])
        self.assertEqual(ComparisonProject.load(self.target).text(1), "另一窗的校訂")
        self.assertTrue(self.project.dirty)
        self.assert_no_pending_file()

    def test_source_change_during_wait_rejects_save_and_preserves_old_project(self):
        with patch.object(comparison.os, "replace", side_effect=self.lock(33)) as replace:
            with patch.object(comparison.time, "sleep", side_effect=lambda _delay: self.source.write_bytes(b"changed source")):
                with self.assertRaises(SourceChangedError):
                    self.project.save(self.target)
        replace.assert_called_once()
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertTrue(self.project.dirty)
        self.assert_no_pending_file()

    def test_protected_hard_link_created_during_wait_is_rejected(self):
        source = self.root / "protected.txt"
        target = self.root / "export.txt"
        source.write_bytes(b"immutable original")
        target.write_bytes(b"previous export")
        def alias(_delay):
            target.unlink()
            os.link(source, target)
        with patch.object(comparison.os, "replace", side_effect=self.lock(5)) as replace:
            with patch.object(comparison.time, "sleep", side_effect=alias):
                with self.assertRaises(ComparisonError):
                    comparison._atomic_write(target, b"must not overwrite", protected=iter((source,)))
        replace.assert_called_once()
        self.assertEqual(source.read_bytes(), b"immutable original")
        self.assertEqual(target.read_bytes(), b"immutable original")
        self.assert_no_pending_file()

    def test_persistent_access_error_is_bounded_and_old_file_remains_intact(self):
        with patch.object(comparison.os, "replace", side_effect=self.lock(5)) as replace:
            with patch.object(comparison.time, "sleep") as sleep:
                with self.assertRaises(PermissionError):
                    self.project.save(self.target)
        self.assertEqual(replace.call_count, 20)
        self.assertEqual(sleep.call_count, 19)
        self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 1.8)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertTrue(self.project.dirty)
        self.assert_no_pending_file()

    def test_unrelated_permission_error_is_not_retried(self):
        with patch.object(comparison.os, "replace", side_effect=self.lock(123)) as replace:
            with patch.object(comparison.time, "sleep") as sleep:
                with self.assertRaises(PermissionError):
                    self.project.save(self.target)
        replace.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assert_no_pending_file()

    def test_deadline_bounds_retry_even_if_access_error_persists(self):
        with patch.object(comparison.os, "replace", side_effect=self.lock(5)) as replace:
            with patch.object(comparison.time, "monotonic", side_effect=(0, 2)), patch.object(comparison.time, "sleep") as sleep:
                with self.assertRaises(PermissionError):
                    self.project.save(self.target)
        replace.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assert_no_pending_file()


if __name__ == "__main__":
    unittest.main()
