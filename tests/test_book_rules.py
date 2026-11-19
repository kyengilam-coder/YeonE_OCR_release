import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile
import pytest
from yeone_ocr.blocks import Block, assemble_blocks
from yeone_ocr.presets import DATA, PresetError, load_preset
from yeone_ocr.rule_library import ensure_examples, library_choices, save_rule
from yeone_ocr.rule_request import prepare_request, sample_pages
from yeone_ocr.structure import apply_structure
from yeone_ocr.document_editing import prepare_records, format_blocks, clean_output_blocks
from yeone_ocr.common_layout import apply_common_layout
from yeone_ocr.hwpx_builder import build_hwpx
from tests._hwpx_fixtures import rec, text_region

HP='{http://www.hancom.co.kr/hwpml/2011/paragraph}'
HH='{http://www.hancom.co.kr/hwpml/2011/head}'

def rule(): return load_preset(DATA/'book_rule_yaolun.json')

def test_examples_remain_user_owned_and_discoverable(tmp_path):
    with patch.dict('os.environ',{'YEONE_OCR_RULES_DIR':str(tmp_path)}):
        directory=ensure_examples(); assert directory==tmp_path
        assert load_preset(directory/'개보본초.json').entry is not None
        assert load_preset(directory/'중의역대약론선.json').citation is not None
        p=directory/'개보본초.json';p.write_text('{"schema":1,"label":"사람이 수정"}',encoding='utf-8')
        ensure_examples();assert load_preset(p).label=='사람이 수정'
        (directory/'broken.json').write_text('{',encoding='utf-8')
        refs=[Path(ref).name for ref,label in library_choices()]
        assert '개보본초.json' in refs and '중의역대약론선.json' in refs and 'broken.json' not in refs

def test_saved_rule_keeps_template_and_alias_settings(tmp_path):
    target=tmp_path/'규칙.json';template=tmp_path/'양식.hwpx'
    save_rule('bencao',target,template=str(template),label='우리 책')
    p=load_preset(target);assert p.template==str(template) and p.entry.when=='always' and p.label=='우리 책'
    source=tmp_path/'relative.json';source.write_text('{"schema":1,"template":"양식.hwpx"}',encoding='utf-8')
    nested=tmp_path/'other';nested.mkdir();save_rule(str(source),nested/'copy.json')
    assert load_preset(nested/'copy.json').template==str(template)

def test_rule_library_loads_empty_optional_heading_lists(tmp_path, monkeypatch):
    monkeypatch.setenv('YEONE_OCR_RULES_DIR', str(tmp_path))
    source = tmp_path / '책별규칙.json'
    headings = {'chapter': ['一、補益類'], 'section': [], 'subsection': []}
    source.write_text(json.dumps({'schema': 1, 'headings': headings}), encoding='utf-8')
    assert str(source.resolve()) in dict(library_choices())
    saved = save_rule(str(source), tmp_path / '사본.json')
    assert load_preset(saved).headings == headings


@pytest.mark.parametrize('field,value',[('citation',{'mode':'script'}),('citation',{'mode':'separate','continuations':1}),
    ('citation',{'mode':'separate','max_title':121}),('headings',{'script':['run']}),
    ('headings',{'section':'補益類'}),('headings',{'section':[3]}),('template','macro.py')])
def test_unsupported_rule_fields_fail_closed(tmp_path,field,value):
    p=tmp_path/'bad.json';p.write_text(json.dumps({'schema':1,field:value}),encoding='utf-8')
    with pytest.raises(PresetError):load_preset(p)

def test_source_split_preserves_numbers_letters_and_evidence():
    text='2.《本草綱目》：  原文云。'
    source=Block(type='para',text=text,spans=[(0,1,'green'),(15,17,'pending')],memos=[(16,'근거')],anchors=[(16,'PDF 8')])
    before=copy.deepcopy(source);blocks,_=apply_structure([source],rule())
    assert source==before
    assert [b.role for b in blocks]==['citation_source','quotation']
    assert blocks[0].text=='2.《本草綱目》：' and blocks[1].text=='原文云。'
    assert blocks[0].spans[0]==(0,1,'green')
    assert blocks[1].memos and blocks[1].anchors

def test_both_existing_citation_patterns_become_separate():
    items=[Block(type='para',text='『本草綱目』'),Block(type='para',text='第一引文。'),
           Block(type='para',text='2.《本草備要》：第二引文。'),Block(type='para',text='同一引文的下一段。')]
    out,_=apply_structure(items,rule())
    assert [b.role for b in out]==['citation_source','quotation','citation_source','quotation','quotation']
    assert ''.join(b.text for b in out)==''.join(b.text for b in items)
    out,_=apply_common_layout(out)
    assert out[0].layout['keep_with_next'] and not out[1].layout['keep_with_next']
    assert all(not b.layout.get('page_break_before') for b in out)

