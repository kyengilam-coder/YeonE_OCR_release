"""New reads preserve source Unicode; historical records retain their policy."""
import contextlib
import copy
import io
import json
from argparse import Namespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from yeone_ocr import cli
from yeone_ocr.merge import merge_passes
from yeone_ocr.schema import Column, PageReading, Paragraph, Region, RegionReread, TableRow, Uncertain
from yeone_ocr.state import Work
from yeone_ocr.text_policy import LEGACY_TEXT_POLICY, SOURCE_TEXT_POLICY, record_text_policy


SOURCE = "靑硏敎眞樂神\U0002f800漢\ufe00字\U000e0100한\u200b"


def page(text=SOURCE):
    return PageReading(pdf_page=1, page_label=text, running_header=text,
        regions=[Region(id="r", kind="본문", bbox=[100, 100, 900, 900], title_text=text,
                        paragraphs=[Paragraph(columns=[Column(n=1, t=text)])])],
        uncertain=[Uncertain(region_id="r", context=text)])


def contents(reading):
    return reading.regions[0].paragraphs[0].columns[0].text


class TextPreservation(unittest.TestCase):
    def test_first_reading_keeps_all_fields_and_explicit_legacy_remains(self):
        raw = page()
        raw.regions.append(Region(id="table", kind="단순표", table_rows=[TableRow(cells=[SOURCE])]))
        snapshot = raw.model_dump()
        self.assertEqual(cli.nfc_reading(raw, text_policy=SOURCE_TEXT_POLICY).model_dump(), snapshot)
        historical = cli.nfc_reading(raw.model_copy(deep=True))
        self.assertNotIn("\ufe00", contents(historical))
        self.assertIn("樂", contents(historical))
        self.assertEqual(raw.model_dump(), snapshot)

    def test_equal_readings_preserve_source_and_whitespace_without_mutating_inputs(self):
        text = "  " + SOURCE + "  甲\t乙  "
        first = page(text)
        second = RegionReread(columns=[Column(n=1, t=text)])
        snapshots = first.model_dump(), second.model_dump()
        merged, stats = merge_passes(first, {"r": second}, text_policy=SOURCE_TEXT_POLICY)
        self.assertEqual(contents(merged), text)
        self.assertEqual(stats["marks"], 0)
        self.assertEqual((first.model_dump(), second.model_dump()), snapshots)

    def test_selector_difference_never_detaches_from_base_and_other_errors_still_mark(self):
        cases = [
            ("甲漢\ufe00乙丙", "甲漢乙丁", "甲漢\ufe00乙〔丙/丁〕"),
            ("甲漢\U000e0100乙", "甲字\U000e0100乙", "甲漢\U000e0100乙"),
            ("甲漢乙", "甲漢\ufe00乙", "甲漢乙"),
            ("甲漢\ufe00乙", "甲漢文\ufe00乙", "甲漢\ufe00乙"),
        ]
        for first, second, expected in cases:
            with self.subTest(first=first, second=second):
                merged, stats = merge_passes(page(first), {"r": RegionReread(columns=[Column(n=1, t=second)])},
                                             text_policy=SOURCE_TEXT_POLICY)
                self.assertEqual(contents(merged), expected)
                self.assertGreater(stats.get("source_identity_holds", 0), 0)
                self.assertFalse(any(marker in contents(merged) for marker in ("〔疑: \ufe00", "〔疑: \U000e0100")))

    def test_compatibility_spelling_stays_first_reading_and_is_not_silently_accepted(self):
        merged, stats = merge_passes(page("音樂甲"), {"r": RegionReread(columns=[Column(n=1, t="音樂乙")])},
                                     text_policy=SOURCE_TEXT_POLICY)
        self.assertEqual(contents(merged), "音樂〔甲/乙〕")
        self.assertEqual(stats["notation_diffs"], 1)

    def test_unknown_policy_is_rejected_without_touching_input(self):
        raw = page()
        before = raw.model_dump()
        with self.assertRaises(ValueError):
            cli.nfc_reading(raw, text_policy="future-unknown")
        with self.assertRaises(ValueError):
            merge_passes(raw, {}, text_policy="future-unknown")
        self.assertEqual(raw.model_dump(), before)
        self.assertEqual(record_text_policy({}), LEGACY_TEXT_POLICY)

    def test_second_pass_policy_preserves_new_and_replays_historical(self):
        class Reader:
            def reread_region(self, *args):
                return RegionReread(columns=[Column(n=1, t=SOURCE)])
        args = Namespace(chunk_lines=0, second_mode="each")
        with Image.new("RGB", (160, 200), "white") as image:
            for policy in (SOURCE_TEXT_POLICY, LEGACY_TEXT_POLICY):
                with self.subTest(policy=policy):
                    result, _ = cli._second_pass(Reader(), image, page(), 1, args, text_policy=policy)
                    got = result["r"].columns[0].text
                    if policy == SOURCE_TEXT_POLICY:
                        self.assertEqual(got, SOURCE)
                    else:
                        self.assertIn("樂", got)
                        self.assertNotIn("樂", got)


