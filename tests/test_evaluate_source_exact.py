"""Source-codepoint evaluation is additive, literal and read-only."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yeone_ocr import evaluate as E
from yeone_ocr.state import Work
from tests.test_evaluate import adj_entry, plain_hwpx, rec, run_main


class SourceCodepointEvaluation(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parents[1] / "work"
        workspace.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="eval-source-exact-", dir=workspace)
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.work = Work(str(self.folder / "input.pdf"), str(self.folder / "records"))

    def unit(self, reference, record):
        self.work.save_record(1, record)
        return E.build_unit(self.work, None, "whole", [1], [reference])

    def test_exact_projection_preserves_identity_and_literal_private_use(self):
        text = "樂神\U0002f800一\ufe00二\U000e0100한\u200b\U000f0000"
        projected = E.flatten_source_exact([(7, " " + text + "\n")], expand=False)
        self.assertEqual(projected.text, text)
        self.assertEqual(projected.page, [7] * len(text))
        marked = E.flatten_source_exact([(7, "甲〔一\ufe00/一\U000e0100〕乙")])
        self.assertEqual(marked.text, "甲一\ufe00乙")
        self.assertEqual(marked.marks[0]["cands"], ["一\ufe00", "一\U000e0100"])
        self.assertEqual((marked.marks[0]["start"], marked.marks[0]["end"]), (1, 3))

    def test_identity_errors_are_visible_without_changing_legacy_metrics(self):
        reference = "樂一\ufe00二\U000e0100한\u200b\U000f0000"
        hypothesis = "樂一二한"
        unit = self.unit(reference, rec(1, [hypothesis], [hypothesis], [hypothesis]))
        before = copy.deepcopy(unit)
        measured = E.eval_unit(unit, True)
        legacy_input = {key: value for key, value in unit.items() if key != "source_exact"}
        legacy = E.eval_unit(legacy_input, True)
        self.assertEqual({k: v for k, v in measured.items() if k != "source_exact"},
                         {k: v for k, v in legacy.items() if k != "source_exact"})
        self.assertEqual(unit, before)
        exact = measured["source_exact"]
        self.assertEqual(exact["ref_chars"], len(reference))
        self.assertEqual(exact["stages"]["final"]["edits"], 8)
        self.assertEqual(exact["outside_errors"], 8)
        self.assertGreater(exact["stages"]["final"]["edits"], measured["stages"]["final"]["edits_raw"])
        self.assertFalse(legacy["source_exact"]["available"])

    def test_literal_oracle_and_reference_position_damage(self):
        entry = adj_entry("〔樂/樂〕", "樂", "樂", "樂", 0, 1, "甲", "乙")
        unit = self.unit("甲樂乙", rec(1, ["甲樂乙"], ["甲樂乙"], ["甲樂乙"], adj=[entry]))
        result = E.eval_unit(unit, False)
        exact = result["source_exact"]
        self.assertEqual(result["stages"]["final"]["edits"], 0)
        self.assertEqual(exact["stages"]["merged_default"]["edits"], 0)
        self.assertEqual(exact["stages"]["final"]["edits"], 1)
        self.assertEqual(exact["reference_position_regressions"], 1)
        marked = self.unit("甲一\U000e0100乙", rec(1, ["甲〔一\ufe00/一\U000e0100〕乙"]))
        exact = E.eval_unit(marked, False)["source_exact"]
        self.assertEqual(exact["stages"]["merged_default"]["edits"], 1)
        self.assertEqual(exact["stages"]["merged_oracle"]["edits"], 0)
        self.assertEqual(exact["outside_errors"], 0)

    def test_supplementary_private_use_is_not_a_generated_stage_sentinel(self):
        text = "甲\U000f0000一\ufe00二\U000e0100乙"
        unit = self.unit(text, rec(1, [text], [text], [text]))
        source = unit["source_exact"]
        for stage in ("pass1", "pass2", "merged_default", "final"):
            self.assertEqual(source[stage].text, text, stage)
        exact = E.eval_unit(unit, False)["source_exact"]
        self.assertTrue(all(exact["stages"][stage]["edits"] == 0 for stage in E.STAGES if stage != "layer"))

    def test_inexact_alignment_never_produces_an_exact_score_or_partial_total(self):
        unit = self.unit("甲乙", rec(1, ["甲丙"]))
        def approximate(reference, hypothesis, stats):
            stats["inexact"] = 1
            return [("R", 0, 0)]
        with patch.object(E, "align", side_effect=approximate):
            result = E.source_exact_metrics(unit)
        self.assertTrue(all(not row["available"] and row["edits"] is None for row in result["stages"].values()))
        self.assertIsNone(result["outside_errors"])
        good = E.source_exact_metrics(unit)
        overall = E._overall_source_exact([{"name": "good", "source_exact": good},
                                           {"name": "limited", "source_exact": result}])
        self.assertIsNone(overall["stages"]["final"]["cer"])
        self.assertEqual(overall["stages"]["final"]["unavailable_units"], ["limited"])

    def test_missing_historical_choice_does_not_invent_a_reconstructed_score(self):
        missing = adj_entry("〔樂/樂〕", "樂", "樂", "樂", 0, 1, "甲", "乙")
        unit = self.unit("甲樂乙", rec(1, ["甲丙乙"], adj=[missing]))
        exact = E.eval_unit(unit, False)["source_exact"]
        self.assertEqual(exact["adj_unlinked"], 1)
        for stage in ("merged_default", "merged_oracle"):
            self.assertFalse(exact["stages"][stage]["available"])
            self.assertEqual(exact["stages"][stage]["reason"], "pre_adjudication_reconstruction_incomplete")
        self.assertEqual(exact["stages"]["final"]["edits"], 1)
        self.assertIsNone(exact["reference_position_regressions"])
        self.assertIsNone(exact["outside_errors"])

    def test_cli_writes_visible_policy_and_preserves_all_source_files(self):
        reference = "甲樂一\ufe00二\U000e0100乙"
        self.work.save_record(1, rec(1, ["甲樂一二乙"]))
        hwpx = plain_hwpx(str(self.folder / "reference.hwpx"), [reference])
        inputs = [Path(hwpx), self.work.record_path(1), self.work.state_path]
        before = {path: path.read_bytes() for path in inputs if path.exists()}
        argv = ["eval", str(self.folder / "input.pdf"), "--work", str(self.work.dir),
                "--pages", "1", "--hwpx", hwpx]
        code, output = run_main(argv)
        self.assertEqual(code, 0, output)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        folder = self.work.dir / "eval"
        summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["schema_version"], 2)
        self.assertEqual(summary["source_exact_policy"], E.SOURCE_EXACT_POLICY)
        self.assertEqual(summary["overall"]["source_exact"]["stages"]["final"]["edits"], 3)
        for name in ("summary.md", "diffs.html"):
            text = (folder / name).read_text(encoding="utf-8")
            self.assertIn("CER(원문 코드)", text)
            self.assertIn("Unicode 정규화·자형 접기를 하지 않습니다", text)


if __name__ == "__main__":
    unittest.main()
