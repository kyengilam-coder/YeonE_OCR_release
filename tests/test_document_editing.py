import copy
import tempfile
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET

from yeone_ocr.blocks import Block, assemble_blocks
from yeone_ocr.document_editing import prepare_records, format_blocks, clean_output_blocks
from yeone_ocr.local_reading import reading_from_run
from yeone_ocr.normalization import normalize_blocks, NormalizationOptions
from yeone_ocr.hwpx_builder import build_hwpx


def test_local_lines_keep_entries_and_source_records():
    reading = reading_from_run({'text':'靑黛 味咸,寒,无毒。\n主解诸药毒。\n\n胡黄连 味苦,平,无毒。\n主久痢成疳。','image_width':100,'image_height':100},1,'horizontal')
    records=[{'pdf_page':1,'file':'fixture.pdf','merged':reading.model_dump(),'mode':'horizontal_zh'}]
    before=copy.deepcopy(records)
    edited,names=prepare_records(records)
    blocks=assemble_blocks(edited,layout='flow',display='simple',edit_paragraphs=True)
    result,stats=format_blocks(blocks,names)
    bodies=[b.text for b in result if b.type=='para']
    assert bodies==['靑黛 味咸, 寒, 无毒。主解诸药毒。','胡黄连 味苦, 平, 无毒。主久痢成疳。']
    assert sum(b.type=='spacer' for b in result)==1
    assert records==before


def test_spacing_preserves_numbers_marks_and_evidence():
    source=Block(type='para',text='胡黃連味苦,平,無毒。\n主治,1,000。〔疑: a,b〕',
                 spans=[(5,6,'red')],memos=[(5,'check')],anchors=[(5,'PDF_1')])
    blocks,_=format_blocks([source],{'胡黃連'})
    text=blocks[0].text
    assert text=='胡黃連 味苦, 平, 無毒。主治, 1,000。〔疑: a,b〕'
    assert blocks[0].anchors==[(6,'PDF_1')]
    assert blocks[0].memos==[(6,'check')]
    assert source.text.find('\n')>=0
    named,_=format_blocks([Block(type='para',text='胡黃連味苦',spans=[(1,2,'red')],memos=[(1,'name')],anchors=[(2,'name-end')])],{'胡黃連'})
    assert named[0].spans==[(1,2,'red')]
    assert named[0].memos==[(1,'name')]
    assert named[0].anchors==[(2,'name-end')]


def test_english_japanese_and_inline_heading():
    blocks,_=format_blocks([Block(type='heading',role='entry',text='胡黃連'),
                           Block(type='para',text='味苦,平。'),
                           Block(type='para',text='English\nwords 1,000, next。日本語\n本文')])
    assert blocks[0].text=='胡黃連 味苦, 平。'
    assert blocks[1].text=='English words 1,000, next。日本語本文'


def test_running_headers_footers_and_navigation_are_output_only():
    from tests._hwpx_fixtures import rec, text_region
    region=lambda rid,text,y0,y1:text_region(rid,[text],box=(0.1,y0,0.9,y1))
    records=[rec(1,[region('local-line-1','188 开宝本草草部中品之上卷第八',.05,.08),
                    region('local-line-2','胡黄连 味苦,平,无毒。',.2,.25),
                    region('local-line-3','188',.94,.96)])]
    before=copy.deepcopy(records)
    edited,names=prepare_records(records)
    assert [r['text'] for r in edited[0]['_document_excluded']]==['188 开宝本草草部中品之上卷第八','188']
    assert records==before
    blocks=clean_output_blocks([Block(type='meta',text='PDF1'),Block(type='para',text='本文〔판독불명〕',anchors=[(0,'PDF1')]),Block(type='note',text='작업 메모')])
    assert len(blocks)==1 and blocks[0].text=='本文〔판독불명〕' and not blocks[0].anchors
    with tempfile.TemporaryDirectory() as d:
        path=Path(d)/'clean.hwpx';build_hwpx(blocks,str(path),edited=True)
        with ZipFile(path) as z:
            assert b'<hp:bookmark' not in z.read('Contents/section0.xml')


def test_title_mentions_and_contents_rows_are_preserved():
    from tests._hwpx_fixtures import rec,text_region
    records=[rec(1,[text_region('local-line-1',['开宝本草']),
                    text_region('local-line-2',['开宝本草草部中品之上 卷第八 ····· 188']),
                    text_region('local-line-3',['正文提及开宝本草及其版本。'])])]
    edited,_=prepare_records(records)
    assert not edited[0]['_document_excluded']
    assert len(edited[0]['merged']['regions'])==3


def test_repeated_side_decoration_excluded_but_body_kept():
    from tests._hwpx_fixtures import rec,text_region
    records=[rec(n,[text_region('local-line-1',['醫書'],box=(.95,.2,.98,.6)),
                    text_region('local-line-2',['醫書'],box=(.2,.2,.8,.6))]) for n in range(1,4)]
    edited,_=prepare_records(records)
    assert all([item['text'] for item in r['_document_excluded']]==['醫書'] for r in edited)
    assert all(len(r['merged']['regions'])==1 for r in edited)


def test_editorial_traditional_and_hwpx_has_real_blank_no_hard_returns():
    blocks=[Block(type='para',text='青黛 味咸,寒,无毒。发热。'),Block(type='para',text='胡黄连 味苦,平,无毒。')]
    blocks,report=normalize_blocks(blocks,options=NormalizationOptions(editorial_simplified=True))
    blocks,stats=format_blocks(blocks,{'靑黛','胡黃連'})
    assert '靑黛 味咸, 寒, 無毒. 發熱.' in blocks[0].text
    assert stats['entries']==2
    with tempfile.TemporaryDirectory() as d:
        path=Path(d)/'test.hwpx'
        build_hwpx(blocks,str(path),edited=True)
        with ZipFile(path) as z:
            root=ET.fromstring(z.read('Contents/section0.xml'))
            ns={'hp':'http://www.hancom.co.kr/hwpml/2011/paragraph'}
            paras=[''.join(t.text or '' for t in p.findall('.//hp:t',ns)) for p in root.findall('hp:p',ns)]
            assert paras[-3:]==[blocks[0].text,'',blocks[2].text]
            assert not root.findall('.//hp:lineBreak',ns)
            assert b'Source Han Serif K Medium' in z.read('Contents/header.xml')
