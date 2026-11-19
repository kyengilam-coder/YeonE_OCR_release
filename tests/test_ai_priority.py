import copy,json
from yeone_ocr.ai_page import merge
from yeone_ocr.ai_priority import cached_priority,confirmed_positions
from yeone_ocr.local_reading import reading_from_run


def fixture(tmp_path):
    text=''.join(chr(0x4e00+i) for i in range(160));raw={'text':text,'image_width':100,'image_height':200,'words':[]}
    answer={'text':text,'model':'fixture-model','uncertain':[text[55:57]]}
    value,red,blue,summary=merge(text,text,'prefer',uncertain=answer['uncertain'])
    summary.update(mode='prefer',model='fixture-model')
    rec={'pdf_page':1,'merged':reading_from_run(raw,1,'horizontal',text=value).model_dump(),
         'local_first':{'ai_page':summary},'local_grades':[dict(region_id='local-line-1',para=0,start=0,end=160,grade='red'),
            dict(region_id='local-line-1',para=0,start=5,end=6,grade='green')]}
    page={'runs':[{'id':'r','raw':raw}],'baseline_run_id':'r','corrected_text':None,'edits':[],
          'decisions':[],'ai_decisions':[],'ai_reviews':[]}
    folder=tmp_path/'ai-page';folder.mkdir();(folder/'legacy.json').write_text(json.dumps(answer),encoding='utf-8')
    return text,rec,page,tmp_path/'comparison/project.json'


def test_completed_cache_replay_reduces_local_red_but_protects_ai_objection_and_history(tmp_path):
    text,rec,page,ref=fixture(tmp_path);before=copy.deepcopy((rec,page))
    page['ai_reviews']=[{'request':{'snapshot_text':text},'proposals':[{'start':10,'end':11}]}]
    out=cached_priority(rec,page,ref,{})
    assert out['_ai_priority']['saved_result_exactly_replayed'] and out['_ai_priority']['new_calls']==0
    def at(pos):return [s['grade'] for s in out['local_grades'] if s['start']<=pos<s['end']][-1]
    assert at(20)=='ai_supported' and at(10)=='red' and at(55)=='red' and at(56)=='red' and at(5)=='green'
    assert rec==before[0] and page['runs']==before[1]['runs']
    assert out['merged']==rec['merged']


def test_human_decisions_unknown_cache_and_witness_are_never_overruled(tmp_path):
    text,rec,page,ref=fixture(tmp_path)
    for field,value in [('corrected_text',text),('edits',[{}]),('decisions',[{}]),('ai_decisions',[{}])]:
        assert '_ai_priority' not in cached_priority(rec,{**page,field:value},ref,{})
    rec['local_first']['ai_page']['mode']='witness'
    assert '_ai_priority' not in cached_priority(rec,page,ref,{})
    rec['local_first']['ai_page']['mode']='prefer';rec['local_first']['ai_page']['taken']+=1
    assert '_ai_priority' not in cached_priority(rec,page,ref,{})


def test_punctuation_and_coincidental_short_matches_do_not_gain_ai_confidence():
    assert not confirmed_positions('甲乙丙丁','甲乙丙丁')
    text='天地玄黃宇宙洪荒, 日月盈昃辰宿列張'
    good=confirmed_positions(text,text.replace(',','，'),red=[(0,1)])
    assert 0 not in good and text.index(',') not in good and 2 in good


def test_reused_matching_blocks_keep_exact_span_mapping_and_are_bounded():
    from yeone_ocr.comparison import ComparisonProject,_matching_blocks,_matcher
    from unittest.mock import patch
    _matching_blocks.cache_clear()
    with patch('yeone_ocr.comparison._matcher',wraps=_matcher) as wrapped:
        assert ComparisonProject._mapped_span('甲乙丙丁','前甲乙丙丁',1,2)==(2,3)
        assert ComparisonProject._mapped_span('甲乙丙丁','前甲乙丙丁',2,3)==(3,4)
        assert wrapped.call_count==1
    assert _matching_blocks.cache_info().maxsize==32
    _matching_blocks.cache_clear()
