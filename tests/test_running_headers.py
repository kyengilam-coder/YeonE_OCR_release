import copy
from yeone_ocr.document_editing import prepare_records
from yeone_ocr.schema import PageReading,paragraph_text
from tests._hwpx_fixtures import rec,text_region


def sample(page,header='各论一、植物药类',*,hybrid=False):
    lines=[text_region('local-line-1',[header+('者，往往多能健飯。' if hybrid else '')],box=(.69,.06,.92,.16 if hybrid else .08))]
    if not hybrid:lines.append(text_region('local-line-2',['者，往往多能健飯。'],box=(.1,.105,.92,.13)))
    lines.append(text_region('local-line-3',['正文提及各論一·植物藥類，應保留。'],box=(.1,.2,.92,.24)))
    return rec(page,lines)


def test_yaolun_running_heads_variants_and_ocr_noise_do_not_join_body():
    records=[sample(i) for i in range(1,4)]+[sample(135,'各 論 ” 一 、 植 物 藥 美 。 119')]
    before=copy.deepcopy(records);out,_=prepare_records(records,common_layout=True)
    assert records==before
    for record in out:
        reading=PageReading.model_validate(record['merged'])
        assert reading.regions[0].id=='local-line-2'
        assert paragraph_text(reading.regions[0].paragraphs[0])=='者，往往多能健飯。'
        assert len(record['_document_excluded'])==1
        assert '各論一·植物藥類' in paragraph_text(reading.regions[1].paragraphs[0])


def test_combined_header_prefix_trims_only_prefix_and_moves_review_spans():
    records=[sample(i) for i in range(1,4)]+[sample(139,hybrid=True)]
    prefix='各论一、植物药类';records[-1]['local_grades']=[dict(region_id='local-line-1',para=0,start=len(prefix),end=len(prefix)+1,grade='red')]
    before=copy.deepcopy(records);out,_=prepare_records(records)
    assert records==before
    region=PageReading.model_validate(out[-1]['merged']).regions[0]
    assert paragraph_text(region.paragraphs[0])=='者，往往多能健飯。'
    assert out[-1]['local_grades']==[dict(region_id='local-line-1',para=0,start=0,end=1,grade='red')]
    assert out[-1]['_document_excluded'][0]['partial'] is True


def test_centered_titles_body_mentions_and_full_page_boxes_are_preserved():
    records=[sample(i) for i in range(1,4)]
    body=rec(8,[text_region('title',['各论一、植物药类'],kind='제목',box=(.3,.05,.7,.08)),
                text_region('body',['各论一、植物药类者，本文。'],box=(.1,.2,.9,.3)),
                text_region('full',['各论一、植物药类者，粗略座標。'],box=(0,0,1,1))])
    out,_=prepare_records(records+[body])
    assert not out[-1]['_document_excluded']
    assert len(out[-1]['merged']['regions'])==3


def test_repeated_numbered_citations_are_not_learned_as_headers():
    records=[rec(n,[text_region('cite',['1.《本草綱目》'],box=(.1,.05,.4,.08)),
                    text_region('body',['正文。'],box=(.1,.11,.9,.15))]) for n in range(1,4)]
    result,_=prepare_records(records)
    assert all(not r['_document_excluded'] for r in result)
