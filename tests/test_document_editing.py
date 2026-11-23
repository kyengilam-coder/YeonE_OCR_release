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
from yeone_ocr.citation_layout import heading_key


def test_entry_fields_split_without_splitting_inline_note_references():
    text = '露水[氣味] 甘, 平, 無毒. [主治] 百草頭上露, 未晞[1]時以盤收取。'
    pos = text.index('百')
    original = Block(type='para', text=text, spans=[(pos, pos+1, 'green')])
    output, _ = format_blocks([original], punctuation=False)
    assert [b.text for b in output] == ['露水', '[氣味] 甘, 平, 無毒.', '[主治] 百草頭上露, 未晞[1]時以盤收取。']
    assert output[-1].spans == [(5, 6, 'green')]
    assert original.text == text


def test_footnote_group_has_one_top_separator_and_keeps_notes_together(tmp_path):
    from yeone_ocr.common_layout import apply_common_layout
    from yeone_ocr.hwpx_writer import HH, HP

    body = Block(type='para', page=1, text='正文[1]。')
    notes = [Block(type='para', kind='소주', role='small', page=1, text=f'[{n}] 附注。',
                   layout={'source_layout_para': 'footnote', 'source_typography': {'size': 8}})
             for n in range(1, 4)]
    blocks, _ = apply_common_layout([body, *notes])
    output = tmp_path/'notes.hwpx'
    build_hwpx(blocks, str(output), edited=True)
    with ZipFile(output) as z:
        header = ET.fromstring(z.read('Contents/header.xml'))
        section = ET.fromstring(z.read('Contents/section0.xml'))
    formats = {p.get('id'): p for p in header.iter(HH+'paraPr')}
    borders = {p.get('id'): p for p in header.iter(HH+'borderFill')}
    formats_used = [formats[p.get('paraPrIDRef')] for p in section.findall(HP+'p')[-3:]]
    rules = [p.find(HH+'border').get('borderFillIDRef') for p in formats_used]
    assert borders[rules[0]].find(HH+'topBorder').get('type') == 'SOLID'
    assert rules[0] not in rules[1:]
    assert [p.find(HH+'breakSetting').get('keepWithNext') for p in formats_used] == ['1','1','0']


def test_marked_and_recovered_herb_titles_use_named_template_style(tmp_path):
    from hwpx import HwpxDocument
    import pymupdf
    from yeone_ocr.hwpx_template import build_hwpx_template, HH, HP
    from yeone_ocr.presets import load_preset
    from yeone_ocr.structure import apply_structure

    raw = {'text':'《本草綱目》\n前文。\n◎當歸\n甘辛，歸肝心脾經。\n©山藥\n歸脾肺經。\n◎藥物……(3)',
           'image_width':500, 'image_height':700, 'words':[]}
    reading = reading_from_run(raw, 1, 'horizontal')
    rule = tmp_path / 'rule.json'
    rule.write_text('{"schema":1,"styles":{"paragraph":{"entry":["약재명","제목-본초명"]}},"citation":{"mode":"separate"}}',
                    encoding='utf-8')
    preset = load_preset(rule)
    with pymupdf.open() as pdf:
        pdf.new_page(width=500,height=700).insert_text((220,200), '◎山藥', fontname='china-t')
        records, _ = prepare_records([{'file':'책.pdf', 'pdf_page':1, 'merged':reading.model_dump()}], source_pdf=pdf)
    blocks = assemble_blocks(records, layout='flow', display='simple', grades=False, legend=False, edit_paragraphs=True)
    blocks, _ = apply_structure(blocks, preset)
    blocks, _ = format_blocks(blocks)
    assert [b.text for b in blocks if b.role=='entry'] == ['◎當歸', '◎山藥']
    template = tmp_path / 'template.hwpx'
    with HwpxDocument.new() as doc:
        for name in ('약재명','제목-본초명'):
            doc.oxml.headers[0].ensure_style(name, style_type='PARA', para_pr_id_ref=0)
        doc.save_to_path(template)
    out = tmp_path / 'out.hwpx'
    build_hwpx_template(blocks, str(out), str(template), preset=preset)
    with ZipFile(out) as z:
        header = ET.fromstring(z.read('Contents/header.xml'))
        style_id = next(s.get('id') for s in header.iter(HH+'style') if s.get('name')=='제목-본초명')
        section = ET.fromstring(z.read('Contents/section0.xml'))
        assert [''.join(t.text or '' for t in p.iter(HP+'t')) for p in section.findall(HP+'p')
                if p.get('styleIDRef')==style_id] == ['◎當歸', '◎山藥']


