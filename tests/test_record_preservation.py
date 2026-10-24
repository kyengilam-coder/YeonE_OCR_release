from copy import deepcopy
from dataclasses import asdict
import unittest
from unittest import mock

from yeone_ocr.record_preservation import (
    PAGE_SCOPE, STRUCTURE_PRESERVATION_NOTE, preserve_manual_regions,
)
from yeone_ocr.schema import Column, PageReading, Paragraph, Region, RegionReread, Uncertain


def reading(first="甲乙", second="丙丁"):
    return PageReading(pdf_page=1, regions=[
        Region(id=rid, kind="본문", paragraphs=[Paragraph(columns=[Column(n=1, t=text)])])
        for rid, text in (("r1", first), ("r2", second))
    ])


def text(page, index=0):
    return page.regions[index].paragraphs[0].columns[0].text


class RecordPreservation(unittest.TestCase):
    def test_unchanged_machine_result_accepts_regeneration_and_is_a_copy(self):
        baseline = reading()
        new = reading("甲樂一\ufe00", "丙丁\U000e0100")
        record = {"merged": baseline.model_dump()}
        got, ids = preserve_manual_regions(record, baseline, new)
        self.assertEqual(got, new)
        self.assertEqual(ids, [])
        got.regions[0].paragraphs[0].columns[0].text = "changed"
        self.assertEqual(text(new), "甲樂一\ufe00")
        self.assertEqual(text(baseline), "甲乙")

    def test_manual_region_and_its_notices_survive_without_mutating_sources(self):
        baseline = reading()
        saved = reading("手樂一\ufe00\U000e0100")
        saved.uncertain = [Uncertain(r="r1", ctx="원본 확인 메모")]
        new = reading("自動甲", "新丙丁")
        new.uncertain = [Uncertain(r="r1", ctx="새 비교 경고")]
        record = {"merged": saved.model_dump(), "pass1": baseline.model_dump(),
                  "pass2": {"r1": {"columns": [{"text": "原始"}]}},
                  "adjudications": [{"choice": "樂\ufe00", "evidence": ["원본"]}]}
        snapshots = deepcopy((record, baseline.model_dump(), new.model_dump()))
        got, ids = preserve_manual_regions(record, baseline, new)
        self.assertEqual(ids, ["r1"])
        self.assertEqual(text(got), "手樂一\ufe00\U000e0100")
        self.assertEqual(text(got, 1), "新丙丁")
        self.assertEqual([u.context for u in got.uncertain], ["새 비교 경고", "원본 확인 메모"])
        self.assertEqual((record, baseline.model_dump(), new.model_dump()), snapshots)
        got.uncertain[1].context = "changed"
        self.assertEqual(record["merged"]["uncertain"][0]["context"], "원본 확인 메모")

    def test_historical_adjudication_in_baseline_is_not_a_manual_edit(self):
        adjudicated = reading("甲人")
        record = {"merged": adjudicated.model_dump(), "pass1": reading("甲〔入/人〕").model_dump(),
                  "adjudications": [{"region_id": "r1", "choice": "人"}]}
        snapshot = deepcopy(record)
        new = reading("甲人", "新文字")
        got, ids = preserve_manual_regions(record, adjudicated, new)
        self.assertEqual(got, new)
        self.assertEqual(ids, [])
        self.assertEqual(record, snapshot)

    def test_manual_page_fields_do_not_replace_unchanged_generated_fields(self):
        baseline = reading()
        baseline.running_header = "舊標題"
        saved = baseline.model_copy(deep=True)
        saved.print_page = "二十三"
        saved.page_label = "樂\ufe00"
        saved.page_note = "人手註記"
        new = reading("新甲", "新乙")
        new.running_header = "新標題"
        got, ids = preserve_manual_regions({"merged": saved.model_dump()}, baseline, new)
        self.assertEqual(ids, [PAGE_SCOPE])
        self.assertEqual((got.print_page, got.page_label, got.page_note), ("二十三", "樂\ufe00", "人手註記"))
        self.assertEqual(got.running_header, "新標題")
        self.assertEqual(text(got), "新甲")

    def test_notice_only_difference_never_replaces_new_text_or_new_warnings(self):
        baseline = reading()
        baseline.uncertain = [Uncertain(r="r1", ctx="이전 파생 경고")]
        saved = baseline.model_copy(deep=True)
        saved.uncertain.append(Uncertain(r="r2", ctx="사용자 메모"))
        new = reading("새 판독")
        new.uncertain = [Uncertain(r="r1", ctx="새 경고")]
        got, ids = preserve_manual_regions({"merged": saved.model_dump()}, baseline, new)
        self.assertEqual(text(got), "새 판독")
        self.assertEqual(ids, [])
        self.assertEqual([u.context for u in got.uncertain], ["새 경고", "사용자 메모"])

    def test_manual_structure_preserves_whole_page_and_adds_explicit_notice(self):
        baseline = reading()
        for change in ("reorder", "paragraph", "duplicate", "deleted"):
            with self.subTest(change=change):
                saved = baseline.model_copy(deep=True)
                if change == "reorder":
                    saved.regions.reverse()
                elif change == "paragraph":
                    saved.regions[0].paragraphs.append(Paragraph(columns=[Column(n=2, t="手入力")]))
                elif change == "duplicate":
                    saved.regions[1].id = "r1"
                else:
                    saved.regions.pop()
                new = reading("새 결과", "다른 결과")
                warning = Uncertain(r="r2", ctx="새 구조 경고")
                new.uncertain = [warning]
                got, ids = preserve_manual_regions({"merged": saved.model_dump()}, baseline, new)
                self.assertEqual(got.regions, saved.regions)
                self.assertEqual(ids, [PAGE_SCOPE, *dict.fromkeys(r.id for r in saved.regions)])
                self.assertIn(warning, got.uncertain)
                self.assertIn(STRUCTURE_PRESERVATION_NOTE, [u.context for u in got.uncertain])

    def test_new_structure_with_old_manual_text_uses_whole_page_fallback(self):
        baseline = reading()
        saved = reading("人手校正")
        new = reading("新甲")
        new.regions.reverse()
        got, ids = preserve_manual_regions({"merged": saved.model_dump()}, baseline, new)
        self.assertEqual(got.regions, saved.regions)
        self.assertIn(PAGE_SCOPE, ids)

    def test_new_structure_without_manual_text_is_allowed(self):
        baseline = reading()
        new = reading("新甲")
        new.regions.reverse()
        got, ids = preserve_manual_regions({"merged": baseline.model_dump()}, baseline, new)
        self.assertEqual(got, new)
        self.assertEqual(ids, [])

    def test_region_note_or_coordinates_are_preserved_only_for_that_region(self):
        baseline = reading()
        saved = baseline.model_copy(deep=True)
        saved.regions[0].note = "사람이 지정한 판독 메모"
        saved.regions[0].bbox = [10, 20, 900, 800]
        new = reading("新甲", "新乙")
        got, ids = preserve_manual_regions({"merged": saved.model_dump()}, baseline, new)
        self.assertEqual(ids, ["r1"])
        self.assertEqual(got.regions[0], saved.regions[0])
        self.assertEqual(got.regions[1], new.regions[1])

    def test_notices_are_deduplicated_and_missing_saved_reading_is_safe(self):
        baseline = reading()
        saved = reading("手校")
        note = Uncertain(r="r1", ctx="같은 확인 메모")
        saved.uncertain = [note]
        new = reading("新甲")
        new.uncertain = [note.model_copy(deep=True)]
        got, _ = preserve_manual_regions({"merged": saved.model_dump()}, baseline, new)
        self.assertEqual(got.uncertain, [note])
        got, ids = preserve_manual_regions({}, baseline, new)
        self.assertEqual(got, new)
        self.assertEqual(ids, [])
        self.assertIsNot(got, new)

    def test_resolved_failure_notice_is_not_treated_as_a_manual_memo(self):
        baseline = reading()
        saved = baseline.model_copy(deep=True)
        saved.uncertain = [Uncertain(r="r1", ctx="2차 판독 실패(과거 오류) — 원본 확인"),
                           Uncertain(r="r1", ctx="사용자 메모: 2차 판독 실패(과거 오류)를 확인함")]
        got, ids = preserve_manual_regions({"merged": saved.model_dump()}, baseline, reading("새 판독"))
        self.assertEqual(ids, [])
        self.assertEqual(text(got), "새 판독")
        self.assertEqual([u.context for u in got.uncertain], ["사용자 메모: 2차 판독 실패(과거 오류)를 확인함"])

    def test_current_failure_notice_survives_manual_preservation(self):
        baseline = reading()
        saved = reading("人手校正")
        saved.uncertain = [Uncertain(r="r1", ctx="2차 판독 실패(이전 오류) — 원본 확인"),
                           Uncertain(r="r1", ctx="수동 확인 메모")]
        new = reading("새 결과")
        new.uncertain = [Uncertain(r="r1", ctx="2차 판독 실패(새 오류) — 원본 확인")]
        got, ids = preserve_manual_regions({"merged": saved.model_dump()}, baseline, new)
        self.assertEqual(ids, ["r1"])
        self.assertEqual(text(got), "人手校正")
        self.assertEqual([u.context for u in got.uncertain], ["2차 판독 실패(새 오류) — 원본 확인", "수동 확인 메모"])

    def test_structure_fallback_drops_resolved_failure_but_keeps_current_warning(self):
        baseline = reading()
        saved = reading("人手校正")
        saved.regions.reverse()
        saved.uncertain = [Uncertain(r="r1", ctx="2차 판독 실패(과거 오류) — 원본 확인"),
                           Uncertain(r="r1", ctx="수동 구조 메모")]
        new = reading("새 결과")
        new.uncertain = [Uncertain(r="r2", ctx="2차 판독 실패(현재 오류) — 원본 확인")]
        got, ids = preserve_manual_regions({"merged": saved.model_dump()}, baseline, new)
        self.assertIn(PAGE_SCOPE, ids)
        self.assertEqual(got.regions, saved.regions)
        self.assertEqual([u.context for u in got.uncertain],
                         ["수동 구조 메모", "2차 판독 실패(현재 오류) — 원본 확인", STRUCTURE_PRESERVATION_NOTE])


