"""최종 문서에서만 변환하고, 원본 기록·문맥 판단 캐시·미확정 상태를 보존한다."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import fitz

from yeone_ocr import cli
from yeone_ocr.schema import Column, PageReading, Paragraph, Region
from yeone_ocr.state import Work


class BuildNormalization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pdf = self.root / "source.pdf"
        with fitz.open() as document:
            document.new_page()
            document.save(self.pdf)
        self.work = Work(str(self.pdf), str(self.root / "work"))
        reading = PageReading(pdf_page=1, regions=[Region(id="r1", kind="본문", bbox=[0, 0, 1000, 1000],
                              paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="髒，汉书。")])])])
        self.record = {"file": self.pdf.name, "pdf_page": 1, "pass1": reading.model_dump(), "merged": reading.model_dump(), "crops": {}}
        self.work.save_record(1, self.record)
        self.original = self.work.record_path(1).read_text(encoding="utf-8")
        cli.CANCEL.clear()

    def args(self, *flags):
        return cli.build_parser().parse_args(["build", str(self.pdf), "--work", str(self.work.dir),
                    "--reader", "openai", "--model", "gpt-test", "--no-context", "--no-highlight", *flags])

    def run_build(self, args):
        with fitz.open(self.pdf) as document:
            return cli.cmd_build(args, self.work, document)

    def test_final_hwpx_docx_normalized_raw_preserved_cached_and_review_linked(self):
        calls = []
        class Reader:
            model = "gpt-test"
            usage = {"calls": 0}
            def resolve_text_choices(self, items):
                calls.append(items)
                self.usage = {"calls": len(calls), "prompt_tokens": 100, "output_tokens": 10}
                return {item["id"]: "臟" if "臟" in item["candidates"] else item["candidates"][-1] for item in items}

        with patch.object(cli, "create_reader", return_value=Reader()) as factory:
            self.assertEqual(self.run_build(self.args("--docx")), 0)
            self.assertEqual(factory.call_count, 1)
            first_calls = len(calls)
            self.assertEqual(self.run_build(self.args("--docx")), 0)
            self.assertEqual(len(calls), first_calls)
            self.assertEqual(factory.call_count, 1)
        self.assertEqual(self.work.record_path(1).read_text(encoding="utf-8"), self.original)
        for name, xml_name in (("source.hwpx", "Contents/section0.xml"), ("source.docx", "word/document.xml")):
            with ZipFile(self.work.out_dir / name) as archive:
                xml = archive.read(xml_name).decode("utf-8")
            self.assertIn("漢書", xml)
            self.assertIn("臟", xml)
            self.assertNotIn("汉书", xml)
        audit = json.loads((self.work.out_dir / "source_normalization.json").read_text(encoding="utf-8"))
        self.assertTrue(audit["complete"])
        self.assertGreater(audit["reader"]["cache_hits"], 0)
        from yeone_ocr.review_html import make_review
        review = Path(make_review(self.work, [self.record])).read_text(encoding="utf-8")
        self.assertIn("source_normalization.html", review)
        self.assertTrue((self.work.out_dir / "source_normalization.html").exists())

    def test_unavailable_model_retains_candidates_and_returns_review_code(self):
        with patch.object(cli, "create_reader", side_effect=cli.ReaderError("설정한 API 키가 없습니다.")):
            self.assertEqual(self.run_build(self.args()), 5)
        audit = json.loads((self.work.out_dir / "source_normalization.json").read_text(encoding="utf-8"))
        self.assertFalse(audit["complete"])
        self.assertGreater(audit["summary"]["context_unresolved"], 0)
        self.assertTrue((self.work.out_dir / "source.hwpx").exists())
        self.assertEqual(self.work.record_path(1).read_text(encoding="utf-8"), self.original)

    def test_disabled_normalization_has_no_model_calls_and_replaces_old_audit(self):
        (self.work.out_dir / "source_normalization.json").write_text('{"enabled":true}', encoding="utf-8")
        with patch.object(cli, "create_reader") as factory:
            self.assertEqual(self.run_build(self.args("--no-normalize")), 0)
            factory.assert_not_called()
        audit = json.loads((self.work.out_dir / "source_normalization.json").read_text(encoding="utf-8"))
        self.assertFalse(audit["enabled"])
        with ZipFile(self.work.out_dir / "source.hwpx") as archive:
            self.assertIn("髒，汉书。", archive.read("Contents/section0.xml").decode("utf-8"))

    def test_cache_does_not_confuse_repeated_target_positions(self):
        inputs = [dict(id="1", context="甲云乙云", source="云", candidates=["云", "雲"], target_offset=1),
                  dict(id="2", context="甲云乙云", source="云", candidates=["云", "雲"], target_offset=3)]
        class Reader:
            model = "gpt-test"
            usage = {}
            def resolve_text_choices(self, items):
                return {item["id"]: "云" if item["target_offset"] == 1 else "雲" for item in items}
        with patch.object(cli, "create_reader", return_value=Reader()):
            resolver, _ = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(resolver(inputs), {"1": "云", "2": "雲"})
        with patch.object(cli, "create_reader") as factory:
            resolver, metadata = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(resolver(inputs), {"1": "云", "2": "雲"})
            self.assertEqual(metadata["cache_hits"], 2)
            factory.assert_not_called()

    def test_provider_change_cannot_reuse_another_models_decision(self):
        item = dict(id="1", context="髒", source="髒", candidates=["髒", "臟"])
        class Reader:
            usage = {}
            def resolve_text_choices(self, items):
                return {"1": "臟"}
        with patch.object(cli, "create_reader", return_value=Reader()) as factory:
            first, _ = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(first([item]), {"1": "臟"})
            other_args = self.args(); other_args.reader = "anthropic"; other_args.model = "claude-test"
            second, metadata = cli._normalization_resolver(self.work, other_args)
            self.assertEqual(second([item]), {"1": "臟"})
            self.assertEqual(factory.call_count, 2)
            self.assertEqual(metadata["cache_hits"], 0)

    def test_simplified_original_cannot_be_accepted_as_final_traditional(self):
        item = dict(id="1", context="发展", source="发", candidates=["发", "發", "髮"],
                    source_is_valid_traditional=False)
        class Reader:
            usage = {}
            def resolve_text_choices(self, items):
                return {"1": "发"}
        with patch.object(cli, "create_reader", return_value=Reader()):
            resolver, _ = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(resolver([item]), {})
        self.assertFalse((self.work.dir / "normalization_cache.json").exists())


if __name__ == "__main__":
    unittest.main()
