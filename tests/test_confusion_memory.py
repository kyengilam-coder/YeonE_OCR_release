"""Per-engine confusion ledger: harvested from comparison projects, used only to route review."""
import json
from pathlib import Path
import tempfile
import unittest
import copy
import multiprocessing
import time

from PIL import Image
from ab_ocr.ocr import RecognitionResult
from yeone_ocr import confusion_memory as memory
from yeone_ocr.comparison import ComparisonError, ComparisonProject
from yeone_ocr.comparison_ai import AISettings, ReviewPolicy, _prompt, prepare_plan


def ledger(engine="ppocrv6", read="己", actual="已", human=3, seen=20, **extra):
    counts = {"human": human, **extra}
    return {"format": memory.FORMAT, "version": memory.VERSION, "enabled": True, "suppressed": [],
            "documents": {"d" * 32: {"at": "2026-11-15T00:00:00+0000", "source": "a.pdf", "pages": 1,
                                     "engines": {engine: {"versions": [], "coverage_basis": memory.COVERAGE_BASIS, "seen": {read: seen},
                                                          "pairs": [[read, actual, counts, ["甲「己」丙"]]]}}}}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "confusions.json"
        self.source = self.root / "synthetic.png"
        with Image.new("RGB", (400, 200), "white") as image:
            image.save(self.source)
        self.project = ComparisonProject.open(self.source)

    def add(self, engine="ppocrv6", text="甲己丙", confidence=99, words=None):
        words = [{"text": text, "bbox": [40, 20, 90, 45], "confidence": confidence}] if words is None else words
        raw = RecognitionResult(text, words, str(self.source), self.project.source_sha256,
                                1, 400, 200, "synthetic", "chi_tra", 6 if engine == "tesseract" else None,
                                0, engine=engine)
        return self.project.add_result(raw, request={"profile": "traditional", "direction": "horizontal",
                                                     "dpi": 300, "engine_root": None, "policy": "local-three-engine-v1"})

    def observed(self):
        return memory.page_observations(self.project.page(1))


class ObservationTests(Fixture):
    def test_human_edit_is_a_human_pair_with_context(self):
        self.add()
        self.project.set_text(1, "甲已丙")
        observed = self.observed()
        self.assertEqual(observed["engine"], "ppocrv6")
        self.assertEqual(observed["pairs"], [("己", "已", "human", "甲「己」丙")])
        self.assertEqual(observed["seen"], {"己": 1})

    def test_unchecked_page_contributes_nothing(self):
        self.add()
        self.assertIsNone(self.observed())

    def test_reviewed_page_counts_reads_without_pairs(self):
        self.add()
        self.project.set_reviewed(1, True)
        observed = self.observed()
        self.assertEqual((observed["pairs"], observed["seen"]["己"]), ([], 1))

    def test_undone_edit_leaves_no_pair(self):
        self.add()
        self.project.set_text(1, "甲已丙")
        self.project.undo_edit(1)
        self.assertIsNone(self.observed())

    def test_later_human_edit_overrides_machine_label(self):
        self.add()
        self.project.set_text(1, "甲已丙")
        self.project.set_text(1, "甲已乙")
        pairs = self.observed()["pairs"]
        self.assertEqual(sorted(p[:3] for p in pairs), [("丙", "乙", "human"), ("己", "已", "human")])

    def test_not_recognition_errors_are_ignored(self):
        self.add(text="会,己")
        self.project.set_text(1, "會，己")       # script-only and width-only changes
        self.assertEqual(self.observed()["pairs"], [])

    def test_large_rewrite_is_skipped(self):
        self.add(text="甲乙丙丁戊己庚辛壬癸")
        self.project.set_text(1, "子丑寅卯辰巳午未申酉")
        self.assertIsNone(self.observed())

    def test_merged_reading_is_one_pair(self):
        self.add(text="甲口口丙")
        self.project.set_text(1, "甲回丙")
        self.assertEqual([p[:3] for p in self.observed()["pairs"]], [("口口", "回", "human")])

    def test_baseline_read_after_the_edits_is_skipped(self):
        self.add()
        self.project.set_text(1, "甲已丙")
        page = self.project.page(1)
        page["runs"][0]["created_at"] = "9999-01-01T00:00:00+00:00"
        self.assertIsNone(memory.page_observations(page))

    def test_actor_marks_candidate_acceptance(self):
        self.add(words=[])
        self.add("tesseract", "甲已丙", words=[])
        self.project.choose_baseline(1, "ppocrv6")
        suggestions = self.project.compare_page(1)
        eligible = [s for s in suggestions if any(e["engine"] == "tesseract" and e["eligible"] for e in s["evidence"])]
        if not eligible:
            self.skipTest("synthetic engines produced no eligible candidate")
        self.project.accept_candidate(1, eligible[0]["id"], "tesseract", actor="auto")
        self.assertEqual(self.project.page(1)["edits"][-1]["actor"], "auto")
        self.assertEqual(self.observed()["pairs"][0][2], "ai_auto")
        self.project.set_reviewed(1, True)               # a person checked the page afterwards
        self.assertEqual(self.observed()["pairs"][0][2], "ai_confirmed")

    def test_actor_value_is_validated(self):
        self.add()
        with self.assertRaises(ComparisonError):
            self.project.accept_candidate(1, "x", "tesseract", actor="robot")


class StoreTests(Fixture):
    def test_harvest_is_idempotent_per_document(self):
        self.add()
        self.project.set_text(1, "甲已丙")
        first = memory.harvest_project(self.project, self.store)
        memory.harvest_project(self.project, self.store)
        self.assertEqual((first["pages"], first["pairs"]), (1, 1))
        learned = memory.LearnedConfusions.load(self.store)
        self.assertEqual([(r.engine, r.read, r.seen, r.weight) for r in learned.rows], [("ppocrv6", "己", 1, 1.0)])
        self.assertFalse(learned.rows[0].risky)          # one human correction is not yet a pattern
        self.project.undo_edit(1)
        memory.harvest_project(self.project, self.store)  # the document's contribution is replaced, not added
        self.assertEqual(memory.LearnedConfusions.load(self.store).rows, ())

    def test_thresholds_and_weights(self):
        cases = [({"human": 2}, 20, True), ({"human": 1}, 20, False), ({"human": 2}, 100, False),
                 ({"human": 0, "ai_auto": 6}, 20, False), ({"human": 0, "ai_auto": 7}, 20, True),
                 ({"human": 0, "ai_confirmed": 3}, 20, True)]
        for counts, seen, risky in cases:
            with self.subTest(counts=counts, seen=seen):
                learned = memory.LearnedConfusions.from_store(ledger(seen=seen, **{"human": 0, **counts}))
                self.assertEqual(bool(learned.risky("ppocrv6")), risky)
                self.assertEqual(learned.risky("tesseract"), {})

    def test_suppress_reset_and_disable(self):
        self.store.write_text(json.dumps(ledger()), encoding="utf-8")
        self.assertTrue(memory.LearnedConfusions.load(self.store).active())
        memory.suppress("ppocrv6", "己", "已", self.store)
        self.assertFalse(memory.LearnedConfusions.load(self.store).active())
        memory.unsuppress("ppocrv6", "己", "已", self.store)
        memory.set_enabled(False, self.store)
        learned = memory.LearnedConfusions.load(self.store)
        self.assertFalse(learned.active())
        self.assertEqual(learned.risky("ppocrv6"), {})
        memory.set_enabled(True, self.store)
        memory.reset(self.store)
        self.assertEqual(memory.LearnedConfusions.load(self.store).rows, ())

    def test_broken_ledger_is_kept_aside_not_used(self):
        self.store.write_text("{not json", encoding="utf-8")
        learned = memory.LearnedConfusions.load(self.store)
        self.assertTrue(learned.warning)
        self.assertFalse(learned.active())
        self.add()
        self.project.set_text(1, "甲已丙")
        memory.harvest_project(self.project, self.store)
        self.assertEqual(len(list(self.root.glob("confusions.json.broken-*"))), 1)
        self.assertEqual(len(memory.LearnedConfusions.load(self.store).rows), 1)

    def test_default_path_follows_environment(self):
        self.assertEqual(memory.store_path(), Path(memory.os.environ[memory.ENV_PATH]))


class RoutingTests(Fixture):
    settings = AISettings("openai", "synthetic-model")

    def plan(self, learned=None, policy=None):
        return prepare_plan(self.project, [1], policy or ReviewPolicy(selective=True), settings=self.settings,
                            confusions=learned)

    def test_confident_word_with_learned_character_is_routed_with_hints(self):
        self.add()
        clean = self.plan()
        self.assertEqual(clean.summary["calls"], 0)
        self.assertNotIn("learned_confusions", clean.summary)
        plan = self.plan(memory.LearnedConfusions.from_store(ledger()))
        target = plan.batches[0]["targets"][0]
        self.assertEqual((target["before"], target["difficulty_reason"]), ("己", "learned-confusable"))
        self.assertEqual(target["context_before"], "甲")
        self.assertEqual(target["learned_confusions"][0]["verified_alternatives"], ["已"])
        self.assertEqual(plan.summary["difficulty_reasons"], {"learned-confusable": 1})
        self.assertEqual(plan.summary["learned_confusions"]["routed_targets"], 1)
        self.assertEqual(plan.summary["unlocalized_targets"], {})
        self.assertIn("LEARNED CONFUSIONS", _prompt(plan.batches[0], plan.policy, []))
        self.assertNotEqual(plan.fingerprint, clean.fingerprint)

    def test_other_engine_or_inactive_ledger_changes_nothing(self):
        self.add()
        baseline = self.plan()
        for learned in (memory.LearnedConfusions.from_store(ledger(engine="tesseract")),
                        memory.LearnedConfusions.from_store({**ledger(), "enabled": False}),
                        memory.LearnedConfusions.from_store(ledger(human=1))):
            with self.subTest(learned=learned.rows):
                plan = self.plan(learned)
                self.assertEqual((plan.fingerprint, plan.summary), (baseline.fingerprint, baseline.summary))

    def test_human_edited_word_is_not_routed_and_not_unresolved(self):
        self.add()
        self.project.set_text(1, "甲已丙")
        plan = self.plan(memory.LearnedConfusions.from_store(ledger()))
        self.assertEqual(plan.summary["calls"], 0)
        self.assertEqual(plan.summary["unlocalized_targets"], {})

    def test_word_without_geometry_is_not_unresolved(self):
        self.add(words=[{"text": "甲己丙", "bbox": [0, 0, 400, 200], "confidence": 99}])
        plan = self.plan(memory.LearnedConfusions.from_store(ledger()))
        self.assertEqual(plan.summary["calls"], 0)
        self.assertEqual(plan.summary["unlocalized_targets"], {})

    def test_low_confidence_target_gets_hints_without_a_second_target(self):
        self.add(confidence=10)
        plan = self.plan(memory.LearnedConfusions.from_store(ledger()))
        targets = [t for b in plan.batches for t in b["targets"]]
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["difficulty_reason"], "low-confidence-ppocrv6")
        self.assertEqual(targets[0]["learned_confusions"][0]["read"], "己")
        self.assertEqual(plan.summary["learned_confusions"], {"digest": plan.summary["learned_confusions"]["digest"],
                                                              "hinted_targets": 1, "routed_targets": 0})

    def test_routing_is_capped_per_page(self):
        words = [{"text": "己", "bbox": [10 + 12 * i, 20, 20 + 12 * i, 45], "confidence": 99} for i in range(30)]
        self.add(text="己" * 30, words=words)
        plan = self.plan(memory.LearnedConfusions.from_store(ledger()), ReviewPolicy(selective=True, batch_size=4))
        self.assertEqual(plan.summary["difficulty_reasons"], {"learned-confusable": memory.MAX_ROUTED_PER_PAGE})


if __name__ == "__main__":
    unittest.main()