class RemergePreservation(unittest.TestCase):
    def machine_record(self, first="甲入乙", second="甲人乙", *, policy=None, mode="vertical"):
        from yeone_ocr import cli, merge
        from yeone_ocr.text_policy import LEGACY_TEXT_POLICY
        p1 = reading(first)
        p2 = {"r1": RegionReread(columns=[Column(n=1, t=second)]),
              "r2": RegionReread(columns=[Column(n=1, t="丙丁")])}
        with mock.patch.object(merge, "FOLD_ZH", mode == "horizontal_zh"):
            merged, stats = merge.merge_passes(p1, p2, text_policy=policy or LEGACY_TEXT_POLICY)
        cli.fill_page_marks(merged)
        record = {"pdf_page": 1, "version": "historical", "mode": mode,
                  "pass1": p1.model_dump(), "pass2": {key: value.model_dump() for key, value in p2.items()},
                  "merged": merged.model_dump(), "merge_stats": stats}
        if policy is not None:
            record["text_policy"] = policy
        return record

    def test_remerge_keeps_manual_unicode_edits_and_adds_current_warnings(self):
        from yeone_ocr import cli, merge
        record = self.machine_record()
        previous = PageReading.model_validate(record["merged"])
        previous.regions[0].paragraphs[0].columns[0].text = "人手樂\ufe00\U000e0100校正"
        previous.page_label = "사람 쪽수"
        previous.uncertain.append(Uncertain(r="r1", ctx="사람이 남긴 원본 확인 메모"))
        record["merged"] = previous.model_dump()
        record["pass2_failed"] = {"r2": "이전 응답이 불완전함"}
        raw_before = deepcopy((record["pass1"], record["pass2"]))
        with mock.patch.object(merge, "FOLD_ZH", False):
            got = cli.remerge_record(record, "甲入乙丙丁")
        result = PageReading.model_validate(got["merged"])
        self.assertEqual(text(result), "人手樂\ufe00\U000e0100校正")
        self.assertEqual(result.page_label, "사람 쪽수")
        self.assertEqual(got["merge_stats"]["manual_preserved_regions"], ["r1", PAGE_SCOPE])
        self.assertIn("사람이 남긴 원본 확인 메모", [u.context for u in result.uncertain])
        self.assertTrue(any(u.region_id == "r2" and u.context.startswith("2차 판독 실패(") for u in result.uncertain))
        self.assertEqual((got["pass1"], got["pass2"]), raw_before)
        once = deepcopy(got["merged"])
        with mock.patch.object(merge, "FOLD_ZH", False):
            self.assertEqual(cli.remerge_record(got, "甲入乙丙丁")["merged"], once)

    def test_empty_window_historical_adjudication_replays_without_becoming_manual(self):
        from yeone_ocr import cli, merge
        from yeone_ocr.adjudicate import apply_adjudications, find_sites
        record = self.machine_record()
        merged = PageReading.model_validate(record["merged"])
        site = find_sites(merged)[0]
        record["adjudications"] = [{**asdict(site), "choice": "人", "window": "", "loc": "uniform",
                                    "evidence": "이전 판에서 확정한 기록"}]
        self.assertEqual(apply_adjudications(merged, record["adjudications"]), 1)
        record["merged"] = merged.model_dump()
        immutable = deepcopy((record["pass1"], record["pass2"], record["adjudications"]))
        with mock.patch.object(merge, "FOLD_ZH", False):
            got = cli.remerge_record(record, "甲人乙丙丁")
        self.assertEqual(text(PageReading.model_validate(got["merged"])), "甲人乙")
        self.assertEqual(got["merge_stats"]["adjudicated"], 1)
        self.assertEqual(got["merge_stats"]["unmapped"], 0)
        self.assertNotIn("manual_preserved_regions", got["merge_stats"])
        self.assertEqual((got["pass1"], got["pass2"], got["adjudications"]), immutable)

    def test_policy_absent_legacy_record_replays_without_policy_upgrade(self):
        from yeone_ocr import cli, merge
        record = self.machine_record("甲樂一\ufe00", "甲樂一")
        self.assertEqual(text(PageReading.model_validate(record["merged"])), "甲樂一")
        before = deepcopy((record["pass1"], record["pass2"], record["merged"]))
        with mock.patch.object(merge, "FOLD_ZH", False):
            got = cli.remerge_record(record, "甲樂一丙丁")
        self.assertNotIn("text_policy", got)
        self.assertNotIn("manual_preserved_regions", got["merge_stats"])
        self.assertEqual((got["pass1"], got["pass2"], got["merged"]), before)

    def test_source_policy_remerge_keeps_codes_selectors_and_jamo(self):
        from yeone_ocr import cli, merge
        from yeone_ocr.text_policy import SOURCE_TEXT_POLICY
        original = "甲樂一\ufe00漢\U000e0100한"
        record = self.machine_record(original, "甲樂一漢한", policy=SOURCE_TEXT_POLICY)
        immutable = deepcopy((record["pass1"], record["pass2"]))
        with mock.patch.object(merge, "FOLD_ZH", False):
            got = cli.remerge_record(record, "甲樂一漢한丙丁")
        self.assertEqual(got["text_policy"], SOURCE_TEXT_POLICY)
        self.assertEqual(text(PageReading.model_validate(got["merged"])), original)
        self.assertNotIn("manual_preserved_regions", got["merge_stats"])
        self.assertGreater(got["merge_stats"]["source_identity_holds"], 0)
        self.assertEqual((got["pass1"], got["pass2"]), immutable)

    def test_previous_baseline_uses_recorded_mode_without_false_manual_edits(self):
        from yeone_ocr import cli, merge
        record = self.machine_record("甲後乙", "甲后乙", mode="horizontal_zh")
        self.assertEqual(text(PageReading.model_validate(record["merged"])), "甲後乙")
        with mock.patch.object(merge, "FOLD_ZH", False):
            got = cli.remerge_record(record, "甲後乙丙丁")
            self.assertFalse(merge.FOLD_ZH)
        self.assertNotIn("manual_preserved_regions", got["merge_stats"])
        self.assertEqual(text(PageReading.model_validate(got["merged"])), "甲後乙")

    def test_explicit_mode_switch_recompares_without_false_manual_edits_and_is_stable(self):
        from yeone_ocr import cli, merge
        record = self.machine_record("甲後乙", "甲后乙", mode="horizontal_zh")
        original = deepcopy((record["pass1"], record["pass2"]))
        with mock.patch.object(merge, "FOLD_ZH", True):
            got = cli.remerge_record(record, "甲后乙丙丁", mode="vertical")
            self.assertTrue(merge.FOLD_ZH)
            self.assertEqual(got["mode"], "vertical")
            self.assertEqual(text(PageReading.model_validate(got["merged"])), "甲〔後/后〕乙")
            self.assertNotIn("manual_preserved_regions", got["merge_stats"])
            self.assertLess(got["textlayer_diff"]["ratio"], 1.0)
            first = deepcopy(got["merged"])
            repeated = cli.remerge_record(got, "甲后乙丙丁", mode="vertical")
            self.assertEqual(repeated["merged"], first)
            self.assertNotIn("manual_preserved_regions", repeated["merge_stats"])
        self.assertEqual((record["pass1"], record["pass2"]), original)

    def test_retry_keeps_manual_edit_in_the_region_being_reread(self):
        from yeone_ocr import cli, merge
        record = self.machine_record()
        saved = PageReading.model_validate(record["merged"])
        saved.regions[1].paragraphs[0].columns[0].text = "사람이 확정한 樂\ufe00"
        saved.uncertain.append(Uncertain(r="r2", ctx="사람 확인 메모"))
        saved.uncertain.append(Uncertain(r="r2", ctx="2차 판독 실패(이전 오류) — 원본 확인"))
        record["merged"] = saved.model_dump()
        before = deepcopy(record)
        newly_merged = reading("甲〔入/人〕乙", "새 2차 결과")
        newly_merged.uncertain.append(Uncertain(r="r2", ctx="새 2차의 경고"))
        with mock.patch.object(merge, "FOLD_ZH", False):
            got, ids = cli._keep_finished(record, newly_merged, PageReading.model_validate(record["pass1"]), {"r2"})
        self.assertEqual(ids, ["r2"])
        self.assertEqual(text(got, 1), "사람이 확정한 樂\ufe00")
        self.assertEqual(text(got), text(saved))
        self.assertIn("사람 확인 메모", [u.context for u in got.uncertain])
        self.assertIn("새 2차의 경고", [u.context for u in got.uncertain])
        self.assertFalse(any(u.context.startswith("2차 판독 실패(") for u in got.uncertain))
        self.assertEqual(record, before)


if __name__ == "__main__":
    unittest.main()
