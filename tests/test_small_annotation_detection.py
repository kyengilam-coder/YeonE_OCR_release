from copy import deepcopy
import unittest

from PIL import Image, ImageDraw

from yeone_ocr.schema import PageReading, Region
from yeone_ocr.small_annotation_detection import DETECTOR_POLICY, HORIZONTAL_DETECTOR_POLICY, detect_small_annotations


def glyph(draw, x, y, width, height):
    draw.rectangle((x, y, x + width - 1, y + height - 1), outline=0, width=2)
    draw.line((x, y + height // 2, x + width - 1, y + height // 2), fill=0, width=2)


def specimen(*, notes=True):
    image = Image.new("L", (420, 400), 255)
    draw = ImageDraw.Draw(image)
    for x in (255, 320):
        for y in range(25, 365, 34):
            glyph(draw, x, y, 24, 26)
    for y in (25, 59, 263, 297, 331):
        glyph(draw, 160, y, 24, 26)
    if notes:
        for x in (160, 176):
            for y in range(110, 207, 16):
                glyph(draw, x, y, 10, 12)
    else:
        for y in range(93, 263, 34):
            glyph(draw, 160, y, 24, 26)
    reading = PageReading(pdf_page=1, regions=[Region(id="body", kind="본문", bbox=[0, 0, 1000, 1000])])
    return image, reading


class SmallAnnotationDetection(unittest.TestCase):
    def test_finds_partial_height_double_column_and_orders_right_first(self):
        image, reading = specimen()
        result = detect_small_annotations(image, reading)
        self.assertEqual(result["detector"], DETECTOR_POLICY)
        self.assertEqual(len(result["candidates"]), 1, result)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["parent_region_id"], "body")
        self.assertEqual([item["side"] for item in candidate["subcolumns"]], ["right", "left"])
        right, left = [item["bbox_px"] for item in candidate["subcolumns"]]
        self.assertGreater(right[0], left[2])
        self.assertLess(candidate["small_pitch_px"], candidate["body_pitch_px"] * 0.8)
        self.assertLessEqual(candidate["bbox_px"][1], 110)
        self.assertGreaterEqual(candidate["bbox_px"][3], 218)

    def test_does_not_depend_on_recognized_text_and_does_not_mutate_inputs(self):
        image, reading = specimen()
        image_before, reading_before = image.tobytes(), deepcopy(reading.model_dump())
        first = detect_small_annotations(image, reading)
        reading.regions[0].note = "different OCR wording"
        second = detect_small_annotations(image, reading)
        self.assertEqual(first, second)
        self.assertEqual(image.tobytes(), image_before)
        reading.regions[0].note = ""
        self.assertEqual(reading.model_dump(), reading_before)

    def test_plain_body_columns_are_not_small_annotation_pairs(self):
        image, reading = specimen(notes=False)
        self.assertEqual(detect_small_annotations(image, reading)["candidates"], [])

    def test_hexagram_bars_do_not_become_two_text_columns(self):
        image, reading = specimen(notes=False)
        draw = ImageDraw.Draw(image)
        for y in range(110, 180, 12):
            draw.rectangle((40, y, 57, y + 2), fill=0)
            draw.rectangle((66, y, 83, y + 2), fill=0)
        self.assertEqual(detect_small_annotations(image, reading)["candidates"], [])

    def test_two_body_sized_radicals_do_not_count_as_small_text(self):
        image, reading = specimen(notes=False)
        draw = ImageDraw.Draw(image)
        draw.rectangle((155, 90, 190, 260), fill=255)
        for y in range(93, 263, 34):
            for x in (160, 176):
                glyph(draw, x, y, 10, 26)
        self.assertEqual(detect_small_annotations(image, reading)["candidates"], [])

    def test_unequal_subcolumn_lengths_remain_separate_boxes(self):
        image, reading = specimen()
        ImageDraw.Draw(image).rectangle((160, 158, 169, 230), fill=255)
        result = detect_small_annotations(image, reading)
        self.assertEqual(len(result["candidates"]), 1, result)
        right, left = result["candidates"][0]["subcolumns"]
        self.assertGreater(right["bbox_px"][3], left["bbox_px"][3])

    def test_excludes_figure_regions(self):
        image, reading = specimen()
        reading.regions.append(Region(id="excluded", kind="도식", bbox=[250, 350, 580, 470]))
        self.assertEqual(detect_small_annotations(image, reading)["candidates"], [])

    def test_existing_small_region_is_checked_even_when_one_column_was_read(self):
        image, reading = specimen()
        reading.regions.append(Region(id="small", kind="소주", bbox=[250, 350, 580, 470],
                                      paragraphs=[{"columns": [{"col_from_right": 1, "text": "only one column"}]}]))
        result = detect_small_annotations(image, reading)
        self.assertEqual(len(result["candidates"]), 1, result)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["parent_region_id"], "small")
        self.assertEqual(len(candidate["subcolumns"]), 2)
        reading.regions[-1].paragraphs[0].columns[0].text = "both columns or no text: irrelevant"
        self.assertEqual(result, detect_small_annotations(image, reading))

    def test_narrow_notes_can_keep_body_vertical_spacing(self):
        image, reading = specimen()
        draw = ImageDraw.Draw(image)
        draw.rectangle((150, 90, 200, 255), fill=255)
        for x in (160, 178):
            for y in range(100, 210, 34):
                glyph(draw, x, y, 12, 19)
        candidates = detect_small_annotations(image, reading)["candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertGreater(candidates[0]["small_pitch_px"], candidates[0]["body_pitch_px"] * 0.9)
        self.assertLessEqual(candidates[0]["bbox_px"][1], 100)
        self.assertGreaterEqual(candidates[0]["bbox_px"][3], 221)

    def test_repeated_body_grid_rejects_pair_crossing_two_note_groups(self):
        image = Image.new("L", (390, 370), 255)
        draw = ImageDraw.Draw(image)
        for center in (80, 124, 168, 212, 256):
            for y in (20, 54, 292, 326):
                glyph(draw, center - 15, y, 30, 26)
        for center in (124, 168):
            for x in (center - 16, center + 6):
                for y in range(112, 232, 17):
                    glyph(draw, x, y, 10, 12)
        result = detect_small_annotations(image, PageReading(pdf_page=1))
        self.assertEqual(len(result["candidates"]), 2, result)
        for candidate in result["candidates"]:
            center = (candidate["bbox_px"][0] + candidate["bbox_px"][2]) / 2
            self.assertLess(min(abs(center - x) for x in (124, 168)), 4)
        self.assertGreater(result["diagnostics"]["rejection_reasons"].get("crosses_body_column_grid", 0), 0)

    def test_overlapping_parent_regions_do_not_duplicate_candidate(self):
        image, reading = specimen()
        reading.regions.append(Region(id="other", kind="본문", bbox=[0, 0, 1000, 1000]))
        result = detect_small_annotations(image, reading)
        self.assertEqual(len(result["candidates"]), 1, result)
        self.assertGreater(result["diagnostics"]["rejection_reasons"].get("overlapping_parent_duplicate", 0), 0)

    def test_whole_page_fallback_only_when_no_regions_exist(self):
        image, _ = specimen()
        result = detect_small_annotations(image, PageReading(pdf_page=1))
        self.assertEqual(len(result["candidates"]), 1, result)
        self.assertEqual(result["candidates"][0]["parent_region_id"], "__page__")
        figures_only = PageReading(pdf_page=1, regions=[Region(id="figure", kind="도식")])
        self.assertEqual(detect_small_annotations(image, figures_only)["candidates"], [])

    def test_horizontal_mode_uses_separate_policy_without_rotating_vertical_notes(self):
        image, reading = specimen()
        result = detect_small_annotations(image, reading, horizontal=True)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["detector"], HORIZONTAL_DETECTOR_POLICY)

    def test_horizontal_region_is_skipped_when_page_mode_is_unrecorded(self):
        image, reading = specimen()
        reading.regions[0].reading_direction = "가로"
        result = detect_small_annotations(image, reading)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["diagnostics"]["skipped_regions"],
                         [{"region_id": "body", "reason": "horizontal_region_not_supported"}])

    def test_id_is_deterministic_and_independent_of_parent_id(self):
        image, reading = specimen()
        first = detect_small_annotations(image, reading)
        self.assertEqual(first, detect_small_annotations(image, reading))
        reading.regions[0].id = "renamed"
        second = detect_small_annotations(image, reading)
        self.assertEqual(first["candidates"][0]["id"], second["candidates"][0]["id"])

    def test_blank_tiny_and_disabled_images_return_reasons(self):
        for image, kwargs, reason in ((Image.new("L", (100, 100), 255), {}, "no_ink_contrast"),
                                      (Image.new("L", (5, 5), 255), {}, "image_too_small"),
                                      (Image.new("L", (100, 100), 255), {"max_candidates": 0}, "disabled_by_candidate_limit")):
            with self.subTest(reason=reason):
                result = detect_small_annotations(image, None, **kwargs)
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["diagnostics"]["reason"], reason)

    def test_invalid_candidate_limit_is_rejected(self):
        image, reading = specimen()
        for value in (-1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                detect_small_annotations(image, reading, max_candidates=value)

    def test_requested_and_hard_candidate_caps_are_enforced(self):
        tile, _ = specimen()
        image = Image.new("L", (tile.width * 8, tile.height * 9), 255)
        for x in range(8):
            for y in range(9):
                image.paste(tile, (x * tile.width, y * tile.height))
        result = detect_small_annotations(image, None, max_candidates=100)
        self.assertEqual(result["diagnostics"]["candidates_before_cap"], 72)
        self.assertEqual(len(result["candidates"]), 64)
        self.assertTrue(result["diagnostics"]["capped"])
        limited = detect_small_annotations(image, None, max_candidates=2)
        self.assertEqual(limited["candidates"], result["candidates"][:2])
        self.assertTrue(limited["diagnostics"]["capped"])


def horizontal_specimen(*, first_lines=2, second_lines=5):
    image = Image.new("L", (900, 650), 255)
    draw = ImageDraw.Draw(image)
    for y in (30, 80, 130):
        for x in range(25, 810, 34):
            glyph(draw, x, y, 26, 30)
    for y in (240, 390, 440, 490):
        for x in range(25, 230, 34):
            glyph(draw, x, y, 26, 30)
    for i in range(first_lines):
        for x in range(280, 800 if i == 0 else 390, 18):
            glyph(draw, x, 235+i*24, 12, 14)
    for i in range(second_lines):
        for x in range(280, 800 if i < second_lines-1 else 600, 26):
            glyph(draw, x, 380+i*32, 18, 20)
    reading = PageReading(pdf_page=1, regions=[Region(id="body", kind="본문", reading_direction="가로")])
    return image, reading


class HorizontalSmallAnnotationDetection(unittest.TestCase):
    def detect(self, image, reading=None, **kwargs):
        return detect_small_annotations(image, reading, horizontal=True, **kwargs)

    def test_two_and_five_lines_are_whole_distinct_groups_in_original_frame(self):
        image, reading = horizontal_specimen()
        result = self.detect(image, reading)
        self.assertEqual(result["detector"], HORIZONTAL_DETECTOR_POLICY)
        self.assertEqual(len(result["candidates"]), 2, result)
        for c, count in zip(result["candidates"], (2,5)):
            self.assertEqual(c["direction"], "horizontal")
            self.assertEqual([s["side"] for s in c["subcolumns"]], [f"line-{i}" for i in range(1,count+1)])
            boxes = [s["bbox_px"] for s in c["subcolumns"]]
            self.assertTrue(all(a[3] < b[1] for a,b in zip(boxes, boxes[1:])))
            self.assertTrue(all(b[0] >= 275 for b in boxes))
            self.assertGreater(boxes[0][2], 790)
        self.assertGreaterEqual(result["candidates"][1]["bbox_px"][3], 528)

    def test_word_gaps_and_single_final_character_remain_in_the_line_crop(self):
        image, reading = horizontal_specimen(second_lines=0)
        draw = ImageDraw.Draw(image)
        draw.rectangle((390, 230, 810, 255), fill=255)
        for x in (430,448,510,528,600):
            glyph(draw,x,235,12,14)
        c = self.detect(image,reading)["candidates"][0]
        self.assertGreaterEqual(c["subcolumns"][0]["bbox_px"][2],612)

    def test_existing_small_region_can_use_adjacent_body_outside_its_box(self):
        image, reading = horizontal_specimen(second_lines=0)
        reading.regions = [Region(id="note", kind="소주", bbox=[350,300,430,920], reading_direction="가로")]
        c = self.detect(image,reading)["candidates"]
        self.assertEqual(len(c),1)
        self.assertEqual(c[0]["parent_region_id"],"note")

    def test_plain_horizontal_body_and_korean_style_disconnected_strokes_are_not_notes(self):
        image, reading = horizontal_specimen(first_lines=0,second_lines=0)
        draw = ImageDraw.Draw(image)
        for y in (550,600):
            for x in range(25,810,34):
                draw.rectangle((x,y,x+12,y+12),outline=0,width=2)
                draw.line((x+20,y,x+20,y+17),fill=0,width=2)
                draw.rectangle((x,y+20,x+24,y+29),outline=0,width=2)
        self.assertEqual(self.detect(image,reading)["candidates"],[])

    def test_single_note_line_does_not_count_as_multiple_lines(self):
        image, reading = horizontal_specimen(first_lines=1,second_lines=1)
        self.assertEqual(self.detect(image,reading)["candidates"],[])

    def test_small_paragraph_without_neighboring_large_body_is_not_a_note(self):
        image, reading = horizontal_specimen(second_lines=0)
        ImageDraw.Draw(image).rectangle((0,225,260,280),fill=255)
        self.assertEqual(self.detect(image,reading)["candidates"],[])

    def test_declared_figure_table_and_vertical_regions_are_excluded(self):
        for kind in ("도식","단순표","복잡한표"):
            with self.subTest(kind=kind):
                image, reading = horizontal_specimen(second_lines=0)
                reading.regions.append(Region(id="excluded",kind=kind,bbox=[340,290,440,940]))
                self.assertEqual(self.detect(image,reading)["candidates"],[])
        image,reading = horizontal_specimen()
        reading.regions[0].reading_direction="세로"
        self.assertEqual(self.detect(image,reading)["candidates"],[])

    def test_ruled_table_without_recorded_region_is_held(self):
        image, _ = horizontal_specimen(second_lines=0)
        draw=ImageDraw.Draw(image)
        draw.rectangle((15,220,830,290),outline=0,width=2)
        draw.line((265,220,265,290),fill=0,width=2)
        result=self.detect(image)
        self.assertEqual(result["candidates"],[])
        self.assertGreater(result["diagnostics"]["ruled_areas_excluded"],0)

    def test_deterministic_bounded_deduplicated_and_input_text_independent(self):
        image,reading=horizontal_specimen()
        before_image=image.tobytes();before_reading=reading.model_dump()
        first=self.detect(image,reading)
        self.assertEqual(first,self.detect(image,reading))
        self.assertEqual(before_image,image.tobytes());self.assertEqual(before_reading,reading.model_dump())
        reading.regions[0].note="Different OCR text"
        self.assertEqual(first,self.detect(image,reading))
        reading.regions.append(Region(id="other",kind="본문",reading_direction="가로"))
        self.assertEqual(len(self.detect(image,reading)["candidates"]),2)
        limited=self.detect(image,reading,max_candidates=1)
        self.assertEqual(len(limited["candidates"]),1);self.assertTrue(limited["diagnostics"]["capped"])


if __name__ == "__main__":
    unittest.main()
