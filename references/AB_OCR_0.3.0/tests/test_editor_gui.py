"""Editor integration checks: source identity, correction preservation, checkpoints."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from ab_ocr.document import DocumentProject
from ab_ocr.editor_gui import EditorWindow
from ab_ocr.ocr import OCRCancelled, RecognitionResult


class EditorTests(unittest.TestCase):
    def setUp(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is required for the source preview")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "source.png"
        self.source.write_bytes(b"unchanged test scan")
        self.project = DocumentProject(self.source, hashlib.sha256(self.source.read_bytes()).hexdigest(), 2, "image")
        renderer = patch("ab_ocr.ocr.render_page", side_effect=lambda *a, **k: Image.new("RGB", (120, 180), "white"), create=True)
        self.renderer = renderer.start()
        self.addCleanup(renderer.stop)
        try:
            self.app = EditorWindow(recovery_dir=self.folder / "recovery")
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.app.withdraw()
        self.addCleanup(lambda: self.app._close(force=True))
        self.errors = []
        error_patch = patch("ab_ocr.editor_gui.messagebox.showerror", side_effect=lambda *a, **k: self.errors.append(a))
        error_patch.start()
        self.addCleanup(error_patch.stop)
        self.app._set_project(self.project)
        self.pump_until(lambda: self.app._preview is not None)

    def pump_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            if self.app._closed:
                break
            self.app.update()
            time.sleep(.01)
        self.assertTrue(predicate(), "Editor operation did not finish")

    def wait_work(self):
        self.pump_until(lambda: not self.app._busy and not self.app._recovery_running and not self.app._recovery_pending)

    def result(self, number=1, text="天地漢字\n한글 원문"):
        return RecognitionResult(text=text, words=[{"text": "天地", "bbox": [1, 2, 40, 20], "confidence": 61.2}],
                                 source_path=str(self.source), source_sha256=self.project.source_sha256,
                                 page_number=number, image_width=120, image_height=180,
                                 engine_version="test", languages="kor+chi_tra", psm=3, duration_seconds=.1)

    def test_page_change_flushes_just_entered_text_without_waiting_for_tk_event(self):
        original = self.result()
        self.project.apply_recognition(original)
        self.app._populate_text()
        self.app.text.delete("1.0", "end")
        self.app.text.insert("1.0", "사람의 교정文\r\n")
        self.app.show_page(2)  # No update(): <<Modified>> has not been dispatched.
        self.assertEqual(self.project.text(1), "사람의 교정文\r\n")
        self.assertEqual(self.project.page(1).raw_text, original.text)
        self.assertEqual(self.app.page_number, 2)
        self.pump_until(lambda: self.app._preview is not None)

    def test_rerecognition_retains_pending_correction_and_caret(self):
        self.project.apply_recognition(self.result())
        self.app._populate_text()
        self.app.text.delete("1.0", "end")
        self.app.text.insert("1.0", "교정된 글")
        self.app.text.mark_set("insert", "1.2")
        self.app._apply_ocr_page(self.result(text="새 OCR"))
        self.assertEqual(self.project.text(1), "교정된 글")
        self.assertEqual(self.project.page(1).raw_text, "새 OCR")
        self.assertEqual(self.app.text.index("insert"), "1.2")
        self.wait_work()

    def test_cancelled_batch_keeps_completed_page_on_disk(self):
        second_started = threading.Event()
        def recognize(_source, *, page_number, should_stop, **_kwargs):
            if page_number == 1:
                return self.result(1)
            second_started.set()
            deadline = time.monotonic() + 3
            while not should_stop() and time.monotonic() < deadline:
                time.sleep(.01)
            raise OCRCancelled("중지")
        with patch("ab_ocr.ocr.recognize_file", side_effect=recognize):
            self.app.recognize(True)
            self.pump_until(lambda: self.project.page(1).raw is not None and second_started.is_set())
            self.app.cancel()
            self.wait_work()
        self.assertIsNotNone(self.project.page(1).raw)
        self.assertIsNone(self.project.page(2).raw)
        snapshot = json.loads(self.app._recovery_path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["pages"]["1"]["raw"]["text"], self.result().text)
        self.assertTrue(self.project.dirty)
        self.assertIn("중지됨", self.app.status.get())

    def test_selected_language_and_resolution_reach_engine_and_restore(self):
        from dataclasses import replace
        self.app.language.set("chi_tra")
        self.app.dpi.set(450)
        self.app.psm.set("가로쓰기 단일 구역")
        def recognize(_source, **options):
            self.assertEqual((options["languages"], options["psm"], options["dpi"]), ("chi_tra", 6, 450))
            return replace(self.result(), languages=options["languages"], psm=options["psm"],
                           preprocessing={"input_type": "pdf", "render_dpi": options["dpi"]})
        with patch("ab_ocr.ocr.recognize_file", side_effect=recognize) as engine:
            self.app.recognize(False)
            self.wait_work()
        engine.assert_called_once()
        self.assertIsNotNone(self.project.page(1).raw)
        self.app.dpi.set(300)
        self.app.language.set("kor")
        self.app._set_project(self.project)
        self.assertEqual(self.app.dpi.get(), 450)

    def test_selected_local_engine_reaches_worker_and_reopens_without_psm(self):
        from dataclasses import replace
        self.app.engine.set("easyocr")
        self.app._engine_changed()
        self.app.language.set("chi_tra")
        self.assertEqual(str(self.app.psm_box.cget("state")), "disabled")
        self.assertEqual(str(self.app.language_box.cget("state")), "readonly")
        def recognize(_source, **options):
            self.assertEqual(options["engine"], "easyocr")
            self.assertEqual(options["languages"], "chi_tra")
            self.assertIsNone(options["psm"])
            return replace(self.result(), engine="easyocr", languages="chi_tra", psm=None)
        with patch("ab_ocr.ocr.recognize_file", side_effect=recognize):
            self.app.recognize()
            self.wait_work()
        self.app.engine.set("tesseract")
        self.app._set_project(self.project)
        self.assertEqual(self.app.engine.get(), "easyocr")
        self.assertEqual(self.app.language.get(), "chi_tra")
        self.assertEqual(str(self.app.psm_box.cget("state")), "disabled")
        self.assertEqual(self.app.language.get(), "chi_tra")

    def test_preview_rejects_a_source_changed_since_project_creation(self):
        self.project.apply_recognition(self.result())
        self.source.write_bytes(b"changed source")
        previous_calls = self.renderer.call_count
        self.app._request_render()
        self.pump_until(lambda: any("원본 표시 실패" in str(self.app.canvas.itemcget(item, "text"))
                                   for item in self.app.canvas.find_all() if self.app.canvas.type(item) == "text"))
        self.assertEqual(self.renderer.call_count, previous_calls)
        self.assertIsNone(self.app._preview)
        self.assertEqual(self.project.page(1).raw_text, self.result().text)

    def test_stale_preview_is_closed_without_replacing_current_image(self):
        from PIL import Image
        stale = Image.new("RGB", (3, 3), "red")
        current = self.app._preview
        self.app._events.put(("render", self.app._generation - 1, self.app._render_id, stale))
        self.pump_until(lambda: self.app._events.empty())
        self.assertIs(self.app._preview, current)
        with self.assertRaises(ValueError):
            stale.getpixel((0, 0))

    def test_completed_scope_exports_partial_document(self):
        self.project.apply_recognition(self.result())
        self.app._populate_text()
        self.app.text.delete("1.0", "end")
        self.app.text.insert("1.0", "교정본")
        destination = self.folder / "partial.txt"
        self.app.export_text(destination, scope="complete")
        self.wait_work()
        self.assertEqual(destination.read_text(encoding="utf-8"), "교정본")
        self.assertEqual(self.source.read_bytes(), b"unchanged test scan")

    def test_restore_cancel_preserves_correction(self):
        self.project.apply_recognition(self.result())
        self.project.set_text(1, "손으로 교정")
        self.app._populate_text()
        with patch("ab_ocr.editor_gui.messagebox.askyesno", return_value=False):
            self.app.restore_raw()
        self.assertEqual(self.project.text(1), "손으로 교정")
        self.assertEqual(self.app.text.get("1.0", "end-1c"), "손으로 교정")

    def test_close_waits_for_project_write_to_finish(self):
        self.project.apply_recognition(self.result())
        release = threading.Event()
        actual_save = self.project.save
        def delayed(path):
            release.wait(2)
            return actual_save(path)
        destination = self.folder / "saved.abocr.json"
        with patch.object(self.project, "save", side_effect=delayed):
            self.app.save_project(destination)
            self.app._close()
            self.assertFalse(self.app._closed)
            release.set()
            self.pump_until(lambda: self.app._closed)
        self.assertTrue(destination.is_file())
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["pages"]["1"]["raw"]["text"], self.result().text)

    def test_final_recovery_locks_edits_and_ignores_stale_completion(self):
        self.project.set_text(1, "마지막 교정문")
        self.app._populate_text()
        release, entered = threading.Event(), threading.Event()
        actual = self.project.save_recovery
        def delayed(path):
            entered.set()
            release.wait(3)
            return actual(path)
        with patch.object(self.project, "save_recovery", side_effect=delayed), \
                patch("ab_ocr.editor_gui.messagebox.askyesnocancel", return_value=False):
            self.app._close()
            self.pump_until(entered.is_set)
            self.assertEqual(str(self.app.text.cget("state")), "disabled")
            self.app.open_source(self.folder / "new.png")
            self.assertIs(self.app.project, self.project)
            self.app._events.put(("recovery_done", self.app._generation - 1, 0, None))
            self.pump_until(lambda: self.app._events.empty())
            self.assertFalse(self.app._closed)
            self.assertTrue(self.app._recovery_running)
            release.set()
            self.pump_until(lambda: self.app._closed)
        saved = json.loads(self.app._recovery_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["pages"]["1"]["corrected_text"], "마지막 교정문")

    def test_document_switch_flushes_latest_pending_old_checkpoint(self):
        self.project.apply_recognition(self.result(1))
        old_recovery = self.app._recovery_path
        actual = self.project.save_recovery
        release, first_saved = threading.Event(), threading.Event()
        calls = []
        def delayed(path):
            destination = actual(path)
            calls.append(path)
            if len(calls) == 1:
                first_saved.set()
                release.wait(3)
            return destination
        next_source = self.folder / "next.png"
        next_source.write_bytes(b"next source")
        next_project = DocumentProject(next_source, hashlib.sha256(next_source.read_bytes()).hexdigest(), 1, "image")
        with patch.object(self.project, "save_recovery", side_effect=delayed), \
                patch("ab_ocr.editor_gui.messagebox.askyesno", return_value=True), \
                patch("ab_ocr.document.DocumentProject.open", return_value=next_project):
            self.app._request_recovery()
            self.pump_until(first_saved.is_set)
            self.project.apply_recognition(self.result(2, "둘째 쪽 완료"))
            self.app._request_recovery()
            self.app.open_source(next_source)
            self.assertIs(self.app.project, self.project)
            self.assertEqual(str(self.app.text.cget("state")), "disabled")
            release.set()
            self.pump_until(lambda: self.app.project is next_project and not self.app._busy)
        saved = json.loads(old_recovery.read_text(encoding="utf-8"))
        self.assertEqual(saved["pages"]["2"]["raw"]["text"], "둘째 쪽 완료")
        self.assertGreaterEqual(len(calls), 2)
        self.pump_until(lambda: self.app._preview is not None)

    def test_review_commits_pending_typing_before_marking_verified(self):
        self.app.text.insert("1.0", "대조한 글")
        self.app.reviewed.set(True)
        self.app._review_changed()
        self.assertEqual(self.project.text(1), "대조한 글")
        self.assertTrue(self.project.page(1).reviewed)

    def test_rejected_recognition_keeps_page_error_and_recovery_evidence(self):
        raw = self.result().to_dict()
        raw["source_sha256"] = "0" * 64
        self.app._events.put(("ocr_page", self.app._generation, self.app._job_id, raw))
        self.pump_until(lambda: bool(self.project.page(1).last_error))
        self.wait_work()
        self.assertIsNone(self.project.page(1).raw)
        self.assertIn("원본", self.project.page(1).last_error)
        saved = json.loads(self.app._recovery_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["pages"]["1"]["last_error"], self.project.page(1).last_error)


if __name__ == "__main__":
    unittest.main()
