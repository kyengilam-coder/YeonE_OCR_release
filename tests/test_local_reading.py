"""Exact text and source coordinate preservation without OCR or network calls."""
import copy
import io
import unittest

import pymupdf
from PIL import Image

from yeone_ocr.local_reading import reading_from_run, render_for_record
from yeone_ocr.schema import region_text


def raw(text="天地  玄黃\n\n宇宙\n", words=None):
    return {"text": text, "image_width": 200, "image_height": 100,
            "words": words if words is not None else [
                {"text": "天地", "bbox": [20, 10, 50, 25], "block": 1, "line": 1},
                {"text": "玄黃", "bbox": [70, 10, 100, 25], "block": 1, "line": 1},
                {"text": "宇宙", "bbox": [20, 50, 50, 65], "block": 1, "line": 2}]}


def plain(reading):
    return "\n".join(region_text(region) for region in reading.regions)


class LocalReadingTests(unittest.TestCase):
    def test_exact_spaces_blank_lines_and_trailing_newline(self):
        original = raw()
        snapshot = copy.deepcopy(original)
        reading = reading_from_run(original, 2, "horizontal")
        self.assertEqual(plain(reading), original["text"])
        self.assertEqual(reading.regions[0].bbox, [100, 100, 250, 500])
        self.assertEqual(reading.regions[2].bbox, [500, 100, 650, 250])
        self.assertEqual(reading.uncertain, [])
        self.assertEqual(original, snapshot)

    def test_raw_order_is_never_sorted_by_geometry(self):
        original = raw("左列\n右列", [{"text": "左列", "bbox": [10, 0, 30, 90]},
                                    {"text": "右列", "bbox": [150, 0, 180, 90]}])
        reading = reading_from_run(original, 1, "vertical")
        self.assertEqual(plain(reading), "左列\n右列")
        self.assertLess(reading.regions[0].bbox[1], reading.regions[1].bbox[1])
        self.assertEqual(reading.regions[0].reading_direction, "세로(우→좌)")

    def test_length_changing_correction_retains_original_line_box(self):
        original = raw("天地\n宇宙", [{"text": "天地", "bbox": [20, 10, 100, 25]},
                                   {"text": "宇宙", "bbox": [20, 50, 100, 65]}])
        reading = reading_from_run(original, 1, "horizontal", "天地玄黃\n宇宙")
        self.assertEqual(plain(reading), "天地玄黃\n宇宙")
        self.assertEqual(reading.regions[0].bbox, [100, 100, 250, 500])
        self.assertEqual(reading.regions[1].bbox, [500, 100, 650, 500])
        self.assertTrue(reading.uncertain)

    def test_changed_line_boundaries_retain_text_and_mark_approximation(self):
        original = raw("天地\n宇宙", [{"text": "天地", "bbox": [20, 10, 100, 25]},
                                   {"text": "宇宙", "bbox": [20, 50, 100, 65]}])
        reading = reading_from_run(original, 1, "horizontal", "天地宇宙")
        self.assertEqual(plain(reading), "天地宇宙")
        self.assertEqual(reading.regions[0].bbox, [100, 100, 650, 500])
        self.assertTrue(reading.uncertain)

    def test_empty_is_distinct_from_text_without_geometry(self):
        empty = reading_from_run(raw("", []), 1, "horizontal")
        self.assertEqual(empty.regions, [])
        missing = reading_from_run(raw("漢字", []), 1, "horizontal")
        self.assertEqual(plain(missing), "漢字")
        self.assertEqual(missing.regions[0].bbox, [0, 0, 1000, 1000])
        self.assertEqual(len(missing.uncertain), 1)

    def test_unknown_and_invalid_geometry_are_not_silently_certified(self):
        reading = reading_from_run(raw("\ufffd", [{"text": "\ufffd", "bbox": [0, 0, 0, 5]}]), 1, "horizontal")
        self.assertEqual(plain(reading), "\ufffd")
        self.assertIn("판독 불명", reading.uncertain[0].context)
        with self.assertRaises(ValueError):
            reading_from_run(dict(raw(), image_width=0), 1, "horizontal")

    def test_partial_word_geometry_cannot_certify_whole_line(self):
        reading = reading_from_run(raw("天地玄黃", [{"text": "天地", "bbox": [20, 10, 50, 25]}]), 1, "horizontal")
        self.assertEqual(plain(reading), "天地玄黃")
        self.assertIn("일부의 좌표", reading.uncertain[0].context)

    def test_whitespace_positioning_fallback_preserves_source_text(self):
        reading = reading_from_run(raw("天 地", [{"text": "天地", "bbox": [20, 10, 50, 25]}]), 1, "horizontal")
        self.assertEqual(plain(reading), "天 地")
        self.assertEqual(reading.regions[0].bbox, [100, 100, 250, 250])
        self.assertEqual(reading.uncertain, [])

    def test_subpixel_boxes_always_have_positive_normalized_area(self):
        reading = reading_from_run(raw("字", [{"text": "字", "bbox": [199.9, 99.9, 200, 100]}]), 1, "horizontal")
        self.assertEqual(reading.regions[0].bbox, [999, 999, 1000, 1000])

    def test_full_page_record_does_not_use_inset_scan_frame(self):
        stream = io.BytesIO()
        Image.new("RGB", (80, 120), "white").save(stream, format="PNG")
        with pymupdf.open() as doc:
            page = doc.new_page(width=200, height=300)
            page.insert_image(pymupdf.Rect(5, 7.5, 195, 292.5), stream=stream.getvalue())
            image, frame = render_for_record(None, doc, 1, 72, {"image_frame": "full-page"})
            try:
                self.assertEqual(image.size, (200, 300))
                self.assertEqual(frame, (0.0, 0.0, 200.0, 300.0))
            finally:
                image.close()
            image, frame = render_for_record(None, doc, 1, 72, {})
            try:
                self.assertEqual(image.size, (80, 120))
                self.assertEqual(frame, (5.0, 7.5, 190.0, 285.0))
            finally:
                image.close()


if __name__ == "__main__":
    unittest.main()
