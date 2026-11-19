import copy,json
from PIL import Image
from yeone_ocr.catalog_recovery import recover_catalog
from yeone_ocr.document_editing import prepare_records
from yeone_ocr.local_reading import _cached_catalog_output,reading_from_run
from yeone_ocr.ai_page import cached_page_path
from yeone_ocr.blocks import assemble_blocks


def contents():
    return '\n'.join(f'藥名{chr(0x4e00+i)}……({100+i})' for i in range(20))


def test_catalog_recovery_uses_name_and_page_both_and_flags_unmatched():
    text=contents();witness=text.replace('藥名一','藥名丁')
    recovered=recover_catalog({'text':text},[witness])
    assert recovered and recovered[2]['independently_matched']==19 and len(recovered[1])==1
    assert recover_catalog({'text':text},[text.replace('藥名','別名')]) is None
    assert recover_catalog({'text':text},[text.replace('(1','(2')]) is None
    assert recover_catalog({'text':text+'\n'+text},[text]) is None
    assert recover_catalog({'text':text+'\n這是並非原文的大段文字'*30},[text]) is None
    assert recover_catalog({'text':1},[text]) is None


def test_cached_catalog_does_not_change_record_or_human_corrections(tmp_path):
    (tmp_path/'pages').mkdir();image=Image.new('RGB',(100,200),'white');image.save(tmp_path/'pages/p014.png')
    cache=tmp_path/'ai-page';cache.mkdir()
    path=cached_page_path(image,direction='horizontal',profile='traditional',model='test',cache_dir=cache)
    path.write_text(json.dumps({'text':contents(),'uncertain':[]}),encoding='utf-8')
    raw={'text':contents(),'image_width':100,'image_height':200,'words':[]}
    record={'file':'fixture.pdf','pdf_page':14,'mode':'horizontal_zh','merged':reading_from_run(raw,14,'horizontal').model_dump(),
            'local_first':{'profile':'traditional','ai_page':{'mode':'prefer','used':False,'model':'test'}},'local_grades':[]}
    page={'corrected_text':None,'runs':[{'raw':raw}]};before=copy.deepcopy(record)
    recovered=_cached_catalog_output(record,page,tmp_path/'comparison/project.json')
    assert recovered['_output_catalog_recovery']['new_calls']==0 and record==before
    prepared,_=prepare_records([recovered]);blocks=assemble_blocks(prepared,layout='flow',edit_paragraphs=True,legend=False)
    assert len([b for b in blocks if b.type=='para'])==20
    for field,value in [('corrected_text','수정'),('edits',[{}]),('decisions',[{}]),('ai_decisions',[{}])]:
        protected={**page,field:value}
        assert _cached_catalog_output(record,protected,tmp_path/'comparison/project.json') is None
    path.unlink()
    assert _cached_catalog_output(record,page,tmp_path/'comparison/project.json') is None
