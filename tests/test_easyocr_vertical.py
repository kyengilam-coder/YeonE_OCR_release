"""Image-only vertical preprocessing, provenance and isolated worker routing."""
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
from ab_ocr import easyocr_worker as worker


class VerticalGeometryTests(unittest.TestCase):
    def test_whitespace_split_preserves_radicals_thin_glyphs_and_scale(self):
        for scale in (1, 2, 3):
            with self.subTest(scale=scale):
                gray = np.full((160, 40), 255, dtype=np.uint8)
                gray[10:13, 8:32] = 0
                gray[17:24, 8:32] = 0  # separate strokes of the same glyph
                gray[75:77, 5:35] = 0  # a thin glyph must retain vertical padding
                gray[125:145, 10:30] = 0
                gray = np.repeat(np.repeat(gray, scale, axis=0), scale, axis=1)
                before = gray.copy()
                boxes = worker.split_vertical_box(gray, [0, 40*scale, 0, 160*scale])
                self.assertEqual(len(boxes), 3)
                self.assertTrue(all(a[3] <= b[2] for a,b in zip(boxes,boxes[1:])))
                covered = np.zeros_like(gray, dtype=bool)
                for left,right,top,bottom in boxes:
                    covered[top:bottom,left:right] = True
                self.assertTrue(np.all(covered[gray == 0]))
                self.assertGreaterEqual(boxes[1][3]-boxes[1][2], 40*scale)
                np.testing.assert_array_equal(gray, before)

    def test_blank_connected_and_horizontal_regions_stay_intact(self):
        for gray, box in ((np.full((100,20),255,np.uint8),[0,20,0,100]),
                          (np.zeros((100,20),np.uint8),[0,20,0,100]),
                          (np.zeros((20,100),np.uint8),[0,100,0,20])):
            self.assertEqual(worker.split_vertical_box(gray, box), [box])

    def test_reflow_keeps_pixels_upright_and_records_exact_mapping(self):
        gray = np.arange(200, dtype=np.uint8).reshape(20,10)
        before = gray.copy()
        segments = [{"box":[1,7,2,6],"detection":3}, {"box":[2,8,10,17],"detection":5}]
        strip, mapping = worker.reflow_image(gray, segments)
        self.assertEqual(strip.shape, (7,12))
        for record in mapping:
            x0,y0,x1,y1 = record['source_bbox']
            a,b,c,d = record['reflow_bbox']
            np.testing.assert_array_equal(strip[b:d,a:c], gray[y0:y1,x0:x1])
        self.assertEqual([m['source_detection_index'] for m in mapping], [3,5])
        self.assertTrue(np.all(strip[0,:6] == 255))
        np.testing.assert_array_equal(gray, before)

    def test_reflow_limits_and_unseparable_region_do_not_shrink_other_glyphs(self):
        parts = [{"box":[0,20,0,20],"detection":i} for i in range(3)]
        parts.insert(1, {"box":[0,10,0,100],"detection":4})
        with patch.object(worker, 'MAX_REFLOW_WIDTH', 30):
            chunks = list(worker.reflow_chunks(parts))
        self.assertEqual([len(c) for c in chunks], [1,1,1,1])
        self.assertEqual([s for c in chunks for s in c], parts)
        with patch.object(worker, 'MAX_REFLOW_PIXELS', 10), self.assertRaises(ValueError):
            list(worker.reflow_chunks(parts))

    def test_vertical_detect_and_recognize_keep_source_coordinates_and_text(self):
        gray = np.full((120,100),255,np.uint8)
        gray[5:15,53:67] = 0
        gray[45:55,53:67] = 0
        gray[85:95,53:67] = 0
        gray[3:8,2:13] = 0
        reader = Mock()
        reader.detect.return_value = ([[[50,70,0,20],[50,70,35,110]]],
                                      [[[[0,0],[15,1],[16,12],[-2,10]]]])
        texts = iter(['金一\ufe00，', '藥药'])
        def recognize(strip, **kwargs):
            self.assertEqual(kwargs['horizontal_list'], [[0,strip.shape[1],0,strip.shape[0]]])
            return [([[0,0],[1,0],[1,1],[0,1]], next(texts), .8)]
        reader.recognize.side_effect = recognize
        results, metadata = worker.recognize_vertical(reader, gray)
        self.assertEqual([r[1] for r in results], ['金一\ufe00，', '藥药'])
        self.assertEqual(results[0][0][0][0], 50)  # source, not virtual strip coordinate
        self.assertEqual(metadata['recognition_policy'], worker.VERTICAL_RECOGNITION_POLICY)
        info = metadata['vertical_preprocessing']
        self.assertEqual([len(r['segments']) for r in info['regions']], [3,1])
        self.assertEqual(info['unresolved_tall_regions'], 0)
        self.assertEqual(info['detection_polygons'][2][-1], [-2,10])
        for name in ('ycenter_ths','height_ths','width_ths'):
            self.assertEqual(reader.detect.call_args.kwargs[name], 0)
        self.assertEqual(info['regions'][1]['source_bbox'][0], 0)

    def test_empty_page_does_not_fall_back_to_whole_page_recognition(self):
        reader = Mock()
        reader.detect.return_value = ([[]],[[]])
        results, metadata = worker.recognize_vertical(reader, np.full((10,10),255,np.uint8))
        self.assertEqual(results, [])
        self.assertEqual(metadata['vertical_preprocessing']['regions'], [])
        reader.recognize.assert_not_called()

    def test_invalid_detection_coordinates_are_rejected(self):
        for box in ([20,30,0,5], [0,float('nan'),0,5]):
            reader = Mock()
            reader.detect.return_value = ([[box]],[[]])
            with self.assertRaises(ValueError):
                worker.recognize_vertical(reader, np.zeros((10,10),np.uint8))
            reader.recognize.assert_not_called()


