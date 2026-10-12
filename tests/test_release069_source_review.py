"""원본 변경·이전 판 이관·보관 실패의 데이터 보존 회귀 검사(모델 호출 없음)."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yeone_ocr.state import Work


class SourceReview(unittest.TestCase):
    def test_constructor_leaves_legacy_paid_records_available_for_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "input.pdf"; source.write_bytes(b"original A")
            dest = root / "result"; (dest / "readings").mkdir(parents=True)
            rec = {"pdf_page": 1, "marker": "paid OCR"}
            (dest / "readings" / "p001.json").write_text(json.dumps(rec), encoding="utf-8")
            work = Work(str(source), str(dest))
            self.assertEqual(work.load_record(1), rec)
            self.assertFalse((dest / "archives").exists())
            with contextlib.redirect_stdout(io.StringIO()):
                work.ensure_source()
            self.assertIsNone(work.load_record(1))
            copies = list((dest / "archives").rglob("p001.json"))
            self.assertEqual(len(copies), 1)
            self.assertEqual(json.loads(copies[0].read_text(encoding="utf-8")), rec)

    def test_same_work_detects_replacement_even_when_size_and_mtime_match(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "input.pdf"; source.write_bytes(b"original A")
            work = Work(str(source), str(root / "result")); work.ensure_source(72)
            work.save_record(1, {"pdf_page": 1, "marker": "original A"})
            old = source.stat(); source.write_bytes(b"original B"); os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
            with contextlib.redirect_stdout(io.StringIO()):
                work.ensure_source(72)
            self.assertIsNone(work.load_record(1))
            self.assertEqual(len(list((work.dir / "archives").rglob("p001.json"))), 1)

    def test_content_only_validation_preserves_recorded_dpi(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "input.pdf"; source.write_bytes(b"original A")
            work = Work(str(source), str(root / "result")); work.ensure_source(72)
            rec = {"pdf_page": 1}; work.save_record(1, rec)
            again = Work(str(source), str(work.dir)); again.ensure_source()
            self.assertEqual(again.load_record(1), rec)
            self.assertEqual(json.loads((work.dir / "source.json").read_text(encoding="utf-8"))["dpi"], 72)

    def test_wrong_manifest_shape_is_archived_without_losing_records(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "input.pdf"; source.write_bytes(b"original A")
            work = Work(str(source), str(root / "result")); rec = {"pdf_page": 1}; work.save_record(1, rec)
            (work.dir / "source.json").write_text("[]", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                work.ensure_source()
            copies = list((work.dir / "archives").rglob("p001.json"))
            self.assertEqual(len(copies), 1)
            self.assertEqual(json.loads(copies[0].read_text(encoding="utf-8")), rec)
            self.assertIsInstance(json.loads((work.dir / "source.json").read_text(encoding="utf-8")), dict)

    def test_archive_move_failure_rolls_back_entire_active_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "input.pdf"; source.write_bytes(b"original A")
            work = Work(str(source), str(root / "result")); rec = {"pdf_page": 1}; work.save_record(1, rec)
            work.page_image_path(1).write_bytes(b"paid bitmap")
            manifest = (work.dir / "source.json").read_bytes()
            source.write_bytes(b"original B")
            replace = os.replace
            # Windows runners may provide a short-name TEMP path (RUNNER~1),
            # while Work archives through its resolved absolute path.
            locked_directory = work.readings_dir.resolve()
            def locked(src, dst):
                if Path(src).resolve() == locked_directory:
                    raise PermissionError("simulated Windows directory lock")
                return replace(src, dst)
            with patch("yeone_ocr.state.os.replace", side_effect=locked), self.assertRaises(PermissionError):
                work.ensure_source()
            self.assertEqual(work.load_record(1), rec)
            self.assertEqual(work.page_image_path(1).read_bytes(), b"paid bitmap")
            self.assertEqual((work.dir / "source.json").read_bytes(), manifest)

    def test_absent_source_allows_reading_saved_records(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); work = Work(str(root / "absent.pdf"), str(root / "result"))
            rec = {"pdf_page": 1}; work.save_record(1, rec)
            work.ensure_source(300)
            self.assertEqual(work.load_record(1), rec)
            self.assertFalse((work.dir / "source.json").exists())


if __name__ == "__main__":
    unittest.main()
