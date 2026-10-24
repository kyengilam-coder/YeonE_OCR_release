"""Saved human examples reach only new first-pass requests, with honest cache provenance."""
from argparse import Namespace
from contextlib import ExitStack, redirect_stdout
from dataclasses import dataclass
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from yeone_ocr import cli, prompts
from yeone_ocr.collate import run
from yeone_ocr.collate.section_edit import ManualProtectionError
from yeone_ocr.reader_gemini import ReaderError
from yeone_ocr.schema import Column, PageReading, Paragraph, Region, RegionReread
from yeone_ocr.state import Work


@dataclass(frozen=True)
class Memory:
    digest: str = "a" * 64
    count: int = 1
    prompt: str = '사람 수정 참고 사례: {"before":"사火","after":"瀉火"}; 이미지 우선'
    path: Path = Path("book_교정.manual.json")
    warning: str = ""


class Reader:
    name = "gemini"
    model = "gemini-2.5-pro"

    def __init__(self):
        self.pages = []
        self.regions = []

    def read_page(self, image, file_name, pdf_page, total, extra=""):
        self.pages.append((file_name, pdf_page, total, extra))
        return PageReading(pdf_page=pdf_page, regions=[
            Region(id="r1", kind="본문", bbox=[100, 100, 900, 900],
                   paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="사火")])]),
        ])

    def reread_region(self, image, kind, region_id, pdf_page):
        self.regions.append((kind, region_id, pdf_page))
        return RegionReread(columns=[Column(col_from_right=1, text="사火")])