def test_inline_mentions_contents_and_bibliography_are_not_citations():
    items=[Block(type='para',text='《本草綱目》中記載此藥。'),Block(type='para',text='正文提及《本草備要》：不可分。'),
           Block(type='para',text='1.《本草綱目》........12'),
           Block(type='heading',text='引用書目',level=2),Block(type='para',text='1.《本草綱目》'),
           Block(type='para',text='2.《本草備要》')]
    out,_=apply_structure(items,rule())
    assert len(out)==len(items) and not any(b.role in ('citation_source','quotation') for b in out)

def test_unclosed_source_is_red_and_not_rewritten():
    text='1.《本草綱目 未閉合原文'
    out,_=apply_structure([Block(type='para',text=text)],rule())
    assert len(out)==1 and out[0].text==text and any(k=='pending' for a,b,k in out[0].spans)

def test_source_boundary_survives_local_line_joining_and_exact_titles():
    records=[rec(2,[text_region('local-line-1',['土茯苓']),text_region('local-line-2',['1.《綱目》：原文。']),
                    text_region('local-line-3',['繼續原文。']),text_region('local-line-4',['2.《本草備要》：次文。'])])]
    before=copy.deepcopy(records)
    prepared,_=prepare_records(records,common_layout=True,citation=rule().citation,headings={'section':['土茯苓']})
    out=assemble_blocks(prepared,layout='flow',grades=False,legend=False,memo_mode='none',edit_paragraphs=True)
    out,_=apply_structure(out,rule());out=clean_output_blocks(out)
    assert records==before
    assert out[0].type=='heading' and out[0].text=='土茯苓'
    assert sum(b.role=='citation_source' for b in out)==2
    assert '繼續原文。' in ''.join(b.text for b in out if b.role=='quotation')

def test_quotation_does_not_become_a_new_herb():
    out,_=apply_structure([Block(type='para',text='1.《綱目》：胡黃連 味苦,平。')],rule())
    out,_=format_blocks(out,{'胡黃連'})
    assert [b.role for b in out]==['citation_source','quotation']

def test_default_hwpx_has_real_quotation_indent_and_keeps_source_with_body(tmp_path):
    out,_=apply_structure([Block(type='para',text='1.《綱目》：原文。')],rule());out,_=apply_common_layout(out)
    target=tmp_path/'output.hwpx';build_hwpx(out,str(target),preset=rule(),edited=True)
    with ZipFile(target) as z:
        header=ET.fromstring(z.read('Contents/header.xml'));sec=ET.fromstring(z.read('Contents/section0.xml'))
    props={p.get('id'):p for p in header.iter(HH+'paraPr')}
    paragraphs=[p for p in sec.findall(HP+'p') if ''.join(t.text or '' for t in p.iter(HP+'t'))]
    q=props[paragraphs[1].get('paraPrIDRef')]
    assert any(n.tag.endswith('left') and int(n.get('value','0'))>0 for n in q.iter())
    assert props[paragraphs[0].get('paraPrIDRef')].find(HH+'breakSetting').get('keepWithNext')=='1'
    assert not sec.findall('.//'+HP+'lineBreak')

def test_request_packet_is_local_and_leaves_inputs_unchanged(tmp_path):
    import pymupdf
    pdf=tmp_path/'책.pdf'
    with pymupdf.open() as doc:
        for _ in range(3): doc.new_page()
        doc.save(pdf)
    before=hashlib.sha256(pdf.read_bytes()).hexdigest()
    folder=prepare_request(pdf,'','1,3',tmp_path/'requests')
    meta=json.loads((folder/'자료목록.json').read_text(encoding='utf-8'))
    assert meta['ai_calls']==0 and not meta['automatic_upload']
    assert len(list(folder.glob('원본_*.png')))==2
    assert hashlib.sha256(pdf.read_bytes()).hexdigest()==before
    assert '현재 대화로 자동 첨부되지는 않습니다' in (folder/'규칙작성_요청.txt').read_text(encoding='utf-8')

@pytest.mark.parametrize('value',['0','3-1','1-40','1;print(1)','1,99'])
def test_request_pages_are_bounded(value):
    with pytest.raises(ValueError): sample_pages(value,40)

def test_missing_linked_template_stops_before_any_model_work(tmp_path):
    from yeone_ocr.cli import build_parser, _check_document
    source=tmp_path/'rule.json';source.write_text('{"schema":1,"template":"missing.hwpx"}',encoding='utf-8')
    args=build_parser().parse_args(['build','source.pdf','--layout','flow','--preset',str(source)])
    assert '양식 HWPX 가 없습니다' in _check_document(args)
