import copy
import unicodedata
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as E
import pytest
from yeone_ocr.grades import styled_runs,GRADE_STYLE,SIMPLE_GRADE_STYLE,page_grades
from yeone_ocr.punctuation_review import cosmetic_comma_offsets,annotate_cosmetic_commas
from yeone_ocr.blocks import Block
from yeone_ocr.local_reading import reading_from_run
from yeone_ocr.hwpx_builder import build_hwpx
from yeone_ocr.hwpx_template import build_hwpx_template
from tests._hwpx_fixtures import make_template


def test_runs_keep_hanja_grades_text_and_memo_without_punctuation_highlights():
    text='甲, 乙; 丙。丁';spans=[(0,3,'red'),(3,6,'blue'),(6,8,'green'),(8,9,'red_ctx')]
    out=styled_runs(text,spans,[(2,'공백 메모')]);assert ''.join(x[0] for x in out)==text
    kinds=[k for t,k,m in out for _ in t]
    assert [kinds[i] for i in (0,3,6,8)]==['red','blue','green','red_ctx']
    assert kinds[1]=='body' and kinds[4]=='body' and kinds[7]=='punct_green'
    assert kinds[2]==kinds[5]=='body' and any(m==['공백 메모'] for _,_,m in out)
    for table in (GRADE_STYLE,SIMPLE_GRADE_STYLE):assert table['pending']['highlight'] is None


def test_cosmetic_commas_require_all_candidates_and_keep_missing_or_type_differences():
    assert cosmetic_comma_offsets('虚，',['虚，',' 虛 , '])=={1}
    assert cosmetic_comma_offsets('病，',['病，','字 ,'])=={1}
    assert cosmetic_comma_offsets('，',['，',''])==set()
    assert cosmetic_comma_offsets('，',['，','。'])==set()
    assert cosmetic_comma_offsets('；',['；',' ; '])=={0}
    assert cosmetic_comma_offsets('，',['，','；'])==set()
    assert cosmetic_comma_offsets('甲，乙',['甲，乙','甲乙，'])==set()


def test_old_record_annotation_is_transient_and_ai_uncertainty_wins():
    raw={'text':'甲，乙；丙','image_width':100,'image_height':200,'words':[]}
    record={'pdf_page':1,'merged':reading_from_run(raw,1,'horizontal').model_dump(),
            'local_grades':[dict(region_id='local-line-1',para=0,start=0,end=5,grade='red')]}
    page={'baseline_run_id':'r','runs':[{'id':'r','raw':raw}],
          'suggestions':[{'start':1,'end':2,'decision':'pending','candidates':{'a':'，','b':' , '}}], 'ai_reviews':[]}
    before=copy.deepcopy((record,page));out=annotate_cosmetic_commas(record,page)
    assert out['_cosmetic_punctuation']==[dict(region_id='local-line-1',para=0,start=1,end=2)]
    assert (record,page)==before
    page['ai_reviews']=[{'request':{'snapshot_text':raw['text']},'proposals':[{'start':1,'end':2}]}]
    assert '_cosmetic_punctuation' not in annotate_cosmetic_commas(record,page)


@pytest.mark.parametrize('template',[False,True])
@pytest.mark.parametrize('simple',[False,True])
def test_hwpx_punctuation_and_spaces_never_receive_review_background(tmp_path,template,simple):
    path=tmp_path/'out.hwpx';text='甲, 乙; 丙。『本經』「引文」《書》()（）!?'
    blocks=[Block(type='para',text=text,role='body',spans=[(0,len(text),'red')])]
    if template:
        tpl=tmp_path/'template.hwpx';make_template(tpl);build_hwpx_template(blocks,str(path),str(tpl),simple=simple)
    else:build_hwpx(blocks,str(path),simple=simple)
    hp='{http://www.hancom.co.kr/hwpml/2011/paragraph}';hh='{http://www.hancom.co.kr/hwpml/2011/head}'
    with ZipFile(path) as z:
        h=E.fromstring(z.read('Contents/header.xml'));s=E.fromstring(z.read('Contents/section0.xml'))
    styles={x.get('id'):x for x in h.iter(hh+'charPr')};seen={}
    for p in s.findall(hp+'p'):
        for run in p.findall(hp+'run'):
            st=styles[run.get('charPrIDRef')]
            for c in ''.join(t.text or '' for t in run.findall(hp+'t')):seen[c]=st
    for c in ', ;。『』「」《》()（）!?':assert seen[c].get('shadeColor','none').lstrip('#').upper()!='FFFF00'
    for c in ',;。『』「」《》()（）!?':assert seen[c].get('textColor').lstrip('#').upper()!='C00000'
    for c in '甲乙丙':assert seen[c].get('shadeColor').lstrip('#').upper()=='FFFF00'
