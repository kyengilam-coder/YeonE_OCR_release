"""Native recrop selection needs consensus and full-line visual evidence."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image
from ab_ocr import ppocrv6_worker as worker, glyph_verifier as glyph, ocr
from yeone_ocr.comparison import ComparisonProject, ENGINES, latest_runs, run_page
from yeone_ocr import comparison


class V6HorizontalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'test.png'
        Image.new('RGB',(160,80),(30,90,150)).save(self.source)
        self.instance=worker.Worker(self.root)
        self.instance.glyph_verifier=Mock(sources=[{'path':'not-read','sha256':'test'}])
        self.quad=[[10,10],[130,10],[130,40],[10,40]]
        self.word={'text':'甲乙','bbox':[10,10,130,40],'confidence':99.,'block':1,'paragraph':1,'line':1}

    def refine(self, candidates=('甲丙','甲丙'), verified='甲丙', accepted=True):
        self.engine=Mock()
        self.engine.recognize_txt.return_value=SimpleNamespace(txts=candidates,scores=(.97,.99))
        with patch.object(worker._shared,'_helper_module',return_value=glyph), \
             patch.object(glyph,'refine_horizontal',return_value=([dict(self.word,text=verified)],
                [{'glyph_verification':{'accepted':accepted}}])) as verify:
            result=self.instance.refine_horizontal(self.engine,self.source,[self.quad],[self.word])
        return result,verify

    def test_native_candidate_consensus_and_visual_proof_choose_only_that_candidate(self):
        (words,records,sources),verify=self.refine()
        self.assertEqual(words,[dict(self.word,text='甲丙',confidence=None)])
        self.assertEqual(self.word['text'],'甲乙')
        self.assertTrue(records[0]['accepted']);self.assertEqual(records[0]['retry_scores'],[.97,.99])
        crop,padded=self.engine.recognize_txt.call_args.args[0]
        self.assertEqual(crop[0,0].tolist(),[150,90,30])
        self.assertTrue(np.all(padded[:6]==255))
        self.assertEqual(records[0]['source_crop_sha256'],hashlib.sha256(crop.tobytes()).hexdigest())
        self.assertEqual(records[0]['padded_crop_sha256'],hashlib.sha256(padded.tobytes()).hexdigest())
        self.assertEqual(verify.call_count,1)

    def test_disagreement_length_change_empty_or_unchanged_reading_never_uses_visual_stage(self):
        for candidates in (('甲丙','甲丁'),('甲','甲'),('',''),(),('甲乙','甲乙')):
            with self.subTest(candidates=candidates):
                (words,records,_),verify=self.refine(candidates)
                self.assertEqual(words,[self.word]);self.assertFalse(records[0]['accepted'])
                verify.assert_not_called()

    def test_visual_candidate_is_never_used_to_replace_a_different_native_candidate(self):
        for candidate,accepted in (('甲丁',True),('甲丙',False)):
            with self.subTest(candidate=candidate,accepted=accepted):
                (words,records,_),_=self.refine(verified=candidate,accepted=accepted)
                self.assertEqual(words,[self.word]);self.assertFalse(records[0]['accepted'])

    def test_han_consensus_can_keep_native_punctuation_only_with_complete_line_proof(self):
        self.word['text']='為為、為為；甲乙。'
        candidate='爲為、為爲；甲乙。'
        engine=Mock()
        engine.recognize_txt.side_effect=[
            SimpleNamespace(txts=(self.word['text'],'爲為為爲；甲乙。'),scores=(.99,.98)),
            SimpleNamespace(txts=('爲為、為爲；甲乙。',),scores=(.97,))]
        with patch.object(worker._shared,'_helper_module',return_value=glyph), \
             patch.object(glyph,'refine_horizontal',return_value=([dict(self.word,text=candidate)],
                [{'glyph_verification':{'accepted':True}}])) as verify:
            words,records,_=self.instance.refine_horizontal(engine,self.source,[self.quad],[self.word])
        self.assertEqual(words,[dict(self.word,text=candidate,confidence=None)])
        self.assertEqual(records[0]['input_text'],self.word['text'])
        self.assertEqual(records[0]['candidate_text'],candidate)
        self.assertEqual(verify.call_args.args[2][0]['text'],candidate)
        self.assertEqual(engine.recognize_txt.call_count,2)
        extra=engine.recognize_txt.call_args.args[0][0]
        self.assertEqual(records[0]['punctuation_retry']['crop_sha256'],hashlib.sha256(extra.tobytes()).hexdigest())

    def test_punctuation_consensus_refuses_missing_han_non_han_scripts_and_new_punctuation(self):
        native='為為、甲乙；丙。'
        for alternatives in ([],['爲為甲乙；丙。'],['爲為甲乙；丙。','爲為甲丁；丙。'],
                             ['爲為甲；丙。']*2,['爲為。甲乙；丙。']*2,
                             ['爲為：甲乙；丙。']*2,['爲為 甲乙；丙。']*2,
                             ['爲為A甲乙；丙。']*2,['爲為1甲乙；丙。']*2,
                             ['爲為\ufe00甲乙；丙。']*2,[native]*2):
            with self.subTest(alternatives=alternatives):
                self.assertIsNone(worker.punctuation_consensus_candidate(native,alternatives))
        self.assertEqual(worker.punctuation_consensus_candidate(native,['爲為甲乙；丙。']*2),'爲為、甲乙；丙。')

    def test_han_consensus_still_abstains_on_unverified_full_line_or_different_visual_reading(self):
        self.word['text']='為為、甲乙。'
        for visual,accepted in (('爲為、甲乙。',False),('爲爲、甲乙。',True)):
            with self.subTest(visual=visual,accepted=accepted):
                engine=Mock();engine.recognize_txt.side_effect=[
                    SimpleNamespace(txts=(self.word['text'],'爲為甲乙。'),scores=(.99,.98)),
                    SimpleNamespace(txts=('爲為甲乙。',),scores=(.97,))]
                with patch.object(worker._shared,'_helper_module',return_value=glyph), \
                     patch.object(glyph,'refine_horizontal',return_value=([dict(self.word,text=visual)],
                        [{'glyph_verification':{'accepted':accepted}}])):
                    words,records,_=self.instance.refine_horizontal(engine,self.source,[self.quad],[self.word])
                self.assertEqual(words,[self.word]);self.assertFalse(records[0]['accepted'])

    def test_unavailable_fonts_or_unsafe_geometry_preserves_native_without_retry(self):
        self.instance.glyph_verifier=Mock(sources=[])
        with patch.object(worker._shared,'_helper_module',return_value=glyph):
            engine=Mock()
            words,records,_=self.instance.refine_horizontal(engine,self.source,[self.quad],[self.word])
            self.assertEqual(words,[self.word]);self.assertEqual(records,[])
            engine.recognize_txt.assert_not_called()
            self.instance.glyph_verifier=Mock(sources=[{}])
            for quad,box in (([[10,10],[130,20],[130,40],[10,40]],[10,10,130,40]),
                             (self.quad,[-1,10,130,40]),(self.quad,[10,10,20,70])):
                words,records,_=self.instance.refine_horizontal(engine,self.source,[quad],[dict(self.word,bbox=box)])
                self.assertFalse(records[0]['accepted']);engine.recognize_txt.assert_not_called()
            self.assertEqual(self.instance.refine_horizontal(engine,self.source,None,[])[0],[])

    def test_only_traditional_horizontal_enters_recrop_and_native_scores_remain_recorded(self):
        for language,direction,enabled in (('chi_sim','horizontal',False),('chi_tra','vertical',False),
                                          ('chi_tra','horizontal',True),('chi_tra+eng','horizontal',True)):
            with self.subTest(language=language,direction=direction):
                engine=Mock(return_value=SimpleNamespace(boxes=[self.quad],txts=['甲乙'],scores=[.99],elapse_list=[.1,None,.2]))
                with patch.object(self.instance,'engine',return_value=(engine,{})), \
                     patch.object(self.instance,'refine_horizontal',return_value=([dict(self.word,text='甲丙',confidence=None)],[],[])) as refine, \
                     patch.object(worker.importlib.metadata,'version',return_value='test'):
                    result=self.instance.recognize(self.source,language,direction)
                self.assertEqual(refine.call_count,int(enabled))
                if enabled:
                    native=result['engine_metadata']['model_result']
                    self.assertEqual(native['text'],'甲乙');self.assertEqual(native['words'][0]['confidence'],99.)
                    self.assertEqual(result['text'],'甲丙');self.assertIsNone(result['words'][0]['confidence'])
                else:self.assertNotIn('model_result',result['engine_metadata'])
                json.dumps(result,allow_nan=False)
        with self.assertRaises(ValueError):self.instance.recognize(self.source,'chi_tra','invalid')

    def test_v1_v6_cache_refreshes_only_traditional_horizontal_and_keeps_history(self):
        for profile,direction,enabled in (('traditional','horizontal',True),('simplified','horizontal',False),
                                           ('traditional','vertical',False)):
            with self.subTest(profile=profile,direction=direction):
                project=ComparisonProject.open(self.source)
                def recognize(source,**kwargs):
                    return ocr.RecognitionResult('甲',[],str(source),project.source_sha256,1,160,80,
                        'test',kwargs['languages'],kwargs['psm'],.1,engine=kwargs['engine'])
                with patch.object(ocr,'recognize_file',side_effect=recognize):
                    run_page(project,1,engines=ENGINES,profile=profile,direction=direction,engine_root=self.root)
                    legacy=next(r for r in project._page(1)['runs'] if r['engine']=='ppocrv6')
                    if enabled:
                        legacy['request']['recognition_policy']='ppocrv6-traditional-original-crop-verified-v1'
                    else:
                        legacy['request'].pop('recognition_policy',None);legacy['request'].pop('glyph_font_sources',None)
                    original=project.page(1)['runs']
                    outcome=run_page(project,1,engines=ENGINES,profile=profile,direction=direction,engine_root=self.root)
                    self.assertEqual(outcome['completed'],['ppocrv6'] if enabled else [])
                    self.assertEqual(project.page(1)['runs'][:4],original)
                    self.assertEqual(run_page(project,1,engines=ENGINES,profile=profile,direction=direction,engine_root=self.root)['skipped'],list(ENGINES))
                    request=latest_runs(project.page(1))['ppocrv6']['request']
                    self.assertEqual('glyph_font_sources' in request,enabled)

    def test_project_reader_uses_small_reads_and_still_enforces_byte_limit_and_digest(self):
        payload=json.dumps({'format':comparison.FORMAT,'version':comparison.VERSION}).encode()+b' '*1_100_000
        for limit,valid in ((len(payload),True),(len(payload)-1,False),(comparison.MAX_BYTES,True)):
            with self.subTest(limit=limit):
                sizes=[]
                class Guarded(io.BytesIO):
                    def read(self,size=-1):
                        sizes.append(size)
                        if not 0<size<=1_048_576:
                            raise AssertionError(size)
                        return super().read(size)
                path=Mock();path.open.return_value=Guarded(payload)
                with patch.object(comparison,'MAX_BYTES',limit):
                    if valid:
                        value,digest=comparison._read(path)
                        self.assertEqual(value['format'],comparison.FORMAT)
                        self.assertEqual(digest,hashlib.sha256(payload).hexdigest())
                    else:
                        with self.assertRaises(comparison.ComparisonError):comparison._read(path)
                self.assertGreater(len(sizes),1)