class VerticalWorkerTests(unittest.TestCase):
    def test_original_readtext_is_retained_and_vertical_uses_separate_path(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'synthetic.png'
            Image.new('RGB',(50,100),'white').save(source)
            instance = worker.EasyOCRWorker(Path(directory))
            instance.models['chi_tra'] = []
            reader = Mock()
            response = [([[0,0],[30,0],[30,50],[0,50]], '金一\ufe00', .8)]
            reader.readtext.return_value = response
            with patch.object(instance,'reader',return_value=reader), \
                 patch.object(worker,'verify_models',return_value=[{'filename':'zh_sim_g2.pth','sha256':'test'}]), \
                 patch.dict(sys.modules, {'easyocr':SimpleNamespace(__version__='synthetic'),
                                          'torch':SimpleNamespace(__version__='synthetic')}), \
                 patch.object(worker,'recognize_vertical',return_value=(response,{'recognition_policy':worker.VERTICAL_RECOGNITION_POLICY})) as vertical:
                horizontal = instance.recognize(source,'chi_tra')
                vertical.assert_not_called()
                self.assertEqual(horizontal['engine_metadata']['recognition_policy'],worker.HORIZONTAL_RECOGNITION_POLICY)
                self.assertEqual(horizontal['engine_metadata']['horizontal_preprocessing']['raw_engine_result']['text'],horizontal['text'])
                result = instance.recognize(source,'chi_tra','vertical')
                self.assertEqual(result['text'],horizontal['text'])
                self.assertEqual(result['engine_metadata']['recognition_policy'],worker.VERTICAL_RECOGNITION_POLICY)
                self.assertEqual(vertical.call_count,1)
                self.assertTrue(callable(vertical.call_args.kwargs['auxiliary_reader']))
                self.assertEqual(result['engine_metadata']['models'][0]['filename'],'zh_sim_g2.pth')
                reader.readtext.assert_called_once_with(str(source),detail=1,paragraph=False,
                    decoder='greedy',batch_size=1,workers=0,canvas_size=2560,mag_ratio=1.0)
            with patch.object(instance,'reader') as load, self.assertRaises(ValueError):
                instance.recognize(source,'chi_tra','invalid')
            load.assert_not_called()

    def test_korean_horizontal_does_not_initialize_chinese_glyph_support(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.png';Image.new('RGB',(50,100),'white').save(source)
            instance=worker.EasyOCRWorker(Path(directory));instance.models['kor']=[]
            reader=Mock();reader.readtext.return_value=[([[0,0],[30,0],[30,50],[0,50]],'한글',.8)]
            with patch.object(instance,'reader',return_value=reader),patch.object(worker,'_glyph_module') as glyph, \
                 patch.dict(sys.modules,{'easyocr':SimpleNamespace(__version__='test'),'torch':SimpleNamespace(__version__='test')}):
                result=instance.recognize(source,'kor','horizontal')
            glyph.assert_not_called()
            self.assertEqual(result['text'],'한글')
            self.assertNotIn('recognition_policy',result['engine_metadata'])


class AuxiliaryRecognitionTests(unittest.TestCase):
    def setUp(self):
        self.gray = np.arange(400,dtype=np.uint8).reshape(20,20)
        self.parts = [{'box':[0,20,0,20],'detection':0}]

    @staticmethod
    def response(text, confidence):
        return [([[0,0],[20,0],[20,20],[0,20]],text,confidence)]

    def test_two_image_agreement_changes_only_uncertain_glyph_and_keeps_evidence(self):
        primary, auxiliary = Mock(), Mock()
        primary.recognize.return_value = self.response('甲',.5)
        auxiliary.recognize.side_effect = [self.response('乙',.96),self.response('乙',.94)]
        before = self.gray.copy()
        text, evidence = worker.refine_vertical_region(primary,lambda:auxiliary,self.gray,self.parts,'甲')
        self.assertEqual(text,'乙')
        self.assertEqual(evidence['primary_text'],'甲')
        self.assertEqual(evidence['changes'],[{'index':0,'before':'甲','after':'乙'}])
        record = evidence['glyphs'][0]
        self.assertEqual(record['source_bbox'],[0,0,20,20])
        self.assertEqual(record['primary']['text'],'甲')
        self.assertEqual(record['auxiliary_padded']['text'],'乙')
        padded = auxiliary.recognize.call_args_list[1].args[0]
        np.testing.assert_array_equal(padded[2:-2,2:-2],before)
        self.assertTrue(np.all(padded[:2]==255))
        np.testing.assert_array_equal(self.gray,before)

    def test_ambiguous_alignment_high_confidence_and_non_han_never_load_auxiliary(self):
        for sequence, isolated, confidence, parts in (
                ('甲乙','甲',.1,self.parts), ('甲','甲',.8,self.parts),
                ('甲','乙',.1,self.parts), ('甲\ufe00','甲',.1,self.parts),
                ('，','，',.1,self.parts), ('䷀','䷀',.1,self.parts),
                ('甲','甲',.1,[{'box':[0,5,0,20],'detection':0}])):
            with self.subTest(sequence=sequence,isolated=isolated,confidence=confidence,parts=parts):
                primary, factory = Mock(), Mock()
                primary.recognize.return_value = self.response(isolated,confidence)
                text,evidence = worker.refine_vertical_region(primary,factory,self.gray,parts,sequence)
                self.assertEqual(text,sequence)
                self.assertEqual(evidence['changes'],[])
                factory.assert_not_called()

    def test_auxiliary_disagreement_uncertainty_and_multiple_characters_keep_original(self):
        for first,second in ((('乙',.89),('乙',.99)),(('乙',.99),('乙',.89)),
                             (('乙',.99),('丙',.99)),(('乙丙',.99),('乙丙',.99)),
                             (('甲',.99),('甲',.99)),(('A',.99),('A',.99))):
            with self.subTest(first=first,second=second):
                primary, auxiliary = Mock(),Mock()
                primary.recognize.return_value=self.response('甲',.5)
                auxiliary.recognize.side_effect=[self.response(*first),self.response(*second)]
                text,evidence=worker.refine_vertical_region(primary,lambda:auxiliary,self.gray,self.parts,'甲')
                self.assertEqual(text,'甲')
                self.assertEqual(evidence['changes'],[])

    def test_changed_region_confidence_is_null_and_primary_sequence_is_preserved(self):
        primary,auxiliary = Mock(),Mock()
        primary.detect.return_value=([[[0,20,0,20]]],[[]])
        primary.recognize.side_effect=[self.response('甲',.4),self.response('甲',.5)]
        auxiliary.recognize.side_effect=[self.response('乙',.96),self.response('乙',.94)]
        result,metadata=worker.recognize_vertical(primary,self.gray,lambda:auxiliary)
        text,words=worker.convert_results(result,(20,20))
        self.assertEqual(text,'乙')
        self.assertIsNone(words[0]['confidence'])
        detail=metadata['vertical_preprocessing']['regions'][0]['recognition_refinement']
        self.assertEqual(detail['primary_text'],'甲')
        self.assertEqual(detail['primary_confidence'],.4)
        json.dumps(metadata,allow_nan=False)

    def test_invalid_model_score_is_not_accepted(self):
        for confidence in (float('nan'),float('inf'),-1,1.1):
            primary=Mock()
            primary.recognize.return_value=self.response('甲',confidence)
            with self.subTest(confidence=confidence),self.assertRaises(ValueError):
                worker.refine_vertical_region(primary,Mock(),self.gray,self.parts,'甲')


class VerticalEntryPointTests(unittest.TestCase):

    def test_cli_and_batch_forward_direction(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(worker,'EasyOCRWorker') as factory:
            folder = Path(directory)
            factory.return_value.recognize.return_value = {'text':'synthetic'}
            base = ['--model-dir',directory]
            worker.main(base + ['--input',str(folder/'in.png'),'--output',str(folder/'out.json'),
                                '--languages','chi_tra','--direction','vertical'])
            self.assertEqual(factory.return_value.recognize.call_args.args[-1],'vertical')
            jobs = [{'input':str(folder/'in.png'),'output':str(folder/f'{i}.json'),
                     'languages':'chi_tra',**extra} for i,extra in enumerate(({}, {'direction':'horizontal'}))]
            path = folder/'batch.json'
            path.write_text(json.dumps(jobs),encoding='utf-8')
            worker.main(base + ['--batch',str(path),'--direction','vertical'])
            self.assertEqual([c.args[-1] for c in factory.return_value.recognize.call_args_list],
                             ['vertical','vertical','horizontal'])

    def test_standalone_isolated_worker_loads_shipped_geometry_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ('easyocr_worker.py','reading_order.py','glyph_verifier.py'):
                shutil.copyfile(Path(worker.__file__).with_name(name),folder/name)
            script = ("import importlib.util,json,sys; "
                      "s=importlib.util.spec_from_file_location('standalone',sys.argv[1]); "
                      "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                      "assert m._glyph_module().POLICY; "
                      "print(json.dumps(m._column_indices([{'bbox':[0,0,10,20]},{'bbox':[30,0,40,20]}])))")
            result = subprocess.run([sys.executable,'-I','-c',script,str(folder/'easyocr_worker.py')],
                                    check=True,capture_output=True,text=True,timeout=15)
            self.assertEqual(json.loads(result.stdout),[[1],[0]])


if __name__ == '__main__':
    unittest.main()
