import unittest
import pymupdf
from yeone_ocr import textlayer as tl
from yeone_ocr.pdfio import layer_chars, text_layer


class FontTests(unittest.TestCase):
    def test_real_plain_font_and_rare_han_keep_normal_point_sizes(self):
        text='天地㕮䵟䈽䚡䓖䗪𠮷한글12'
        with pymupdf.open() as doc:
            page=doc.new_page(width=300,height=200)
            font,cids=tl.add_font(doc,text)
            line=tl.Line('h',[(c,(.05+i*.055,.3,.105+i*.055,.38)) for i,c in enumerate(text)],'ink')
            tl.write_lines(doc,page,[line],(0,0,300,200),font,cids)
            data=doc.extract_font(font)[3]
            self.assertGreater(len(data),100_000)  # real outlines, not the old empty .notdef font
            self.assertEqual(''.join(tl.own_text(doc,1)).replace(' ',''),text)
            spans=[s for b in page.get_text('dict')['blocks'] for l in b.get('lines',[]) for s in l['spans']]
            self.assertTrue(all('Glyphless' not in s['font'] for s in spans))
            self.assertTrue(all(8 < s['size'] < 22 for s in spans))

    def test_same_family_native_text_remains_an_independent_witness(self):
        with pymupdf.open() as doc:
            page=doc.new_page(width=300,height=200)
            native=page.insert_font(fontname='Native',fontbuffer=tl._pdf_font().buffer)
            page.insert_text((30,40),'天地',fontname='Native',fontsize=12)
            before=doc.xref_get_key(page.xref,'Contents')
            font,cids=tl.add_font(doc,'玄黃')
            tl.write_lines(doc,page,[tl.Line('h',[('玄',(.1,.5,.15,.58)),('黃',(.15,.5,.2,.58))],'ink')],(0,0,300,200),font,cids)
            contents=doc.xref_get_key(page.xref,'Contents')
            self.assertEqual(''.join(text_layer(doc,1).plain().split()),'天地')
            self.assertEqual(''.join(c for row in layer_chars(doc,1) for c,_ in row['chars']),'天地')
            self.assertEqual(''.join(tl.own_text(doc,1)),'玄黃')
            self.assertEqual(doc.xref_get_key(page.xref,'Contents'),contents)
            self.assertTrue(tl.strip_own_layer(doc,page))
            self.assertEqual(page.get_text().strip(),'天地')

    def test_optimizer_merged_layer_is_not_used_as_ocr_evidence(self):
        with pymupdf.open() as doc:
            page=doc.new_page(width=300,height=200)
            font,cids=tl.add_font(doc,'天地')
            tl.write_lines(doc,page,[tl.Line('h',[('天',(.1,.5,.15,.58)),('地',(.15,.5,.2,.58))],'ink')],(0,0,300,200),font,cids)
            page.clean_contents()
            self.assertTrue(tl.has_own_layer(page))
            self.assertEqual(layer_chars(doc,1),[])
            self.assertFalse(tl.strip_own_layer(doc,page))