class ManualCorrectionReader(unittest.TestCase):
    def setUp(self):
        cli.CANCEL.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf = self.root / "source.png"
        Image.new("RGB", (80, 80), "white").save(self.pdf)
        self.work = Work(str(self.pdf), str(self.root / "work"))
        self.work.ensure_source()
        self.args = cli.build_parser().parse_args([
            "read", str(self.pdf), "--work", str(self.work.dir), "--mode", "vertical",
            "--workers", "1", "--upscale", "1", "--chunk-lines", "0", "--second-mode", "each",
        ])
        cli.apply_mode(self.args)
        self.reader = Reader()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.stack.enter_context(patch.object(cli, "_load_page_image",
                                               return_value=(Image.new("RGB", (80, 80), "white"), "test")))
        self.stack.enter_context(patch.object(cli, "_load_layer", return_value={"plain": ""}))
        self.stack.enter_context(patch.object(cli, "refit_regions", return_value={"fixed": 0}))
        self.stack.enter_context(patch.object(cli, "cover_check", return_value=[]))
        self.stack.enter_context(patch.object(cli, "detect_figures", return_value=[]))

    def read(self):
        return cli.read_one(self.work, [None], self.reader, self.reader, 1, self.args)

    def partial(self):
        with patch.object(cli, "_second_pass", side_effect=ReaderError("interrupted")), self.assertRaises(ReaderError):
            self.read()
        return json.loads(cli.partial_path(self.work, 1).read_text("utf-8"))

    def test_examples_are_request_local_and_second_pass_is_independent(self):
        self.args._correction_memory = Memory()
        system_before = prompts.system_prompt()
        rec = self.read()
        self.assertEqual(self.reader.pages[0][-1], Memory().prompt)
        self.assertEqual(self.reader.regions, [("본문", "r1", 1)])
        self.assertEqual(prompts.system_prompt(), system_before)
        self.assertFalse(hasattr(self.reader, "_correction_memory"))
        self.assertEqual(rec["provenance"]["pass1"]["manual_corrections"], {
            "digest": Memory().digest, "count": 1, "path": str(Memory().path),
        })
        self.assertNotIn(str(Memory().path), self.reader.pages[0][-1])
        # The model ignored the hint: no string replacement is allowed afterward.
        self.assertEqual(rec["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"], "사火")

    def test_no_memory_preserves_legacy_config_and_request(self):
        rec = self.read()
        self.assertEqual(self.reader.pages[0][-1], "")
        self.assertEqual(rec["pass1_cfg"], cli.PASS1_CFG0)
        self.assertNotIn("manual_corrections", rec["provenance"]["pass1"])

    def test_empty_memory_does_not_claim_it_was_used(self):
        self.args._correction_memory = Memory(count=0, prompt="")
        rec = self.read()
        self.assertEqual(self.reader.pages[0][-1], "")
        self.assertEqual(rec["pass1_cfg"], cli.PASS1_CFG0)
        self.assertNotIn("manual_corrections", rec["provenance"]["pass1"])

    def test_same_memory_partial_resumes_without_first_pass_call(self):
        self.args._correction_memory = Memory()
        part = self.partial()
        self.assertEqual(part["provenance"]["pass1"]["manual_corrections"]["digest"], Memory().digest)
        rec = self.read()
        self.assertEqual(len(self.reader.pages), 1)
        self.assertTrue(rec["pass1_reused"])
        self.assertEqual(rec["provenance"]["pass1"], part["provenance"]["pass1"])

    def test_changed_memory_has_distinct_fingerprint_but_preserves_paid_partial(self):
        self.args._correction_memory = Memory()
        part = self.partial()
        self.args._correction_memory = Memory(digest="b" * 64, prompt="changed case")
        self.assertFalse(cli._same_pass1(part, self.reader, self.args))
        rec = self.read()
        self.assertEqual(len(self.reader.pages), 1)
        self.assertEqual(rec["provenance"]["pass1"], part["provenance"]["pass1"])
        self.assertEqual(rec["pass1_cfg"][-1], {"manual_corrections": "a" * 64})
        self.assertIn("기존 1차에 적용하지 않았습니다", self.output.getvalue())

    def test_legacy_partial_is_not_stamped_with_new_memory(self):
        part = self.partial()
        self.args._correction_memory = Memory()
        rec = self.read()
        self.assertEqual(len(self.reader.pages), 1)
        self.assertEqual(rec["pass1_cfg"], cli.PASS1_CFG0)
        self.assertNotIn("manual_corrections", rec["provenance"]["pass1"])
        self.assertEqual(rec["provenance"]["pass1"], part["provenance"]["pass1"])

    def test_force_changed_memory_reads_again_and_records_new_digest(self):
        self.args._correction_memory = Memory()
        self.partial()
        self.args._correction_memory = Memory(digest="b" * 64, prompt="changed case")
        self.args.force = True
        rec = self.read()
        self.assertEqual(len(self.reader.pages), 2)
        self.assertEqual(self.reader.pages[-1][-1], "changed case")
        self.assertEqual(rec["provenance"]["pass1"]["manual_corrections"]["digest"], "b" * 64)
        self.assertFalse(rec["pass1_reused"])

    def test_force_same_memory_can_resume_interrupted_force(self):
        self.args._correction_memory = Memory()
        self.partial()
        self.args.force = True
        rec = self.read()
        self.assertEqual(len(self.reader.pages), 1)
        self.assertTrue(rec["pass1_reused"])

    def test_removing_memory_keeps_prior_partial_provenance(self):
        self.args._correction_memory = Memory()
        self.partial()
        del self.args._correction_memory
        rec = self.read()
        self.assertEqual(len(self.reader.pages), 1)
        self.assertEqual(rec["provenance"]["pass1"]["manual_corrections"]["digest"], Memory().digest)

    def test_finished_record_is_unchanged_and_never_rebought(self):
        self.read()
        before = self.work.record_path(1).read_bytes()
        with patch.object(cli, "_load_manual_corrections", return_value=Memory()) as load, \
                patch.object(cli, "make_reader") as make:
            self.assertEqual(cli.cmd_read(self.args, self.work, [None]), 0)
        load.assert_called_once()
        self.assertEqual(load.call_args.args[0].pdf, self.args.pdf)
        make.assert_not_called()
        self.assertEqual(self.work.record_path(1).read_bytes(), before)
        self.assertFalse(hasattr(self.args, "_correction_memory"))

    def test_unreadable_or_invalid_manual_protection_stops_read_before_api_calls(self):
        for error in (PermissionError("locked history"), ManualProtectionError("invalid history")):
            with self.subTest(error=type(error).__name__), \
                    patch.object(cli, "_load_manual_corrections", side_effect=error), \
                    patch.object(cli, "make_reader") as make:
                self.assertEqual(cli.cmd_read(self.args, self.work, [None]), 1)
                make.assert_not_called()
                self.assertIsNone(self.work.load_record(1))
        self.assertIn("오류: 사람 수정 기록을 확인하지 못했습니다", self.output.getvalue())
        self.assertFalse(hasattr(self.args, "_correction_memory"))

    def test_command_loads_once_without_leaking_snapshot_to_next_book(self):
        observed = []
        real_read = cli.read_one

        def read(work, doc, reader, reader2, n, args):
            observed.append(args._correction_memory)
            return real_read(work, doc, reader, reader2, n, args)

        self.args.no_second_pass = True
        with patch.object(cli, "_load_manual_corrections", side_effect=[Memory(), None]) as load, \
                patch.object(cli, "make_reader", return_value=self.reader), \
                patch.object(cli, "read_one", side_effect=read):
            self.assertEqual(cli.cmd_read(self.args, self.work, [None, None]), 0)
            self.assertFalse(hasattr(self.args, "_correction_memory"))
            second = Work(str(self.pdf), str(self.root / "second-work"))
            self.assertEqual(cli.cmd_read(self.args, second, [None]), 0)
        self.assertEqual(load.call_count, 2)
        self.assertEqual(observed, [Memory(), Memory(), None])

    def test_bookmark_snapshot_is_shared_with_section_read(self):
        memory = Memory()
        self.args._correction_memory = memory
        observed = []
        real_read = cli.read_one

        def read(*args):
            observed.append(args[-1]._correction_memory)
            return real_read(*args)

        self.args.no_second_pass = True
        with patch.object(cli, "_load_manual_corrections") as load, \
                patch.object(cli, "cmd_prepare"), \
                patch.object(cli, "make_reader", return_value=self.reader), \
                patch.object(cli, "read_one", side_effect=read):
            self.assertFalse(run.ensure_pages_read(self.args, self.work, [None], (1, 1)))
        load.assert_not_called()
        self.assertEqual(observed, [memory])
        self.assertIs(observed[0], memory)

    def test_unrelated_read_configuration_still_invalidates_same_provider_partial(self):
        self.args._correction_memory = Memory()
        part = self.partial()
        self.args.page_thinking = "high"
        self.assertFalse(cli._can_resume_pass1(part, self.reader, self.args))

    def test_unknown_future_settings_are_not_ignored_as_memory_changes(self):
        self.args._correction_memory = Memory()
        base = {"reader": self.reader.name, "model": self.reader.model, "mode": self.args.mode}
        for extension in (
                [{"future_page_setting": "different"}],
                [{"manual_corrections": "old", "future_page_setting": "different"}],
                [{"manual_corrections": "old"}, {"future_page_setting": "different"}]):
            with self.subTest(extension=extension):
                record = {**base, "pass1_cfg": [*cli.PASS1_CFG0, *extension]}
                self.assertFalse(cli._can_resume_pass1(record, self.reader, self.args))

    def test_memory_changes_do_not_affect_cross_provider_resume_provenance(self):
        self.args._correction_memory = Memory()
        part = self.partial()
        self.reader.name = "openai"
        self.reader.model = "gpt-4.1"
        self.args._correction_memory = Memory(digest="b" * 64)
        rec = self.read()
        self.assertEqual(len(self.reader.pages), 1)
        self.assertEqual(rec["provenance"]["pass1"], part["provenance"]["pass1"])
        self.assertEqual(rec["provenance"]["pass2"]["r1"]["provider"], "openai")

    def test_manual_option_available_for_read_run_and_collate(self):
        parser = cli.build_parser()
        for command in ("read", "run", "collate"):
            argv = [command, "book.pdf", "--manual-corrections", "book_교정.hwpx"]
            if command == "collate":
                argv += ["--hwpx", "book.hwpx"]
            self.assertEqual(parser.parse_args(argv).manual_corrections, "book_교정.hwpx")