def test_repeated_side_title_requires_original_page_position():
    import pymupdf

    title = '古今名醫藥論'
    body = '去脾胃濕二，除脾胃熱三。'
    records = []
    with pymupdf.open() as pdf:
        for number in range(1, 6):
            page = pdf.new_page(width=500, height=700)
            if number in (2, 3, 4, 5):
                for y in range(115, 260, 18):
                    page.insert_text((95, y), 'The horizontal body line holds visible text.', fontsize=12)
            if number in (2, 3, 4):
                x = 54 if number != 3 else 451
                for i, glyph in enumerate('ABCDGF'):
                    page.insert_text((x, 112 + i * 20), glyph, fontsize=15)
            else:
                page.insert_text((180, 92), 'The centered title', fontsize=15)
            raw = {'text': title + '\n' + body, 'image_width': 500, 'image_height': 700, 'words': []}
            reading = reading_from_run(raw, number, 'horizontal')
            records.append({'pdf_page': number, 'merged': reading.model_dump()})
        before = copy.deepcopy(records)
        result, _ = prepare_records(records, source_pdf=pdf)
    assert records == before
    assert [r['pdf_page'] for r in result if r['_document_excluded']] == [2, 3, 4]
    for record in result:
        texts = [p['columns'][0]['text'] for region in record['merged']['regions'] for p in region['paragraphs']]
        assert body in texts
        if record['pdf_page'] in (2, 3, 4):
            assert title not in texts
            assert record['_document_excluded'][0]['source_bbox']
        else:
            assert title in texts


def test_numbered_title_glued_to_prose_is_split_only_with_template_evidence():
    title='一一六·本草藥用部位不同功效亦不同(『醫話拾零』)'
    prose='威靈仙、柴胡諸藥，原是用根。'
    source=Block(type='para',text=title+prose,page=55,spans=[(len(title),len(title)+1,'red')],
                 memos=[(len(title),'원문 검수')])
    titles={heading_key(title):('catalog','')}
    out,_=format_blocks([source],titles=titles)
    assert [b.text for b in out]==[title,prose]
    assert out[0].type=='heading' and out[0].role=='section' and out[0].layout['alignment']=='CENTER'
    assert not out[0].layout['page_break_before']
    assert out[1].spans==[(0,1,'red')] and out[1].memos==[(0,'원문 검수')]
    assert source.text==title+prose
    unchanged,_=format_blocks([source]);assert len(unchanged)==1
    toc,_=format_blocks([Block(type='para',text=title+'……(42)')],titles=titles)
    assert len(toc)==1 and toc[0].type=='para'


def test_spaced_circle_zero_number_matches_catalog_without_rewriting_title():
    reference='一〇一. 論十劑要義(『要藥分劑』)'
    source='一 ○ 一 、 论 十 剂 要 义 (《 要 药 分 剂 》)'
    titles={heading_key(reference):('catalog','')}
    out,_=format_blocks([Block(type='para',text=source)],titles=titles)
    assert len(out)==1 and out[0].type=='heading'
    assert ''.join(out[0].text.split())==''.join(source.split())
    assert heading_key('一0一、論十劑要義(『要藥分劑』)')==heading_key(reference)
    raw={'text':source,'image_width':1000,'image_height':1000,'words':[]}
    rec={'pdf_page':50,'merged':reading_from_run(raw,50,'horizontal').model_dump()}
    prepared,_=prepare_records([rec],template_headings=titles)
    assert not prepared[0]['merged']['regions'][0]['paragraphs'][0]['continues_from_previous']


def test_omission_marks_have_one_ellipsis_and_spaces_preserving_toc_and_evidence():
    text='甲……乙......丙 … … 丁⋯⋯戊'
    i=text.index('乙');source=Block(type='para',text=text,spans=[(i,i+1,'red')],memos=[(i,'한자 검수')])
    out,_=format_blocks([source,Block(type='para',text='藥名……(42)'),Block(type='para',text='〔疑: 甲……乙〕')])
    assert out[0].text=='甲 … 乙 … 丙 … 丁 … 戊'
    assert out[0].spans==[(4,5,'red')] and out[0].memos==[(4,'한자 검수')]
    assert out[1].text=='藥名……(42)' and out[2].text=='〔疑: 甲……乙〕'
    plain,_=format_blocks([source],punctuation=False);assert plain[0].text==text
    assert source.text==text


