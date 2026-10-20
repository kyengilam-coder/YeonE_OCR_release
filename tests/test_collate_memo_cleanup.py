"""Memo cleanup preserves user work and publishes only a verified archive."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from lxml import etree

from yeone_ocr.collate.hwpx_edit import HP, paragraph_text
from yeone_ocr.collate.memo_cleanup import MemoCleanupError, clean_corrected_memos
from yeone_ocr.collate.section_edit import (
    _Archive, SectionEditSession, manual_history_path, manual_protected_paragraphs,
)
from yeone_ocr.grades import COLLATE_LEGEND, LEGEND
from yeone_ocr.hwpx_memo import MemoWriter, memo_field


class CorrectedMemoCleanup(unittest.TestCase):
    def setUp(self):
        from hwpx.document import HwpxDocument
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / "book.hwpx"
        self.target = self.folder / "book_교정.hwpx"
        self.pdf = self.folder / "book.pdf"
        self.pdf.write_bytes(b"synthetic source PDF")
        document = HwpxDocument.new()
        try:
            document.add_heading("瀉白散", level=1)
            style = document.styles.ensure_run(size=13, color="#1F4FBF")
            body = document.add_paragraph("Alpha beta.", style_id_ref=0,
                                          char_pr_id_ref=style, inherit_style=False)
            MemoWriter(document, author="Person").wrap_run(body.element.find(HP + "run"), "[확인 필요] Keep this user memo.")
            other = document.add_paragraph("Other body.", style_id_ref=0,
                                           char_pr_id_ref=style, inherit_style=False)
            MemoWriter(document).wrap_run(other.element.find(HP + "run"), LEGEND)
            document.add_heading("葦莖湯", level=1)
            document.save_to_path(str(self.source))
        finally:
            document.close()
        with zipfile.ZipFile(self.source, "a") as archive:
            archive.writestr("BinData/cleanup-test.bin", b"\x00preserved\xff")
        self.original_bytes = self.source.read_bytes()
        archive = _Archive(self.original_bytes)
        self.body = next(i for i, p in enumerate(archive.paragraphs) if paragraph_text(p) == "Alpha beta.")
        self.heading = next(i for i, p in enumerate(archive.paragraphs) if paragraph_text(p) == "瀉白散")
        self.end = next(i for i, p in enumerate(archive.paragraphs) if paragraph_text(p) == "葦莖湯")
        self.target.write_bytes(self.original_bytes)

    def add_app_memos(self):
        archive = _Archive(self.target.read_bytes())
        for index, number, text in (
                (self.heading, 30, COLLATE_LEGEND),
                (self.body, 31, "[자동 교정] 원래 글자"),
                (self.body, 32, "[제안] 빠진 글자")):
            run = archive.paragraphs[index].find(HP + "run")
            begin, end = memo_field(text, number, "YeonE_OCR", "1")
            run.insert(0, begin)
            run.append(end)
        archive.write(self.target, {name for name, _ in archive.locations})

    def session(self):
        return SectionEditSession.open(self.pdf, self.source, "瀉白散", self.heading, self.end)

    def assert_no_temps(self):
        self.assertFalse(list(self.folder.glob(".*.tmp")))

    def test_cleanup_preserves_body_styles_user_memos_history_and_package_members(self):
        self.add_app_memos()
        session = self.session()
        session.save({self.body: "Alpha better."})
        session.save({self.body: "Alpha best."})
        records = session.correction_records()
        before_bytes = self.target.read_bytes()
        history_bytes = manual_history_path(self.target).read_bytes()
        history_backup = manual_history_path(self.target).with_suffix(".bak.json")
        self.assertNotEqual(history_backup.read_bytes(), history_bytes)
        before = _Archive(before_bytes)
        result = clean_corrected_memos(self.source)
        self.assertEqual((result.removed, result.skipped), (3, 0))
        self.assertEqual(result.backup_path.read_bytes(), before_bytes)
        self.assertEqual(result.corrected_path, self.target)
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        self.assertEqual(manual_history_path(self.target).read_bytes(), history_bytes)
        self.assertEqual(history_backup.read_bytes(), history_bytes)
        self.assertEqual(manual_protected_paragraphs(self.target), {self.body})
        self.assertEqual(self.session().correction_records(), records)
        after = _Archive(self.target.read_bytes())
        self.assertEqual([paragraph_text(p) for p in before.paragraphs],
                         [paragraph_text(p) for p in after.paragraphs])
        for name, data in before.entries.items():
            if name not in before.roots:
                self.assertEqual(after.entries[name], data, name)
        remaining = [etree.tostring(f, encoding="utf-8") for p in after.paragraphs for f in p.iter(HP + "fieldBegin") if f.get("type") == "MEMO"]
        self.assertEqual(len(remaining), 2)
        self.assertTrue(any(b"Keep this user memo." in f for f in remaining))
        self.assertTrue(any(LEGEND.encode() in f for f in remaining))
        self.assert_no_temps()

    def test_no_app_memos_is_byte_identical_noop_without_backup(self):
        before = self.target.read_bytes(), self.target.stat().st_mtime_ns
        result = clean_corrected_memos(self.source)
        self.assertEqual((result.removed, result.skipped), (0, 0))
        self.assertIsNone(result.backup_path)
        self.assertEqual((self.target.read_bytes(), self.target.stat().st_mtime_ns), before)
        self.assertFalse(self.target.with_suffix(".bak.hwpx").exists())
        self.assert_no_temps()

    def test_leading_whitespace_in_owned_memo_is_supported(self):
        self.add_app_memos()
        archive = _Archive(self.target.read_bytes())
        for root in archive.roots.values():
            for begin in root.iter(HP + "fieldBegin"):
                author = begin.find(HP + "parameters/" + HP + "stringParam[@name='Author']")
                if author is not None and author.text == "YeonE_OCR":
                    text = begin.find(".//" + HP + "t")
                    text.text = " \t" + (text.text or "")
        archive.write(self.target, set(archive.roots))
        self.assertEqual(clean_corrected_memos(self.source).removed, 3)

    def test_broken_zip_is_reported_without_writing(self):
        self.target.write_bytes(b"not a ZIP document")
        with self.assertRaises(MemoCleanupError):
            clean_corrected_memos(self.source)
        self.assertEqual(self.target.read_bytes(), b"not a ZIP document")
        self.assertFalse(self.target.with_suffix(".bak.hwpx").exists())

    def test_missing_copy_and_selected_corrected_copy_are_rejected(self):
        self.target.unlink()
        with self.assertRaises(MemoCleanupError):
            clean_corrected_memos(self.source)
        self.assertFalse(self.target.exists())
        self.target.write_bytes(self.original_bytes)
        with self.assertRaises(MemoCleanupError):
            clean_corrected_memos(self.target)
        self.assertFalse((self.folder / "book_교정_교정.hwpx").exists())

    def test_hardlink_to_original_is_rejected(self):
        self.target.unlink()
        os.link(self.source, self.target)
        with self.assertRaises(MemoCleanupError):
            clean_corrected_memos(self.source)
        self.assertEqual(self.source.read_bytes(), self.original_bytes)

    def test_malformed_manual_history_stops_before_any_write(self):
        self.add_app_memos()
        before = self.target.read_bytes()
        manual_history_path(self.target).write_text("{}", encoding="utf-8")
        with self.assertRaises(MemoCleanupError):
            clean_corrected_memos(self.source)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(manual_history_path(self.target).read_text(), "{}")
        self.assertFalse(self.target.with_suffix(".bak.hwpx").exists())

    def test_external_change_to_protected_text_fails_closed(self):
        self.add_app_memos()
        self.session().save({self.body: "Alpha better."})
        archive = _Archive(self.target.read_bytes())
        archive.paragraphs[self.body].find(HP + "run/" + HP + "t").text = "Unrecorded external edit."
        archive.write(self.target, {name for name, _ in archive.locations})
        before = self.target.read_bytes()
        with self.assertRaises(MemoCleanupError):
            clean_corrected_memos(self.source)
        self.assertEqual(self.target.read_bytes(), before)

    def test_serialization_failure_preserves_copy_and_journal(self):
        self.add_app_memos()
        before = self.target.read_bytes()
        with patch.object(_Archive, "write", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(MemoCleanupError):
                clean_corrected_memos(self.source)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertFalse(self.target.with_suffix(".bak.hwpx").exists())
        self.assert_no_temps()

    def test_locked_copy_keeps_old_copy_and_valid_backup(self):
        self.add_app_memos()
        before = self.target.read_bytes()
        actual_replace = os.replace

        def fail_target(src, dst):
            if Path(dst) == self.target:
                raise PermissionError("synthetic file lock")
            return actual_replace(src, dst)

        with patch("yeone_ocr.collate.memo_cleanup.os.replace", side_effect=fail_target):
            with self.assertRaises(MemoCleanupError):
                clean_corrected_memos(self.source)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.target.with_suffix(".bak.hwpx").read_bytes(), before)
        self.assert_no_temps()

    def test_external_target_change_during_stage_is_not_overwritten(self):
        self.add_app_memos()
        external = self.original_bytes + b"external sentinel"
        actual_write = _Archive.write

        def race(archive, path, changed):
            actual_write(archive, path, changed)
            self.target.write_bytes(external)

        with patch.object(_Archive, "write", race):
            with self.assertRaisesRegex(MemoCleanupError, "외부에서 변경"):
                clean_corrected_memos(self.source)
        self.assertEqual(self.target.read_bytes(), external)
        self.assertFalse(self.target.with_suffix(".bak.hwpx").exists())
        self.assert_no_temps()

    def test_history_created_during_stage_is_preserved(self):
        self.add_app_memos()
        before = self.target.read_bytes()
        actual_write = _Archive.write

        def race(archive, path, changed):
            actual_write(archive, path, changed)
            manual_history_path(self.target).write_text("external journal", encoding="utf-8")

        with patch.object(_Archive, "write", race):
            with self.assertRaises(MemoCleanupError):
                clean_corrected_memos(self.source)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(manual_history_path(self.target).read_text(), "external journal")
        self.assert_no_temps()

    def test_source_change_after_backup_stops_final_replace(self):
        self.add_app_memos()
        before = self.target.read_bytes()
        actual_replace = os.replace
        backup = self.target.with_suffix(".bak.hwpx")

        def race(src, dst):
            actual_replace(src, dst)
            if Path(dst) == backup:
                self.source.write_bytes(self.original_bytes + b"external source")

        with patch("yeone_ocr.collate.memo_cleanup.os.replace", side_effect=race):
            with self.assertRaisesRegex(MemoCleanupError, "외부에서 변경"):
                clean_corrected_memos(self.source)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(backup.read_bytes(), before)
        self.assert_no_temps()

    def test_unexpected_body_mutation_in_cleanup_is_rejected(self):
        self.add_app_memos()
        before = self.target.read_bytes()
        from yeone_ocr.collate.memo_cleanup import remove_collate_memos

        def corrupt(book):
            result = remove_collate_memos(book)
            book.paragraphs[self.body].element.find(HP + "run/" + HP + "t").text = "unintended edit"
            return result

        with patch("yeone_ocr.collate.memo_cleanup.remove_collate_memos", side_effect=corrupt):
            with self.assertRaises(MemoCleanupError):
                clean_corrected_memos(self.source)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertFalse(self.target.with_suffix(".bak.hwpx").exists())
        self.assert_no_temps()


if __name__ == "__main__":
    unittest.main()
