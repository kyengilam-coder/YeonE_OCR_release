"""Human correction provenance, committed chains and conservative OCR hints."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yeone_ocr.collate.hwpx_edit import paragraph_text
from yeone_ocr.collate.section_edit import (
    ManualProtectionError, SectionEditError, SectionEditSession, _Archive,
    _replace_text, manual_history_path, manual_protected_paragraphs,
)
from yeone_ocr.collate.sections import HwpxBook
from yeone_ocr.correction_memory import (
    MAX_CASES, MAX_PROMPT_CHARS, load_correction_memory, memory_from_snapshot,
)


class CorrectionMemoryTests(unittest.TestCase):
    def setUp(self):
        from hwpx.document import HwpxDocument
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.pdf = self.root / "book.pdf"
        self.pdf.write_bytes(b"synthetic source PDF, never sent to an API")
        self.source = self.root / "book.hwpx"
        self.target = self.root / "book_교정.hwpx"
        document = HwpxDocument.new()
        try:
            document.add_heading("瀉白散", level=1)
            style = document.styles.ensure_run(size=11)
            for text in ("方解：사火之劑。清肺熱。", "方解：사火之劑。清肺熱。", "肝熱甚，氣上逆。"):
                document.add_paragraph(text, style_id_ref=0, char_pr_id_ref=style, inherit_style=False)
            document.add_heading("葦莖湯", level=1)
            document.add_paragraph("另一區劃。", style_id_ref=0, char_pr_id_ref=style, inherit_style=False)
            document.save_to_path(str(self.source))
        finally:
            document.close()
        book = HwpxBook(str(self.source))
        try:
            section = book.section(book.anchors(), 0)
            self.start, self.end = section.start, section.end
            self.index = book.texts().index("方解：사火之劑。清肺熱。")
            self.before = book.texts()[self.index]
        finally:
            book.doc.close()
        self.after = self.before.replace("사", "瀉")
        self.session = self.open()

    def open(self):
        return SectionEditSession.open(self.pdf, self.source, "瀉白散", self.start, self.end)

    def memory(self):
        return load_correction_memory(self.target, self.pdf)

    def history(self):
        return json.loads(manual_history_path(self.target).read_text("utf-8"))

    def write_history(self, history):
        manual_history_path(self.target).write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")

    def test_success_records_actual_saved_text_and_source_identity(self):
        source = self.source.read_bytes()
        result = self.session.save({self.index: self.after})
        self.assertEqual(result.learning_count, 1)
        self.assertEqual(result.learning_path, manual_history_path(self.target))
        self.assertEqual(result.learning_warning, "")
        event = self.history()["correction_events"][0]
        self.assertEqual((event["before"], event["after"]), (self.before, self.after))
        self.assertEqual(event["pdf"]["sha256"], hashlib.sha256(self.pdf.read_bytes()).hexdigest())
        self.assertEqual(event["source"]["sha256"], hashlib.sha256(source).hexdigest())
        self.assertEqual(event["section"], {"name": "瀉白散", "start": self.start, "end": self.end})
        memory = self.memory()
        self.assertEqual(memory.count, 1)
        self.assertIn('"수정 전":"사"', memory.prompt)
        self.assertIn('"사람이 저장한 글자":"瀉"', memory.prompt)
        self.assertIn("일괄 치환", memory.prompt)
        self.assertEqual(self.source.read_bytes(), source)
        self.assertEqual(manual_protected_paragraphs(self.target), {self.index})

    def test_before_is_current_corrected_text_not_original_hwpx(self):
        archive = _Archive(self.source.read_bytes())
        corrected_before = self.before.replace("사", "샤")
        _replace_text(archive.paragraphs[self.index], corrected_before)
        archive.write(self.target, {archive.locations[self.index][0]})
        self.open().save({self.index: self.after})
        self.assertEqual(self.history()["correction_events"][0]["before"], corrected_before)
        self.assertIn('"수정 전":"샤"', self.memory().prompt)
        self.assertNotIn('"수정 전":"사"', self.memory().prompt)

    def test_noop_and_invalid_unsaved_edits_create_no_learning(self):
        self.session.save({self.index: self.before})
        self.assertFalse(manual_history_path(self.target).exists())
        with self.assertRaises(SectionEditError):
            self.session.save({self.index: "invalid\nparagraph"})
        self.assertFalse(self.target.exists())
        self.assertEqual(self.session.correction_records(), [])
        self.assertEqual(self.memory().count, 0)

    def test_followup_keeps_first_correction_and_undo_removes_reversed_example(self):
        self.session.save({self.index: self.after})
        final = self.after.replace("肺熱", "肺火")
        self.session.save({self.index: final})
        self.assertEqual(self.memory().count, 2)
        events = self.history()["correction_events"]
        self.assertEqual(events[1]["parent_id"], events[0]["id"])
        self.session.save({self.index: self.before.replace("肺熱", "肺火")})
        memory = self.memory()
        self.assertEqual(memory.count, 1)
        self.assertNotIn('"사람이 저장한 글자":"瀉"', memory.prompt)
        self.assertNotIn('"수정 전":"瀉"', memory.prompt)
        self.session.save({self.index: self.before})
        self.assertEqual((self.memory().count, self.memory().digest, self.memory().prompt), (0, "", ""))
        self.assertEqual(len(self.session.correction_records()), 4)

    def test_failed_publication_and_rollback_do_not_learn_unpublished_intent(self):
        self.session.save({self.index: self.after})
        original = self.target.read_bytes()
        real_replace = os.replace
        publications = 0

        def fail_publication_and_rollback(src, dst):
            nonlocal publications
            if Path(dst) == self.target:
                raise PermissionError("synthetic locked document")
            if Path(dst) == manual_history_path(self.target):
                publications += 1
                if publications > 1:
                    raise PermissionError("synthetic locked rollback")
            return real_replace(src, dst)

        tentative = self.after.replace("肺熱", "肺火")
        with patch("yeone_ocr.collate.section_edit.os.replace", side_effect=fail_publication_and_rollback):
            with self.assertRaises(PermissionError):
                self.session.save({self.index: tentative})
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(len(self.history()["correction_events"]), 2)  # durable intent survives
        self.assertEqual(self.memory().count, 1)
        self.assertNotIn('"사람이 저장한 글자":"火"', self.memory().prompt)
        reopened = self.open()
        self.assertEqual(len(reopened.correction_records()), 1)
        reopened.save({self.index: self.after.replace("肺熱", "肺寒")})
        events = self.history()["correction_events"]
        self.assertEqual(events[2]["parent_id"], events[0]["id"])
        self.assertEqual(self.memory().count, 2)
        self.assertNotIn('"사람이 저장한 글자":"火"', self.memory().prompt)

    def test_first_failed_save_with_durable_intent_produces_no_samples(self):
        # Simulate the first save of an already existing corrected document.
        self.target.write_bytes(self.source.read_bytes())
        session = self.open()
        real_replace = os.replace

        def fail(src, dst):
            if Path(dst) == self.target:
                raise PermissionError("document locked")
            return real_replace(src, dst)

        with patch("yeone_ocr.collate.section_edit.os.replace", side_effect=fail), \
                patch("pathlib.Path.unlink", side_effect=PermissionError("rollback locked")):
            with self.assertRaises(PermissionError):
                session.save({self.index: self.after})
        self.assertEqual(self.memory().count, 0)

    def test_other_pdf_is_not_used_and_legacy_hashes_are_not_backfilled(self):
        self.session.save({self.index: self.after})
        other = self.root / "other.pdf"
        other.write_bytes(b"different original PDF")
        self.assertEqual(load_correction_memory(self.target, other).count, 0)
        history = self.history()
        history.pop("feedback_version")
        history.pop("correction_events")
        self.write_history(history)
        self.assertEqual(self.memory().count, 0)
        reopened = self.open()
        self.assertEqual(reopened.correction_records(), [])
        reopened.save({self.index: self.after.replace("肺熱", "肺火")})
        memory = self.memory()
        self.assertEqual(memory.count, 1)
        self.assertNotIn('"사람이 저장한 글자":"瀉"', memory.prompt)

    def test_external_ascii_spaces_do_not_activate_learning_or_join_old_chain(self):
        self.session.save({self.index: self.after})
        self.assertEqual(self.memory().count, 1)
        archive = _Archive(self.target.read_bytes())
        current = self.after.replace("火", " 火 ")
        _replace_text(archive.paragraphs[self.index], current)
        archive.write(self.target, {archive.locations[self.index][0]})
        target_bytes = self.target.read_bytes()
        history_bytes = manual_history_path(self.target).read_bytes()
        memory = self.memory()
        self.assertEqual(memory.count, 0)
        self.assertEqual(memory.prompt, "")
        self.assertEqual(memory.warning, "")
        self.assertEqual(manual_protected_paragraphs(self.target), {self.index})
        self.assertEqual(self.open().correction_records(), [])
        self.assertEqual(self.target.read_bytes(), target_bytes)
        self.assertEqual(manual_history_path(self.target).read_bytes(), history_bytes)
        self.open().save({self.index: current.replace("肺熱", "肺火")})
        events = self.history()["correction_events"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["before"], current)
        self.assertIsNone(events[-1]["parent_id"])
        self.assertEqual(self.memory().count, 1)
        self.assertNotIn('"사람이 저장한 글자":"瀉"', self.memory().prompt)

    def test_conflicting_same_context_examples_are_excluded(self):
        self.session.save({self.index: self.after, self.index + 1: self.before.replace("사", "紗")})
        self.assertEqual(self.memory().count, 0)
        self.assertEqual(len(self.session.correction_records()), 2)

    def test_insert_delete_are_preserved_but_not_used_as_replacement_rules(self):
        self.session.save({self.index: self.before + "增加", self.index + 2: "肝熱甚，"})
        memory = self.memory()
        self.assertEqual(memory.count, 0)
        events = self.history()["correction_events"]
        self.assertEqual(len(events), 2)
        self.assertTrue(events[0]["after"].endswith("增加"))

    def test_malformed_learning_does_not_weaken_manual_protection_or_block_save(self):
        self.session.save({self.index: self.after})
        history = self.history()
        history["correction_events"][0]["before_sha256"] = "invalid"
        self.write_history(history)
        memory = self.memory()
        self.assertEqual(memory.count, 0)
        self.assertTrue(memory.warning)
        self.assertEqual(manual_protected_paragraphs(self.target), {self.index})
        with self.assertRaises(SectionEditError):
            self.open().correction_records()
        result = self.open().save({self.index: self.after.replace("肺熱", "肺火")})
        self.assertTrue(result.learning_warning)
        self.assertEqual(paragraph_text(_Archive(self.target.read_bytes()).paragraphs[self.index]),
                         self.after.replace("肺熱", "肺火"))
        history = self.history()
        history["paragraphs"][str(self.index)]["signature"] = "bad"
        self.write_history(history)
        with self.assertRaises(ManualProtectionError):
            self.memory()

    def test_bad_parent_chain_and_missing_version_are_not_silently_accepted(self):
        self.session.save({self.index: self.after})
        valid = self.history()
        bad = json.loads(json.dumps(valid))
        bad["correction_events"][0]["parent_id"] = bad["correction_events"][0]["id"]
        self.write_history(bad)
        self.assertTrue(self.memory().warning)
        valid.pop("feedback_version")
        self.write_history(valid)
        self.assertTrue(self.memory().warning)

    def test_digest_ignores_timestamps_and_snapshot_records_do_not_rehash_pdf(self):
        self.session.save({self.index: self.after})
        initial = self.memory()
        history = self.history()
        history["correction_events"][0]["time"] = "2099-01-01T00:00:00+0900"
        self.write_history(history)
        self.assertEqual(self.memory().digest, initial.digest)
        with patch("yeone_ocr.collate.section_edit._fingerprint", side_effect=AssertionError("unexpected hash")):
            records = self.session.correction_records()
        self.assertEqual(records[0]["before"], self.before)
        self.assertEqual(records[0]["after"], self.after)

    def test_prompt_is_bounded_and_does_not_embed_entire_paragraph(self):
        # Small substitutions far apart create many separate examples.
        before = "".join(f"A{i:03d}BCDEFG사HIJ{i:03d}KLMNO" for i in range(40))
        after = before.replace("사", "瀉")
        source = _Archive(self.source.read_bytes())
        _replace_text(source.paragraphs[self.index], before)
        source.write(self.source, {source.locations[self.index][0]})
        session = self.open()
        session.save({self.index: after})
        memory = self.memory()
        self.assertLessEqual(memory.count, MAX_CASES)
        self.assertLessEqual(len(memory.prompt), MAX_PROMPT_CHARS)
        self.assertNotIn(before, memory.prompt)
        self.assertEqual(memory.count, MAX_CASES)

    def test_recent_hangul_correction_is_not_starved_by_older_alphabetical_cases(self):
        before = "".join(f"L{i:03d}ABCDEFGHaIJKLMN{i:03d}OPQ" for i in range(25))
        source = _Archive(self.source.read_bytes())
        _replace_text(source.paragraphs[self.index], before)
        source.write(self.source, {source.locations[self.index][0]})
        session = self.open()
        session.save({self.index: before.replace("a", "b")})
        self.assertEqual(self.memory().count, MAX_CASES)
        session.save({self.index + 1: self.after})
        memory = self.memory()
        self.assertEqual(memory.count, MAX_CASES)
        self.assertIn('"수정 전":"사"', memory.prompt)
        self.assertIn('"사람이 저장한 글자":"瀉"', memory.prompt)

    def test_duplicate_case_uses_latest_valid_save_priority(self):
        tokens = [f"L{i:03d}ABCDEFGHaIJKLMN{i:03d}OPQ" for i in range(25)]
        before = "".join(tokens)
        source = _Archive(self.source.read_bytes())
        _replace_text(source.paragraphs[self.index], before)
        _replace_text(source.paragraphs[self.index + 1], tokens[-1])
        source.write(self.source, {source.locations[self.index][0]})
        session = self.open()
        session.save({self.index: before.replace("a", "b")})
        self.assertNotIn('"앞 문맥":"L024ABCDEFGH"', self.memory().prompt)
        session.save({self.index + 1: tokens[-1].replace("a", "b")})
        memory = self.memory()
        self.assertEqual(memory.count, MAX_CASES)
        self.assertIn('"앞 문맥":"L024ABCDEFGH"', memory.prompt)

    def test_effective_case_set_keeps_digest_when_priority_changes(self):
        self.session.save({self.index: self.after})
        self.session.save({self.index + 2: "肝寒甚，氣上逆。"})
        initial = self.memory()
        self.assertEqual(initial.count, 2)
        self.session.save({self.index: self.before})
        self.session.save({self.index: self.after})
        current = self.memory()
        self.assertEqual(current.prompt, initial.prompt)
        self.assertEqual(current.digest, initial.digest)

    def test_snapshot_memory_is_immutable(self):
        self.session.save({self.index: self.after})
        memory = self.memory()
        with self.assertRaises(AttributeError):
            memory.count = 999

    def test_records_follow_save_order_across_paragraphs(self):
        self.session.save({self.index + 1: self.after})
        self.session.save({self.index: self.after})
        self.session.save({self.index + 1: self.after.replace("肺熱", "肺火")})
        records = self.session.correction_records()
        self.assertEqual([record["paragraph"] for record in records],
                         [self.index + 1, self.index, self.index + 1])


if __name__ == "__main__":
    unittest.main()