def test_spaced_hanja_ocr_is_compacted_without_changing_hanja_or_marks():
    text='葵 , 氣 味 俱 薄 , 淡 滑 爲 陽 , 故 能 利 竅 通 乳 , 消 腫 滑 胎 也 。'
    at=text.index('竅');source=Block(type='para',text=text,spans=[(at,at+1,'red')],memos=[(at,'검토')])
    out,stats=format_blocks([source,Block(type='heading',role='section',text='冬 葵 子'),
                           Block(type='para',role='citation_source',text='1.『綱 目』')])
    assert out[0].text=='葵, 氣味俱薄, 淡滑爲陽, 故能利竅通乳, 消腫滑胎也。'
    new=out[0].text.index('竅');assert out[0].spans==[(new,new+1,'red')] and out[0].memos==[(new,'검토')]
    assert out[1].text=='冬葵子' and out[2].text=='1.『本草綱目』'
    assert stats['removed_character_spaces']>0 and source.text==text
    for value in ('本草 原文 保留 分組','한자 本 草 혼용 띄어쓰기','Chinese OCR text'):
        same,_=format_blocks([Block(type='para',text=value)]);assert same[0].text==value


def test_citation_colons_and_yue_spacing_preserve_inline_prose_and_evidence():
    source=Block(type='para',text='元素 曰 : 其用有三,',spans=[(7,8,'red')],memos=[(7,'검토')])
    blocks,_=format_blocks([source,Block(type='para',role='citation_source',text='12.『本草新編』 : '),
                           Block(type='para',text='元素曰: 原文。 http://example.com 12:30 C:\\資料'),
                           Block(type='para',text='〔疑: 元素 曰 : 原文〕')])
    assert blocks[0].text=='元素曰: 其用有三, '
    assert blocks[0].spans==[(5,6,'red')] and blocks[0].memos==[(5,'검토')]
    assert blocks[1].text=='12.『本草新編』'
    assert blocks[2].text=='元素曰: 原文。 http://example.com 12:30 C:\\資料'
    assert blocks[3].text=='〔疑: 元素 曰 : 原文〕'
    assert source.text=='元素 曰 : 其用有三,'
    unchanged,_=format_blocks([source],punctuation=False)
    assert unchanged[0].text==source.text


def test_printed_notes_are_separate_paragraphs_with_review_marks_preserved():
    text='前文之失. 【注】[1]『珍』 : 『珍珠囊』的簡稱. [2]躓(zhì質):羈絆·羈留. '
    at=text.index('躓');source=Block(type='para',role='quotation',text=text,
                                   spans=[(at,at+1,'red')],memos=[(at,'검토')])
    out,_=format_blocks([source])
    assert [b.text for b in out]==['前文之失.','【注】[1]『珍』: 『珍珠囊』的簡稱.','[2]躓(zhì質):羈絆·羈留. ']
    assert out[2].spans==[(3,4,'red')] and out[2].memos==[(3,'검토')]
    assert source.text==text and all(b.role=='quotation' for b in out)
    plain='本文[1]用法[2]仍是本文。〔疑: 【注】候補〕'
    unchanged,_=format_blocks([Block(type='para',text=plain)])
    assert len(unchanged)==1 and unchanged[0].text==plain


def test_gangmu_is_expanded_only_on_source_title_lines():
    blocks=[Block(type='para',role='citation_source',text='3.『綱目』 : ',spans=[(3,5,'blue')]),
            Block(type='para',role='citation_source',text='『綱目』'),
            Block(type='para',text='本文『綱目』曰: 原文。'),
            Block(type='para',text='3.『本草綱目』')]
    out,_=format_blocks(blocks)
    assert [b.text for b in out]==['3.『本草綱目』','『本草綱目』','本文『綱目』曰: 原文。','3.『本草綱目』']
    assert blocks[0].text=='3.『綱目』 : ' and out[0].spans
    again,_=format_blocks(out)
    assert [b.text for b in again]==[b.text for b in out]


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


def test_contents_reference_page_at_bottom_is_body_not_a_footer():
    from tests._hwpx_fixtures import rec,text_region
    regions=[text_region(f'local-line-{i+1}',[f'藥名本经 {100+i}']) for i in range(6)]
    regions+=[text_region('local-line-7',['卷第十三'],box=(.1,.91,.6,.94)),
              text_region('local-line-8',['273'],box=(.85,.92,.9,.94))]
    result,_=prepare_records([rec(30,regions)],common_layout=True)
    assert any(r['id']=='local-line-8' for r in result[0]['merged']['regions'])
    assert not any(v['text']=='273' for v in result[0]['_document_excluded'])


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
    assert '靑黛 味鹹, 寒, 無毒. 發熱.' in blocks[0].text
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
