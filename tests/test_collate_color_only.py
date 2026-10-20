"""Color-only correction and selective removal of app-owned bookmark memos."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from lxml import etree

from yeone_ocr.collate.collate import Item
from yeone_ocr.collate.hwpx_edit import (
    HP, Editor, paragraph_text, remove_collate_memos,
)
from yeone_ocr.grades import COLLATE_LEGEND, GRADE_STYLE, LEGEND
from yeone_ocr.hwpx_memo import memo_field


class ColorOnlyEditing(unittest.TestCase):
    def book(self, text="甲乙丙丁"):
        from hwpx.document import HwpxDocument
        doc = HwpxDocument.new()
        self.addCleanup(doc.close)
        normal = doc.styles.ensure_run(size=11)
        paragraph = doc.add_paragraph(text, style_id_ref=0, char_pr_id_ref=normal, inherit_style=False)
        book = SimpleNamespace(doc=doc, paragraphs=doc.paragraphs, _texts=None)
        return book, len(book.paragraphs) - 1, paragraph.element

    def color(self, editor, paragraph, char):
        for run in paragraph:
            if char in "".join(t.text or "" for t in run if t.tag == HP + "t"):
                return editor._char_pr(run.get("charPrIDRef")).get("textColor")
        self.fail("Missing character")

    def test_none_applies_auto_changes_and_three_colors_without_notes(self):
        book, pi, paragraph = self.book("甲乙丙丁")
        editor = Editor(book, memo="none")
        records = editor.apply([
            Item("A", "replace", pi, 0, 1, "甲", "戊", "戊", auto=True, witness="glyph=ocr"),
            Item("B2", "replace", pi, 1, 2, "乙", "发", "發", auto=True),
            Item("B1", "mark", pi, 2, 3, "丙", "丁", ""),
        ])
        self.assertEqual(paragraph_text(paragraph), "戊發丙丁")
        self.assertEqual([self.color(editor, paragraph, char) for char in "戊發丙"],
                         [GRADE_STYLE[grade]["color"] for grade in ("green", "blue", "red")])
        self.assertTrue(all(record.ok and not record.memo for record in records))
        self.assertFalse(editor.add_legend_memo())
        self.assertEqual(list(paragraph.iter(HP + "fieldBegin")), [])

    def test_missing_span_marks_one_nearby_character_without_inserting_text(self):
        book, pi, paragraph = self.book("甲 乙丙")
        editor = Editor(book, memo="none", heads={pi})
        records = editor.apply([Item("C", "insert", pi, 1, 1, "", "新" * 30, "新" * 30)])
        self.assertEqual(paragraph_text(paragraph), "甲 乙丙")
        self.assertEqual(self.color(editor, paragraph, "乙"), GRADE_STYLE["red"]["color"])
        self.assertNotEqual(self.color(editor, paragraph, "甲"), GRADE_STYLE["red"]["color"])
        self.assertNotEqual(self.color(editor, paragraph, "丙"), GRADE_STYLE["red"]["color"])
        self.assertTrue(records[0].ok)
        self.assertFalse(records[0].memo or records[0].hidden)
        self.assertEqual(list(paragraph.iter(HP + "fieldBegin")), [])

    def test_end_of_paragraph_missing_span_marks_previous_character(self):
        book, pi, paragraph = self.book("甲乙 ")
        editor = Editor(book, memo="none")
        editor.apply([Item("C", "insert", pi, 3, 3, "", "新", "新")])
        self.assertEqual(paragraph_text(paragraph), "甲乙 ")
        self.assertEqual(self.color(editor, paragraph, "乙"), GRADE_STYLE["red"]["color"])
        self.assertNotEqual(self.color(editor, paragraph, "甲"), GRADE_STYLE["red"]["color"])

    def test_deletion_marks_surviving_character_and_does_not_add_marker_text(self):
        book, pi, paragraph = self.book("甲乙丙")
        editor = Editor(book, memo="none")
        records = editor.apply([Item("A", "delete", pi, 1, 2, "乙", "", "", auto=True)])
        self.assertEqual(paragraph_text(paragraph), "甲丙")
        self.assertEqual(self.color(editor, paragraph, "丙"), GRADE_STYLE["blue"]["color"])
        self.assertTrue(records[0].ok)
        self.assertFalse(records[0].memo or records[0].hidden)

    def test_deletion_marker_does_not_downgrade_existing_red(self):
        book, pi, paragraph = self.book("甲乙丙")
        editor = Editor(book, memo="none")
        editor.apply([Item("A", "delete", pi, 1, 2, "乙", "", "", auto=True),
                      Item("B1", "mark", pi, 2, 3, "丙", "丁", "")])
        self.assertEqual(paragraph_text(paragraph), "甲丙")
        self.assertEqual(self.color(editor, paragraph, "丙"), GRADE_STYLE["red"]["color"])

    def test_no_visible_character_is_report_only_without_new_body_characters(self):
        for original, item in [
                ("乙", ("A", "delete", 0, 1, "乙", "", "", True)),
                ("  ", ("C", "insert", 1, 1, "", "新", "新", False))]:
            with self.subTest(original=original):
                book, pi, paragraph = self.book(original)
                kind, action, start, end, hwpx, ocr, new, auto = item
                records = Editor(book, memo="none").apply([
                    Item(kind, action, pi, start, end, hwpx, ocr, new, auto=auto)])
                self.assertEqual(paragraph_text(paragraph), "" if auto else original)
                self.assertTrue(records[0].ok and records[0].hidden)
                self.assertFalse(records[0].memo)

    def test_no_cleanup_occurs_implicitly_when_opening_editor(self):
        book, _, paragraph = self.book()
        begin, end = memo_field("[확인 필요] 기존 메모", 1, "YeonE_OCR", "1")
        run = paragraph.find(HP + "run")
        run.insert(0, begin)
        run.append(end)
        original = etree.tostring(paragraph)
        Editor(book, memo="none")
        self.assertEqual(etree.tostring(paragraph), original)

    def test_repeated_zero_width_mark_is_idempotent_and_can_upgrade_blue(self):
        book, pi, paragraph = self.book("甲乙丙")
        Editor(book, memo="none").apply([Item("B2", "replace", pi, 1, 2, "乙", "发", "發", auto=True)])
        item = Item("C", "insert", pi, 1, 1, "", "新", "新")
        editor = Editor(book, memo="none")
        editor.apply([item])
        self.assertEqual(paragraph_text(paragraph), "甲發丙")
        self.assertEqual(self.color(editor, paragraph, "發"), GRADE_STYLE["red"]["color"])
        before = etree.tostring(paragraph)
        Editor(book, memo="none").apply([item])
        self.assertEqual(etree.tostring(paragraph), before)


class SelectiveMemoCleanup(unittest.TestCase):
    def make_book(self):
        paragraph = etree.Element(HP + "p", id="body", paraPrIDRef="42")
        run = etree.SubElement(paragraph, HP + "run", charPrIDRef="31")
        etree.SubElement(run, HP + "t").text = "瀉白散 — 人工校正"
        return SimpleNamespace(paragraphs=[SimpleNamespace(element=paragraph)]), paragraph, run

    def add_memo(self, run, text, author="YeonE_OCR", number=1):
        begin, end = memo_field(text, number, author, "1")
        run.insert(0, begin)
        run.append(end)
        return begin, end

    def test_removes_only_bookmark_owned_fields_preserving_body_and_other_comments(self):
        book, paragraph, run = self.make_book()
        user = self.add_memo(run, "[확인 필요] 사람이 쓴 메모", author="Person")
        ocr = self.add_memo(run, "[확인 필요 — 일반 OCR] 판독", number=2)
        ocr_legend = self.add_memo(run, LEGEND, number=3)
        unknown = self.add_memo(run, "다른 기능이 남긴 메모", number=4)
        preserved = {id(node): etree.tostring(node) for pair in (user, ocr, ocr_legend, unknown) for node in pair}
        baseline = deepcopy(paragraph)
        self.add_memo(run, "[자동 교정] 甲 → 乙", number=5)
        self.add_memo(run, "[제안] 빠진 글자 넣기", number=6)
        self.add_memo(run, COLLATE_LEGEND, number=7)
        result = remove_collate_memos(book)
        self.assertEqual((result.removed, result.skipped), (3, 0))
        self.assertEqual(etree.tostring(paragraph), etree.tostring(baseline))
        for pair in (user, ocr, ocr_legend, unknown):
            for node in pair:
                self.assertEqual(etree.tostring(node), preserved[id(node)])
        self.assertEqual(paragraph_text(paragraph), "瀉白散 — 人工校正")
        self.assertEqual((paragraph.get("paraPrIDRef"), run.get("charPrIDRef")), ("42", "31"))
        self.assertEqual(remove_collate_memos(book).removed, 0)

    def test_missing_or_mismatched_pair_preserves_entire_xml(self):
        for defect in ("missing", "fieldid", "reverse", "duplicate_begin", "duplicate_end", "different_paragraph"):
            with self.subTest(defect=defect):
                book, paragraph, run = self.make_book()
                begin, end = self.add_memo(run, "[확인 필요] 판독")
                if defect == "missing":
                    run.remove(end)
                elif defect == "fieldid":
                    end[0].set("fieldid", "unknown")
                elif defect == "reverse":
                    run.remove(end)
                    run.insert(0, end)
                elif defect == "duplicate_begin":
                    run.append(deepcopy(begin))
                elif defect == "duplicate_end":
                    run.append(deepcopy(end))
                elif defect == "different_paragraph":
                    other = etree.Element(HP + "p")
                    other_run = etree.SubElement(other, HP + "run")
                    other_run.append(end)
                    book.paragraphs.append(SimpleNamespace(element=other))
                before = [etree.tostring(p.element) for p in book.paragraphs]
                result = remove_collate_memos(book)
                self.assertEqual(result.removed, 0)
                self.assertGreater(result.skipped, 0)
                self.assertEqual([etree.tostring(p.element) for p in book.paragraphs], before)

    def test_begin_and_end_in_different_runs_are_removed_without_changing_runs(self):
        book, paragraph, run = self.make_book()
        next_run = etree.SubElement(paragraph, HP + "run", charPrIDRef="32")
        etree.SubElement(next_run, HP + "t").text = "後文"
        before = etree.tostring(paragraph)
        begin, end = memo_field("[확인 필요] 판독", 1, "YeonE_OCR", "1")
        run.insert(0, begin)
        next_run.append(end)
        result = remove_collate_memos(book)
        self.assertEqual((result.removed, result.skipped), (1, 0))
        self.assertEqual(etree.tostring(paragraph), before)

    def test_mixed_control_or_embedded_field_is_preserved(self):
        for defect in ("mixed_ctrl", "nested_field", "outside_text"):
            with self.subTest(defect=defect):
                book, paragraph, run = self.make_book()
                begin, _ = self.add_memo(run, "[자동 교정] 판독")
                if defect == "mixed_ctrl":
                    etree.SubElement(begin, HP + "bookmark", name="User bookmark")
                elif defect == "nested_field":
                    sub = begin[0].find(HP + "subList")
                    self.add_memo(sub.find(f"{HP}p/{HP}run"), "User nested memo", author="Person")
                else:
                    begin.tail = "keep this text"
                before = etree.tostring(paragraph)
                result = remove_collate_memos(book)
                self.assertEqual((result.removed, result.skipped), (0, 1))
                self.assertEqual(etree.tostring(paragraph), before)


if __name__ == "__main__":
    unittest.main()