class ReadPolicyLifecycle(unittest.TestCase):
    class Reader:
        name = "mock"
        model = "mock"

        def __init__(self):
            self.calls = 0
            self.fail_second = False

        def read_page(self, *args):
            self.calls += 1
            return page()

        def reread_region(self, *args):
            if self.fail_second:
                raise cli.ReaderError("synthetic failure", fatal=False)
            return RegionReread(columns=[Column(n=1, t=SOURCE)])

    def run_read(self, work, reader):
        args = Namespace(pdf=str(work.pdf_path), dpi=72, upscale=1.0, no_second_pass=False,
                         print_offset=None, mode="vertical", chunk_lines=0, second_mode="each")
        with Image.new("RGB", (160, 200), "white") as image, contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(cli, "_load_page_image", return_value=(image, "synthetic")))
            stack.enter_context(patch.object(cli, "_load_layer", return_value={"plain": ""}))
            stack.enter_context(patch.object(cli, "refit_regions", return_value={"fixed": 0}))
            stack.enter_context(patch.object(cli, "cover_check", return_value=[]))
            stack.enter_context(patch.object(cli, "detect_figures", return_value=[]))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            return cli.read_one(work, [None], reader, reader, 1, args)

    def test_new_complete_record_and_roundtrip_preserve_source(self):
        with tempfile.TemporaryDirectory() as td:
            work = Work(str(Path(td) / "source.pdf"), str(Path(td) / "work"))
            result = self.run_read(work, self.Reader())
            self.assertEqual(result["text_policy"], SOURCE_TEXT_POLICY)
            for name in ("pass1", "merged"):
                self.assertEqual(contents(PageReading.model_validate(result[name])), SOURCE)
            self.assertEqual(result["pass2"]["r"]["columns"][0]["text"], SOURCE)
            self.assertEqual(work.load_record(1), result)

    def test_partial_resume_inherits_policy_without_buying_first_read_again(self):
        with tempfile.TemporaryDirectory() as td:
            work = Work(str(Path(td) / "source.pdf"), str(Path(td) / "work"))
            reader = self.Reader()
            reader.fail_second = True
            with self.assertRaises(cli.ReaderError):
                self.run_read(work, reader)
            partial_path = cli.partial_path(work, 1)
            part = json.loads(partial_path.read_text(encoding="utf-8"))
            self.assertEqual(part["text_policy"], SOURCE_TEXT_POLICY)
            self.assertEqual(contents(PageReading.model_validate(part["pass1"])), SOURCE)
            reader.fail_second = False
            result = self.run_read(work, reader)
            self.assertEqual(reader.calls, 1)
            self.assertEqual(result["text_policy"], SOURCE_TEXT_POLICY)
            self.assertEqual(contents(PageReading.model_validate(result["merged"])), SOURCE)

    def test_unversioned_partial_keeps_legacy_policy(self):
        with tempfile.TemporaryDirectory() as td:
            work = Work(str(Path(td) / "source.pdf"), str(Path(td) / "work"))
            reader = self.Reader()
            reader.fail_second = True
            with self.assertRaises(cli.ReaderError):
                self.run_read(work, reader)
            path = cli.partial_path(work, 1)
            part = json.loads(path.read_text(encoding="utf-8"))
            part.pop("text_policy")
            part["pass1"] = cli.nfc_reading(PageReading.model_validate(part["pass1"])).model_dump()
            path.write_text(json.dumps(part, ensure_ascii=False), encoding="utf-8")
            old_pass1 = copy.deepcopy(part["pass1"])
            reader.fail_second = False
            result = self.run_read(work, reader)
            self.assertEqual(reader.calls, 1)
            self.assertEqual(result["text_policy"], LEGACY_TEXT_POLICY)
            self.assertEqual(result["pass1"], old_pass1)
            self.assertNotIn("\ufe00", contents(PageReading.model_validate(result["merged"])))

    def test_remerge_command_backs_up_and_keeps_human_correction(self):
        import pymupdf
        with tempfile.TemporaryDirectory() as td, pymupdf.open() as document:
            pdf = Path(td) / "source.pdf"
            document.new_page()
            document.save(pdf)
            work = Work(str(pdf), str(Path(td) / "work"))
            record = self.run_read(work, self.Reader())
            corrected = "人工確定：" + SOURCE
            record["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"] = corrected
            work.save_record(1, record)
            before = work.record_path(1).read_bytes()
            args = cli.build_parser().parse_args(["remerge", str(pdf), "--work", str(work.dir), "--mode", "vertical"])
            with contextlib.redirect_stdout(io.StringIO()), \
                    patch.object(cli, "_load_layer", return_value={"plain": ""}), \
                    patch.object(cli, "refit_regions", return_value={"fixed": 0}):
                self.assertEqual(cli.cmd_remerge(args, work, document), 0)
            after = work.load_record(1)
            self.assertEqual(contents(PageReading.model_validate(after["merged"])), corrected)
            self.assertEqual((after["pass1"], after["pass2"], after["text_policy"]),
                             (record["pass1"], record["pass2"], record["text_policy"]))
            self.assertEqual((work.readings_dir / "prev" / "p001.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
