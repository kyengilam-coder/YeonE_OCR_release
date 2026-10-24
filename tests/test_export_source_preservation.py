"""Source-policy readings retain exact Unicode in exports when editing is off.

These inspect stored text, not rendered font/glyph support. Default editorial
normalization remains intentional and is outside this preservation contract.
"""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz

from yeone_ocr import cli, comparison_bridge as bridge
from yeone_ocr.comparison import ComparisonProject
from yeone_ocr.merge import merge_passes
from yeone_ocr.schema import Column, PageReading, Paragraph, Region, RegionReread
from yeone_ocr.state import Work
from yeone_ocr.text_policy import SOURCE_TEXT_POLICY


SOURCE = "靑硏樂神\U0002f800一\ufe00二\U000e0100한ᄒᆞᆫ\u200b。"
SECOND = "乙\U000e0101甲\ufe01簾，云雲。"


class ExportSourcePreservation(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parents[1] / "work"
        workspace.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="export-source-preserve-", dir=workspace)
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.enterContext(patch.object(tempfile, "tempdir", str(self.folder)))
        self.pdf = self.folder / "source.pdf"
        with fitz.open() as document:
            document.new_page()
            document.save(self.pdf)
        self.work = Work(str(self.pdf), str(self.folder / "records"))
        reading = PageReading(pdf_page=1, regions=[Region(id="body", kind="본문",
            paragraphs=[Paragraph(columns=[Column(col_from_right=1, text=SOURCE)]),
                        Paragraph(columns=[Column(col_from_right=2, text=SECOND)])])])
        first = cli.nfc_reading(reading, text_policy=SOURCE_TEXT_POLICY)
        second = {"body": RegionReread(columns=[Column(col_from_right=1, text=SOURCE),
                                                Column(col_from_right=2, text=SECOND)])}
        merged, stats = merge_passes(first, second, text_policy=SOURCE_TEXT_POLICY)
        self.record = {"file": self.pdf.name, "pdf_page": 1, "text_policy": SOURCE_TEXT_POLICY,
                       "pass1": first.model_dump(), "pass2": {k: v.model_dump() for k, v in second.items()},
                       "merged": merged.model_dump(), "merge_stats": stats, "crops": {}}
        self.work.save_record(1, self.record)
        self.original_record = self.work.record_path(1).read_bytes()
        self.original_pdf = self.pdf.read_bytes()
        cli.CANCEL.clear()

    def test_main_build_keeps_source_codepoints_with_all_editing_disabled(self):
        for flags in (("--no-normalize",), ("--no-traditional", "--no-yun", "--no-punctuation")):
            with self.subTest(flags=flags):
                args = cli.build_parser().parse_args(["build", str(self.pdf), "--work", str(self.work.dir),
                    "--docx", "--no-context", "--no-highlight", *flags])
                self.assertFalse(cli.normalization_options(args).enabled)
                with fitz.open(self.pdf) as document, contextlib.redirect_stdout(io.StringIO()), \
                        patch.object(cli, "create_reader") as factory:
                    self.assertEqual(cli.cmd_build(args, self.work, document), 0)
                    factory.assert_not_called()
                for kind in ("hwpx", "docx"):
                    with self.subTest(kind=kind):
                        paragraphs = bridge._read_export(self.work.out_dir / ("source." + kind), kind)
                        self.assertIn(SOURCE, paragraphs)
                        self.assertIn(SECOND, paragraphs)
                audit = json.loads((self.work.out_dir / "source_normalization.json").read_text(encoding="utf-8"))
                self.assertFalse(audit["enabled"])
                self.assertEqual(self.work.record_path(1).read_bytes(), self.original_record)
                self.assertEqual(self.pdf.read_bytes(), self.original_pdf)

    def test_import_export_and_reopen_keep_source_and_human_snapshot(self):
        project = ComparisonProject.open(self.pdf)
        self.assertEqual(bridge.import_yeone_records(project, self.work.dir), 1)
        expected = SOURCE + "\n" + SECOND
        self.assertEqual(project.text(1), expected)
        raw = copy.deepcopy(project.page(1)["runs"])
        corrected = expected + "\n人工確定：\U0002f800\U000e0100。"
        project.set_text(1, corrected)
        saved = self.folder / "source.yeone-compare.json"
        project.save(saved)
        original_project = saved.read_bytes()
        reopened = ComparisonProject.load(saved, source_path=self.pdf)
        page_before = copy.deepcopy(reopened.page(1))
        for kind in ("txt", "hwpx", "docx"):
            with self.subTest(kind=kind):
                path = self.folder / ("export." + kind)
                bridge.export_project(reopened, path, kind)
                actual = path.read_text(encoding="utf-8") if kind == "txt" else "\n".join(bridge._read_export(path, kind)).strip("\n")
                self.assertEqual(actual, corrected)
                self.assertEqual(reopened.page(1), page_before)
        self.assertEqual(reopened.page(1)["runs"], raw)
        self.assertEqual(saved.read_bytes(), original_project)
        self.assertEqual(self.work.record_path(1).read_bytes(), self.original_record)
        self.assertEqual(self.pdf.read_bytes(), self.original_pdf)


if __name__ == "__main__":
    unittest.main()
