import tempfile,statistics,unittest
from pathlib import Path
from yeone_ocr import textlayer as tl
from yeone_ocr.pdfio import open_pdf,page_image,page_frame
from yeone_ocr.schema import PageReading
from tests.test_textlayer import scan,HLINES


class PageContextTests(unittest.TestCase):
    def plan(self, *, donors=True, joined=False, suffix=False, orphan=False):
        with tempfile.TemporaryDirectory() as folder:
            path,truth=scan(Path(folder),HLINES,False)
            texts=[' '.join(HLINES)] if joined else HLINES
            if suffix:
                texts=[text+' ····· '+str(287+i*20) for i,text in enumerate(texts)]
            reading=PageReading.model_validate({'pdf_page':1,'regions':[
                {'id':f'line-{i}','k':'본문','bbox':[0,0,1000,1000],'dir':'가로',
                 'paras':[{'cols':[{'n':1,'t':text}]}]} for i,text in enumerate(texts)]})
            layer=[{'dir':'h','chars':[('1',[300,15,310,25])]}]
            layer.extend({'dir':'h','chars':[(('斤' if c=='辛' else c),box) for c,box in row]} for row in truth)
            if orphan:
                box=truth[0][-1][1]
                layer[1]['chars'].append((HLINES[1][0],[box[0]+22,box[1],box[2]+22,box[3]]))
                for i,c in enumerate('甲乙丙丁戊'):
                    layer[2]['chars'][i]=(c,layer[2]['chars'][i][1])
            with open_pdf(path) as doc:
                image,_=page_image(doc,1,dpi=200)
                lines=tl.plan_page(reading,image,page_frame(doc,1),layer if donors else None,False)
            self.assertEqual(''.join(c for line in lines for c,_ in line.chars),''.join(''.join(t.split()) for t in texts))
            self.assertEqual(len(lines),3)
            centers=[statistics.median((box[1]+box[3])/2*480 for _,box in line.chars) for line in lines]
            for center,row in zip(centers,truth):
                expected=statistics.median((box[1]+box[3])/2 for _,box in row)
                self.assertLess(abs(center-expected),6)
            tl._check_line_collisions(lines,1)

    def test_full_page_review_boxes_use_distinct_physical_lines(self):self.plan()
    def test_joined_logical_paragraph_is_split_into_physical_lines(self):self.plan(joined=True)
    def test_image_only_page_context_shares_ink_bands(self):self.plan(donors=False)
    def test_missing_toc_leaders_and_numbers_stay_on_their_anchored_row(self):self.plan(suffix=True)
    def test_bad_donor_cannot_move_next_line_initial_into_previous_line(self):self.plan(orphan=True)
    def test_collapsed_text_is_rejected_even_when_all_characters_exist(self):
        lines=[tl.Line('h',[(c,(.1+i*.02,.8,.12+i*.02,.82)) for i,c in enumerate(text)],'ink',f'line-{n}',0) for n,text in enumerate(HLINES)]
        with self.assertRaisesRegex(tl.TextlayerVerificationError,'글자 위치 겹침'):
            tl._check_line_collisions(lines,238)
