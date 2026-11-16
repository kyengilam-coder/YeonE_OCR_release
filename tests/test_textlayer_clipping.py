import re
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import pymupdf
from yeone_ocr import textlayer as layer


class ClippingTests(unittest.TestCase):
    def test_existing_replacement_character_is_preserved(self):
        text='甲\ufffd乙'
        self.check_roundtrip([layer.Line('h',[(c,(.1+i*.1,.1,.2+i*.1,.2)) for i,c in enumerate(text)],'uniform')],text)
    def test_failed_extraction_does_not_replace_previous_pdf(self):
        from yeone_ocr.schema import PageReading
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)/'existing.pdf';out.write_bytes(b'previous output')
            with pymupdf.open() as doc:
                doc.new_page(width=400,height=600)
                line=layer.Line('h',[('甲',(.1,.1,.2,.2))],'uniform')
                with patch.object(layer,'plan_page',return_value=[line]),patch.object(layer,'own_text',return_value=['乙']):
                    with self.assertRaises(layer.TextlayerVerificationError):
                        layer.searchable_pdf(doc,[1],lambda n:(PageReading(pdf_page=1),Image.new('RGB',(400,600)),(0,0,400,600),False),out)
            self.assertEqual(out.read_bytes(),b'previous output')
    def check_roundtrip(self, lines, expected):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'result.pdf'
            with pymupdf.open() as doc:
                page=doc.new_page(width=400,height=600)
                font,cids=layer.add_font(doc,list(expected))
                layer.write_lines(doc,page,lines,(0,0,400,600),font,cids)
                doc.save(path)
            with pymupdf.open(path) as doc:
                self.assertEqual(re.sub(r'\s','',''.join(layer.own_text(doc,1))),expected)

    def test_half_width_stars_in_full_page_fallback_keep_last_character(self):
        lines=[layer.Line('h',[('*',(i/n,0,(i+1)/n,1)) for i in range(n)],'uniform') for n in (3,2)]
        self.check_roundtrip(lines,'*****')

    def test_dense_overlapping_boxes_do_not_force_text_outside_page(self):
        text=''.join(chr(0x4e00+i) for i in range(80))
        line=layer.Line('h',[(c,(.05+i*.006,.1,.10+i*.006,.13)) for i,c in enumerate(text)],'ink~')
        self.check_roundtrip([line],text)

    def test_extrapolated_fallback_boxes_are_fitted_inside_page(self):
        text=''.join(chr(0x4e00+i) for i in range(80))
        line=layer.Line('h',[(c,(.2+i*.02,.1,.22+i*.02,.13)) for i,c in enumerate(text)],'ink~')
        self.check_roundtrip([line],text)
