"""PP-OCRv5 source evidence, direction routing and conservative column joins."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image
from ab_ocr import rapidocr_worker as worker, glyph_verifier as glyph


def region(text,box):
    return {'text':text,'bbox':box,'confidence':90.,'block':1,'paragraph':1,'line':1}


class ColumnVerificationTests(unittest.TestCase):
    def image(self):
        gray=np.full((140,40),255,np.uint8)
        gray[10:30,8:32]=0;gray[75:95,8:32]=0
        return gray

    def test_overlapping_detections_are_segmented_once_and_all_glyphs_must_verify(self):
        gray=self.image();before=gray.copy()
        words=[region('甲',[0,0,40,90]),region('乙',[0,60,40,140])]
        decisions=[{'status':'checked','candidate':'丙','incumbent':'甲','similarity':.99,'margin':.1,'accepted':True},
                   {'status':'checked','candidate':'乙','incumbent':'乙','similarity':.99,'margin':.1,'accepted':False}]
        verifier=Mock();verifier.match.side_effect=decisions
        result,evidence=glyph.refine_vertical_columns(verifier,gray,words,[[0,1]])
        self.assertEqual([w['text'] for w in result],['丙乙'])
        self.assertIsNone(result[0]['confidence'])
        self.assertEqual(len(evidence[0]['segments']),2)
        self.assertEqual(evidence[0]['input_text'],'甲\n乙')
        self.assertEqual(evidence[0]['glyph_refinement']['input_text'],'甲乙')
        self.assertEqual(evidence[0]['indices'],[0,1])
        np.testing.assert_array_equal(gray,before)

    def test_one_uncertain_unchanged_glyph_prevents_all_column_changes_and_joins(self):
        words=[region('甲',[0,0,40,90]),region('乙',[0,60,40,140])]
        verifier=Mock();verifier.match.side_effect=[
            {'status':'checked','candidate':'丙','incumbent':'甲','similarity':.99,'margin':.1,'accepted':True},
            {'status':'checked','candidate':'乙','incumbent':'乙','similarity':.97,'margin':.1,'accepted':False}]
        result,evidence=glyph.refine_vertical_columns(verifier,self.image(),words,[[0,1]])
        self.assertEqual(result,words)
        self.assertFalse(evidence[0]['accepted'])

    def test_large_paragraph_gap_is_not_merged(self):
        gray=np.full((300,40),255,np.uint8)
        words=[region('甲',[0,0,40,40]),region('乙',[0,250,40,290])]
        verifier=Mock();verifier.match.side_effect=[
            {'status':'checked','candidate':c,'incumbent':c,'similarity':1.,'margin':.1,'accepted':False}
            for c in ('甲','乙')]
        result,evidence=glyph.refine_vertical_columns(verifier,gray,words,[[0,1]])
        self.assertEqual([w['text'] for w in result],['甲','乙'])
        self.assertEqual([r['indices'] for r in evidence],[[0],[1]])


class WorkerTests(unittest.TestCase):
    def test_native_model_result_is_kept_and_korean_does_not_use_glyph_helpers(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.png';Image.new('RGB',(100,100),'white').save(source)
            engine=Mock(return_value=SimpleNamespace(boxes=[[[0,0],[80,0],[80,30],[0,30]]],
                txts=['甲乙'],scores=[.9],elapse_list=[.1]))
            instance=worker.Worker(Path(directory))
            with patch.object(instance,'engine',return_value=(engine,{})), \
                 patch.object(glyph,'font_sources',return_value=[]),patch.object(worker.importlib.metadata,'version',return_value='test'):
                result=instance.recognize(source,'chi_tra','horizontal')
            self.assertEqual(result['engine_metadata']['model_result']['text'],'甲乙')
            self.assertEqual(result['engine_metadata']['model_result']['words'][0]['confidence'],90.)
            self.assertEqual(result['engine_metadata']['recognition_policy'],worker.RECOGNITION_POLICY)
            json.dumps(result,allow_nan=False)
            with patch.object(instance,'engine',return_value=(engine,{})),patch.object(worker,'_helper_module') as helper, \
                 patch.object(worker.importlib.metadata,'version',return_value='test'):
                korean=instance.recognize(source,'kor','horizontal')
            helper.assert_not_called()
            self.assertNotIn('recognition_policy',korean['engine_metadata'])
            with patch.object(instance,'engine') as load,self.assertRaises(ValueError):
                instance.recognize(source,'chi_tra','bad')
            load.assert_not_called()

    def test_batch_direction_is_forwarded_and_per_item_can_override(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);jobs=folder/'jobs.json'
            jobs.write_text(json.dumps({'items':[{'input':'in.png','output':str(folder/'a.json')},
                {'input':'in.png','output':str(folder/'b.json'),'direction':'horizontal'}]}),encoding='utf-8')
            with patch.object(sys,'argv',['worker','--model-dir',directory,'--batch',str(jobs),'--direction','vertical']), \
                 patch.object(worker,'block_network'),patch.object(worker,'atomic_output'),patch('builtins.print'), \
                 patch.object(worker,'Worker') as factory:
                factory.return_value.recognize.return_value={'duration_seconds':.1}
                self.assertEqual(worker.main(),0)
            self.assertEqual([c.args[-1] for c in factory.return_value.recognize.call_args_list],['vertical','horizontal'])

    def test_isolated_worker_loads_only_shipped_helpers(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            for name in ('rapidocr_worker.py','glyph_verifier.py','reading_order.py'):
                shutil.copyfile(Path(worker.__file__).with_name(name),folder/name)
            code=("import importlib.util,sys; s=importlib.util.spec_from_file_location('w',sys.argv[1]); "
                  "m=importlib.util.module_from_spec(s);s.loader.exec_module(m); "
                  "assert m._helper_module('glyph_verifier').POLICY; "
                  "assert m._helper_module('reading_order').VERTICAL_RTL_POLICY")
            subprocess.run([sys.executable,'-I','-c',code,str(folder/'rapidocr_worker.py')],check=True,timeout=15)


if __name__=='__main__':unittest.main()
