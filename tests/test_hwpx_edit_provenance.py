"""Generated documents retain exact pre-OCR styles for later human edits."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from zipfile import ZipFile

from lxml import etree
from hwpx import HwpxDocument

from yeone_ocr.blocks import Block
from yeone_ocr.hwpx_builder import build_hwpx
from yeone_ocr.hwpx_template import build_hwpx_template
from yeone_ocr.hwpx_writer import EDIT_PROVENANCE_PART, HH, HP, _remember_grade_base
from yeone_ocr.presets import resolve_preset


def read_document(path):
    with ZipFile(path) as archive:
        metadata = json.loads(archive.read(EDIT_PROVENANCE_PART))
        header = etree.fromstring(archive.read("Contents/header.xml"))
        section = etree.fromstring(archive.read("Contents/section0.xml"))
    styles = {style.get("id"): style for style in header.iter(HH + "charPr")}
    return metadata, styles, section


class WriterEditProvenance(unittest.TestCase):
    def test_generated_grade_points_to_actual_plain_text_style_in_both_presets(self):
        with TemporaryDirectory() as folder:
            for preset in (None, "basic"):
                with self.subTest(preset=preset):
                    path = Path(folder) / f"{preset}.hwpx"
                    result = build_hwpx([
                        Block(type="para", text="甲乙丙丁", spans=[(0, 1, "green"), (1, 2, "blue"), (2, 3, "red")]),
                        Block(type="para", text="戊己", kind="소주", spans=[(0, 1, "red_ctx")]),
                    ], str(path), preset=preset)
                    self.assertEqual(result["validate_issues"], 0)
                    metadata, styles, section = read_document(path)
                    self.assertEqual(set(metadata), {"schema_version", "producer", "grade_styles"})
                    self.assertEqual(metadata["schema_version"], 1)
                    mapping = metadata["grade_styles"]
                    self.assertEqual(len(mapping), 4)
                    for paragraph in section.findall(HP + "p"):
                        runs = [run for run in paragraph.findall(HP + "run")
                                if (run.findtext(HP + "t") or "").strip()]
                        if not runs:
                            continue
                        base = runs[-1].get("charPrIDRef")
                        for run in runs[:-1]:
                            derived = run.get("charPrIDRef")
                            self.assertEqual(mapping[derived], base)
                            self.assertEqual(styles[derived].get("height"), styles[base].get("height"))
                            self.assertNotEqual(styles[derived].get("textColor"), styles[base].get("textColor"))

    def test_template_keeps_exact_author_style_and_replaces_stale_provenance(self):
        with TemporaryDirectory() as folder:
            template, output = Path(folder) / "template.hwpx", Path(folder) / "output.hwpx"
            document = HwpxDocument.new()
            try:
                author = str(document.oxml.ensure_run_style(size=19, bold=True, color="#603080"))
                document.oxml.headers[0].ensure_style("문단-1", style_type="PARA", para_pr_id_ref=0,
                                                    char_pr_id_ref=author)
                document.package.set_part(EDIT_PROVENANCE_PART, '{"schema_version":1,"grade_styles":{"999":"998"}}')
                document.save_to_path(str(template))
            finally:
                document.close()
            before = template.read_bytes()
            result = build_hwpx_template([
                Block(type="para", text="甲乙丙丁", role="body", spans=[(0, 1, "red"), (1, 2, "blue"), (2, 3, "green")])
            ], str(output), str(template), preset=resolve_preset("basic"))
            metadata, styles, _ = read_document(output)
            self.assertEqual(result["validate_issues"], 0)
            self.assertEqual(len(metadata["grade_styles"]), 3)
            self.assertEqual(set(metadata["grade_styles"].values()), {author})
            self.assertNotIn("999", metadata["grade_styles"])
            self.assertEqual(styles[author].get("textColor"), "#603080")
            self.assertIsNotNone(styles[author].find(HH + "bold"))
            self.assertEqual(template.read_bytes(), before)

    def test_no_highlight_does_not_claim_author_colors(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "plain.hwpx"
            build_hwpx([Block(type="para", text="甲乙", spans=[(0, 1, "red")])], str(path), highlight=False)
            self.assertEqual(read_document(path)[0]["grade_styles"], {})

    def test_template_author_grade_colored_style_is_not_claimed(self):
        with TemporaryDirectory() as folder:
            template, output = Path(folder) / "template.hwpx", Path(folder) / "output.hwpx"
            document = HwpxDocument.new()
            try:
                base = str(document.oxml.ensure_run_style(size=19))
                red = str(document.oxml.ensure_run_style(base_char_pr_id=base, bold=True,
                                                       color="#C00000", highlight="#FFFF00"))
                header = document.oxml.headers[0]
                header.ensure_style("문단-1", style_type="PARA", para_pr_id_ref=0, char_pr_id_ref=base)
                header.ensure_style("문단-2", style_type="PARA", para_pr_id_ref=0, char_pr_id_ref=red)
                document.save_to_path(str(template))
            finally:
                document.close()
            result = build_hwpx_template([
                Block(type="para", text="甲乙", role="body", spans=[(0, 1, "red")]),
                Block(type="para", text="作者紅色", role="commentary"),
            ], str(output), str(template), preset=resolve_preset("basic"))
            metadata, _, section = read_document(output)
            self.assertEqual(result["validate_issues"], 0)
            self.assertNotIn(red, metadata["grade_styles"])
            self.assertEqual(list(metadata["grade_styles"].values()), [base])
            author_runs = [run for run in section.iter(HP + "run")
                           if run.findtext(HP + "t") == "作者紅色"]
            self.assertEqual(len(author_runs), 1)
            self.assertEqual(author_runs[0].get("charPrIDRef"), red)

    def test_ambiguous_or_self_referential_base_is_never_claimed(self):
        look = SimpleNamespace(_grade_bases={}, _ambiguous_grade_bases=set())
        for derived, base in ((4, 1), (4, 2), (4, 1), (8, 8), (8, 2), (9, 3)):
            _remember_grade_base(look, derived, base)
        self.assertEqual(look._grade_bases, {"9": "3"})


if __name__ == "__main__":
    unittest.main()
