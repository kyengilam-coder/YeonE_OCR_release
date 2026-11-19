import copy
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET
from yeone_ocr.blocks import Block, assemble_blocks
from yeone_ocr.common_layout import apply_common_layout
from yeone_ocr.document_editing import prepare_records,format_blocks,clean_output_blocks
from yeone_ocr.hwpx_builder import build_hwpx
from tests._hwpx_fixtures import rec,text_region,make_template2

HP='{http://www.hancom.co.kr/hwpml/2011/paragraph}'
HH='{http://www.hancom.co.kr/hwpml/2011/head}'


def test_heading_alignment_and_only_major_page_breaks():
    original=[Block(type='heading',text='書名',level=1,layout={'kind':'book'}),
              Block(type='para',text='前文'),Block(type='heading',text='卷第一',level=1),
              Block(type='para',text='本文'),Block(type='heading',text='第一節',level=2),
              Block(type='para',role='entry',text='胡黃連 味苦, 平, 無毒.')]
    before=copy.deepcopy(original);result,stats=apply_common_layout(original)
    assert original==before
    assert all(b.layout['alignment']=='CENTER' for b in result if b.type=='heading')
    assert [b.layout.get('page_break_before') for b in result if b.type=='heading']==[False,True,False]
    assert result[-1].layout.get('alignment') is None
    assert result[-1].layout['widow_orphan'] and not result[-1].layout['keep_lines']
    assert stats['chapter_page_breaks']==1


def test_infer_chapter_section_not_contents_and_preserve_records():
    regions=[text_region('local-line-1',['卷第一']),text_region('local-line-2',['']),
             text_region('local-line-3',['正文']),text_region('local-line-4',['']),
             text_region('local-line-5',['第一節']),text_region('local-line-6',['']),
             text_region('local-line-7',['正文']),text_region('local-line-8',['']),
             text_region('local-line-9',['卷第二 ····· 88'])]
    records=[rec(3,regions)];before=copy.deepcopy(records)
    prepared,names=prepare_records(records,common_layout=True)
    assert records==before
    hints=prepared[0]['_common_layout_regions']
    assert hints['local-line-1']['kind']=='volume' and hints['local-line-5']['kind']=='section'
    assert 'local-line-9' not in hints
    blocks=assemble_blocks(prepared,layout='flow',display='simple',edit_paragraphs=True)
    blocks,_=format_blocks(blocks,names);blocks=clean_output_blocks(blocks)
    output,_=apply_common_layout(blocks)
    assert [b.text for b in output if b.type=='heading']==['卷第一','第一節']


def test_real_center_box_only_and_no_coarse_guess():
    records=[rec(4,[text_region('local-line-1',['一段話'],box=(.4,.2,.6,.23)),
                    text_region('local-line-2',['']),text_region('local-line-3',['另一段話'],box=(0,0,1,1))])]
    prepared,_=prepare_records(records,common_layout=True)
    assert prepared[0]['_common_layout_regions']['local-line-1']['alignment']=='CENTER'
    assert 'local-line-3' not in prepared[0]['_common_layout_regions']


def test_contents_volume_label_and_misread_running_header_are_not_chapters():
    contents=[text_region(f'local-line-{i+1}',[f'藥名本经 {100+i}']) for i in range(6)]
    contents+=[text_region('local-line-7',['']),text_region('local-line-8',['卷第十三'])]
    records=[rec(30,contents),rec(115,[text_region('local-line-1',['92 开宝木草序例下卷第一']),text_region('local-line-2',['正文'])])]
    output,_=prepare_records(records,common_layout=True)
    assert 'local-line-8' not in output[0]['_common_layout_regions']
    assert output[1]['_document_excluded'][0]['text']=='92 开宝木草序例下卷第一'


def test_split_title_fragments_join_without_losing_letters_or_grades():
    records=[rec(280,[text_region('local-line-1',['开宝本草木部上品'],box=(.09,.10,.48,.15)),
                      text_region('local-line-2',['卷第十二'],box=(.49,.10,.68,.15)),
                      text_region('local-line-3',['茯苓味甘，平，无毒。'],box=(.1,.3,.9,.35))])]
    output,names=prepare_records(records,common_layout=True)
    blocks=assemble_blocks(output,layout='flow',edit_paragraphs=True,display='simple')
    blocks,_=format_blocks(blocks,names);blocks=clean_output_blocks(blocks)
    blocks,stats=apply_common_layout(blocks)
    assert blocks[0].text=='开宝本草木部上品 卷第十二'
    assert blocks[0].type=='heading' and blocks[1].type=='para'
    assert stats['centered_headings']==1 and stats['chapter_page_breaks']==0


def test_image_reviewed_layout_requires_the_same_current_text():
    record=rec(40,[text_region('local-line-1',['开宝重定序'])])
    record['_verified_layout']={'local-line-1':{'verified':True,'kind':'section','text':'开宝重定序'}}
    result,_=prepare_records([record],common_layout=True)
    assert result[0]['_common_layout_regions']['local-line-1']['evidence']=='image-reviewed-layout'
    record['_verified_layout']['local-line-1']['text']='different text'
    result,_=prepare_records([record],common_layout=True)
    assert 'local-line-1' not in result[0]['_common_layout_regions']


def test_common_properties_written_to_default_and_template(tmp_path):
    from yeone_ocr.hwpx_template import build_hwpx_template
    blocks,_=apply_common_layout([Block(type='heading',text='書名',level=1),Block(type='para',text='正文'),
                                 Block(type='heading',text='第二章',level=1),Block(type='spacer'),
                                 Block(type='para',text='正文'),Block(type='heading',text='第一節',level=2),Block(type='para',text='正文')])
    template=tmp_path/'template.hwpx';make_template2(template,page_number=True)
    for name in ('default','template'):
        out=tmp_path/f'{name}.hwpx'
        if name=='default':build_hwpx(blocks,str(out),edited=True)
        else:build_hwpx_template(blocks,str(out),str(template),edited=True)
        with ZipFile(out) as z:
            sec=ET.fromstring(z.read('Contents/section0.xml'));header=ET.fromstring(z.read('Contents/header.xml'))
            styles={n.get('id'):n for n in header.iter(HH+'paraPr')}
            ps=sec.findall(HP+'p');texts=[''.join(n.text or '' for n in p.iter(HP+'t')) for p in ps]
            for title in ('書名','第二章','第一節'):
                p=ps[texts.index(title)];style=styles[p.get('paraPrIDRef')]
                assert style.find(HH+'align').get('horizontal')=='CENTER'
                assert style.find(HH+'breakSetting').get('keepWithNext')=='1'
                assert (p.get('pageBreak')=='1')==(title=='第二章')
            p=ps[texts.index('正文')];breaks=styles[p.get('paraPrIDRef')].find(HH+'breakSetting')
            if name=='default':assert breaks.get('widowOrphan')=='1' and breaks.get('keepLines')=='0'
            else:
                style=next(s for s in header.iter(HH+'style') if s.get('id')==p.get('styleIDRef'))
                assert p.get('paraPrIDRef')==style.get('paraPrIDRef')
            assert not sec.findall('.//'+HP+'lineBreak')
