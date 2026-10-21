"""Manual section editing: XML preservation, atomic save and conflict checks."""
from copy import deepcopy
from dataclasses import replace
import gc
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import weakref

from lxml import etree

from yeone_ocr.collate.hwpx_edit import HP, HH, Editor, _slots, _tag_of, paragraph_text
from yeone_ocr.collate.section_edit import (
    ConflictError, ManualProtectionError, SaveAccessError, SectionEditError, SectionEditSession,
    TextStyleSpan, _Archive, _style_key, manual_history_path, manual_protected_paragraphs,
    _replace_text,
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

    def mark_copy(self, index=2, grade="red", *, transform=None):
        book = HwpxBook(str(self.source))
        try:
            editor = Editor(book, memo="none")
            for slot in _slots(book.paragraphs[index].element):
                pid = editor._style_for(slot.run.get("charPrIDRef"), grade)
                if transform:
                    pid = transform(editor, pid)
                slot.run.set("charPrIDRef", pid)
            book.paragraphs[index].section.mark_dirty()
            editor.save_copy(str(self.corrected))
        finally:
            book.doc.close()

    def styles_at(self, path, index=2):
        archive = _Archive(path.read_bytes())
        header = etree.fromstring(archive.entries["Contents/header.xml"])
        styles = {style.get("id"): style for style in header.iter(HH + "charPr")}
        return [styles[slot.run.get("charPrIDRef")] for slot in _slots(archive.paragraphs[index])
                for _ in slot.text]

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

    def test_display_spans_include_actual_ocr_colors_even_when_text_is_unchanged(self):
        expected = {
            "green": ("#0B7A3B", "", False, True, "green"),
            "blue": ("#1F4FBF", "#E1EBFF", False, True, "blue"),
            "red": ("#C00000", "#FFFF00", True, False, "red"),
            "red_ctx": ("#C00000", "#FFFF00", True, True, "red"),
        }
        source = self.source.read_bytes()
        for grade, style in expected.items():
            with self.subTest(grade=grade):
                self.mark_copy(grade=grade)
                corrected = self.corrected.read_bytes()
                row = next(p for p in self.open().paragraphs if p.index == 2)
                self.assertEqual(row.corrected_text, row.original_text)
                self.assertEqual(row.corrected_styles, (TextStyleSpan(0, len(row.corrected_text), *style),))
                self.assertEqual(self.corrected.read_bytes(), corrected)
                self.assertEqual(self.source.read_bytes(), source)
                self.assertFalse(manual_history_path(self.corrected).exists())

    def test_display_spans_refresh_after_manual_color_cleanup_and_reopen(self):
        self.mark_copy(grade="blue")
        session = self.open()
        session.save({2: "Alpha zeta gamma."})
        row = next(p for p in session.paragraphs if p.index == 2)
        self.assertNotEqual(row.original_text, row.corrected_text)
        blue = ("#1F4FBF", "#E1EBFF", False, True, "blue")
        self.assertEqual(row.corrected_styles, (
            TextStyleSpan(0, 6, *blue), TextStyleSpan(6, 7, "#000000"),
            TextStyleSpan(7, len(row.corrected_text), *blue),
        ))
        self.assertEqual(next(p for p in self.open().paragraphs if p.index == 2).corrected_styles,
                         row.corrected_styles)

    def test_display_spans_keep_author_red_white_background_and_bold_underline(self):
        source = _Archive(self.source.read_bytes())
        header = etree.fromstring(source.entries["Contents/header.xml"])
        pid = _slots(source.paragraphs[2])[0].run.get("charPrIDRef")
        style = next(s for s in header.iter(HH + "charPr") if s.get("id") == pid)
        style.set("textColor", "#c00000")
        style.set("shadeColor", "#ffffff")
        style.find(HH + "strikeout").set("color", "#000000")
        style.find(HH + "underline").set("type", "BOTTOM")
        etree.SubElement(style, HH + "bold")
        source.entries["Contents/header.xml"] = etree.tostring(header, encoding="utf-8", xml_declaration=True)
        source.write(self.source, set())
        before = self.source.read_bytes()
        row = next(p for p in self.open().paragraphs if p.index == 2)
        self.assertEqual(row.corrected_styles,
                         (TextStyleSpan(0, len(row.corrected_text), "#C00000", "#FFFFFF", True, True),))
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(self.corrected.exists())

    def test_display_offsets_include_atoms_tails_and_cross_run_markpen_but_not_memos(self):
        source = _Archive(self.source.read_bytes())
        paragraph = source.paragraphs[4]
        run = paragraph.find(HP + "run")
        text = run.find(HP + "t")
        text.text = "甲😀"
        etree.SubElement(text, HP + "markpenBegin", color="#aabbcc").tail = "乙"
        etree.SubElement(text, HP + "tab").tail = "丙"
        etree.SubElement(text, HP + "lineBreak").tail = "丁"
        etree.SubElement(run, HP + "tab")
        etree.SubElement(run, HP + "ctrl", id="tab")
        bold_id = _slots(source.paragraphs[3])[-1].run.get("charPrIDRef")
        other_run = etree.SubElement(paragraph, HP + "run", charPrIDRef=bold_id)
        other_text = etree.SubElement(other_run, HP + "t")
        other_text.text = "己"
        etree.SubElement(other_text, HP + "markpenEnd").tail = "庚"
        source.write(self.source, {"Contents/section0.xml"})
        before = self.source.read_bytes()
        row = next(p for p in self.open().paragraphs if p.index == 4)
        self.assertFalse(row.editable)
        self.assertEqual(row.corrected_text, "甲😀乙\t丙\n丁\t\t己庚")
        self.assertEqual(row.corrected_styles, (
            TextStyleSpan(0, 2, "#000000"), TextStyleSpan(2, 9, "#000000", "#AABBCC"),
            TextStyleSpan(9, 10, "#000000", "#AABBCC", True),
            TextStyleSpan(10, 11, "#000000", "", True),
        ))
        self.assertEqual(self.source.read_bytes(), before)

    def test_display_automatic_colors_and_empty_paragraph_have_no_invented_mark(self):
        source = _Archive(self.source.read_bytes())
        header = etree.fromstring(source.entries["Contents/header.xml"])
        pid = _slots(source.paragraphs[2])[0].run.get("charPrIDRef")
        style = next(s for s in header.iter(HH + "charPr") if s.get("id") == pid)
        style.set("textColor", "auto")
        style.set("shadeColor", "none")
        source.entries["Contents/header.xml"] = etree.tostring(header, encoding="utf-8", xml_declaration=True)
        source.write(self.source, set())
        session = self.open()
        row = next(p for p in session.paragraphs if p.index == 2)
        self.assertEqual(row.corrected_styles, (TextStyleSpan(0, len(row.corrected_text)),))
        session.save({2: ""})
        self.assertEqual(next(p for p in session.paragraphs if p.index == 2).corrected_styles, ())

    def test_each_grade_is_removed_only_from_replaced_characters(self):
        baseline = self.styles_at(self.source)
        for grade in ("green", "blue", "red", "red_ctx"):
            with self.subTest(grade=grade):
                manual_history_path(self.corrected).unlink(missing_ok=True)
                self.mark_copy(grade=grade)
                before = self.corrected.read_bytes()
                marked = self.styles_at(self.corrected)
                result = self.open().save({2: "Alpha zeta gamma."})
                after = self.styles_at(self.corrected)
                self.assertEqual(result.backup_path.read_bytes(), before)
                for position in range(len(after)):
                    expected = baseline[position] if position == 6 else marked[position]
                    self.assertEqual(_style_key(after[position]), _style_key(expected), position)
                self.assertEqual(manual_protected_paragraphs(self.corrected), {2})

    def test_mixed_source_styles_restore_bold_without_clearing_other_marks(self):
        baseline = self.styles_at(self.source, 3)
        self.mark_copy(index=3, grade="red")
        marked = self.styles_at(self.corrected, 3)
        self.open().save({3: "XBCY"})
        saved = self.styles_at(self.corrected, 3)
        self.assertEqual([_style_key(s) for s in saved],
                         [_style_key(s) for s in (baseline[0], marked[1], marked[2], baseline[3])])
        self.assertIsNone(saved[0].find(HH + "bold"))
        self.assertIsNotNone(saved[3].find(HH + "bold"))

    def test_cross_style_contraction_restores_original_styles_hidden_by_ocr(self):
        baseline = self.styles_at(self.source, 3)
        self.mark_copy(index=3, grade="red")
        before = self.corrected.read_bytes()
        # Red makes both original normal and bold runs look identical.
        result = self.open().save({3: "XYZ"})
        self.assertEqual(result.backup_path.read_bytes(), before)
        self.assertEqual(paragraph_text(_Archive(self.corrected.read_bytes()).paragraphs[3]), "XYZ")
        self.assertEqual([_style_key(s) for s in self.styles_at(self.corrected, 3)],
                         [_style_key(s) for s in baseline[:3]])
        self.assertEqual(manual_protected_paragraphs(self.corrected), {3})

    def test_cross_style_expansion_clears_only_changed_ocr_marks(self):
        baseline = self.styles_at(self.source, 3)
        self.mark_copy(index=3, grade="red")
        marked = self.styles_at(self.corrected, 3)
        self.open().save({3: "ALONGD"})
        saved = self.styles_at(self.corrected, 3)
        expected = [marked[0], baseline[1], baseline[1],
                    baseline[2], baseline[2], marked[3]]
        self.assertEqual([_style_key(s) for s in saved], [_style_key(s) for s in expected])
        self.assertEqual(paragraph_text(_Archive(self.corrected.read_bytes()).paragraphs[3]), "ALONGD")

    def test_cross_style_expansion_keeps_target_font_size_and_italic(self):
        def resized_green(editor, pid):
            green = editor._style_for(pid, "green")
            return editor.doc.styles.ensure_run(base_char_pr_id=green, size=17, italic=True)
        baseline = self.styles_at(self.source, 3)
        self.mark_copy(index=3, transform=resized_green)
        self.open().save({3: "LONGER"})
        saved = self.styles_at(self.corrected, 3)
        self.assertEqual(len(saved), 6)
        for index, style in enumerate(saved):
            source = baseline[0 if index < 3 else 2]
            self.assertEqual(style.get("height"), "1700")
            self.assertIsNotNone(style.find(HH + "italic"))
            self.assertEqual(style.find(HH + "bold") is not None, index >= 3)
            self.assertEqual(style.get("textColor"), source.get("textColor"))
            self.assertEqual(style.get("shadeColor"), source.get("shadeColor"))
            for name in ("underline", "strikeout"):
                self.assertEqual(_style_key(style.find(HH + name)),
                                 _style_key(source.find(HH + name)))

    def test_cross_style_edit_after_ocr_already_changed_run_lengths(self):
        baseline = self.styles_at(self.source, 3)
        self.mark_copy(index=3)
        target = _Archive(self.corrected.read_bytes())
        slots = _slots(target.paragraphs[3])
        slots[0].text, slots[1].text = "AXYB", "ZD"
        target.write(self.corrected, {"Contents/section0.xml"})
        marked = self.styles_at(self.corrected, 3)
        self.open().save({3: "AKLMNOPD"})
        self.assertEqual(paragraph_text(_Archive(self.corrected.read_bytes()).paragraphs[3]), "AKLMNOPD")
        expected = [marked[0]] + [baseline[0]] * 5 + [baseline[2], marked[-1]]
        self.assertEqual([_style_key(s) for s in self.styles_at(self.corrected, 3)],
                         [_style_key(s) for s in expected])

    def test_cross_style_length_change_preserves_memo_and_markpen_boundaries(self):
        source = _Archive(self.source.read_bytes())
        paragraph = source.paragraphs[4]
        text = next(slot.t for slot in _slots(paragraph) if slot.text)
        text.text = "A"
        begin = etree.SubElement(text, HP + "markpenBegin", color="#ABCDEF")
        begin.tail = "B"
        end = etree.SubElement(text, HP + "markpenEnd")
        end.tail = "CD"
        second_run = Editor._split_after(None, paragraph, _slots(paragraph)[1], 2)
        second_run.set("charPrIDRef", _slots(source.paragraphs[3])[-1].run.get("charPrIDRef"))
        source.write(self.source, {"Contents/section0.xml"})
        original = self.source.read_bytes()
        baseline = self.styles_at(self.source, 4)
        self.mark_copy(index=4)
        before = _Archive(self.corrected.read_bytes()).paragraphs[4]
        controls = [etree.tostring(node) for node in before.iter(HP + "ctrl")]
        self.open().save({4: "LONGER"})
        saved = _Archive(self.corrected.read_bytes()).paragraphs[4]
        self.assertEqual(paragraph_text(saved), "LONGER")
        self.assertEqual([etree.tostring(node) for node in saved.iter(HP + "ctrl")], controls)
        for tag in ("markpenBegin", "markpenEnd"):
            self.assertEqual(len(list(saved.iter(HP + tag))), 1)
        self.assertEqual([slot.start for slot in _slots(saved) if slot.holder.tag == HP + "markpenBegin"], [2])
        self.assertEqual([slot.start for slot in _slots(saved) if slot.holder.tag == HP + "markpenEnd"], [3])
        self.assertEqual([_style_key(s) for s in self.styles_at(self.corrected, 4)],
                         [_style_key(baseline[0])] * 3 + [_style_key(baseline[2])] * 3)
        self.assertEqual(self.source.read_bytes(), original)

    def test_restoring_empty_mixed_style_paragraph_uses_last_existing_slot(self):
        baseline = self.styles_at(self.source, 3)
        self.mark_copy(index=3)
        session = self.open()
        session.save({3: ""})
        session.save({3: "Restored"})
        self.assertEqual(paragraph_text(_Archive(self.corrected.read_bytes()).paragraphs[3]), "Restored")
        self.assertEqual([_style_key(s) for s in self.styles_at(self.corrected, 3)],
                         [_style_key(baseline[-1])] * len("Restored"))

    def test_append_after_deleting_last_style_inherits_visible_neighbor_with_or_without_ocr(self):
        baseline = self.styles_at(self.source, 3)
        for marked in (False, True):
            with self.subTest(marked=marked):
                self.corrected.unlink(missing_ok=True)
                manual_history_path(self.corrected).unlink(missing_ok=True)
                if marked:
                    self.mark_copy(index=3)
                session = self.open()
                session.save({3: "AB"})
                session.save({3: "ABX"})
                saved = _Archive(self.corrected.read_bytes()).paragraphs[3]
                self.assertEqual(paragraph_text(saved), "ABX")
                self.assertEqual([s.text for s in _slots(saved) if s.text], ["AB", "X"] if marked else ["ABX"])
                self.assertEqual(_style_key(self.styles_at(self.corrected, 3)[-1]), _style_key(baseline[1]))

    def test_insert_delete_and_restore_keep_only_remaining_untouched_marks(self):
        self.mark_copy(grade="blue")
        baseline = self.styles_at(self.source)[0]
        session = self.open()
        session.save({2: "Alpha NEW beta gamma."})
        styles = self.styles_at(self.corrected)
        self.assertTrue(all(_style_key(s) == _style_key(baseline) for s in styles[5:9]))
        self.assertTrue(all(_tag_of(s) == "#1F4FBF" for s in styles[:5] + styles[9:]))
        session.save({2: "Alpha NEW gamma."})
        styles = self.styles_at(self.corrected)
        self.assertTrue(all(_tag_of(s) == "#1F4FBF" for s in styles[:5] + styles[9:]))
        session.save({2: ""})
        self.open().save({2: "Restored"})
        self.assertTrue(all(_style_key(s) == _style_key(baseline) for s in self.styles_at(self.corrected)))

    def test_author_grade_colors_without_ownership_marker_are_preserved(self):
        def untag(editor, pid):
            editor._char_pr(pid).find(HH + "strikeout").set("color", "#000000")
            return pid
        self.mark_copy(transform=untag)
        styles = self.styles_at(self.corrected)
        self.open().save({2: "Alpha zeta gamma."})
        self.assertEqual([_style_key(s) for s in self.styles_at(self.corrected)],
                         [_style_key(s) for s in styles])

    def test_restored_style_preserves_target_typography_and_valid_child_order(self):
        def resized_green(editor, pid):
            green = editor._style_for(pid, "green")  # Red -> green removes original bold.
            return editor.doc.styles.ensure_run(base_char_pr_id=green, size=17, italic=True)
        baseline = self.styles_at(self.source, 3)
        self.mark_copy(index=3, transform=resized_green)
        old_header = _Archive(self.corrected.read_bytes()).entries["Contents/header.xml"]
        self.open().save({3: "ABXY"})
        saved = self.styles_at(self.corrected, 3)
        self.assertNotEqual(_Archive(self.corrected.read_bytes()).entries["Contents/header.xml"], old_header)
        self.assertEqual(saved[2].get("height"), "1700")
        self.assertIsNotNone(saved[2].find(HH + "italic"))
        self.assertIsNotNone(saved[2].find(HH + "bold"))
        for name in ("textColor", "shadeColor"):
            self.assertEqual(saved[2].get(name), baseline[2].get(name))
        for name in ("underline", "strikeout"):
            self.assertEqual(_style_key(saved[2].find(HH + name)),
                             _style_key(baseline[2].find(HH + name)))
        tags = [etree.QName(child).localname for child in saved[2]]
        self.assertLess(tags.index("italic"), tags.index("bold"))
        self.assertLess(tags.index("bold"), tags.index("underline"))
        self.assertLess(tags.index("underline"), tags.index("strikeout"))
        self.assertEqual(saved[2].get("id"), saved[3].get("id"))
        header = _Archive(self.corrected.read_bytes()).entries["Contents/header.xml"]
        self.open().save({3: "ABXZ"})
        self.assertEqual(_Archive(self.corrected.read_bytes()).entries["Contents/header.xml"], header)

    def test_partial_cleanup_preserves_memo_and_markpen_nodes_once(self):
        source = _Archive(self.source.read_bytes())
        paragraph = source.paragraphs[4]
        text = next(slot.t for slot in _slots(paragraph) if slot.text)
        text.text = "Memo "
        begin = etree.SubElement(text, HP + "markpenBegin", color="#ABCDEF")
        begin.tail = "body"
        end = etree.SubElement(text, HP + "markpenEnd")
        end.tail = " text."
        source.write(self.source, {"Contents/section0.xml"})
        self.mark_copy(index=4)
        before = _Archive(self.corrected.read_bytes()).paragraphs[4]
        controls = [etree.tostring(node) for node in before.iter(HP + "ctrl")]
        self.open().save({4: "Memo bXdy text."})
        saved = _Archive(self.corrected.read_bytes()).paragraphs[4]
        self.assertEqual(paragraph_text(saved), "Memo bXdy text.")
        self.assertEqual([etree.tostring(node) for node in saved.iter(HP + "ctrl")], controls)
        for tag in ("markpenBegin", "markpenEnd"):
            self.assertEqual(len(list(saved.iter(HP + tag))), 1)
        styles = self.styles_at(self.corrected, 4)
        self.assertNotEqual(_tag_of(styles[6]), "#C00000")
        self.assertTrue(all(_tag_of(s) == "#C00000" for i, s in enumerate(styles) if i != 6))

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

    def test_cross_style_length_change_keeps_nodes_and_distributes_new_text(self):
        original = self.source.read_bytes()
        before = _Archive(original)
        attributes = [dict(s.run.attrib) for s in _slots(before.paragraphs[3])]
        self.open().save({3: "LONGER"})
        saved = _Archive(self.corrected.read_bytes())
        self.assertEqual([s.text for s in _slots(saved.paragraphs[3])], ["LON", "GER"])
        self.assertEqual([dict(s.run.attrib) for s in _slots(saved.paragraphs[3])], attributes)
        self.assertEqual(self.source.read_bytes(), original)
        for name, data in before.entries.items():
            if name != "Contents/section0.xml":
                self.assertEqual(saved.entries[name], data, name)
        for index in range(len(before.paragraphs)):
            if index != 3:
                self.assertEqual(etree.tostring(saved.paragraphs[index]), etree.tostring(before.paragraphs[index]))
        self.assertEqual(manual_protected_paragraphs(self.corrected), {3})

    def test_cross_style_replacement_can_shrink_to_one_character(self):
        baseline = self.styles_at(self.source, 3)
        self.mark_copy(index=3)
        self.open().save({3: "X"})
        saved = _Archive(self.corrected.read_bytes()).paragraphs[3]
        self.assertEqual(paragraph_text(saved), "X")
        self.assertEqual(len(_slots(saved)), 2)  # Empty second run remains.
        self.assertEqual(_style_key(self.styles_at(self.corrected, 3)[0]), _style_key(baseline[0]))

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

    def test_export_section_contains_complete_current_body_without_edit_labels_or_writes(self):
        session = self.open()
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        changes = {2: "수정한 漢文\n둘째 줄", 7: "Other section draft"}
        original_changes = changes.copy()
        exported = session.export_text(changes)
        self.assertEqual(exported, "\n\n".join([
            "瀉白散", changes[2], "ABCD", "Memo body text.", "Before\tAfter",
        ]))
        self.assertEqual(changes, original_changes)
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, before)
        self.assertEqual(next(p.corrected_text for p in session.paragraphs if p.index == 2), "Alpha beta gamma.")

    def test_export_document_uses_saved_corrections_and_all_unsaved_replacement_drafts(self):
        session = self.open()
        session.save({2: "Saved correction."})
        plan = session.preview_replace("Other", "Outside draft", scope="document",
                                       changes={4: "Memo pending correction."})
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        exported = session.export_text(plan.changes, scope="document")
        expected = [paragraph_text(p) for p in _Archive(self.corrected.read_bytes()).paragraphs]
        expected[4], expected[7] = "Memo pending correction.", "Outside draft section remains here."
        self.assertEqual(exported, "\n\n".join(expected))
        self.assertIn("Saved correction.", exported)
        self.assertIn("葦莖湯", exported)
        self.assertIn("Second physical section.", exported)
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, before)
        self.assertEqual(session._generation, 1)
        session._check_plan(plan)  # Export neither consumes nor invalidates a preview.

    def test_export_includes_table_note_and_textbox_body_but_excludes_memos_and_page_furniture(self):
        source = _Archive(self.source.read_bytes())
        paragraph = source.paragraphs[4]
        run = paragraph.find(HP + "run")

        def body(parent, text):
            sublist = etree.SubElement(parent, HP + "subList")
            child = etree.SubElement(sublist, HP + "p", id="9000", paraPrIDRef="0", styleIDRef="0")
            child_run = etree.SubElement(child, HP + "run", charPrIDRef=run.get("charPrIDRef"))
            etree.SubElement(child_run, HP + "t").text = text

        table = etree.SubElement(run, HP + "tbl")
        row = etree.SubElement(table, HP + "tr")
        for text in ("First table cell.", "Second table cell."):
            body(etree.SubElement(row, HP + "tc"), text)
        control = etree.SubElement(run, HP + "ctrl")
        body(etree.SubElement(control, HP + "footNote"), "Footnote body.")
        body(etree.SubElement(run, HP + "drawText"), "Text box body.")
        body(etree.SubElement(control, HP + "header"), "Repeated header.")
        body(etree.SubElement(control, HP + "footer"), "Repeated footer.")
        body(etree.SubElement(control, HP + "fieldBegin", type="MEMO"), "App correction memo.")
        source.write(self.source, {"Contents/section0.xml"})
        session = self.open()
        self.assertFalse(next(p.editable for p in session.paragraphs if p.index == 4))
        before = self.source.read_bytes()
        for scope in ("section", "document"):
            with self.subTest(scope=scope):
                text = session.export_text({}, scope=scope)
                self.assertIn("Memo body text.\n\nFirst table cell.\n\nSecond table cell."
                              "\n\nFootnote body.\n\nText box body.", text)
                for excluded in ("Keep this user comment.", "App correction memo.",
                                 "Repeated header.", "Repeated footer."):
                    self.assertNotIn(excluded, text)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(self.corrected.exists())

    def test_export_uses_snapshot_even_when_actual_document_and_history_change(self):
        session = self.open()
        session.save({2: "Saved correction."})
        expected = [paragraph_text(p) for p in _Archive(self.corrected.read_bytes()).paragraphs]
        expected[2] = "Still recoverable unsaved draft."
        self.source.write_bytes(b"externally changed source")
        self.corrected.write_bytes(b"externally changed corrected document")
        manual_history_path(self.corrected).write_text("invalid external history", encoding="utf-8")
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        for scope in ("section", "document"):
            with self.subTest(scope=scope):
                rows = expected[session.start:session.end] if scope == "section" else expected
                self.assertEqual(session.export_text({2: expected[2]}, scope=scope), "\n\n".join(rows))
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, before)

    def test_export_allows_plain_text_overlays_but_validates_indices_and_values(self):
        session = self.open()
        self.assertEqual(session.export_text({1: "Draft heading", 2: "", 5: "Plain\ttext\nlines"}),
                         "Draft heading\n\n\n\nABCD\n\nMemo body text.\n\nPlain\ttext\nlines")
        for changes, scope in ((None, "section"), ({-1: "bad"}, "section"),
                               ({len(session._source.paragraphs): "bad"}, "document"),
                               ({True: "bad"}, "section"), ({2: None}, "section"),
                               ({"2": "bad"}, "document"), ({}, "changed")):
            with self.subTest(changes=changes, scope=scope), self.assertRaises(SectionEditError):
                session.export_text(changes, scope=scope)
        self.assertFalse(self.corrected.exists())
        self.assertFalse(manual_history_path(self.corrected).exists())

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

    def rewrite_source(self, texts):
        source = _Archive(self.source.read_bytes())
        for index, text in texts.items():
            slots = _slots(source.paragraphs[index])
            slots[0].text = text
            for slot in slots[1:]:
                slot.text = ""
        source.write(self.source, {source.locations[index][0] for index in texts})

    def test_existing_literal_separators_do_not_block_character_correction(self):
        self.rewrite_source({2: "甲皰\n乙皰\t丙皰\r丁皰\u2028戊皰\u2029己皰"})
        session = self.open()
        before = next(p.corrected_text for p in session.paragraphs if p.index == 2)
        session.save({2: before.replace("皰", "疱")})
        self.assertEqual(paragraph_text(_Archive(self.corrected.read_bytes()).paragraphs[2]),
                         before.replace("皰", "疱"))

    def test_new_literal_separators_are_rejected_with_paragraph_number(self):
        self.rewrite_source({2: "甲皰\n乙皰\t丙皰"})
        for text in ("甲疱\n乙皰\t丙皰\n", "甲疱\t乙皰\n丙皰", "甲疱\u2028乙皰"):
            with self.subTest(text=text), self.assertRaisesRegex(SectionEditError, "3번 문단"):
                self.open().save({2: text})
        self.assertFalse(self.corrected.exists())
        with self.assertRaises(SectionEditError) as raised:
            self.open().save({2: "甲疱\n乙皰\t丙皰\n"})
        self.assertEqual((raised.exception.paragraph_index, raised.exception.offset,
                          raised.exception.character), (2, 8, "\n"))

    def test_existing_literal_separator_can_be_explicitly_removed(self):
        self.rewrite_source({2: "甲皰\n乙皰"})
        self.open().save({2: "甲疱 乙皰"})
        self.assertEqual(paragraph_text(_Archive(self.corrected.read_bytes()).paragraphs[2]), "甲疱 乙皰")

    def test_replace_preview_scope_counts_readonly_skips_and_no_writes(self):
        self.rewrite_source({2: "皰 text 皰", 4: "Memo 皰", 5: "Tab 皰", 7: "Other 皰"})
        session = self.open()
        source = self.source.read_bytes()
        paragraph = session.preview_replace("皰", "疱", scope="paragraph", paragraph_index=2)
        self.assertEqual((paragraph.matches, paragraph.paragraph_count, paragraph.skipped_count), (2, 1, 0))
        section = session.preview_replace("皰", "疱")
        self.assertEqual((section.matches, section.paragraph_count, section.skipped_count), (3, 2, 1))
        self.assertEqual(section.skipped[0].index, 5)
        document = session.preview_replace("皰", "疱", scope="document")
        self.assertEqual((document.matches, document.paragraph_count, document.skipped_count), (4, 3, 1))
        self.assertEqual(document.changes, {2: "疱 text 疱", 4: "Memo 疱", 7: "Other 疱"})
        self.assertEqual(self.source.read_bytes(), source)
        self.assertFalse(self.corrected.exists())
        self.assertFalse(manual_history_path(self.corrected).exists())
        find_only = session.preview_replace("皰", "皰", scope="document")
        self.assertEqual((find_only.matches, find_only.skipped_count), (4, 1))
        self.assertEqual(find_only.changes, {})

    def test_whole_document_replace_preserves_source_backups_history_and_marks(self):
        self.rewrite_source({2: "皰 text 皰", 7: "Other 皰"})
        self.mark_copy(index=7, grade="red")
        source = self.source.read_bytes()
        marked = self.styles_at(self.corrected, 7)
        old = self.corrected.read_bytes()
        session = self.open()
        plan = session.preview_replace("皰", "疱", scope="document")
        result = session.save(plan.changes, replacement_plan=plan)
        self.assertEqual(result.saved_count, 2)
        self.assertEqual(result.backup_path.read_bytes(), old)
        self.assertEqual(self.source.read_bytes(), source)
        self.assertEqual(manual_protected_paragraphs(self.corrected), {2, 7})
        saved = _Archive(self.corrected.read_bytes())
        before = _Archive(old)
        self.assertEqual(paragraph_text(saved.paragraphs[7]), "Other 疱")
        self.assertEqual(saved.entries["Contents/section1.xml"], before.entries["Contents/section1.xml"])
        self.assertEqual(saved.entries["BinData/manual-test.bin"], before.entries["BinData/manual-test.bin"])
        styles = self.styles_at(self.corrected, 7)
        self.assertEqual([_style_key(s) for s in styles[:6]], [_style_key(s) for s in marked[:6]])
        self.assertNotEqual(_tag_of(styles[6]), "#C00000")
        history = json.loads(manual_history_path(self.corrected).read_text("utf-8"))
        from yeone_ocr.correction_memory import correction_records
        records = correction_records(history, saved.paragraphs, session._pdf_hash)
        self.assertEqual({entry["paragraph"] for entry in records}, {2, 7})
        self.assertTrue(all(entry["section"] == "교정본 전체" for entry in records))

    def test_replace_preview_includes_unsaved_changes_and_single_match(self):
        self.rewrite_source({2: "皰 text 皰", 7: "Other 皰 皰"})
        session = self.open()
        plan = session.preview_replace("皰", "疱", changes={2: "皰 revised 皰"},
                                       scope="document", match=(7, 8))
        self.assertEqual(plan.matches, 1)
        self.assertEqual(plan.entries[0].before, "Other 皰 皰")
        self.assertEqual(plan.changes, {2: "皰 revised 皰", 7: "Other 皰 疱"})
        second = session.preview_replace("皰", "疱", scope="document",
                                         changes=plan.changes, previous_plan=plan)
        self.assertEqual(second.changes, {2: "疱 revised 疱", 7: "Other 疱 疱"})
        # Merely opening and cancelling another preview must not invalidate it.
        session.preview_replace("text", "words")
        session.save(second.changes, replacement_plan=second)
        saved = _Archive(self.corrected.read_bytes())
        self.assertEqual(paragraph_text(saved.paragraphs[2]), "疱 revised 疱")
        self.assertEqual(paragraph_text(saved.paragraphs[7]), "Other 疱 疱")

    def test_unapproved_outside_changes_forged_and_stale_plans_are_rejected(self):
        self.rewrite_source({7: "Other 皰"})
        session = self.open()
        plan = session.preview_replace("皰", "疱", scope="document")
        for changes, approved in ((plan.changes, None), ({7: "Arbitrary"}, plan),
                                  (plan.changes, replace(plan)),
                                  (plan.changes, self.open().preview_replace("皰", "疱", scope="document"))):
            with self.subTest(changes=changes), self.assertRaises(SectionEditError):
                session.save(changes, replacement_plan=approved)
        session.save({2: "Alpha better gamma."})
        with self.assertRaises(ConflictError):
            session.save(plan.changes, replacement_plan=plan)
        self.assertEqual(paragraph_text(_Archive(self.corrected.read_bytes()).paragraphs[7]), "Other 皰")

    def test_replace_plan_property_mutation_cannot_authorize_another_change(self):
        self.rewrite_source({7: "Other 皰"})
        session = self.open()
        plan = session.preview_replace("皰", "疱", scope="document")
        plan.changes[7] = "Injected"
        self.assertEqual(plan.changes[7], "Other 疱")
        with self.assertRaises(SectionEditError):
            session.preview_replace("皰", "疱", scope="document", changes={7: "Injected"}, previous_plan=plan)

    def test_discarded_previews_do_not_accumulate_document_drafts(self):
        session = self.open()
        kept = session.preview_replace("a", "A")
        discarded = session.preview_replace("a", "B")
        reference = weakref.ref(discarded)
        del discarded
        gc.collect()
        self.assertIsNone(reference())
        self.assertEqual(len(session._replace_plans), 1)
        self.assertGreater(session.save(kept.changes, replacement_plan=kept).saved_count, 0)

    def test_replace_preview_accepts_cross_style_change_but_skips_unrelated_body(self):
        target = _Archive(self.source.read_bytes())
        _slots(target.paragraphs[7])[0].text = "Entirely divergent 皰 replacement with unrelated contents"
        target.write(self.corrected, {"Contents/section0.xml"})
        session = self.open()
        plan = session.preview_replace("BC", "LONG", scope="document")
        self.assertEqual((plan.matches, plan.skipped_count), (1, 0))
        self.assertEqual(plan.changes, {3: "ALONGD"})
        session.save(plan.changes, replacement_plan=plan)
        self.assertEqual(paragraph_text(_Archive(self.corrected.read_bytes()).paragraphs[3]), "ALONGD")
        plan = session.preview_replace("皰", "疱", scope="document")
        self.assertEqual((plan.matches, plan.skipped_count, plan.skipped[0].index), (0, 1, 7))
        self.assertIn("위치 대응", plan.skipped[0].reason)

    def test_replace_excludes_heading_and_preserves_literal_case_and_deletion(self):
        session = self.open()
        plan = session.preview_replace("白", "黑", scope="document")
        self.assertEqual((plan.matches, plan.skipped_count), (0, 1))
        plan = session.preview_replace("a", "", scope="paragraph", paragraph_index=2)
        self.assertEqual(plan.changes, {2: "Alph bet gmm."})
        session.save(plan.changes, replacement_plan=plan)
        self.assertEqual(paragraph_text(_Archive(self.corrected.read_bytes()).paragraphs[2]), "Alph bet gmm.")

    def test_bad_replace_inputs_and_external_change_do_not_write(self):
        session = self.open()
        for find, replacement, options in (("", "a", {}), ("a", "\n", {}), ("\t", "a", {}),
                                            ("a", "b", {"scope": "other"}),
                                            ("a", "b", {"scope": "paragraph", "paragraph_index": 7}),
                                            ("a", "b", {"match": (2, 900)})):
            with self.subTest(options=options), self.assertRaises(SectionEditError):
                session.preview_replace(find, replacement, **options)
        self.pdf.write_bytes(b"changed")
        with self.assertRaises(ConflictError):
            session.preview_replace("a", "b")
        self.assertFalse(self.corrected.exists())

    def test_whole_document_failed_publish_preserves_drafts_and_previous_file(self):
        self.rewrite_source({2: "皰 one", 7: "Other 皰"})
        self.corrected.write_bytes(self.source.read_bytes())
        session = self.open()
        old = self.corrected.read_bytes()
        plan = session.preview_replace("皰", "疱", scope="document")
        real_replace = os.replace
        def fail_document(src, dst):
            if Path(dst) == self.corrected:
                raise PermissionError("document locked")
            return real_replace(src, dst)
        with patch("yeone_ocr.collate.section_edit.os.replace", side_effect=fail_document), self.assertRaises(PermissionError):
            session.save(plan.changes, replacement_plan=plan)
        self.assertEqual(self.corrected.read_bytes(), old)
        self.assertFalse(manual_history_path(self.corrected).exists())
        # The same draft can still be saved after the external lock is released.
        result = session.save(plan.changes, replacement_plan=plan)
        self.assertEqual(result.saved_count, 2)

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

    @staticmethod
    def access_error(code=5):
        error = PermissionError(13, "synthetic Windows access failure")
        error.winerror = code
        return error

    def test_transient_access_retries_all_publication_destinations(self):
        session = self.open()
        session.save({2: "Alpha better gamma."})
        before = self.corrected.read_bytes()
        history = manual_history_path(self.corrected)
        before_history = history.read_bytes()
        original = self.source.read_bytes()
        real_replace = os.replace
        attempts = {}
        def transient(src, dst):
            destination = Path(dst)
            count = attempts.get(destination, 0)
            attempts[destination] = count + 1
            if count < 3:
                raise self.access_error((5, 32, 33)[count])
            return real_replace(src, dst)
        with patch("yeone_ocr.collate.section_edit.os.replace", side_effect=transient), \
                patch("yeone_ocr.collate.section_edit.time.sleep"):
            result = session.save({4: "Memo changed text."})
        self.assertEqual(attempts, {self.corrected.with_suffix(".bak.hwpx"): 4,
                                    history.with_suffix(".bak.json"): 4, history: 4, self.corrected: 4})
        self.assertEqual(result.backup_path.read_bytes(), before)
        self.assertEqual(history.with_suffix(".bak.json").read_bytes(), before_history)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(manual_protected_paragraphs(self.corrected), {2, 4})
        self.assertFalse(list(self.root.glob(".*.tmp")))

    def test_persistent_access_error_is_bounded_identifies_path_and_same_session_retries(self):
        session = self.open()
        session.save({2: "Alpha better gamma."})
        before = self.corrected.read_bytes()
        history_path = manual_history_path(self.corrected)
        before_history = history_path.read_bytes()
        real_replace = os.replace
        attempts = []
        def inaccessible(src, dst):
            if Path(dst) == self.corrected:
                attempts.append(True)
                raise self.access_error(5)
            return real_replace(src, dst)
        changes = {4: "Memo changed text."}
        with patch("yeone_ocr.collate.section_edit.os.replace", side_effect=inaccessible), \
                patch("yeone_ocr.collate.section_edit.time.sleep") as sleep, \
                self.assertRaises(SaveAccessError) as caught:
            session.save(changes)
        self.assertIsInstance(caught.exception, PermissionError)
        self.assertEqual((caught.exception.path, caught.exception.winerror, caught.exception.error_code),
                         (self.corrected, 5, 5))
        self.assertEqual(len(attempts), 5)
        self.assertEqual(sleep.call_count, 4)
        self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 0.75)
        self.assertEqual(self.corrected.read_bytes(), before)
        self.assertEqual(history_path.read_bytes(), before_history)
        self.assertEqual(next(p.corrected_text for p in session.paragraphs if p.index == 4), "Memo body text.")
        self.assertEqual(changes, {4: "Memo changed text."})
        self.assertFalse(list(self.root.glob(".*.tmp")))
        self.assertEqual(session.save(changes).saved_count, 1)
        self.assertEqual(next(p.corrected_text for p in self.open().paragraphs if p.index == 4), changes[4])

    def test_unrelated_access_codes_are_not_retried_and_deadline_limits_retry(self):
        session = self.open()
        for code in (123, None):
            error = self.access_error(code)
            with self.subTest(code=code), patch("yeone_ocr.collate.section_edit.time.sleep") as sleep, \
                    patch("yeone_ocr.collate.section_edit.os.replace", side_effect=error) as replace, \
                    self.assertRaises(SaveAccessError):
                session.save({2: "Alpha better gamma."})
            replace.assert_called_once()
            sleep.assert_not_called()
        with patch("yeone_ocr.collate.section_edit.time.monotonic", side_effect=(0, 1)), \
                patch("yeone_ocr.collate.section_edit.time.sleep") as sleep, \
                patch("yeone_ocr.collate.section_edit.os.replace", side_effect=self.access_error()) as replace, \
                self.assertRaises(SaveAccessError):
            session._retry_save_access(lambda: os.replace("stage", self.corrected), self.corrected)
        replace.assert_called_once()
        sleep.assert_not_called()
        self.assertFalse(self.corrected.exists())
        self.assertFalse(manual_history_path(self.corrected).exists())

    def test_competing_writes_during_access_wait_are_never_overwritten(self):
        session = self.open()
        session.save({2: "Alpha better gamma."})
        history_path = manual_history_path(self.corrected)
        baseline = {path: path.read_bytes() for path in (self.source, self.corrected, self.pdf, history_path)}
        real_replace = os.replace
        for changed in baseline:
            with self.subTest(changed=changed.name):
                for path, data in baseline.items():
                    path.write_bytes(data)
                current = self.open()
                attempts = []
                external = baseline[changed] + b"external writer"
                def inaccessible(src, dst):
                    if Path(dst) == self.corrected:
                        attempts.append(True)
                        raise self.access_error(32)
                    return real_replace(src, dst)
                with patch("yeone_ocr.collate.section_edit.os.replace", side_effect=inaccessible), \
                        patch("yeone_ocr.collate.section_edit.time.sleep", side_effect=lambda _: changed.write_bytes(external)), \
                        self.assertRaises(ConflictError):
                    current.save({4: "Memo changed text."})
                self.assertEqual(len(attempts), 1)
                self.assertEqual(changed.read_bytes(), external)
                if changed != history_path:
                    self.assertEqual(history_path.read_bytes(), baseline[history_path])
                if changed != self.corrected:
                    self.assertEqual(self.corrected.read_bytes(), baseline[self.corrected])
                self.assertFalse(list(self.root.glob(".*.tmp")))

    def test_rollback_retry_compares_own_intent_but_does_not_require_unchanged_document(self):
        self.open().save({2: "Alpha better gamma."})
        history_path = manual_history_path(self.corrected)
        target_bytes, history_bytes = self.corrected.read_bytes(), history_path.read_bytes()
        real_replace = os.replace
        for competing in (self.corrected, history_path):
            with self.subTest(competing=competing.name):
                self.corrected.write_bytes(target_bytes)
                history_path.write_bytes(history_bytes)
                session = self.open()
                publications = []
                external = (target_bytes if competing == self.corrected else history_bytes) + b"competing"
                def fail_then_rollback(src, dst):
                    if Path(dst) == self.corrected:
                        raise PermissionError("persistent primary failure")
                    if Path(dst) == history_path:
                        publications.append(True)
                        if len(publications) == 2:
                            raise self.access_error(33)
                    return real_replace(src, dst)
                with patch("yeone_ocr.collate.section_edit.os.replace", side_effect=fail_then_rollback), \
                        patch("yeone_ocr.collate.section_edit.time.sleep", side_effect=lambda _: competing.write_bytes(external)), \
                        self.assertRaises(SaveAccessError) as caught:
                    session.save({4: "Memo changed text."})
                self.assertEqual(caught.exception.path, self.corrected)
                self.assertEqual(competing.read_bytes(), external)
                self.assertEqual(len(publications), 3 if competing == self.corrected else 2)
                if competing == self.corrected:
                    self.assertEqual(history_path.read_bytes(), history_bytes)

    def test_rollback_first_attempt_rechecks_history_after_staging(self):
        from yeone_ocr.collate import section_edit
        session = self.open()
        session.save({2: "Alpha better gamma."})
        history_path = manual_history_path(self.corrected)
        history_bytes, target_bytes = history_path.read_bytes(), self.corrected.read_bytes()
        external = history_bytes + b"external history during rollback staging"
        real_stage, real_replace = section_edit._stage_bytes, os.replace
        def competing_stage(destination, data):
            result = real_stage(destination, data)
            if destination == history_path and data == history_bytes:
                history_path.write_bytes(external)
            return result
        def fail_document(src, dst):
            if Path(dst) == self.corrected:
                raise PermissionError("primary access failure")
            return real_replace(src, dst)
        with patch.object(section_edit, "_stage_bytes", side_effect=competing_stage), \
                patch.object(section_edit.os, "replace", side_effect=fail_document), \
                self.assertRaises(SaveAccessError):
            session.save({4: "Memo changed text."})
        self.assertEqual(history_path.read_bytes(), external)
        self.assertEqual(self.corrected.read_bytes(), target_bytes)
        with self.assertRaises(ConflictError):
            session.save({4: "Memo changed text."})

    def test_access_error_names_blocked_backup_or_history_file(self):
        self.open().save({2: "Alpha better gamma."})
        history_path = manual_history_path(self.corrected)
        history_bytes, target_bytes = history_path.read_bytes(), self.corrected.read_bytes()
        real_replace = os.replace
        for blocked in (self.corrected.with_suffix(".bak.hwpx"), history_path.with_suffix(".bak.json"), history_path):
            with self.subTest(blocked=blocked.name):
                session = self.open()
                def denied(src, dst):
                    if Path(dst) == blocked:
                        raise self.access_error(32)
                    return real_replace(src, dst)
                with patch("yeone_ocr.collate.section_edit.os.replace", side_effect=denied), \
                        patch("yeone_ocr.collate.section_edit.time.sleep"), self.assertRaises(SaveAccessError) as caught:
                    session.save({4: "Memo changed text."})
                self.assertEqual(caught.exception.path, blocked)
                self.assertEqual(caught.exception.error_code, 32)
                self.assertEqual(history_path.read_bytes(), history_bytes)
                self.assertEqual(self.corrected.read_bytes(), target_bytes)
                self.assertFalse(list(self.root.glob(".*.tmp")))

    def test_stage_cleanup_failure_does_not_hide_save_error_or_success(self):
        real_unlink, real_replace = Path.unlink, os.replace
        def locked_stage(path, *args, **kwargs):
            if path.suffix == ".tmp":
                raise PermissionError("temporary file cleanup locked")
            return real_unlink(path, *args, **kwargs)
        def fail_document(src, dst):
            if Path(dst) == self.corrected:
                raise PermissionError("primary document access failure")
            return real_replace(src, dst)
        session = self.open()
        with patch("pathlib.Path.unlink", new=locked_stage), \
                patch("yeone_ocr.collate.section_edit.os.replace", side_effect=fail_document), \
                self.assertRaises(SaveAccessError) as caught:
            session.save({2: "Alpha better gamma."})
        self.assertEqual(caught.exception.path, self.corrected)
        self.assertFalse(self.corrected.exists())
        self.assertFalse(manual_history_path(self.corrected).exists())
        with patch("pathlib.Path.unlink", new=locked_stage):
            self.assertEqual(session.save({2: "Alpha better gamma."}).saved_count, 1)
        self.assertEqual(next(p.corrected_text for p in self.open().paragraphs if p.index == 2), "Alpha better gamma.")

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
        # The session adopts only its own still-valid durable intent, allowing
        # another save after both locks are released without losing the draft.
        self.assertEqual(session.save({4: "Memo changed text."}).saved_count, 1)
        self.assertEqual(next(p.corrected_text for p in self.open().paragraphs if p.index == 4), "Memo changed text.")

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

    def test_ascii_space_only_external_edit_stays_protected_without_rewriting_text_color_or_history(self):
        self.mark_copy()
        saved_text = "Alpha better gamma. "
        self.open().save({2: saved_text})
        saved_bytes = self.corrected.read_bytes()
        history_path = manual_history_path(self.corrected)
        history_bytes = history_path.read_bytes()
        source_bytes = self.source.read_bytes()
        # The first case removes exactly three U+0020 characters. The second
        # moves word boundaries; protection preserves the actual current text.
        for current in (saved_text.replace(" ", ""), " Al phabetterga mma.  ", "Alphabeta gamma."):
            with self.subTest(current=current):
                self.corrected.write_bytes(saved_bytes)
                history_path.write_bytes(history_bytes)
                archive = _Archive(saved_bytes)
                _replace_text(archive.paragraphs[2], current)
                archive.write(self.corrected, {archive.locations[2][0]})
                external_bytes = self.corrected.read_bytes()
                self.assertEqual(manual_protected_paragraphs(self.corrected), {2})
                session = self.open()
                self.assertEqual(next(p.corrected_text for p in session.paragraphs if p.index == 2), current)
                self.assertEqual(session.save({}).saved_count, 0)
                self.assertEqual(self.corrected.read_bytes(), external_bytes)
                self.assertEqual(history_path.read_bytes(), history_bytes)
                self.assertEqual(self.source.read_bytes(), source_bytes)
                session.save({2: current + "!"})
                self.assertEqual(next(p.corrected_text for p in self.open().paragraphs if p.index == 2), current + "!")
                self.assertEqual(manual_protected_paragraphs(self.corrected), {2})

    def test_space_equivalence_never_normalizes_other_characters_or_structure(self):
        saved_text = "Alpha bétta gamma."
        self.open().save({2: saved_text})
        saved = self.corrected.read_bytes()
        for changed in ("Alpha betta gamma.", "Alpha be\u0301tta gamma.", "Alpha bétta gamma,",
                        "Alpha\tbétta gamma.", "Alpha\nbétta gamma.", "Alpha\r bétta gamma.",
                        "Alpha\u00a0bétta gamma.", "Alpha\u3000bétta gamma.", "Alpha\u200bbétta gamma."):
            with self.subTest(changed=changed):
                archive = _Archive(saved)
                slots = _slots(archive.paragraphs[2])
                for slot in slots:
                    slot.text = ""
                slots[0].text = changed
                archive.write(self.corrected, {archive.locations[2][0]})
                before = self.corrected.read_bytes()
                with self.assertRaises(ManualProtectionError):
                    manual_protected_paragraphs(self.corrected)
                self.assertEqual(self.corrected.read_bytes(), before)
        archive = _Archive(saved)
        _replace_text(archive.paragraphs[2], saved_text.replace(" ", ""))
        archive.paragraphs[2].set("id", "999999")
        archive.write(self.corrected, {archive.locations[2][0]})
        with self.assertRaises(ManualProtectionError):
            manual_protected_paragraphs(self.corrected)

    def test_space_fallback_requires_valid_bound_event_and_both_registered_hashes(self):
        self.open().save({2: "Alpha better gamma."})
        archive = _Archive(self.corrected.read_bytes())
        _replace_text(archive.paragraphs[2], "Alphabettergamma.")
        archive.write(self.corrected, {archive.locations[2][0]})
        path = manual_history_path(self.corrected)
        original = json.loads(path.read_text("utf-8"))
        variants = []
        legacy = deepcopy(original)
        legacy.pop("feedback_version")
        legacy.pop("correction_events")
        variants.append(legacy)
        for mutate in (
            lambda history: history.update(feedback_version=2),
            lambda history: history["correction_events"][0].update(after_sha256="0" * 64),
            lambda history: history["correction_events"][0].update(signature="0" * 64),
            lambda history: history["correction_events"][0].update(paragraph=3),
            lambda history: history["correction_events"][0].update(parent_id=history["correction_events"][0]["id"]),
            lambda history: history["paragraphs"]["2"]["text_hashes"].remove(history["correction_events"][0]["before_sha256"]),
            lambda history: history["paragraphs"]["2"]["text_hashes"].remove(history["correction_events"][0]["after_sha256"]),
        ):
            history = deepcopy(original)
            mutate(history)
            variants.append(history)
        for index, history in enumerate(variants):
            with self.subTest(variant=index):
                path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(ManualProtectionError):
                    manual_protected_paragraphs(self.corrected)
        path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(manual_protected_paragraphs(self.corrected), {2})

    def test_exact_hash_fast_path_does_not_validate_or_require_learning_extension(self):
        self.open().save({2: "Alpha better gamma."})
        with patch("yeone_ocr.correction_memory._events", side_effect=AssertionError("exact hash needs no fallback")):
            self.assertEqual(manual_protected_paragraphs(self.corrected), {2})

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