class ManualMemoryDiscovery(unittest.TestCase):
    @staticmethod
    def bookmark_args(root):
        import pymupdf
        from hwpx.document import HwpxDocument

        pdf_path, source = root / "book.pdf", root / "book.hwpx"
        with pymupdf.open() as pdf:
            pdf.new_page()
            pdf.new_page()
            pdf.set_toc([[1, "瀉白散", 1], [1, "葦莖湯", 2]])
            pdf.save(pdf_path)
        document = HwpxDocument.new()
        try:
            document.add_heading("瀉白散", level=1)
            document.add_heading("葦莖湯", level=1)
            document.save_to_path(str(source))
        finally:
            document.close()
        return cli.build_parser().parse_args([
            "collate", str(pdf_path), "--hwpx", str(source), "--all", "--work", str(root / "work"),
        ])

    def test_bookmark_command_loads_one_snapshot_without_mutating_caller(self):
        with tempfile.TemporaryDirectory() as folder:
            args = self.bookmark_args(Path(folder))
            memory = Memory()
            with patch.object(cli, "_load_manual_corrections", return_value=memory) as load, \
                    patch.object(run, "run_all", return_value={}) as all_sections, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(run.cmd_collate(args), 0)
            load.assert_called_once()
            self.assertIs(all_sections.call_args.args[0]._correction_memory, memory)
            self.assertFalse(hasattr(args, "_correction_memory"))

    def test_bookmark_protection_failure_stops_before_automatic_collation(self):
        with tempfile.TemporaryDirectory() as folder:
            args = self.bookmark_args(Path(folder))
            source_before = Path(args.hwpx).read_bytes()
            for error in (PermissionError("locked history"), ManualProtectionError("invalid history")):
                output = io.StringIO()
                with self.subTest(error=type(error).__name__), \
                        patch.object(cli, "_load_manual_corrections", side_effect=error), \
                        patch.object(run, "run_all") as all_sections, redirect_stdout(output):
                    self.assertEqual(run.cmd_collate(args), 1)
                all_sections.assert_not_called()
                self.assertIn("오류: 사람 수정 기록을 확인하지 못했습니다", output.getvalue())
                self.assertEqual(Path(args.hwpx).read_bytes(), source_before)
            self.assertFalse(Path(run.corrected_path(args.hwpx)).exists())

    def test_bookmark_automatically_uses_corrected_sibling(self):
        args = Namespace(pdf="book.pdf", hwpx="book.hwpx")
        with patch("yeone_ocr.correction_memory.load_correction_memory", return_value=Memory()) as load, \
                patch.object(cli, "log"):
            self.assertEqual(cli._load_manual_corrections(args), Memory())
        load.assert_called_once_with(Path(run.corrected_path("book.hwpx")), Path("book.pdf"))

    def test_custom_bookmark_output_and_explicit_override(self):
        args = Namespace(pdf="book.pdf", hwpx="book.hwpx", out="custom.hwpx")
        with patch("yeone_ocr.correction_memory.load_correction_memory", return_value=Memory()) as load, \
                patch.object(cli, "log"):
            cli._load_manual_corrections(args)
            load.assert_called_once_with(Path("custom.hwpx"), Path("book.pdf"))
            load.reset_mock()
            args.manual_corrections = "explicit.hwpx"
            cli._load_manual_corrections(args)
            load.assert_called_once_with(Path("explicit.hwpx"), Path("book.pdf"))

    def test_general_read_never_searches_an_unrelated_book(self):
        with patch("yeone_ocr.correction_memory.load_correction_memory") as load:
            self.assertIsNone(cli._load_manual_corrections(Namespace(pdf="another.pdf")))
        load.assert_not_called()

    def test_unusable_or_different_pdf_examples_are_not_used(self):
        with patch("yeone_ocr.correction_memory.load_correction_memory",
                   return_value=Memory(count=0, prompt="", warning="PDF가 다릅니다")), \
                patch.object(cli, "log") as log:
            args = Namespace(pdf="another.pdf", manual_corrections="book_교정.hwpx")
            args._correction_memory = cli._load_manual_corrections(args)
            self.assertEqual(cli._manual_correction_provenance(args), {})
        self.assertIn("PDF가 다릅니다", log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
