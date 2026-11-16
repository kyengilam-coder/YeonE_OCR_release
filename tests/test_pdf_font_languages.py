import hashlib,json,re,tempfile,unittest
from pathlib import Path
import pymupdf
from yeone_ocr import textlayer as tl
from yeone_ocr.pdfio import text_layer
from yeone_ocr.schema import PageReading
from PIL import Image


class FontLanguageTests(unittest.TestCase):
    def test_upstream_font_matches_pinned_original_and_covers_languages(self):
        manifest=json.loads(tl._FONT_FILE.with_name('manifest.json').read_text(encoding='utf-8-sig'))
        self.assertEqual(hashlib.sha256(tl._FONT_FILE.read_bytes()).hexdigest(),manifest['sha256'])
        self.assertIn('SIL OPEN FONT LICENSE',tl._FONT_FILE.with_name('LICENSE.txt').read_text())
        for c in 'English OCR café naïve 日本語ひらがなカタカナ漢字㕮䈽䓖䗪䚡䵟':
            self.assertTrue(tl._pdf_font().has_glyph(ord(c),fallback=0),repr(c))

    def test_english_word_spaces_survive_without_an_original_text_layer(self):
        text='The quick brown fox: OCR 123. Save and reopen; café naïve.'
        reading=PageReading.model_validate({'pdf_page':1,'regions':[{'id':'english','k':'본문','bbox':[50,300,950,400],
            'dir':'가로','paras':[{'cols':[{'n':1,'t':text}]}]}]})
        lines=tl.plan_page(reading,Image.new('L',(1200,800),255),(0,0,600,400),None,False)
        self.assertEqual(''.join(c for line in lines for c,_ in line.chars),text)
        with pymupdf.open() as doc:
            doc.new_page(width=600,height=400)
            font,cids=tl.add_font(doc,text)
            tl.write_lines(doc,doc[0],lines,(0,0,600,400),font,cids)
            self.assertEqual(''.join(tl.own_text(doc,1)),text)
            self.assertTrue(doc[0].search_for('quick brown fox'))
        with tempfile.TemporaryDirectory() as folder,pymupdf.open() as doc:
            doc.new_page(width=600,height=400)
            target=Path(folder)/'english.pdf'
            result=tl.searchable_pdf(doc,[1],lambda _: (reading,Image.new('L',(1200,800),255),(0,0,600,400),False),target)
            self.assertEqual(result['mismatch'],[])
            with pymupdf.open(target) as reopened:
                self.assertEqual(''.join(tl.own_text(reopened,1)),text)

    def test_japanese_horizontal_and_vertical_roundtrip_in_saved_pdf(self):
        text='日本語の保存。ひらがな・カタカナ「漢字」㕮䗪'
        for direction in ('h','v'):
            with self.subTest(direction=direction),tempfile.TemporaryDirectory() as folder,pymupdf.open() as doc:
                doc.new_page(width=600,height=600)
                boxes=[(.1+i*.025,.3,.125+i*.025,.33) if direction=='h' else (.6,.1+i*.025,.63,.125+i*.025) for i in range(len(text))]
                font,cids=tl.add_font(doc,text)
                tl.write_lines(doc,doc[0],[tl.Line(direction,list(zip(text,boxes)),'uniform')],(0,0,600,600),font,cids)
                path=Path(folder)/'japanese.pdf';doc.save(path)
                with pymupdf.open(path) as reopened:
                    self.assertEqual(re.sub(r'\s','',''.join(tl.own_text(reopened,1))),text)
                    self.assertTrue(reopened[0].search_for('日本語'))
                    self.assertEqual(text_layer(reopened,1).char_count,0)
