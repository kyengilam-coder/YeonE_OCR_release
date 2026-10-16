from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ab_ocr import document
from ab_ocr.document import DocumentProject, ProjectError, SourceChangedError
from ab_ocr.ocr import RecognitionResult


class DocumentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = self.root / "원본.png"
        self.source.write_bytes(b"original source content")
        def info(path):
            path = Path(path)
            return {"source_path": str(path.resolve()), "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "page_count": 3, "kind": "image"}
        metadata = patch.object(document.ocr, "document_info", side_effect=info, create=True)
        metadata.start()
        self.addCleanup(metadata.stop)
        self.project = DocumentProject.open(self.source)

    def result(self, number=1, text="原文\r\n天地樂𠮷\U000e0100\n"):
        return RecognitionResult(text=text, words=[{"text": "天地", "bbox": [1, 2, 40, 20], "confidence": 71.2}],
                                 source_path=str(self.source), source_sha256=self.project.source_sha256,
                                 page_number=number, image_width=120, image_height=180,
                                 engine_version="test engine", languages="kor+chi_tra", psm=3,
                                 duration_seconds=0.1)

    def test_round_trip_preserves_raw_correction_unicode_newlines_and_source_bytes(self):
        original = self.source.read_bytes()
        raw = self.result()
        corrected = "교정\r\n\u1100\u1161樂𠮷\U000e0100\r끝\n"
        self.project.apply_recognition(raw)
        self.project.set_text(1, corrected)
        self.project.set_reviewed(1, True)
        target = self.root / "문헌.abocr.json"
        self.assertTrue(self.project.dirty)
        self.project.save(target)
        self.assertFalse(self.project.dirty)
        reopened = DocumentProject.load(target)
        self.assertEqual(reopened.page(1).raw_text, raw.text)
        self.assertEqual(reopened.text(1), corrected)
        self.assertTrue(reopened.page(1).reviewed)
        self.assertEqual(reopened.completed_pages, [1])
        self.assertFalse(reopened.dirty)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(reopened.page(2).status, "미처리")

    def test_failed_page_and_rerecognition_preserve_successful_work_and_raw_history(self):
        self.project.apply_recognition(self.result(1))
        self.project.set_text(1, "사람 교정문")
        self.project.record_error(1, "재인식 취소")
        self.project.record_error(2, "실행 제한 시간")
        self.assertEqual(self.project.completed_pages, [1])
        self.assertEqual(self.project.text(1), "사람 교정문")
        self.assertIn("보존", self.project.page(1).status)
        self.project.apply_recognition(self.result(1, "새 인식문"))
        self.assertEqual(self.project.text(1), "사람 교정문")
        self.assertEqual(self.project.page(1).previous_recognitions[0]["text"], self.result().text)
        self.assertEqual(self.project.page(1).last_error, "")
        self.project.save(self.root / "partial.abocr.json")
        reopened = DocumentProject.load(self.root / "partial.abocr.json")
        self.assertEqual(reopened.page(2).last_error, "실행 제한 시간")
        self.assertEqual(reopened.text(1), "사람 교정문")

    def test_restore_raw_is_explicit_and_review_status_invalidates_on_edit(self):
        self.project.apply_recognition(self.result())
        self.project.set_reviewed(1, True)
        self.project.set_text(1, "수정")
        self.assertFalse(self.project.page(1).reviewed)
        self.assertTrue(self.project.has_corrections(1))
        self.project.restore_raw(1)
        self.assertEqual(self.project.text(1), self.result().text)
        self.assertFalse(self.project.has_corrections(1))
        self.project.set_text(1, "수정")
        self.project.set_text(1, self.result().text)
        self.assertFalse(self.project.has_corrections(1))
        with self.assertRaises(ProjectError):
            self.project.restore_raw(2)

    def test_source_change_blocks_load_and_save_without_damaging_last_save(self):
        target = self.root / "keep.abocr.json"
        self.project.apply_recognition(self.result())
        self.project.save(target)
        before = target.read_bytes()
        self.project.set_text(1, "new correction")
        self.source.write_bytes(b"different source")
        for operation in (lambda: self.project.save(), lambda: DocumentProject.load(target), self.project.validate_source):
            with self.assertRaises(SourceChangedError):
                operation()
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(self.project.dirty)

    def test_same_content_source_can_be_relocated_without_losing_raw_provenance(self):
        target = self.root / "move.abocr.json"
        self.project.apply_recognition(self.result())
        self.project.save(target)
        relocated = self.root / "새 위치.png"
        self.source.rename(relocated)
        reopened = DocumentProject.load(target, source_path=relocated)
        self.assertEqual(reopened.source_path, relocated.resolve())
        self.assertEqual(reopened.page(1).raw["source_path"], str(self.source))
        reopened.save()
        self.assertEqual(DocumentProject.load(target).source_path, relocated.resolve())

    def test_atomic_replace_failure_keeps_previous_project_and_dirty_state(self):
        target = self.root / "atomic.abocr.json"
        self.project.apply_recognition(self.result())
        self.project.save(target)
        original = target.read_bytes()
        self.project.set_text(1, "아직 저장하지 못한 교정")
        with patch("ab_ocr.reports.os.replace", side_effect=PermissionError("test write failure")):
            with self.assertRaises(PermissionError):
                self.project.save()
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue(self.project.dirty)
        self.assertEqual(DocumentProject.load(target).text(1), self.result().text)
        self.assertEqual(list(self.root.glob(".abocr-*.tmp")), [])

    def test_recovery_reopens_completed_pages_without_marking_user_project_saved(self):
        self.project.apply_recognition(self.result(1))
        self.project.set_text(1, "사람 교정\r\n")
        self.project.record_error(2, "중단")
        recovery = self.root / "recovery" / "checkpoint.abocr.json"
        self.project.save_recovery(recovery)
        self.assertTrue(self.project.dirty)
        self.assertIsNone(self.project.project_path)
        reopened = DocumentProject.load(recovery)
        self.assertEqual(reopened.completed_pages, [1])
        self.assertEqual(reopened.text(1), "사람 교정\r\n")
        self.assertEqual(reopened.page(2).last_error, "중단")
        self.project.apply_recognition(self.result(2, "둘째 쪽"))
        self.project.save_recovery(recovery)
        self.assertEqual(DocumentProject.load(recovery).completed_pages, [1, 2])

    def test_recovery_preserves_work_when_source_disappears_then_allows_verified_relink(self):
        self.project.apply_recognition(self.result())
        self.project.set_text(1, "원본이 끊겨도 보존할 교정\r\n")
        relocated = self.root / "moved.png"
        self.source.rename(relocated)
        recovery = self.root / "missing-source.abocr.json"
        self.project.save_recovery(recovery)
        self.assertTrue(self.project.dirty)
        with self.assertRaises(SourceChangedError):
            DocumentProject.load(recovery)
        reopened = DocumentProject.load(recovery, source_path=relocated)
        self.assertEqual(reopened.text(1), "원본이 끊겨도 보존할 교정\r\n")
        self.assertEqual(reopened.page(1).raw_text, self.result().text)

    def test_recovery_preserves_original_fingerprint_after_external_source_replacement(self):
        self.project.apply_recognition(self.result())
        original_hash = self.project.source_sha256
        self.source.write_bytes(b"different, invalid image")
        target = self.root / "changed-source.abocr.json"
        self.project.save_recovery(target)
        payload = json.loads(target.read_bytes())
        self.assertEqual(payload["source"]["sha256"], original_hash)
        self.assertEqual(payload["pages"]["1"]["raw"]["text"], self.result().text)
        with self.assertRaises(SourceChangedError):
            DocumentProject.load(target)

    def test_save_snapshot_does_not_clear_edits_made_during_disk_write(self):
        self.project.apply_recognition(self.result())
        original_write = document.atomic_write
        def concurrent_write(*args, **kwargs):
            self.project.set_text(1, "저장 도중 새 수정")
            return original_write(*args, **kwargs)
        target = self.root / "snapshot.abocr.json"
        with patch.object(document, "atomic_write", side_effect=concurrent_write):
            self.project.save(target)
        self.assertTrue(self.project.dirty)
        self.assertEqual(self.project.text(1), "저장 도중 새 수정")
        self.assertEqual(DocumentProject.load(target).text(1), self.result().text)

    def test_detached_snapshots_and_input_dicts_cannot_mutate_project(self):
        raw = self.result().to_dict()
        self.project.apply_recognition(raw)
        raw["text"] = "outside change"
        self.project.page(1).raw["text"] = "outside page change"
        self.project.to_dict()["pages"]["1"]["raw"]["text"] = "outside snapshot change"
        self.assertEqual(self.project.text(1), self.result().text)

    def test_corrupt_or_future_project_is_never_reset_or_overwritten(self):
        target = self.root / "broken.abocr.json"
        target.write_bytes(b'{"format":')
        for operation in (lambda: DocumentProject.load(target), lambda: self.project.save(target)):
            with self.assertRaises(ProjectError):
                operation()
        self.assertEqual(target.read_bytes(), b'{"format":')
        payload = self.project.to_dict()
        payload["version"] = 999
        target.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ProjectError):
            DocumentProject.load(target)

    def test_duplicate_json_keys_and_nonfinite_values_are_rejected_without_repairing_file(self):
        target = self.root / "duplicates.abocr.json"
        for content in ('{"format":"ab-ocr-document","version":1,"version":1}',
                        '{"format":"ab-ocr-document","version":1,"pages":{"1":{},"1":{}}}',
                        '{"format":"ab-ocr-document","version":1,"x":NaN}'):
            target.write_text(content, encoding="utf-8")
            with self.assertRaises(ProjectError):
                DocumentProject.load(target)
            self.assertEqual(target.read_text(encoding="utf-8"), content)

    def test_external_project_change_is_detected_before_overwrite(self):
        target = self.root / "shared.abocr.json"
        self.project.apply_recognition(self.result())
        self.project.save(target)
        other = DocumentProject.load(target)
        other.set_text(1, "다른 창의 교정")
        other.save()
        self.project.set_text(1, "이 창의 교정")
        with self.assertRaisesRegex(ProjectError, "다른 창"):
            self.project.save()
        self.assertEqual(DocumentProject.load(target).text(1), "다른 창의 교정")

    def test_invalid_or_foreign_recognition_is_rejected_without_changing_existing_result(self):
        self.project.apply_recognition(self.result())
        for key, value in (("source_sha256", "0" * 64), ("page_number", 0), ("image_width", -1), ("words", [])):
            bad = self.result().to_dict()
            bad[key] = value
            if key == "words":
                bad["words"] = [{"text": "x", "bbox": [0, 0, 999, 2]}]
            with self.assertRaises(ProjectError):
                self.project.apply_recognition(bad)
        self.assertEqual(self.project.text(1), self.result().text)
        self.assertEqual(self.project.page(1).previous_recognitions, [])

    def test_txt_export_preserves_each_page_exactly_and_rejects_unread_pages(self):
        first, second = "樂\r\n𠮷\n", "\r둘째\r\n"
        self.project.apply_recognition(self.result(1, first))
        self.project.apply_recognition(self.result(2, second))
        self.project.set_text(1, "교정\r\n")
        target = self.root / "원시.txt"
        self.project.export_text(target, corrected=False, page_numbers=[1, 2])
        self.assertEqual(target.read_bytes().decode("utf-8"), first + "\n\f\n" + second)
        self.project.export_text(self.root / "교정.txt", page_numbers=[1])
        self.assertEqual((self.root / "교정.txt").read_bytes().decode("utf-8"), "교정\r\n")
        with self.assertRaisesRegex(ProjectError, "미처리"):
            self.project.export_text(self.root / "incomplete.txt")
        with self.assertRaises(ProjectError):
            self.project.export_text(target, page_numbers=[1, 1])

    def test_protects_original_and_other_projects_and_recovery_destination(self):
        self.project.apply_recognition(self.result())
        target = self.root / "saved.abocr.json"
        self.project.save(target)
        with self.assertRaises(ProjectError):
            self.project.save_recovery(target)
        other = DocumentProject.open(self.source)
        with self.assertRaises(ProjectError):
            other.save(target)
        with self.assertRaises(ProjectError):
            self.project.save(self.source)
        alias = self.root / "original.abocr.json"
        try:
            alias.hardlink_to(self.source)
        except OSError:
            return
        with self.assertRaises(ProjectError):
            self.project.save(alias)
        self.assertEqual(self.source.read_bytes(), b"original source content")


if __name__ == "__main__":
    unittest.main()
