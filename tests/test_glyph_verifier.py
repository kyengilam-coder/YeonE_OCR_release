"""Visual evidence gates, ambiguous variants and font provenance."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import ImageFont
from ab_ocr import glyph_verifier as glyph


class GlyphDecisionTests(unittest.TestCase):
    def matcher(self, scores):
        verifier=glyph.GlyphVerifier([{'path':'not opened','sha256':'test','face_index':0}])
        verifier.labels=['甲','乙','丙']
        verifier.matrix=np.ones((3,1024),np.float32)
        verifier._native_score=lambda _target,c,_font:scores[c]
        return verifier

    def test_requires_high_absolute_similarity_and_distinct_character_margin(self):
        crop=np.full((30,30),255,np.uint8);crop[5:25,8:20]=0
        for scores,accepted in (({'甲':.8,'乙':.99,'丙':.9},True),
                                ({'甲':.8,'乙':.97,'丙':.9},False),
                                ({'甲':.8,'乙':.99,'丙':.98},False),
                                ({'甲':.99,'乙':.8,'丙':.7},False),
                                ({'甲':.99,'乙':.99,'丙':.7},False)):
            with self.subTest(scores=scores):
                result=self.matcher(scores).match(crop,'甲')
                self.assertEqual(result['accepted'],accepted)
                self.assertEqual(result['source_pixels_sha256'],hashlib.sha256(crop.tobytes()).hexdigest())
                json.dumps(result,allow_nan=False)

    def test_blank_dark_oversized_and_missing_fonts_are_rejected_without_build(self):
        for crop in (np.full((20,20),255,np.uint8),np.zeros((20,20),np.uint8),
                     np.zeros((600,600),np.uint8)):
            verifier=self.matcher({'甲':0,'乙':1,'丙':0})
            self.assertFalse(verifier.match(crop,'甲')['accepted'])
        self.assertFalse(glyph.GlyphVerifier([]).match(np.zeros((10,10),np.uint8),'甲')['accepted'])

    def test_unicode_alignment_and_unseparated_regions_are_not_guessed(self):
        verifier=Mock()
        gray=np.zeros((30,30),np.uint8)
        for text,parts in (('甲乙',[{'box':[0,20,0,20]}]),
                           ('甲\ufe00',[{'box':[0,20,0,20]}]),
                           ('，',[{'box':[0,20,0,20]}]),
                           ('甲',[{'box':[0,5,0,30]}])):
            output,evidence=glyph.refine_region(verifier,gray,parts,text)
            self.assertEqual(output,text)
            self.assertEqual(evidence['changes'],[])
        verifier.match.assert_not_called()

    def test_refinement_preserves_input_text_coordinates_and_pixels(self):
        gray=np.arange(900,dtype=np.uint8).reshape(30,30);before=gray.copy()
        verifier=Mock()
        verifier.match.return_value={'accepted':True,'candidate':'乙'}
        text,evidence=glyph.refine_region(verifier,gray,[{'box':[2,22,3,23]}],'甲')
        self.assertEqual(text,'乙')
        self.assertEqual(evidence['input_text'],'甲')
        self.assertEqual(evidence['glyphs'][0]['source_bbox'],[2,3,22,23])
        np.testing.assert_array_equal(gray,before)
        np.testing.assert_array_equal(verifier.match.call_args.args[0],gray[3:23,2:22])


class InstalledFontTests(unittest.TestCase):
    def test_font_mutation_is_detected_before_templates_are_used(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'font.ttf';path.write_bytes(b'changed')
            verifier=glyph.GlyphVerifier([{'path':str(path),'sha256':'0'*64,'face_index':0}])
            with self.assertRaisesRegex(ValueError,'changed'):
                verifier._build()

    @unittest.skipUnless(Path('C:/Windows/Fonts/simsun.ttc').is_file(),'local CJK font required')
    def test_native_rasterization_distinguishes_close_characters_and_variant_codepoints(self):
        path=Path('C:/Windows/Fonts/simsun.ttc')
        source={'path':str(path),'face_index':0,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        verifier=glyph.GlyphVerifier([source])
        for char,other in (('已','己'),('朮','尤'),('爲','為'),('甲','由'),('士','土')):
            font=ImageFont.truetype(str(path),43)
            mask=font.getmask(char,mode='L')
            ink=np.asarray(mask,dtype=np.uint8).reshape(mask.size[1],mask.size[0])
            target=glyph._trim(ink)
            exact=verifier._native_score(target,char,0)
            different=verifier._native_score(target,other,0)
            with self.subTest(char=char):
                self.assertGreaterEqual(exact,.99)
                self.assertGreater(exact-different,.03)
        # Build from font coverage, not a correction dictionary; duplicate
        # missing-glyph boxes must not become candidates.
        with patch.object(glyph,'CJK_RANGES',((0x4e00,0x4e05),)):
            verifier._build()
        self.assertEqual(verifier.labels,[chr(n) for n in range(0x4e00,0x4e05)])


if __name__=='__main__':
    unittest.main()
