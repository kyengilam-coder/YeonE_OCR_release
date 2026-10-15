"""한 번에 문자 PDF: 새 판독·쪽 범위·이어 하기·중지와 이미지 입력을 실제 파일로 확인한다."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pymupdf

from tests.test_textlayer import VCOLS, scan
from yeone_ocr import cli, textlayer
from yeone_ocr.pdfio import text_layer
from yeone_ocr.reader_mock import MockReader
from yeone_ocr.state import Work


class PdfOnlyRun(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        pdf, truth = scan(self.root, VCOLS, vertical=True)
        self.source = self.root / "원본.pdf"
        with pymupdf.open(pdf) as doc:
            for row in truth:
                x0, y0 = row[0][1][:2]
                doc[0].insert_text((x0 + 4, y0), "".join(c for c, _ in row), fontname="china-t", fontsize=22, rotate=270, render_mode=3)
            doc.fullcopy_page(0)
            doc.save(self.source)
        self.work = self.root / "work"
        self.out = textlayer.out_path(self.work, self.source)
        self.argv = ["run", str(self.source), "--work", str(self.work), "--reader", "mock", "--mode", "vertical",
                     "--workers", "1", "--checkpoint", "1", "--pdf-only"]
        cli.CANCEL.clear()
        self.addCleanup(cli.CANCEL.clear)

    def run_cli(self, argv=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = cli.main(argv or self.argv)
        return rc, output.getvalue()

    def test_fresh_run_respects_range_and_resume_without_hwpx_or_template(self):
        source_bytes = self.source.read_bytes()
        argv = self.argv + ["--pages", "2", "--layout", "flow", "--template", str(self.root / "missing.hwpx"),
                            "--preset", str(self.root / "missing.json")]
        rc, log = self.run_cli(argv)
        self.assertEqual(rc, 0, log)
        self.assertEqual(sorted(p.name for p in (self.work / "readings").glob("p*.json")), ["p002.json"])
        self.assertFalse(list(self.work.rglob("*.hwpx")))
        self.assertTrue((self.work / "review.html").exists())
        self.assertTrue((self.work / "report.md").exists())
        with pymupdf.open(self.out) as done:
            self.assertEqual(len(done), 2)
            self.assertEqual(textlayer.own_text(done, 1), [])
            self.assertEqual([c["text"] for c in text_layer(done, 2, own=True).vertical_columns], VCOLS)
        work = Work(self.source, self.work)
        rec = work.load_record(2)
        rec["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"] = "手校保存"
        work.save_record(2, rec)
        with mock.patch.object(MockReader, "read_page", side_effect=AssertionError("완료한 쪽을 다시 판독함")):
            rc, log = self.run_cli(argv)
        self.assertEqual(rc, 0, log)
        with pymupdf.open(self.out) as done:
            self.assertIn("手校保存", "".join(textlayer.own_text(done, 2)))
        self.assertEqual(self.source.read_bytes(), source_bytes)
        self.assertFalse(list(self.work.rglob("*.hwpx")))

    def test_stop_in_pdf_stage_preserves_output_and_ocr_resumes_without_calls(self):
        Work(self.source, self.work).ensure_source(300)
        self.out.write_bytes(b"previous completed PDF")
        def progress(stage, done, total):
            if stage == "textlayer" and done == 1:
                cli.CANCEL.set()
        with mock.patch.object(cli, "PROGRESS", progress):
            rc, log = self.run_cli()
        self.assertEqual(rc, 130, log)
        self.assertEqual(self.out.read_bytes(), b"previous completed PDF")
        self.assertEqual(len(list((self.work / "readings").glob("p*.json"))), 2)
        self.assertFalse(list(self.work.rglob("*.hwpx")))
        cli.CANCEL.clear()
        with mock.patch.object(MockReader, "read_page", side_effect=AssertionError("중지 전 완료한 판독을 다시 호출함")):
            rc, log = self.run_cli()
        self.assertEqual(rc, 0, log)
        with pymupdf.open(self.out) as done:
            self.assertTrue(all(textlayer.own_text(done, n) for n in (1, 2)))

    def test_stop_during_ocr_saves_reading_without_hwpx_and_resumes_remaining_page(self):
        def progress(stage, done, total):
            if stage == "read" and done == 1:
                cli.CANCEL.set()
        with mock.patch.object(cli, "PROGRESS", progress):
            rc, log = self.run_cli()
        self.assertEqual(rc, 130, log)
        self.assertTrue((self.work / "readings" / "p001.json").exists())
        self.assertFalse((self.work / "readings" / "p002.json").exists())
        self.assertFalse(list(self.work.rglob("*.hwpx")))
        self.assertFalse(self.out.exists())
        self.assertNotIn("HWPX 를 남겼습니다", log)
        cli.CANCEL.clear()
        with mock.patch.object(cli, "read_one", wraps=cli.read_one) as read:
            rc, log = self.run_cli()
        self.assertEqual(rc, 0, log)
        self.assertEqual([call.args[4] for call in read.call_args_list], [2])
        self.assertTrue(self.out.exists())

    def test_image_input_uses_same_pdf_pipeline(self):
        image_source = self.root / "scan.png"
        with pymupdf.open(self.source) as doc:
            reader = MockReader({1: text_layer(doc, 1)})
        argv = self.argv.copy()
        argv[1] = str(image_source)
        with mock.patch.object(cli, "make_reader", return_value=reader):
            rc, log = self.run_cli(argv)
        self.assertEqual(rc, 0, log)
        self.assertEqual(reader.calls["read_page"], 1)
        out = textlayer.out_path(self.work, image_source)
        with pymupdf.open(out) as done:
            self.assertEqual(len(done), 1)
            self.assertEqual([c["text"] for c in text_layer(done, 1, own=True).vertical_columns], VCOLS)
        self.assertFalse(list(self.work.rglob("*.hwpx")))

    def test_pdf_save_failure_is_not_reported_as_success(self):
        with mock.patch.object(textlayer, "searchable_pdf", side_effect=PermissionError("PDF is open")):
            rc, log = self.run_cli()
        self.assertEqual(rc, 1, log)
        self.assertFalse(self.out.exists())
        self.assertNotIn("끝. 결과: out/ 의 검색 가능한 PDF", log)
        self.assertTrue((self.work / "report.md").exists())


if __name__ == "__main__":
    unittest.main()
