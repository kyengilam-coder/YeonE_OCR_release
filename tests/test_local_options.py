"""Opt-in local-first refinements (stage 5). No models, no network."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from ab_ocr import ocr
from yeone_ocr.comparison import ComparisonProject, latest_runs
from yeone_ocr.local_options import FastPlan, apply_votes, column_order, is_blank, witness_spans
from yeone_ocr.schema import Column, Paragraph, Region


def reading(project, engine, text):
    return ocr.RecognitionResult(text=text, engine=engine, source_path=str(project.source_path),
        source_sha256=project.source_sha256, page_number=1, image_width=400, image_height=200,
        engine_version="synthetic", languages="chi_tra", psm=6 if engine == "tesseract" else None,
        duration_seconds=.01, words=[{"text": text, "bbox": [10, 10, 390, 100], "confidence": 90}])


class Blank(unittest.TestCase):
    def test_white_page_is_blank_and_a_few_strokes_are_not(self):
        page = Image.new("RGB", (1000, 1000), "white")
        self.assertTrue(is_blank(page))
        page.putpixel((5, 5), (0, 0, 0))              # dust
        self.assertTrue(is_blank(page))
        ImageDraw.Draw(page).rectangle([100, 100, 160, 104], fill="black")
        self.assertFalse(is_blank(page))


class Fast(unittest.TestCase):
    def selection(self, engine, score=.9):
        return {"selected_engine": engine, "scores": [{"engine": engine, "score": score}]}

    def test_samples_then_keeps_winning_engines(self):
        plan = FastPlan(sample=5)
        for engine in ("ppocrv6", "ppocrv6", "rapidocr", "ppocrv6", "tesseract"):
            self.assertIsNone(plan.engines())
            plan.observe(self.selection(engine), narrowed=False)
        self.assertEqual(plan.engines(), ["ppocrv6", "rapidocr"])
        plan.observe(self.selection("ppocrv6"), narrowed=True)      # narrowed pages do not vote
        self.assertEqual(plan.seen, 5)

    def test_low_score_or_no_selection_falls_back(self):
        self.assertTrue(FastPlan.needs_fallback(None))
        self.assertTrue(FastPlan.needs_fallback(self.selection("ppocrv6", .4)))
        self.assertFalse(FastPlan.needs_fallback(self.selection("ppocrv6", .8)))


class Votes(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        source = Path(self.folder.name) / "page.png"
        Image.new("RGB", (400, 200), "white").save(source)
        self.project = ComparisonProject.open(source)

    def load(self, texts, baseline="ppocrv6"):
        for engine, text in texts.items():
            self.project.add_result(reading(self.project, engine, text),
                                    request={"profile": "traditional", "direction": "horizontal", "dpi": 300,
                                             "engine_root": None, "policy": "local-three-engine-v1"})
        self.project.compare_page(1, baseline=baseline)

    def test_majority_over_the_baseline_is_accepted_and_recorded(self):
        self.load({"ppocrv6": "天地玄黄", "rapidocr": "天地玄黃", "easyocr": "天地玄黃"})
        self.assertEqual(apply_votes(self.project, 1), 1)
        self.assertEqual(self.project.text(1), "天地玄黃")
        self.assertEqual(latest_runs(self.project.page(1))["ppocrv6"]["raw"]["text"], "天地玄黄")
        self.assertEqual(self.project.page(1)["decisions"][-1]["decision"], "accepted")

    def test_no_majority_two_engines_or_human_edit_changes_nothing(self):
        self.load({"ppocrv6": "天地玄黄", "rapidocr": "天地玄黃", "easyocr": "天地玄皇"})
        self.assertEqual(apply_votes(self.project, 1), 0)
        self.assertEqual(self.project.text(1), "天地玄黄")

    def test_two_engines_are_not_enough(self):
        self.load({"ppocrv6": "天地玄黄", "rapidocr": "天地玄黃"})
        self.assertEqual(apply_votes(self.project, 1), 0)


def region(i, x0, x1, y0):
    return Region(id=f"local-line-{i}", kind="본문", bbox=[y0, x0, y0 + 30, x1],
                  paragraphs=[Paragraph(columns=[Column(col_from_right=1, text=str(i))])])


class Columns(unittest.TestCase):
    ROWS = (100, 150, 200, 250, 300, 350)

    def test_two_columns_read_left_then_right_between_full_width_lines(self):
        regions = [region(1, 50, 950, 20)]
        for k, y in enumerate(self.ROWS):
            regions += [region(10 + k, 50, 450, y), region(20 + k, 550, 950, y)]
        regions.append(region(2, 50, 950, 400))
        ordered = [r.id for r in column_order(regions)]
        self.assertEqual(ordered, ["local-line-1"] + [f"local-line-{10 + k}" for k in range(6)]
                         + [f"local-line-{20 + k}" for k in range(6)] + ["local-line-2"])

    def test_a_title_crossing_the_gutter_does_not_hide_the_columns(self):
        # 의방집해 p.2: a centred title box spans the gutter; lines alternate left/right in engine order.
        regions = [region(1, 300, 750, 20)]
        for k, y in enumerate(self.ROWS):
            regions += [region(20 + k, 520, 880, y), region(10 + k, 120, 480, y + 5)]
        ordered = [r.id for r in column_order(regions)]
        self.assertEqual(ordered, ["local-line-1"] + [f"local-line-{10 + k}" for k in range(6)]
                         + [f"local-line-{20 + k}" for k in range(6)])

    def test_table_cells_and_photo_captions_keep_row_order(self):
        # 대조도감: a six-column table, and captions under paired photos.
        table = [region(100 * r + c, 80 + c * 140, 200 + c * 140, 100 + r * 40) for r in range(6) for c in range(6)]
        self.assertEqual(column_order(table), table)
        captions = []
        for k, y in enumerate((300, 520, 740)):
            captions += [region(10 + k, 80, 240, y), region(20 + k, 260, 330, y),
                         region(30 + k, 420, 560, y), region(40 + k, 590, 700, y)]
        self.assertEqual(column_order(captions), captions)

    def test_single_column_is_unchanged(self):
        regions = [region(i, 50, 950, 20 + 40 * i) for i in range(8)]
        self.assertEqual(column_order(regions), regions)


class Witness(unittest.TestCase):
    def test_differences_map_onto_current_text_ignoring_spaces(self):
        current = "天地 玄黃\n宇宙洪荒"
        spans = witness_spans(current, "天地玄黄宇宙洪荒")
        self.assertEqual([current[a:b] for a, b in spans], ["黃"])

    def test_unrelated_layer_is_ignored(self):
        self.assertEqual(witness_spans("天地玄黃", "abcdefgh"), [])


class Enhancement(unittest.TestCase):
    def test_contrast_keeps_size_records_details_and_changes_pixels(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "grey.png"
            image = Image.new("RGB", (40, 30), (120, 120, 120))
            ImageDraw.Draw(image).rectangle([5, 5, 20, 20], fill=(140, 140, 140))
            image.save(source)
            plain, enhanced = Path(td) / "a.png", Path(td) / "b.png"
            details = {}
            ocr._render(source, plain, 1, 300)
            with ocr.input_enhancement("contrast"):
                size = ocr._render(source, enhanced, 1, 300, details=details)
            self.assertEqual(size, (40, 30))
            self.assertEqual(details["enhancement"]["mode"], "contrast")
            with Image.open(plain) as a, Image.open(enhanced) as b:
                self.assertNotEqual(a.getextrema(), b.getextrema())
                self.assertEqual(b.convert("L").getextrema(), (0, 255))

    def test_render_cache_separates_enhanced_and_plain(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "grey.png"
            Image.new("RGB", (40, 30), (120, 120, 120)).save(source)
            with mock.patch.object(ocr.time, "time_ns", return_value=time.time_ns() + 10_000_000_000):
                with ocr.render_cache():
                    ocr._render(source, Path(td) / "a.png", 1, 300)
                    with ocr.input_enhancement("contrast"):
                        details = {}
                        ocr._render(source, Path(td) / "b.png", 1, 300, details=details)
            self.assertIn("enhancement", details)

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            with ocr.input_enhancement("sharpen"):
                pass


class RequestKey(unittest.TestCase):
    def test_enhanced_runs_are_not_reused_for_plain_requests(self):
        from yeone_ocr.comparison import run_page
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "page.png"
            Image.new("RGB", (400, 200), "white").save(source)
            project = ComparisonProject.open(source)
            calls = []

            def recognize(path, *, engine, **kwargs):
                calls.append(engine)
                return reading(project, engine, "天地")
            run_page(project, 1, engines=["ppocrv6"], recognizer=recognize, input_enhancement="contrast")
            run_page(project, 1, engines=["ppocrv6"], recognizer=recognize, input_enhancement="contrast")
            run_page(project, 1, engines=["ppocrv6"], recognizer=recognize)
            self.assertEqual(calls, ["ppocrv6", "ppocrv6"])
            self.assertEqual(project.page(1)["runs"][0]["request"]["input_enhancement"], "contrast")


if __name__ == "__main__":
    unittest.main()
