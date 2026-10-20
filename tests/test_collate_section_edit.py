"""Manual section editing: XML preservation, atomic save and conflict checks."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from lxml import etree

from yeone_ocr.collate.hwpx_edit import HP, Editor, _slots, paragraph_text
from yeone_ocr.collate.section_edit import (
    ConflictError, ManualProtectionError, SectionEditError, SectionEditSession,
    _Archive, manual_history_path, manual_protected_paragraphs,
)
from yeone_ocr.collate.sections import HwpxBook


def make_source(path):
    from hwpx.document import HwpxDocument
    from yeone_ocr.hwpx_memo import MemoWriter
    doc = HwpxDocument.new()
    normal = doc.styles.ensure_run(size=11)
    bold = doc.styles.ensure_run(size=11, bold=True)
    doc.add_heading("瀉白散", level=2)
    doc.add_paragraph("Alpha beta gamma.", style_id_ref=0, char_pr_id_ref=normal, inherit_style=False)
    mixed = doc.add_paragraph("", style_id_ref=0, include_run=False, inherit_style=False)
    mixed.add_run("AB", char_pr_id_ref=normal)
    mixed.add_run("CD", char_pr_id_ref=bold)
    memo = doc.add_paragraph("Memo body text.", style_id_ref=0, char_pr_id_ref=normal, inherit_style=False)
    MemoWriter(doc, author="Person").wrap_run(memo.element.find(HP + "run"), "Keep this user comment.")
    special = doc.add_paragraph("Before", style_id_ref=0, char_pr_id_ref=normal, inherit_style=False)
    etree.SubElement(special.element.find(HP + "run").find(HP + "t"), HP + "tab").tail = "After"
    doc.add_heading("葦莖湯", level=2)
    doc.add_paragraph("Other section remains here.", style_id_ref=0, char_pr_id_ref=normal, inherit_style=False)
    doc.add_section()
    doc.add_paragraph("Second physical section.", style_id_ref=0, inherit_style=False)
    doc.save_to_path(str(path))
    doc.close()
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("BinData/manual-test.bin", b"\x00sentinel\xff")


class ManualSectionEditing(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "book.hwpx"
        self.pdf = self.root / "book.pdf"
        self.pdf.write_bytes(b"unchanged source PDF")
        make_source(self.source)
        self.corrected = self.root / "book_교정.hwpx"

    def open(self):
        return SectionEditSession.open(self.pdf, self.source, "瀉白散", 1, 6)

    def test_save_changes_only_selected_text_and_preserves_zip_members_and_comments(self):
        original = self.source.read_bytes()
        session = self.open()
        self.assertFalse(session.paragraphs[0].editable)  # heading
        self.assertFalse(session.paragraphs[-1].editable)  # tab atom
        before = _Archive(original)
        result = session.save({2: "Alpha better gamma.", 4: "Memo corrected text."})
        self.assertEqual(result.path, self.corrected)
        self.assertEqual(result.saved_count, 2)
        self.assertIsNone(result.backup_path)
        self.assertTrue(result.reports_stale)
        self.assertEqual(self.source.read_bytes(), original)
        saved = _Archive(self.corrected.read_bytes())
        self.assertEqual(paragraph_text(saved.paragraphs[2]), "Alpha better gamma.")
        self.assertEqual(paragraph_text(saved.paragraphs[4]), "Memo corrected text.")
        for name, data in before.entries.items():
            if name != "Contents/section0.xml":
                self.assertEqual(saved.entries[name], data, name)
        for index in (0, 1, 3, 5, 6, 7):
            self.assertEqual(etree.tostring(saved.paragraphs[index]), etree.tostring(before.paragraphs[index]))
        for before_control, after_control in zip(before.paragraphs[4].iter(HP + "ctrl"), saved.paragraphs[4].iter(HP + "ctrl")):
            self.assertEqual(etree.tostring(before_control), etree.tostring(after_control))
        self.assertEqual(manual_protected_paragraphs(self.corrected), {2, 4})
        self.assertEqual(next(p for p in session.paragraphs if p.index == 2).corrected_text, "Alpha better gamma.")

    def test_same_length_replacement_keeps_mixed_run_styles(self):
        session = self.open()
        before = _Archive(self.source.read_bytes())
        styles = [s.run.get("charPrIDRef") for s in _slots(before.paragraphs[3])]
        session.save({3: "WXYZ"})
        saved = _Archive(self.corrected.read_bytes())
        self.assertEqual([s.text for s in _slots(saved.paragraphs[3])], ["WX", "YZ"])
        self.assertEqual([s.run.get("charPrIDRef") for s in _slots(saved.paragraphs[3])], styles)

    def test_empty_paragraph_can_be_restored_without_recreating_its_runs(self):
        session = self.open()
        original = _Archive(self.source.read_bytes())
        attributes = [dict(s.run.attrib) for s in _slots(original.paragraphs[2])]
        session.save({2: ""})
        reopened = self.open()
        self.assertTrue(next(p.editable for p in reopened.paragraphs if p.index == 2))
        reopened.save({2: "Restored text."})
        saved = _Archive(self.corrected.read_bytes())
        self.assertEqual(paragraph_text(saved.paragraphs[2]), "Restored text.")
        self.assertEqual([dict(s.run.attrib) for s in _slots(saved.paragraphs[2])], attributes)

    def test_cross_style_pure_deletion_preserves_both_style_runs(self):
        original = _Archive(self.source.read_bytes())
        attributes = [dict(s.run.attrib) for s in _slots(original.paragraphs[3])]
        self.open().save({3: ""})
        saved = _Archive(self.corrected.read_bytes())
        self.assertEqual(paragraph_text(saved.paragraphs[3]), "")
        self.assertEqual([dict(s.run.attrib) for s in _slots(saved.paragraphs[3])], attributes)

    def test_ambiguous_cross_style_length_change_is_rejected_without_writing(self):
        with self.assertRaises(SectionEditError):
            self.open().save({3: "LONGER"})
        self.assertFalse(self.corrected.exists())
        self.assertFalse(manual_history_path(self.corrected).exists())

    def test_existing_corrected_copy_accumulates_edits_and_keeps_backup(self):
        session = self.open()
        session.save({2: "Alpha better gamma."})
        first = self.corrected.read_bytes()
        result = session.save({4: "Memo changed text."})
        self.assertEqual(result.backup_path.read_bytes(), first)
        self.assertEqual(manual_protected_paragraphs(self.corrected), {2, 4})
        reopened = self.open()
        self.assertEqual(next(p.corrected_text for p in reopened.paragraphs if p.index == 2), "Alpha better gamma.")
        history = json.loads(manual_history_path(self.corrected).read_text("utf-8"))
        self.assertEqual(set(history["paragraphs"]), {"2", "4"})
        self.assertTrue(manual_history_path(self.corrected).with_suffix(".bak.json").exists())

    def test_noop_does_not_create_output_or_change_existing_bytes(self):
        session = self.open()
        result = session.save({2: session.paragraphs[1].corrected_text})
        self.assertEqual(result.saved_count, 0)
        self.assertFalse(self.corrected.exists())
        session.save({2: "Alpha better gamma."})
        before = self.corrected.read_bytes(), manual_history_path(self.corrected).read_bytes()
        session.save({})
        self.assertEqual(before, (self.corrected.read_bytes(), manual_history_path(self.corrected).read_bytes()))

    def test_source_pdf_target_and_history_external_changes_conflict(self):
        session = self.open()
        self.pdf.write_bytes(b"externally replaced PDF")
        with self.assertRaises(ConflictError):
            session.save({2: "Changed"})
        self.pdf.write_bytes(b"unchanged source PDF")
        session = self.open()
        self.corrected.write_bytes(self.source.read_bytes())  # created after opening
        with self.assertRaises(ConflictError):
            session.save({2: "Changed"})
        session = self.open()
        self.source.write_bytes(self.source.read_bytes() + b"external")
        with self.assertRaises(ConflictError):
            session.save({2: "Changed"})
        session = self.open()
        manual_history_path(self.corrected).write_text("{}", encoding="utf-8")
        with self.assertRaises(ConflictError):
            session.save({2: "Changed"})

    def test_unselected_heading_atom_and_newline_edits_are_rejected(self):
        for change in ({7: "Other"}, {1: "Changed heading"}, {5: "Lost tab"}, {2: "First\nSecond"}):
            with self.subTest(change=change), self.assertRaises(SectionEditError):
                self.open().save(change)
        self.assertFalse(self.corrected.exists())

    def test_reordered_body_and_bad_scope_are_rejected(self):
        archive = _Archive(self.source.read_bytes())
        first, second = archive.paragraphs[2], archive.paragraphs[4]
        # Even repeated paragraph IDs cannot disguise unrelated body text.
        for slot in _slots(first):
            slot.text = "Completely unrelated replacement with no matching words"
        archive.write(self.corrected, {"Contents/section0.xml"})
        with self.assertRaises(SectionEditError):
            self.open()
        with self.assertRaises(SectionEditError):
            SectionEditSession.open(self.pdf, self.source, "瀉白散", 1, 7)
        with self.assertRaises(SectionEditError):
            SectionEditSession.open(self.pdf, self.corrected, "瀉白散", 1, 6)

    def test_renamed_bookmark_keeps_alignment_when_heading_and_position_agree(self):
        original = _Archive(self.source.read_bytes())
        run = original.paragraphs[6].find(HP + "run")
        etree.SubElement(run, HP + "bookmark", name="葦莖湯")
        original.write(self.source, {"Contents/section0.xml"})
        corrected = _Archive(self.source.read_bytes())
        bookmark = next(corrected.paragraphs[6].iter(HP + "bookmark"))
        bookmark.set("name", "위경탕")
        corrected.write(self.corrected, {"Contents/section0.xml"})
        session = self.open()
        self.assertEqual((session.start, session.end), (1, 6))
        session.save({2: "Alpha better gamma."})
        self.assertEqual(next(_Archive(self.corrected.read_bytes()).paragraphs[6].iter(HP + "bookmark")).get("name"), "위경탕")

    def test_failed_publish_keeps_document_and_previous_protection(self):
        session = self.open()
        session.save({2: "Alpha better gamma."})
        original = self.corrected.read_bytes()
        history = manual_history_path(self.corrected).read_bytes()
        real_replace = os.replace
        def fail_document(src, dst):
            if Path(dst) == self.corrected:
                raise PermissionError("simulated locked corrected document")
            return real_replace(src, dst)
        with patch("yeone_ocr.collate.section_edit.os.replace", side_effect=fail_document), self.assertRaises(PermissionError):
            session.save({4: "Memo changed text."})
        self.assertEqual(self.corrected.read_bytes(), original)
        self.assertEqual(manual_history_path(self.corrected).read_bytes(), history)
        self.assertEqual(manual_protected_paragraphs(self.corrected), {2})
        self.assertFalse(list(self.root.glob(".*.tmp")))

    def test_failed_document_and_metadata_rollback_leave_safe_durable_intent(self):
        session = self.open()
        session.save({2: "Alpha better gamma."})
        before = self.corrected.read_bytes()
        real_replace = os.replace
        history_publications = 0
        def fail_publish_and_rollback(src, dst):
            nonlocal history_publications
            if Path(dst) == self.corrected:
                raise PermissionError("document locked")
            if Path(dst) == manual_history_path(self.corrected):
                history_publications += 1
                if history_publications > 1:
                    raise PermissionError("history rollback locked")
            return real_replace(src, dst)
        with patch("yeone_ocr.collate.section_edit.os.replace", side_effect=fail_publish_and_rollback), self.assertRaises(PermissionError):
            session.save({4: "Memo changed text."})
        self.assertEqual(self.corrected.read_bytes(), before)
        # Tentative paragraph 4 is conservatively protected in its old state.
        self.assertEqual(manual_protected_paragraphs(self.corrected), {2, 4})
        reopened = self.open()
        self.assertEqual(next(p.corrected_text for p in reopened.paragraphs if p.index == 4), "Memo body text.")
        self.assertFalse(list(self.root.glob(".*.tmp")))

    def test_history_intent_is_durable_before_document_replacement(self):
        session = self.open()
        real_replace = os.replace
        seen = []
        def verify_intent(src, dst):
            if Path(dst) == self.corrected:
                history = json.loads(manual_history_path(self.corrected).read_text("utf-8"))
                self.assertEqual(set(history["paragraphs"]), {"2"})
                self.assertEqual(len(history["paragraphs"]["2"]["text_hashes"]), 2)
                seen.append(True)
            return real_replace(src, dst)
        with patch("yeone_ocr.collate.section_edit.os.replace", side_effect=verify_intent):
            session.save({2: "Alpha better gamma."})
        self.assertEqual(seen, [True])

    def test_last_moment_external_save_is_detected_after_intent_publication(self):
        session = self.open()
        session.save({2: "Alpha better gamma."})
        before_history = manual_history_path(self.corrected).read_bytes()
        external = self.corrected.read_bytes() + b"external-save"
        real_replace = os.replace
        def external_save(src, dst):
            result = real_replace(src, dst)
            if Path(dst) == manual_history_path(self.corrected):
                self.corrected.write_bytes(external)
            return result
        with patch("yeone_ocr.collate.section_edit.os.replace", side_effect=external_save), self.assertRaises(ConflictError):
            session.save({4: "Memo changed text."})
        self.assertEqual(self.corrected.read_bytes(), external)
        self.assertEqual(manual_history_path(self.corrected).read_bytes(), before_history)

    def test_malformed_or_drifted_history_blocks_protection_instead_of_ignoring_it(self):
        self.open().save({2: "Alpha better gamma."})
        path = manual_history_path(self.corrected)
        history = path.read_bytes()
        path.write_text("broken", encoding="utf-8")
        with self.assertRaises(ManualProtectionError):
            manual_protected_paragraphs(self.corrected)
        path.write_bytes(history)
        archive = _Archive(self.corrected.read_bytes())
        _slots(archive.paragraphs[2])[0].text = "Externally altered protected text."
        archive.write(self.corrected, {"Contents/section0.xml"})
        with self.assertRaises(ManualProtectionError):
            manual_protected_paragraphs(self.corrected)

    def test_plain_editor_serialization_keeps_manual_history_valid(self):
        self.open().save({2: "Alpha better gamma."})
        book = HwpxBook(str(self.corrected))
        try:
            Editor(book).save_copy(str(self.corrected))
        finally:
            book.doc.close()
        self.assertEqual(manual_protected_paragraphs(self.corrected), {2})


if __name__ == "__main__":
    unittest.main()
