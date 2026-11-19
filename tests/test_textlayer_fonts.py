import unittest
from unittest.mock import patch
import pymupdf
from yeone_ocr import textlayer as tl
from yeone_ocr.pdfio import layer_chars, text_layer


class FontTests(unittest.TestCase):
    def test_export_rejects_opentype_container_declared_as_raw_cff(self):
        import tempfile
        from pathlib import Path
        from PIL import Image
        from yeone_ocr.schema import PageReading

        with tempfile.TemporaryDirectory() as folder, pymupdf.open() as doc:
            out = Path(folder) / 'existing.pdf'
            out.write_bytes(b'previous output')
            doc.new_page(width=300, height=200)
            line = tl.Line('h', [('甲', (.1, .2, .2, .3))], 'ink')
            with patch.object(tl, 'plan_page', return_value=[line]), \
                    patch.object(tl, '_font_program', return_value=tl._FONT_FILE.read_bytes()):
                with self.assertRaisesRegex(tl.TextlayerVerificationError, 'CFF'):
                    tl.searchable_pdf(doc, [1], lambda n: (
                        PageReading(pdf_page=n), Image.new('L', (300, 200)), (0, 0, 300, 200), False), out)
            self.assertEqual(out.read_bytes(), b'previous output')

    def test_font_program_matches_pdf_subtype_and_common_encoding(self):
        with pymupdf.open() as doc:
            font, _ = tl.add_font(doc, '天地')
            self.assertEqual(doc.xref_get_key(font, 'Encoding'), ('name', '/Identity-H'))
            descendant = int(doc.xref_get_key(font, 'DescendantFonts')[1][1:].split()[0])
            descriptor = int(doc.xref_get_key(descendant, 'FontDescriptor')[1].split()[0])
            program = int(doc.xref_get_key(descriptor, 'FontFile3')[1].split()[0])
            self.assertEqual(doc.xref_get_key(program, 'Subtype')[1], '/CIDFontType0C')
            self.assertEqual(doc.xref_stream(program)[:2], b'\x01\x00')

    def test_missing_glyphs_keep_distinct_searchable_codes(self):
        with pymupdf.open() as doc:
            page = doc.new_page()
            chars = '\U0010fffc\U0010fffd'
            font, codes = tl.add_font(doc, chars)
            self.assertNotEqual(codes[chars[0]], codes[chars[1]])
            line = tl.Line('h', [(c, (.1 + i*.1, .2, .2 + i*.1, .3)) for i,c in enumerate(chars)], 'ink')
            tl.write_lines(doc, page, [line], (0, 0, page.rect.width, page.rect.height), font, codes)
            self.assertEqual(''.join(''.join(tl.own_text(doc, 1)).split()), chars)

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
