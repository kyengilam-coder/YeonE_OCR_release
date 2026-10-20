"""Image-only additional vetoes and retained valid edits across all stages."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from ab_ocr.ocr import RecognitionResult
from yeone_ocr import comparison as core
from yeone_ocr import comparison_image_safety as safety


class ImageSafetyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="image-safety-")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "image.png"
        Image.new("RGB", (320, 100), "white").save(self.path)
        self.project = core.ComparisonProject.open(self.path)
        self.left, self.right = "甲乙丙丁", "己庚辛壬"

    def add(self, engine, middle, *, complete=True, active=True):
        text = self.left + middle + self.right
        raw = RecognitionResult(text=text, words=[] if engine == "glmocr" else [
            {"text": text, "bbox": [5, 5, 300, 90], "confidence": None}],
            source_path=str(self.path), source_sha256=self.project.source_sha256,
            page_number=1, image_width=320, image_height=100, engine=engine,
            engine_version="synthetic", languages="chi_tra", psm=6 if engine=="tesseract" else None,
            duration_seconds=0, engine_metadata={"generation_complete": complete}).to_dict()
        self.project.add_result(raw,request={"profile":"traditional","direction":"horizontal",
            "dpi":300,"engine_root":None,"policy":"local-three-engine-v1"})
        self.project._page(1)["runs"][-1]["active"] = active

    def assess(self, before, after, *, stage="local", kind=None, allow_explicit_traditional=False):
        self.project.choose_baseline(1,"glmocr")
        page=self.project.page(1)
        baseline=self.project._baseline(page)
        runs={run['id']:run for run in page['runs']}
        p=dict(id="synthetic",start=4,end=4+len(before),before=before,after=after)
        if kind is not None:
            p['kind']=kind
        return safety.assess(baseline['raw']['text'],baseline,runs,p,stage=stage,
                             allow_explicit_traditional=allow_explicit_traditional)

    def test_two_original_families_block_kind_change_in_all_stages(self):
        self.add("glmocr","·"); self.add("easyocr",":")
        self.add("rapidocr","·"); self.add("surya","·")
        before=copy.deepcopy(self.project.page(1))
        for stage in ("local","region","roi","ai"):
            verdict=self.assess("·",":",stage=stage)
            self.assertEqual(verdict['reason_codes'],['independent_original_punctuation'])
            self.assertFalse(verdict['new_approval'])
        self.assertEqual(before['runs'],self.project.page(1)['runs'])
        self.assertEqual(self.project.page(1)['edits'],[])

    def test_one_original_family_is_insufficient_even_with_two_paddle_engines(self):
        for engine,middle in [('glmocr','·'),('easyocr',':'),('rapidocr','·'),('paddle_server','·')]:
            self.add(engine,middle)
        self.assertFalse(self.assess('·',':')['blocked'])

    def test_baseline_family_does_not_supply_an_independent_vote(self):
        self.add('glmocr','·'); self.add('surya','·'); self.add('easyocr',':')
        self.assertFalse(self.assess('·',':')['blocked'])

    def test_width_only_change_keeps_existing_policy(self):
        self.add('glmocr','：');self.add('rapidocr','：');self.add('surya','：');self.add('easyocr',':')
        self.assertFalse(self.assess('：',':')['blocked'])

    def test_missing_or_incomplete_readings_cannot_vote(self):
        self.add('glmocr','·');self.add('easyocr',':');self.add('rapidocr','·')
        self.add('surya','·',complete=False)
        self.assertFalse(self.assess('·',':')['blocked'])
        self.add('surya','·',active=False)
        self.assertFalse(self.assess('·',':')['blocked'])

    def test_deletion_requires_exact_whole_gap_agreement(self):
        self.add('glmocr','·');self.add('easyocr','');self.add('rapidocr','·');self.add('surya','··')
        self.assertFalse(self.assess('·','')['blocked'])
        self.add('surya','·')
        self.assertTrue(self.assess('·','')['blocked'])

    def test_unchanged_context_can_wrap_unique_minimal_punctuation(self):
        self.add('glmocr','·');self.add('easyocr',':');self.add('rapidocr','·');self.add('surya','·')
        self.project.choose_baseline(1,'glmocr');page=self.project.page(1);base=self.project._baseline(page)
        p=dict(id='wrapped',start=3,end=6,before='丁·己',after='丁:己')
        v=safety.assess(base['raw']['text'],base,core.usable_runs(page),p,stage='roi')
        self.assertTrue(v['blocked']);self.assertEqual(v['source_evidence']['punctuation_target']['start'],4)

    def test_source_variants_are_protected_in_every_stage(self):
        self.add('glmocr','卽');self.add('surya','即');self.add('rapidocr','即')
        for stage in ('local','region','roi','ai'):
            self.assertIn('protected_source_variant',self.assess('卽','即',stage=stage)['reason_codes'])

    def test_nonvariant_han_does_not_get_new_approval_or_extra_threshold(self):
        self.add('glmocr','錯');self.add('surya','戊');self.add('rapidocr','戊')
        v=self.assess('錯','戊',stage='roi')
        self.assertFalse(v['blocked']);self.assertFalse(v['new_approval'])

    def test_explicit_traditional_AI_edit_skips_only_the_additional_form_veto(self):
        self.add('glmocr','汉');self.add('surya','漢');self.add('rapidocr','漢')
        strict=self.assess('汉','漢',stage='ai',kind='traditional')
        self.assertIn('protected_source_variant',strict['reason_codes'])
        permitted=self.assess('汉','漢',stage='ai',kind='traditional',allow_explicit_traditional=True)
        self.assertFalse(permitted['blocked']);self.assertFalse(permitted['new_approval'])
        self.assertTrue(permitted['source_evidence']['explicit_traditional_editorial'])
        self.assertFalse(permitted['source_evidence']['form_related_veto_applies'])
        self.assertEqual(permitted['source_evidence']['variant_pairs'],strict['source_evidence']['variant_pairs'])
        self.assertNotEqual(permitted['verdict_sha256'],strict['verdict_sha256'])

    def test_editorial_flag_is_not_accepted_for_recognition_or_local_region(self):
        self.add('glmocr','汉');self.add('surya','漢')
        for stage,kind in [('ai','recognition'),('local','traditional'),('region','traditional')]:
            with self.subTest(stage=stage,kind=kind),self.assertRaises(core.ComparisonError):
                self.assess('汉','漢',stage=stage,kind=kind,allow_explicit_traditional=True)
        with self.assertRaises(core.ComparisonError):
            self.assess('汉','漢',stage='ai',kind='traditional',allow_explicit_traditional=1)
        self.assertTrue(self.assess('汉','漢',stage='ai',kind='recognition')['blocked'])

    def test_explicit_traditional_flag_preserves_compatibility_and_selector_identity(self):
        for before,after in [('神','神'),('错\ufe00','錯\ufe00')]:
            with self.subTest(before=before):
                self.add('glmocr',before);self.add('surya',after)
                verdict=self.assess(before,after,stage='ai',kind='traditional',allow_explicit_traditional=True)
                self.assertIn('protected_unicode_identity',verdict['reason_codes'])

    def test_explicit_traditional_flag_does_not_bypass_missing_dictionary(self):
        self.add('glmocr','汉');self.add('surya','漢')
        with patch.object(safety.identity,'dictionary_manifest',return_value={'available':False}):
            verdict=self.assess('汉','漢',stage='ai',kind='traditional',allow_explicit_traditional=True)
        self.assertIn('variant_dictionary_unavailable',verdict['reason_codes'])

    def test_missing_dictionary_fails_closed_for_han_only(self):
        self.add('glmocr','錯');self.add('surya','戊')
        with patch.object(safety.identity,'dictionary_manifest',return_value={'available':False}):
            self.assertIn('variant_dictionary_unavailable',self.assess('錯','戊')['reason_codes'])

    def test_selector_base_identity_cannot_be_changed(self):
        self.add('glmocr','錯\ufe00');self.add('surya','戊\ufe00')
        self.assertIn('protected_unicode_identity',self.assess('錯','戊')['reason_codes'])

    def test_identity_and_position_tampering_is_rejected(self):
        self.add('glmocr','·');self.add('surya','·');self.add('rapidocr','·')
        self.project.choose_baseline(1,'glmocr');page=self.project.page(1);base=self.project._baseline(page)
        runs=core.usable_runs(page);p=dict(id='tamper',start=4,end=5,before='·',after=':')
        for bad in ({**p,'start':False},{**p,'before':'。'},{**p,'snapshot_sha256':'0'*64}):
            with self.assertRaises(core.ComparisonError): safety.assess(base['raw']['text'],base,runs,bad,stage='local')
        altered=copy.deepcopy(runs);altered['surya']['raw']['text']='tampered'
        with self.assertRaises(core.ComparisonError):safety.assess(base['raw']['text'],base,altered,p,stage='local')

    def test_deterministic_replay_and_no_input_mutation(self):
        self.add('glmocr','·');self.add('surya','·');self.add('rapidocr','·')
        first=self.assess('·',':');second=self.assess('·',':')
        self.assertEqual(first,second)
        self.assertEqual(first['verdict_sha256'],core._digest({k:v for k,v in first.items() if k!='verdict_sha256'}))


if __name__=='__main__':unittest.main()
