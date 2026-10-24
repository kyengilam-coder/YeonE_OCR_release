"""New model answers need a verified occurrence; stored decisions remain replayable."""
import copy
import unittest
from dataclasses import asdict
from unittest.mock import patch

from PIL import Image, ImageDraw

from yeone_ocr import adjudicate as glyph
from yeone_ocr.schema import Column, GlyphChoice, PageReading, Paragraph, Region


def record(text="甲〔入/人〕乙"):
    reading = PageReading(pdf_page=1, regions=[Region(
        id="r1", kind="본문", bbox=[100, 100, 900, 900],
        paragraphs=[Paragraph(columns=[Column(col_from_right=1, text=text)])])])
    return {"pdf_page": 1, "pass1": reading.model_dump(), "merged": reading.model_dump()}


def location(method="ink", measured=True):
    return glyph.GlyphWindow((.1, .1, .9, .9), method, pos=2, count=3,
                             measured=measured, target=(.3, .1, .6, .9), em_px=30)


class Reader:
    model = "offline-fixture"

    def __init__(self, window="甲人乙", choice="人"):
        self.window = window
        self.choice = choice

    def adjudicate(self, *args):
        return GlyphChoice(choice=self.choice, window=self.window, evidence="offline shape fixture")


class LocationHold(unittest.TestCase):
    def setUp(self):
        self.image = Image.new("L", (120, 120), 255)
        self.addCleanup(self.image.close)

    def run_answer(self, window="甲人乙", method="ink", *, text="甲〔入/人〕乙", measured=True):
        current = record(text)
        with patch.object(glyph, "locate_glyph", return_value=location(method, measured)):
            glyph.adjudicate_page(current, self.image, Reader(window), True, batch=1)
        return current

    def test_missing_window_uniform_and_wrong_occurrence_stay_unresolved(self):
        cases = [("", "ink", "missing-window"), ("  ", "layer", "missing-window"),
                 ("甲人乙", "uniform", "unmeasured-location"),
                 ("丙人丁", "layer", "window-mismatch")]
        for window, method, reason in cases:
            with self.subTest(window=window, method=method):
                current = self.run_answer(window, method)
                self.assertEqual(current["merged"], current["pass1"])
                entry = current["adjudications"][0]
                self.assertEqual((entry["choice"], entry["unconfirmed_choice"]), ("", "人"))
                self.assertEqual(entry["location_check"]["policy"], glyph.LOCATION_POLICY)
                self.assertEqual(entry["location_check"]["reason"], reason)
                self.assertFalse(entry["location_check"]["verified"])
                self.assertEqual(current["adjudicate_stats"]["location_unverified"], 1)

    def test_answer_without_neighbours_does_not_prove_occurrence(self):
        current = self.run_answer("人", text="〔入/人〕")
        self.assertEqual(current["adjudications"][0]["location_check"]["reason"], "missing-neighbours")
        self.assertEqual(current["merged"], current["pass1"])

    def test_unknown_location_or_empty_candidate_cannot_be_verified(self):
        current = self.run_answer(method="unsupported")
        self.assertFalse(current["adjudications"][0]["location_check"]["verified"])
        current = record()
        with patch.object(glyph, "locate_glyph", return_value=location()):
            glyph.adjudicate_page(current, self.image, Reader(choice=""), True, batch=1)
        entry = current["adjudications"][0]
        self.assertEqual(entry["choice"], "")
        self.assertNotIn("location_check", entry)
        site = glyph.find_sites(PageReading.model_validate(current["merged"]))[0]
        for choice in ("", "八"):
            self.assertFalse(glyph._location_check(location(), site, "甲人乙", choice)["verified"])
        site.a = "\ufe00"
        self.assertFalse(glyph._location_check(location(), site, "甲乙", "\ufe00")["verified"])

    def test_real_coordinate_methods_with_matching_window_remain_usable(self):
        for method, measured, window in (("layer", True, "甲人乙"), ("ink", True, "甲人乙"),
                                         ("ink", False, "甲人乙"), ("layer", True, "甲人")):
            with self.subTest(method=method, measured=measured, window=window):
                current = self.run_answer(window, method, measured=measured)
                entry = current["adjudications"][0]
                self.assertEqual(entry["choice"], "人")
                self.assertTrue(entry["location_check"]["verified"])
                self.assertEqual(current["adjudicate_stats"]["decided"], 1)

    def test_real_ink_detection_can_confirm_a_matching_occurrence(self):
        # Only the reader is a fixture; exercise the real image-to-crop geometry.
        drawing = ImageDraw.Draw(self.image)
        for left in (20, 45, 70):
            drawing.rectangle((left, 40, left + 15, 60), fill=0)
        current = record()
        glyph.adjudicate_page(current, self.image, Reader(), True, batch=1)
        entry = current["adjudications"][0]
        self.assertEqual((entry["loc"], entry["choice"]), ("ink", "人"))
        self.assertTrue(entry["location_check"]["verified"])

    def test_selector_combining_and_format_marks_cannot_stand_in_for_neighbour(self):
        for mark in ("\ufe00", "\U000e0100", "\u0301", "\u200b"):
            with self.subTest(mark=ascii(mark)):
                text = "甲漢" + mark + "〔入/人〕乙"
                current = self.run_answer("字" + mark + "人丁", text=text)
                entry = current["adjudications"][0]
                self.assertEqual(entry["choice"], "")
                self.assertEqual(entry["location_check"]["reason"], "window-mismatch")
                self.assertEqual(current["merged"], current["pass1"])
                verified = self.run_answer("漢" + mark + "人乙", text=text)
                self.assertEqual(verified["adjudications"][0]["choice"], "人")
                self.assertEqual(verified["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"],
                                 "甲漢" + mark + "人乙")
        current = self.run_answer("\ufe00人", text="\ufe00〔入/人〕")
        self.assertEqual(current["adjudications"][0]["location_check"]["reason"], "missing-neighbours")

    def test_old_uniform_decision_replays_without_new_gate(self):
        current = record()
        reading = PageReading.model_validate(current["merged"])
        site = glyph.find_sites(reading)[0]
        old = {**asdict(site), "choice": "人", "loc": "uniform", "window": "", "evidence": "legacy"}
        original = copy.deepcopy(old)
        self.assertEqual(glyph.apply_adjudications(reading, [old]), 1)
        self.assertEqual(reading.regions[0].paragraphs[0].columns[0].text, "甲人乙")
        self.assertEqual(old, original)
        self.assertTrue(glyph.window_ok("", "甲", "人", "乙"))  # Historical helper contract.

    def test_incomplete_historical_entry_is_preserved_and_counted_unmapped(self):
        reading = PageReading.model_validate(record()["merged"])
        site = glyph.find_sites(reading)[0]
        incomplete = {"region_id": "r1", "col_index": 0, "char_index": 1,
                      "a": "入", "b": "人", "choice": "人"}
        complete = {**asdict(site), "choice": "人", "loc": "uniform", "window": ""}
        history = [incomplete, complete]
        original = copy.deepcopy(history)
        stats = {}
        self.assertEqual(glyph.apply_adjudications(reading, history, stats), 1)
        self.assertEqual(stats["unmapped"], 1)
        self.assertEqual(reading.regions[0].paragraphs[0].columns[0].text, "甲人乙")
        self.assertEqual(history, original)

    def test_new_hold_appends_without_changing_old_adjudication_or_human_text(self):
        current = record("甲人乙丙〔入/人〕丁")
        old = {"region_id": "r1", "col_index": 0, "char_index": 1, "n_chars": 6,
               "a": "入", "b": "人", "pre": "甲", "post": "乙丙入丁", "mark": "〔入/人〕",
               "choice": "人", "loc": "uniform", "window": "", "evidence": "legacy"}
        current["adjudications"] = [copy.deepcopy(old)]
        current["human_edits"] = [{"text": "甲人乙", "reason": "human"}]
        before = copy.deepcopy(current)
        glyph.adjudicate_page(current, self.image, Reader("丙人丁"), True, batch=1)
        self.assertEqual(current["adjudications"][0], old)
        self.assertEqual(len(current["adjudications"]), 2)
        self.assertEqual(current["merged"], before["merged"])
        self.assertEqual(current["human_edits"], before["human_edits"])
        self.assertEqual(current["adjudications"][-1]["choice"], "")

    def test_forced_suspect_location_hold_preserves_old_decision_and_notice(self):
        current = record("甲入乙")
        site = glyph.Site("r1", 0, 1, 1, 3, "入", "人", "甲", "乙", "")
        old = {"region_id": "r1", "col": 0, "char_index": 1, "n_chars": 3,
               "col_from_right": 1, "pre": "甲", "post": "乙", "consensus": "入", "layer": "人",
               "choice": "人", "outcome": "differs", "loc": "uniform", "window": "", "evidence": "legacy"}
        current["suspects"] = [copy.deepcopy(old)]
        current["merged"]["uncertain"] = [glyph.suspect_uncertain(old).model_dump()]
        before = copy.deepcopy(current["merged"])
        with patch.object(glyph, "find_suspects", return_value=([site], {"ratio": 1})), \
                patch.object(glyph, "locate_glyph", return_value=location("uniform")):
            glyph.suspect_page(current, self.image, Reader(), True, "甲人乙", force=True, batch=1)
        self.assertEqual(current["suspects"][0], old)
        self.assertEqual(len(current["suspects"]), 2)
        self.assertEqual(current["suspects"][-1]["outcome"], "undecided")
        self.assertFalse(current["suspects"][-1]["location_check"]["verified"])
        self.assertTrue(current["suspects"][-1]["preserves_previous_decision"])
        self.assertEqual(current["merged"], before)
        self.assertEqual(glyph._asked_before(current["suspects"], site), 0)
        # A later explicit, located recheck still addresses the original decision.
        with patch.object(glyph, "find_suspects", return_value=([site], {"ratio": 1})), \
                patch.object(glyph, "locate_glyph", return_value=location()):
            glyph.suspect_page(current, self.image, Reader("甲入乙", "入"), True, "甲人乙", force=True, batch=1)
        self.assertEqual(current["suspects"][-1]["outcome"], "confirmed")
        self.assertEqual(current["merged"]["uncertain"], [])


if __name__ == "__main__":
    unittest.main()
