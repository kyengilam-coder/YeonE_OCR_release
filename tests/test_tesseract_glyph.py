"""Independent TXT alignment, line geometry, abstention and opt-in routing."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from ab_ocr import ocr, tesseract_glyph as tess, glyph_verifier as glyph
from yeone_ocr.comparison import ComparisonProject, ENGINES, run_page, latest_runs

HEADER = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
TSV = HEADER+('4\t1\t1\t1\t1\t0\t10\t10\t80\t30\t-1\t\n'
              '5\t1\t1\t1\t1\t1\t10\t10\t70\t30\t80\t甲\n'
              '5\t1\t1\t1\t1\t2\t30\t10\t60\t30\t90\t乙\n'
              '4\t1\t1\t2\t1\t0\t10\t60\t80\t30\t-1\t\n'
              '5\t1\t1\t2\t1\t1\t10\t60\t80\t30\t70\t丙丁\n')
TEXT = '甲 乙\r\n\r\n丙 丁\n'


class TesseractGlyphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'source.png'
        Image.new('RGB',(100,100),'white').save(self.source)
        self.words = ocr.parse_tsv(TSV,100,100)

    def refine(self, text=TEXT, tsv=TSV, words=None, **kwargs):
        return tess.refine(self.source,text,tsv,self.words if words is None else words,**kwargs)

    def test_missing_fonts_retains_exact_native_text_words_and_tsv_without_image_read(self):
        with patch.object(glyph,'font_sources',return_value=[]):
            text,words,metadata = tess.refine('missing.png',TEXT,TSV,self.words)
        self.assertEqual(text,TEXT); self.assertEqual(words,self.words)
        self.assertEqual(metadata['model_result'],dict(text=TEXT,tsv=TSV,words=self.words))
        self.assertFalse(metadata['glyph_verification_applied'])

    def test_partial_acceptance_preserves_native_paragraphs_and_abstained_words(self):
        def verify(_verifier,_gray,words):
            word=words[0]
            accepted=word['paragraph']==1
            return [{**word,'text':'戊己' if accepted else word['text']}], [
                {'glyph_verification':{'accepted':accepted}}]
        with patch.object(glyph,'font_sources',return_value=[{'path':'unused'}]), \
             patch.object(glyph,'refine_horizontal',side_effect=verify) as verify_mock:
            text,words,metadata=self.refine()
        self.assertEqual(text,'戊己\r\n\r\n丙 丁\n')
        self.assertEqual(words[-1],self.words[-1])
        self.assertIsNone(words[0]['confidence'])
        self.assertEqual(words[0]['text'],'戊己')
        self.assertEqual(metadata['model_result']['words'],self.words)
        self.assertEqual(verify_mock.call_count,2)
        # Both overlapping words are verified once inside the level-4 envelope.
        self.assertEqual(verify_mock.call_args_list[0].args[2][0]['bbox'],[2,2,98,48])
        self.assertEqual(metadata['glyph_verification'][0]['source_indices'],[0,1])

    def test_uncertain_txt_tsv_alignment_abstains_before_matching(self):
        for text,tsv,words in ((TEXT+'額外\n',TSV,self.words),
                               (TEXT.replace('乙','庚'),TSV,self.words),
                               (TEXT,TSV.replace('4\t1','3\t1'),self.words),
                               (TEXT,TSV,self.words+[{**self.words[0],'line':3}])):
            with self.subTest(text=text,tsv=tsv), \
                 patch.object(glyph,'font_sources',return_value=[{'path':'unused'}]), \
                 patch.object(glyph,'GlyphVerifier') as verifier:
                output,regions,metadata=self.refine(text,tsv,words)
                self.assertEqual(output,text);self.assertEqual(regions,words)
                self.assertFalse(metadata['glyph_verification_applied'])
                verifier.assert_not_called()

    def test_invalid_line_geometry_is_not_clipped_into_a_different_line(self):
        for invalid in ('-1\t10\t80\t30','10\t10\t0\t30','10\t10\t180\t30'):
            self.assertEqual(tess.line_regions(TSV.replace('10\t10\t80\t30',invalid),100,100),[])

    def test_vertical_uses_column_verification_and_keeps_native_reading_order(self):
        def verify(_verifier,_gray,words,columns):
            self.assertEqual(columns,[[0]])
            return [{**words[0],'text':'戊己'}],[{'accepted':True}]
        with patch.object(glyph,'font_sources',return_value=[{'path':'unused'}]), \
             patch.object(glyph,'refine_vertical_columns',side_effect=verify), \
             patch.object(glyph,'refine_horizontal') as horizontal:
            text,words,_=self.refine(vertical=True)
        horizontal.assert_not_called()
        self.assertEqual(text,'戊己\r\n\r\n戊己\n')
        self.assertEqual([w['paragraph'] for w in words],[1,2])

    def test_cancel_callback_prevents_verifier_work(self):
        def cancel(): raise ocr.OCRCancelled('cancel')
        with patch.object(glyph,'font_sources',return_value=[{'path':'unused'}]), \
             patch.object(glyph,'refine_horizontal') as verify:
            with self.assertRaises(ocr.OCRCancelled):self.refine(check=cancel)
        verify.assert_not_called()

    def test_ocr_api_native_default_and_opt_in_metadata(self):
        def run(argv,**_kwargs):
            Path(argv[2]+'.txt').write_bytes(TEXT.encode())
            Path(argv[2]+'.tsv').write_bytes(TSV.encode())
        info={'available':True,'languages':['chi_tra_vert'],'version':'test','executable':'test'}
        with patch.object(ocr,'doctor',return_value=info),patch.object(ocr,'_run',side_effect=run), \
             patch.object(ocr,'_engine_metadata',return_value={'oem':1}), \
             patch.object(tess,'refine',return_value=('verified',[],{'recognition_policy':tess.RECOGNITION_POLICY})) as refine:
            native=ocr.recognize_file(self.source,languages='chi_tra_vert',psm=5)
            self.assertEqual(native.text,TEXT);refine.assert_not_called()
            result=ocr.recognize_file(self.source,languages='chi_tra_vert',psm=5,verify_chinese_glyphs=True)
        self.assertEqual(result.text,'verified')
        self.assertTrue(refine.call_args.kwargs['vertical'])
        self.assertEqual(refine.call_args.args[1:4],(TEXT,TSV,self.words))
        self.assertEqual(result.engine_metadata,{'oem':1,'recognition_policy':tess.RECOGNITION_POLICY})
        for language,engine in (('kor+chi_tra','tesseract'),('chi_sim','easyocr')):
            with self.assertRaises(ValueError):
                ocr.recognize_file(self.source,languages=language,engine=engine,verify_chinese_glyphs=True)

    def test_previous_native_tesseract_cache_refreshes_only_tesseract(self):
        project=ComparisonProject.open(self.source)
        def recognize(source,**kwargs):
            return ocr.RecognitionResult('甲',[],str(source),project.source_sha256,1,100,100,
                'test',kwargs['languages'],kwargs['psm'],.1,engine=kwargs['engine'])
        with patch.object(ocr,'recognize_file',side_effect=recognize) as mock:
            run_page(project,1,engines=ENGINES,engine_root=self.root)
            request=next(r for r in project._page(1)['runs'] if r['engine']=='tesseract')['request']
            request.pop('recognition_policy');request.pop('glyph_font_sources')
            old=project.page(1)['runs']
            result=run_page(project,1,engines=ENGINES,engine_root=self.root)
            self.assertEqual(result['completed'],['tesseract'])
            self.assertEqual(result['skipped'],list(ENGINES[1:]))
            self.assertTrue(mock.call_args.kwargs['verify_chinese_glyphs'])
            self.assertEqual(project.page(1)['runs'][:4],old)
            self.assertEqual(run_page(project,1,engines=ENGINES,engine_root=self.root)['skipped'],list(ENGINES))
            run_page(project,1,engines=('tesseract',),profile='mixed',engine_root=self.root)
            self.assertNotIn('verify_chinese_glyphs',mock.call_args.kwargs)
            self.assertNotIn('recognition_policy',latest_runs(project.page(1))['tesseract']['request'])
