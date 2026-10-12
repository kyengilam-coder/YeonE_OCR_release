"""0.6.9: 모델 응답 검증·누락 방지·이체자 정확 비교의 오프라인 회귀 검사."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from pydantic import ValidationError
from yeone_ocr import evaluate as E
from yeone_ocr.adjudicate import adjudicate_page, apply_choice, blind_agrees, find_sites
from yeone_ocr.blocks import assemble_blocks
from yeone_ocr.merge import merge_passes
from yeone_ocr.schema import Column, GlyphChoice, KINDS, PageReading, PageResponse, Paragraph, Region, RegionReread, Uncertain, region_text
from yeone_ocr.state import Work
from yeone_ocr.verify import verify_page


def region(text="甲乙丙", rid="r1", **kw):
    return Region(id=rid, kind="본문", paragraphs=[Paragraph(columns=[Column(n=1, t=text)])], **kw)


def record(page):
    return {"file": "book.pdf", "pdf_page": page.pdf_page, "merged": page.model_dump()}


class ResponseValidation(unittest.TestCase):
    def test_unknown_kind_fails_before_incomplete_export(self):
        rec = record(PageReading(pdf_page=1, regions=[region()]))
        rec["merged"]["regions"][0]["kind"] = "본 문"
        with self.assertRaises(ValidationError):
            PageResponse.model_validate(rec["merged"])
        with self.assertRaises(ValidationError):
            assemble_blocks([rec], grades=False)

    def test_bypassed_validation_still_reports_unknown_kind(self):
        reg = region()
        page = PageReading(pdf_page=1, regions=[reg])
        rec = record(page)
        reg.kind = "미지원 영역"
        self.assertTrue(any(i["rule"] == "지원하지 않는 영역 종류" for i in verify_page(page)))
        with patch("yeone_ocr.blocks.PageReading.model_validate", return_value=page):
            with self.assertRaisesRegex(ValueError, "본문 누락"):
                assemble_blocks([rec], grades=False)

    def test_bbox_rejects_bad_length_range_and_zero_area(self):
        for bbox in ([0, 0, 100], [0, 0, 100, 100, 200], [-1, 0, 100, 100],
                     [0, 0, 1001, 1000], [100, 0, 100, 500], [0, 100, 500, 100]):
            with self.subTest(bbox=bbox), self.assertRaises(ValidationError):
                region(bbox=bbox)
        self.assertEqual(region(bbox=[900, 800, 100, 200]).bbox, [100, 200, 900, 800])

    def test_new_response_rejects_duplicate_ids_but_legacy_text_is_preserved(self):
        legacy = PageReading(pdf_page=1, regions=[region("甲乙"), region("丙丁")])
        with self.assertRaisesRegex(ValidationError, "영역 id 겹침"):
            PageResponse.model_validate(legacy.model_dump())
        blocks = assemble_blocks([record(legacy)])
        self.assertEqual([b.text for b in blocks if b.type == "para"], ["甲乙", "丙丁"])
        self.assertTrue(any(i["rule"] == "영역 id 겹침" for i in verify_page(legacy)))

    def test_google_schema_accepts_enum_and_bbox_constraints_offline(self):
        from google import genai
        from google.genai import _transformers
        client = genai.Client(api_key="offline-schema-test")
        try:
            schema = _transformers.t_schema(client._api_client, PageResponse)
            reg = schema.properties["regions"].items
            self.assertEqual(set(reg.properties["k"].enum), set(KINDS))
            self.assertEqual((reg.properties["bbox"].min_items, reg.properties["bbox"].max_items), (4, 4))
            self.assertEqual((reg.properties["bbox"].items.minimum, reg.properties["bbox"].items.maximum), (0, 1000))
        finally:
            client.close()


class MergeAndAdjudication(unittest.TestCase):
    def test_equal_body_retains_independent_warning_once(self):
        warning = Uncertain(r="r1", n=1, ctx="아래 글자 원본 훼손 의심")
        p1 = PageReading(pdf_page=1, regions=[region()])
        rr = RegionReread(columns=[Column(n=1, t="甲乙丙")], uncertain=[warning])
        for same_note in (False, True):
            with self.subTest(existing=same_note):
                p1.uncertain = [warning] if same_note else []
                merged, _ = merge_passes(p1, {"r1": rr})
                self.assertEqual([u.context for u in merged.uncertain], [warning.context])
        empty_rr = RegionReread(uncertain=[Uncertain(ctx="조각 판독 불가")])
        merged, _ = merge_passes(PageReading(pdf_page=1, regions=[region()]), {"r1": empty_rr})
        self.assertEqual((merged.uncertain[0].region_id, merged.uncertain[0].context), ("r1", "조각 판독 불가"))

    def test_marked_pass2_keeps_mark_without_duplicate_notice(self):
        page = PageReading(pdf_page=1, regions=[region("甲")])
        rr = RegionReread(columns=[Column(n=1, t="〔疑: 甲〕")], uncertain=[Uncertain(r="r1", ctx="「〔疑: 甲〕」")])
        merged, _ = merge_passes(page, {"r1": rr})
        self.assertEqual(region_text(merged.regions[0]), "〔疑: 甲〕")
        self.assertEqual(len(merged.uncertain), 1)

    def test_duplicate_id_never_edits_another_region(self):
        page = PageReading(pdf_page=1, regions=[region("甲〔入/人〕乙"), region("丙〔入/人〕丁", "r2")])
        site = find_sites(page)[1]
        page.regions[1].id = site.region_id = "r1"
        original = [region_text(r) for r in page.regions]
        self.assertFalse(apply_choice(page, site, "人"))
        self.assertEqual(find_sites(page), [])
        self.assertEqual([region_text(r) for r in page.regions], original)
        merged, stats = merge_passes(page, {"r1": RegionReread(columns=[Column(n=1, t="丙人丁")])})
        self.assertEqual([region_text(r) for r in merged.regions], original)
        self.assertEqual(stats["regions_compared"], 0)
        self.assertTrue(any("영역 id 겹침" in u.context for u in merged.uncertain))

    def test_unapplied_choice_is_not_counted_as_decided(self):
        class Reader:
            def adjudicate(self, *args):
                return GlyphChoice(choice="人", window="甲人乙", evidence="획 확인")
        rec = record(PageReading(pdf_page=1, regions=[region("甲〔入/人〕乙")]))
        with patch("yeone_ocr.adjudicate.apply_choice", return_value=False):
            adjudicate_page(rec, Image.new("L", (100, 100), 255), Reader(), True)
        self.assertEqual(rec["adjudicate_stats"]["decided"], 0)
        self.assertEqual(rec["adjudicate_stats"]["unmapped"], 1)
        self.assertEqual(rec["adjudications"][0]["choice"], "")
        self.assertIn("〔入/人〕", rec["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"])

    def test_blind_variant_candidates_require_printed_form_agreement(self):
        for variant, standard in (("\uFA10", "\u585A"), ("\uFA22", "\u8AF8"), ("\U0002F800", "\u4E3D")):
            with self.subTest(variant=ord(variant)):
                self.assertFalse(blind_agrees(variant, standard, (variant, standard)))
                self.assertFalse(blind_agrees(standard, variant, (variant, standard)))
                self.assertTrue(blind_agrees(variant, variant, (variant, standard)))
        self.assertTrue(blind_agrees("\uF914", "樂", ("樂", "藥")))


class EvaluationVariants(unittest.TestCase):
    def test_raw_cer_preserves_variants_while_folded_cer_accepts_them(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Work(str(Path(temp) / "book.pdf"), str(Path(temp) / "result"))
            work.save_record(1, record(PageReading(pdf_page=1, regions=[region("甲塚乙")])))
            unit = E.build_unit(work, None, "전체", [1], ["甲\uFA10乙"], 0)
            self.assertEqual(unit["ref"].text, "甲\uFA10乙")
            result = E.eval_unit(unit, False)["stages"]["final"]
            self.assertEqual((result["edits"], result["edits_raw"]), (0, 1))
            self.assertAlmostEqual(result["cer_raw"], 1 / 3)


class TextLayerImageFrame(unittest.TestCase):
    def test_margin_scan_layer_refit_preserves_first_and_last_lines(self):
        import pymupdf
        from yeone_ocr.cli import _write_layer
        from yeone_ocr.pdfio import page_frame, text_layer
        from yeone_ocr.refit import refit_by_layer
        from yeone_ocr.schema import bbox_frac, frac_bbox
        with tempfile.TemporaryDirectory() as temp, pymupdf.open() as doc:
            page = doc.new_page(width=400, height=600)
            data = io.BytesIO()
            Image.new("RGB", (370, 560), "white").save(data, "PNG")
            page.insert_image(pymupdf.Rect(20, 30, 390, 590), stream=data.getvalue())
            page.insert_text((30, 58), "FIRSTLINE", fontsize=10, render_mode=3)
            page.insert_text((30, 83), "LASTLINE", fontsize=10, render_mode=3)
            raw = text_layer(doc, 1)
            frame = page_frame(doc, 1)
            self.assertEqual(frame, (20, 30, 370, 560))
            work = Work(str(Path(temp) / "absent.pdf"), str(Path(temp) / "result"))
            layer = _write_layer(work, doc, 1)
            self.assertEqual((layer["page_width"], layer["page_height"]), frame[2:])
            self.assertEqual(layer["plain"], raw.plain())
            self.assertEqual(json.loads(work.textlayer_path(1).read_text(encoding="utf-8")), layer)
            boxes = [line["bbox"] for line in layer["horizontal_lines"]]
            first, last = raw.horizontal_lines[0]["bbox"], raw.horizontal_lines[-1]["bbox"]
            coords = (min(b[0] for b in boxes) / 370, min(b[1] for b in boxes) / 560,
                      max(b[2] for b in boxes) / 370, max(b[3] for b in boxes) / 560)
            reg = Region(id="r1", kind="본문", bbox=frac_bbox(coords), paragraphs=[Paragraph(columns=[
                Column(n=1, t="FIRSTLINE"), Column(n=2, t="LASTLINE")])])
            fixes = refit_by_layer(PageReading(pdf_page=1, regions=[reg]), layer, True)
            self.assertIn("r1", fixes)
            _, top, _, bottom = bbox_frac(reg.bbox)
            self.assertLessEqual(30 + top * 560, first[1])
            self.assertGreaterEqual(30 + bottom * 560, last[3])

    def test_vertical_layer_uses_image_origin_without_changing_plaintext(self):
        from yeone_ocr import cli
        from yeone_ocr.pdfio import TextLayer
        layer = TextLayer(vertical_columns=[{"x": 200, "y0": 45, "y1": 85, "text": "天地玄黃"}],
                          horizontal_lines=[], page_width=400, page_height=600)
        with tempfile.TemporaryDirectory() as temp:
            work = Work(str(Path(temp) / "absent.pdf"), str(Path(temp) / "result"))
            with patch.object(cli, "text_layer", return_value=layer), patch("yeone_ocr.pdfio.page_frame", return_value=(20, 30, 370, 560)):
                got = cli._write_layer(work, object(), 1)
            self.assertEqual(got["vertical_columns"], [{"x": 180, "y0": 15, "y1": 55, "text": "天地玄黃"}])
            self.assertEqual(got["plain"], "天地玄黃")


if __name__ == "__main__":
    unittest.main()
