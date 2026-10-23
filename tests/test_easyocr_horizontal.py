"""Horizontal source geometry, abstention and literal punctuation evidence."""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from ab_ocr import glyph_verifier as glyph


def word(text, box, confidence=85):
    return {'text':text,'bbox':box,'confidence':confidence,'block':1,'paragraph':1,'line':1}


class HorizontalLayoutTests(unittest.TestCase):
    def test_only_nearby_fragments_of_one_row_are_joined(self):
        words=[word('甲',[0,0,30,20]),word('乙',[40,1,70,21]),
               word('丙',[150,0,180,20]),word('丁',[0,50,30,70])]
        groups=glyph.horizontal_groups(words)
        self.assertEqual([g['indices'] for g in groups],[[0,1],[2],[3]])
        self.assertEqual(groups[0]['source_bbox'],[0,0,70,21])
        overlapping=[word('甲',[0,0,30,20]),word('乙',[25,0,50,20])]
        self.assertEqual(len(glyph.horizontal_groups(overlapping)),2)

    def test_verified_group_keeps_source_mapping_and_abstention_keeps_original_regions(self):
        gray=np.arange(10000,dtype=np.uint8).reshape(100,100);before=gray.copy()
        words=[word('甲:',[2,3,32,23]),word('乙',[40,4,70,24]),word('丙',[2,50,32,70])]
        with patch.object(glyph,'_horizontal_line',side_effect=[('甲：乙',{'accepted':True}),
                (None,{'accepted':False,'status':'ambiguous cell'})]) as parse:
            result,evidence=glyph.refine_horizontal(Mock(),gray,words)
        self.assertEqual([w['text'] for w in result],['甲：乙','丙'])
        self.assertEqual(result[0]['bbox'],[2,3,70,24])
        self.assertIsNone(result[0]['confidence'])
        self.assertEqual(result[1],words[2])
        self.assertEqual(evidence[0]['indices'],[0,1])
        self.assertEqual(evidence[0]['input_text'],'甲:\n乙')
        np.testing.assert_array_equal(parse.call_args_list[0].args[1],gray[3:24,2:70])
        np.testing.assert_array_equal(gray,before)

    def test_missing_fonts_unsupported_anchor_and_bad_geometry_never_build_a_bank(self):
        crop=np.full((30,150),255,np.uint8);crop[5:25,5:20]=0
        verifier=glyph.GlyphVerifier([])
        self.assertIsNone(glyph._horizontal_line(verifier,crop,'甲')[0])
        verifier=glyph.GlyphVerifier([{'path':'not opened','face_index':0,'sha256':'test'}])
        with patch.object(verifier,'_build') as build:
            for image,anchor in ((crop,'A'),(crop,'，'),(np.zeros((600,150),np.uint8),'甲')):
                self.assertIsNone(glyph._horizontal_line(verifier,image,anchor)[0])
        build.assert_not_called()


@unittest.skipUnless(Path('C:/Windows/Fonts/simsun.ttc').is_file(),'local CJK font required')
class NativeHorizontalTests(unittest.TestCase):
    def setUp(self):
        self.path=Path('C:/Windows/Fonts/simsun.ttc')
        self.verifier=glyph.GlyphVerifier([{'path':str(self.path),'face_index':0,
            'sha256':hashlib.sha256(self.path.read_bytes()).hexdigest()}])
        # Small controlled candidate bank for behavior tests; production builds
        # the full CJK ranges. Include near forms as competing candidates.
        chars=list('中文土士己已巳空格')
        self.verifier.labels=chars
        self.verifier.matrix=np.array([glyph._vector(self.verifier._mask(0,c,64)) for c in chars])

    def render(self,text):
        image=Image.new('L',(500,70),255)
        ImageDraw.Draw(image).text((8,8),text,font=ImageFont.truetype(str(self.path),36),fill=0)
        return np.array(image)

    def test_full_width_punctuation_and_ascii_advances_are_recognized_literally(self):
        for reference in ('中文：A9，土士。','中文:A9,土士.'):
            with self.subTest(reference=reference):
                crop=self.render(reference)
                text,proof=glyph._horizontal_line(self.verifier,crop,'中')
                self.assertEqual(text,reference)
                self.assertTrue(proof['accepted'])
                self.assertGreaterEqual(proof['line_similarity'],.999)
                self.assertEqual(proof['source_pixels_sha256'],proof['rendered_pixels_sha256'])
                self.assertTrue(all(g['margin']>=glyph.MIN_MARGIN for g in proof['glyphs']))
                json.dumps(proof,allow_nan=False)

    def test_real_spaces_are_not_deleted_to_make_a_line_fit(self):
        text='中文 空格。';crop=self.render(text)
        result,proof=glyph._horizontal_line(self.verifier,crop,'中')
        self.assertIsNone(result)
        self.assertFalse(proof['accepted'])

    def test_leading_ink_cannot_be_discarded_by_a_later_anchor(self):
        crop=self.render('中文。');crop[2:4,1:3]=0
        anchor={'similarity':1.,'font_index':0,'size':36,'x':8,'y':8}
        with patch.object(glyph,'_horizontal_anchor',return_value=anchor):
            result,proof=glyph._horizontal_line(self.verifier,crop,'中')
        self.assertIsNone(result)
        self.assertEqual(proof['status'],'anchor would omit leading ink')


if __name__=='__main__':
    unittest.main()
