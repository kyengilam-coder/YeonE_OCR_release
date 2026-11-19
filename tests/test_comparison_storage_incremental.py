"""Storage contracts: old books, edits, checkpoints, failure and single-file copies."""
import gzip
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import fitz

from ab_ocr import ocr
from yeone_ocr import comparison as core, comparison_storage as storage


class IncrementalStorageTests(unittest.TestCase):
    def test_corrupt_snapshot_never_replaces_published_project(self):
        self.project.save(self.target)
        before = self.target.read_bytes()
        self.project.set_text(1, 'pending correction')
        original = storage._write_rows
        def corrupt(connection, *args):
            generation = original(connection, *args)
            connection.execute('PRAGMA writable_schema=ON')
            connection.execute("UPDATE sqlite_master SET rootpage=999999 WHERE name='pages'")
            return generation
        with patch.object(storage, '_write_rows', side_effect=corrupt):
            with self.assertRaises(storage.ProjectStorageError):
                self.project.save()
        self.assertEqual(self.target.read_bytes(), before)
        self.assertTrue(self.project.dirty)

    def test_truncated_database_has_specific_diagnostic(self):
        self.project.save(self.target)
        data = self.target.read_bytes()
        self.target.write_bytes(data[:-4096])
        with self.assertRaises(storage.ProjectStorageError) as raised:
            self.load()
        self.assertEqual(raised.exception.storage_detail['missing_bytes'], 4096)
        self.assertEqual(raised.exception.storage_detail['sqlite_errorname'], 'SQLITE_CORRUPT')

    def test_busy_commit_waits_for_reader_without_repeating_transaction(self):
        self.project.save(self.target)
        reader = sqlite3.connect(self.target, check_same_thread=False)
        reader.execute('BEGIN')
        reader.execute('SELECT * FROM metadata').fetchone()
        released = threading.Event()
        def release():
            time.sleep(.25)
            reader.rollback()
            reader.close()
            released.set()
        worker = threading.Thread(target=release)
        worker.start()
        try:
            with closing(sqlite3.connect(self.target, timeout=.01)) as writer:
                writer.execute("UPDATE metadata SET generation='retry-proof'")
                storage._commit(writer, timeout=2)
            self.assertTrue(released.is_set())
        finally:
            worker.join()
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = self.root / "book.pdf"
        with fitz.open() as doc:
            for _ in range(64):
                doc.new_page(width=400, height=200)
            doc.save(self.source)
        self.project = core.ComparisonProject.open(self.source)
        for number in (1, 2):
            raw = ocr.RecognitionResult("神 神 漢，\n" * 100, [], str(self.source),
                self.project.source_sha256, number, 400, 200, "synthetic", "chi_tra", 6, 0,
                engine="tesseract")
            self.project.add_result(raw)
            self.project.set_text(number, "人校正" + str(number), reason="image checked")
            self.project.set_reviewed(number)
        self.target = self.root / "book.yeone-compare.json"

    def load(self, path=None, **kwargs):
        return core.ComparisonProject.load(path or self.target, **kwargs)

    def test_paged_load_checkpoint_eviction_and_dirty_recovery(self):
        self.project.save(self.target)
        project = self.load(paged=True)
        self.assertIsInstance(project._pages, storage.CheckpointPages)
        self.assertEqual(project._pages.cached, {})
        self.assertEqual(project.text(1), '人校正1')
        project.set_text(2, 'new unsaved correction')
        project._pages.trim()
        self.assertEqual(set(project._pages.cached), {2})
        recovery = self.root / 'paged-recovery.json'
        project.save_recovery(recovery)
        self.assertEqual(self.load(recovery).text(2), 'new unsaved correction')
        self.assertEqual(self.load().text(2), '人校正2')
        project.save()
        self.assertEqual(project._pages.cached, {})
        self.assertEqual(self.load(paged=True).to_dict(), self.load().to_dict())
        self.assertEqual(project.text(2), 'new unsaved correction')

    def test_paged_load_detects_corrupt_or_concurrently_replaced_pages(self):
        self.project.save(self.target)
        project = self.load(paged=True)
        other = self.load()
        other.set_text(2, 'other writer')
        other.save()
        with self.assertRaises(core.ComparisonError):
            project.text(1)
        with closing(sqlite3.connect(self.target)) as c, c:
            c.execute("UPDATE pages SET digest='invalid' WHERE number=1")
        with self.assertRaises(core.ComparisonError):
            self.load(paged=True)

    def test_lazy_resume_validates_accessed_page_and_preserves_untouched_rows(self):
        self.project.save(self.target)
        with patch.object(storage, "_decode_page", wraps=storage._decode_page) as decode:
            project = self.load(paged=True, lazy_pages=True)
            decode.assert_not_called()
            self.assertEqual(project.text(2), "人校正2")
            self.assertEqual(decode.call_count, 1)
            project.set_text(2, "수정한 2쪽")
            project.save()
        reloaded = self.load()
        self.assertEqual(reloaded.page(1), self.project.page(1))
        self.assertEqual(reloaded.text(2), "수정한 2쪽")
        with closing(sqlite3.connect(self.target)) as c, c:
            c.execute("UPDATE pages SET digest='invalid' WHERE number=1")
        lazy = self.load(paged=True, lazy_pages=True)
        self.assertEqual(lazy.text(2), "수정한 2쪽")
        with self.assertRaises(core.ComparisonError):
            lazy.page(1)

    def test_long_book_roundtrip_changed_page_only_and_portable_copy(self):
        self.project.save(self.target)
        self.assertTrue(storage.is_database(self.target))
        project = self.load()
        self.assertEqual(project.to_dict(), self.project.to_dict())
        before = self.target.read_bytes()
        project.page(1)  # public read must not dirty a page
        project.save()
        self.assertEqual(self.target.read_bytes(), before)
        project.set_text(2, "第二次校正")
        with patch.object(storage, "_write_rows", wraps=storage._write_rows) as rows, \
                patch.object(project, "_encoded", side_effect=AssertionError("whole-book serialization")):
            project.save()
        self.assertEqual(rows.call_args.args[2], [2])
        self.assertEqual(self.load().to_dict(), project.to_dict())
        copy = self.root / "copy.json"
        shutil.copyfile(self.target, copy)
        self.assertEqual(self.load(copy).to_dict(), project.to_dict())
        project.save(self.root / "save-as.json")
        self.assertEqual(self.load(project.project_path).to_dict(), project.to_dict())
        self.assertFalse(list(self.root.glob("*-wal")))
        self.assertFalse(list(self.root.glob("*-journal")))

    def test_legacy_plain_and_gzip_migration_preserve_every_record(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                encoded = self.project._encoded()
                self.target.write_bytes(gzip.compress(encoded) if compressed else encoded)
                project = self.load()
                before = project.to_dict()
                project.save()
                self.assertTrue(storage.is_database(self.target))
                self.assertEqual(self.load().to_dict(), before)

    def test_failed_migration_leaves_old_file_intact(self):
        self.target.write_bytes(gzip.compress(self.project._encoded()))
        project = self.load()
        before = self.target.read_bytes()
        project.set_text(1, "new")
        with patch.object(core, "_replace_file", side_effect=OSError("disk unavailable")):
            with self.assertRaises(storage.ProjectStorageError):
                project.save()
        self.assertEqual(self.target.read_bytes(), before)
        self.assertTrue(project.dirty)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_failed_transaction_rolls_back_and_retry_keeps_changes(self):
        self.project.save(self.target)
        before = self.project.to_dict()
        self.project.set_text(1, "pending")
        original = storage._write_rows

        def fail(*args):
            args[0].execute("PRAGMA cache_size=1")
            original(*args)
            # Force writes to the main database before rollback (a small
            # transaction fits in cache and would not exercise this case).
            args[0].execute("UPDATE pages SET payload=zeroblob(2000000) WHERE number=1")
            raise OSError("simulated full disk")

        with patch.object(storage, "_write_rows", side_effect=fail):
            with self.assertRaises(core.ComparisonError):
                self.project.save()
        self.assertEqual(self.load().to_dict(), before)
        self.assertTrue(self.project.dirty)
        self.project.save()
        self.assertEqual(self.load().text(1), "pending")

    def test_stale_window_and_external_edit_are_not_overwritten(self):
        self.project.save(self.target)
        stale = self.load()
        self.project.set_text(1, "newer")
        self.project.save()
        stale.set_text(2, "stale")
        with self.assertRaisesRegex(core.ComparisonError, "다른 창에서"):
            stale.save()
        with closing(sqlite3.connect(self.target)) as connection, connection:
            connection.execute("UPDATE pages SET digest='damaged' WHERE number=1")
        with self.assertRaisesRegex(core.ComparisonError, "다른 창에서"):
            self.project.save()
        with self.assertRaises(core.ComparisonError):
            self.load()

    def test_recovery_tracks_changes_independently_and_keeps_missing_source(self):
        self.project.save(self.target)
        recovery = self.root / "recovery.json"
        self.project.save_recovery(recovery)
        self.project.set_text(2, "changed")
        self.project.save()
        self.project.save_recovery(recovery)
        self.assertEqual(self.load(recovery).to_dict(), self.project.to_dict())
        self.project.set_text(1, "unsaved main")
        self.project.save_recovery(recovery)
        self.assertTrue(self.project.dirty)
        self.assertNotEqual(self.load().text(1), self.load(recovery).text(1))
        self.source.unlink()
        self.project.record_error(1, "tesseract", "source disappeared")
        self.project.save_recovery(recovery)
        self.assertEqual(self.load(recovery, allow_missing_source=True).to_dict(), self.project.to_dict())
        with self.assertRaises(core.SourceChangedError):
            self.project.save()

    def test_killed_writer_hot_journal_recovers_previous_snapshot(self):
        self.project.save(self.target)
        before = self.project.to_dict()
        script = """
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('PRAGMA cache_size=1')
c.execute('BEGIN IMMEDIATE')
c.execute('UPDATE pages SET payload=zeroblob(2000000) WHERE number=1')
os._exit(17)
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(self.target)], timeout=20)
        self.assertEqual(result.returncode, 17)
        self.assertTrue(Path(str(self.target) + "-journal").exists())
        self.assertEqual(self.load().to_dict(), before)
        self.assertFalse(Path(str(self.target) + "-journal").exists())

    def test_long_database_ignores_legacy_whole_json_ceiling_and_recovery_reopens(self):
        self.project.save(self.target)
        self.project.set_text(1, 'new human correction')
        with patch.object(core, 'MAX_JSON_BYTES', 10), patch.object(core, 'MAX_BYTES', 100000):
            self.project.save()
            self.assertEqual(self.load().text(1), 'new human correction')
            recovery = self.root / 'long.recovery.json'
            self.project.save_recovery(recovery)
            self.assertEqual(core.ComparisonProject.load(recovery).text(1), 'new human correction')

    def test_size_limit_and_untrusted_schema_do_not_modify_saved_work(self):
        self.project.save(self.target)
        before = self.project.to_dict()
        self.project.set_text(1, "pending")
        with patch.object(storage, "MAX_DATABASE_RAW_BYTES", 100):
            with self.assertRaises(core.ComparisonError):
                self.project.save()
        self.assertEqual(self.load().to_dict(), before)
        with closing(sqlite3.connect(self.target)) as connection, connection:
            connection.execute("CREATE TRIGGER unwanted AFTER INSERT ON pages BEGIN DELETE FROM pages; END")
        with self.assertRaises(core.ComparisonError):
            self.load()

    def test_relinked_source_and_source_change_during_commit(self):
        self.project.save(self.target)
        rebound = self.root / "rebound.pdf"
        shutil.copyfile(self.source, rebound)
        project = self.load(source_path=rebound)
        project.save()
        self.assertEqual(self.load().source_path, rebound)
        before = self.load().to_dict()
        project.set_text(1, "pending")
        original = storage._write_rows

        def change_source(*args):
            generation = original(*args)
            rebound.write_bytes(b"changed source")
            return generation

        with patch.object(storage, "_write_rows", side_effect=change_source):
            with self.assertRaises(core.SourceChangedError):
                project.save()
        rebound.write_bytes(self.source.read_bytes())
        self.assertEqual(self.load().to_dict(), before)
        project.save()
        self.assertEqual(self.load().text(1), "pending")

    def test_hardlinked_source_and_recovery_target_are_protected(self):
        alias = self.root / "source-alias.json"
        os.link(self.source, alias)
        original = self.source.read_bytes()
        with self.assertRaises(core.ComparisonError):
            self.project.save(alias)
        self.assertEqual(self.source.read_bytes(), original)
        self.project.save(self.target)
        journal = Path(str(self.target) + "-journal")
        os.link(self.source, journal)
        self.project.set_text(1, "pending")
        with self.assertRaises(core.ComparisonError):
            self.project.save()
        self.assertEqual(self.source.read_bytes(), original)
        journal.unlink()
        self.project.save()
        recovery_alias = self.root / "project-alias.json"
        os.link(self.target, recovery_alias)
        with self.assertRaises(core.ComparisonError):
            self.project.save_recovery(recovery_alias)

    def test_ai_response_decision_and_manual_history_roundtrip(self):
        from tests.test_comparison_ai import PipelineTests
        case = PipelineTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.test_two_phase_combined_proposals_preserve_raw_and_human_text()
        project = case.project
        with patch.object(core, "INCREMENTAL_PAGE_COUNT", 1):
            project.save(self.target)
            candidate = project.ai_proposals(1)[0]
            project.accept_ai_proposal(1, candidate["review_id"], candidate["id"])
            project.save()
            self.assertEqual(self.load().to_dict(), project.to_dict())
            candidate = next(p for p in project.ai_proposals(1) if p["decision"] == "pending")
            project.reject_ai_proposal(1, candidate["review_id"], candidate["id"])
            project.save()
            self.assertEqual(self.load().to_dict(), project.to_dict())
            project.set_text(1, "人校正文")
            project.set_reviewed(1)
            project.save()
            self.assertEqual(self.load().to_dict(), project.to_dict())

    def test_cancel_after_ai_response_resumes_without_duplicate_call(self):
        from tests.test_local_first import LocalFirstTests
        case = LocalFirstTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        with patch.object(core, "INCREMENTAL_PAGE_COUNT", 1), patch.dict(os.environ,
                {"YEONE_OCR_CONFUSIONS_PATH": str(self.root / 'resume-confusions.json')}), \
                patch('yeone_ocr.confusion_memory.load_for_review', return_value=None):
            case.test_cancel_after_paid_response_checkpoints_and_resumes_without_resend()
        self.assertTrue(storage.is_database(Path(case.record()["local_first"]["project_path"])))


if __name__ == "__main__":
    unittest.main()
