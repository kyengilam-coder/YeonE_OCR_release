"""V6 is a Paddle-family witness without changing existing source proofs."""
import copy
import unittest
from unittest.mock import patch

from ab_ocr.ocr import RecognitionResult
from yeone_ocr import comparison_fidelity as current
from yeone_ocr import comparison_fidelity_v1 as v1
from yeone_ocr import comparison_fidelity_v2 as v2
from yeone_ocr import comparison_fidelity_v3 as v3


class PPOCRv6FidelityTests(unittest.TestCase):
    original = "天地玄黃甲宇宙洪荒，寒來暑往"

    def fixture(self, module=current, *, baseline="ppocrv6", others=None):
        changed = self.original.replace("甲", "乙")
        texts = {baseline: self.original, **(others or {})}
        runs = {}
        for engine, text in texts.items():
            raw = RecognitionResult(text=text, words=[], source_path="public-synthetic.png", source_sha256="a" * 64,
                page_number=1, image_width=800, image_height=400, engine_version="synthetic", languages="chi_tra",
                psm=6 if engine == "tesseract" else None, duration_seconds=0, engine=engine,
                engine_metadata={"generation_complete": True}).to_dict()
            runs[engine] = {"id": engine, "engine": engine, "raw": raw, "raw_sha256": v1._digest(raw),
                            "request": {"profile": "traditional", "direction": "horizontal", "dpi": 300}}
        policy = {"traditional": False, "punctuation": True, "preserve_variants": True}
        batch = {"phase": "visual", "snapshot_text": self.original, "snapshot_sha256": v1._text_sha(self.original),
                 "baseline_run_id": baseline, "source_fidelity_contract": module.SOURCE_FIDELITY_CONTRACT,
                 "source_fidelity_options": policy,
                 "runs": [{"run_id": r["id"], "engine": r["engine"], "raw_sha256": r["raw_sha256"]} for r in runs.values()]}
        evidence = module.build_source_fidelity_evidence(batch, runs, policy=policy)
        batch.update(source_fidelity_evidence=evidence, source_fidelity_evidence_sha256=v1._digest(evidence))
        proposal = {"id": "synthetic-proposal", "start": 4, "end": 5, "before": "甲", "after": "乙",
                    "kind": "recognition", "reason": "synthetic image observation", "uncertain": False}
        return batch, runs, policy, proposal

    def test_single_v6_baseline_has_valid_evidence_but_no_automatic_han_change(self):
        batch, runs, policy, proposal = self.fixture()
        current.validate_source_fidelity_evidence(batch, runs, policy=policy)
        evidence = batch["source_fidelity_evidence"]
        self.assertEqual(evidence["baseline_engine"], "ppocrv6")
        self.assertEqual(evidence["runs"][0]["family"], "paddle")
        before = copy.deepcopy((batch, runs))
        verdict = current.assess_source_fidelity(batch, [proposal], runs=runs, policy=policy)["proposals"][0]
        self.assertTrue(verdict["blocked"])
        self.assertEqual(verdict["supporting_families"], [])
        self.assertEqual((batch, runs), before)

    def test_v6_and_v5_server_do_not_become_independent_families(self):
        changed = self.original.replace("甲", "乙")
        batch, runs, policy, proposal = self.fixture(others={"rapidocr": changed, "paddle_server": changed,
                                                           "easyocr": changed})
        verdict = current.assess_source_fidelity(batch, [proposal], runs=runs, policy=policy)["proposals"][0]
        self.assertTrue(verdict["blocked"])
        self.assertEqual(verdict["supporting_families"], ["easyocr"])

    def test_v6_is_one_paddle_witness_for_another_baseline_family(self):
        changed = self.original.replace("甲", "乙")
        batch, runs, policy, proposal = self.fixture(baseline="glmocr",
            others={"ppocrv6": changed, "rapidocr": changed, "surya": changed})
        verdict = current.assess_source_fidelity(batch, [proposal], runs=runs, policy=policy)["proposals"][0]
        self.assertFalse(verdict["blocked"])
        self.assertEqual(verdict["supporting_families"], ["paddle", "surya"])

    def test_existing_v1_through_v4_proofs_remain_byte_equivalent(self):
        original_registry = {key: value for key, value in v1.ENGINE_FAMILIES.items() if key != "ppocrv6"}
        changed = self.original.replace("甲", "乙")
        for module in (v1, v2, v3, current):
            with self.subTest(contract=module.SOURCE_FIDELITY_CONTRACT):
                with patch.dict(v1.ENGINE_FAMILIES, original_registry, clear=True):
                    old_batch, runs, policy, proposal = self.fixture(module, baseline="glmocr",
                        others={"rapidocr": changed, "surya": changed})
                    old_proof = module.assess_source_fidelity(old_batch, [proposal], runs=runs, policy=policy)
                module.validate_source_fidelity_evidence(old_batch, runs, policy=policy)
                rebuilt = module.build_source_fidelity_evidence(old_batch, runs, policy=policy)
                self.assertEqual(v1._digest(rebuilt), old_batch["source_fidelity_evidence_sha256"])
                self.assertEqual(module.assess_source_fidelity(old_batch, [proposal], runs=runs, policy=policy), old_proof)

    def test_changed_v6_family_claim_cannot_authenticate(self):
        batch, runs, policy, _proposal = self.fixture()
        batch["source_fidelity_evidence"]["runs"][0]["family"] = "independent-v6"
        batch["source_fidelity_evidence_sha256"] = v1._digest(batch["source_fidelity_evidence"])
        with self.assertRaises(current.FidelityError):
            current.validate_source_fidelity_evidence(batch, runs, policy=policy)


if __name__ == "__main__":
    unittest.main()
