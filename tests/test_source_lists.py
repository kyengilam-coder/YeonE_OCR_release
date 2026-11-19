from collections import Counter
from hwpx import HwpxDocument
import pymupdf
from yeone_ocr.source_lists import page_lists, pdf_lists, apply_source_lists, HP, HH, paragraph_text
from yeone_ocr.hwpx_builder import build_hwpx
from yeone_ocr.blocks import Block
from yeone_ocr.toc_tabs import align_toc_tabs


def bibliography_lines():
    lines=[]
    for column,x in enumerate((.1,.55)):
        for i in range(4):
            n=column*4+i+1;y=.2+i*.04
            lines.extend([(f'{n}.《本草{n}》',x,y,x+.14,y+.02),('清·作者',x+.2,y,x+.35,y+.02)])
    return lines


def test_pdf_geometry_distinguishes_bibliography_and_prose():
    rows=page_lists(bibliography_lines(),467)
    assert len(rows)==8 and all(r['columns']==2 and r['kind']=='bibliography' for r in rows)
    assert page_lists([(f'{i}.《本草》曰正文',.1,.2+i*.03,.9,.22+i*.03) for i in range(8)],1)==[]


def test_bibliography_reorders_pairs_preserving_text_colors_and_plain_tabs(tmp_path):
    doc=HwpxDocument.new();doc.add_paragraph('文獻及作者')
    before=[]
    for n in (1,5,2,6,3,7,4,8):
        doc.add_paragraph(f'{n}.『本草{n}』');author=doc.add_paragraph('淸·作者')
        author.add_run(str(n),char_pr_id_ref='0')
        before.extend([f'{n}.『本草{n}』',f'淸·作者{n}'])
    doc.add_paragraph('後記')
    result=apply_source_lists(doc,page_lists(bibliography_lines(),1))
    assert result['source_bibliography_rows']==8
    ps=list(doc.sections[0].element.findall(HP+'p'))
    rows=[p for p in ps if '\t' in paragraph_text(p)]
    assert [paragraph_text(p) for p in rows]==[f'{n}.『本草{n}』\t淸·作者{n}' for n in range(1,9)]
    assert Counter(part for p in rows for part in paragraph_text(p).split('\t'))==Counter(before)
    assert all(p.find('.//'+HP+'tab').get('leader')=='0' for p in rows)
    assert rows[0].find('.//'+HP+'colPr').get('colCount')=='2'
    assert ps[-1].find('.//'+HP+'colPr').get('colCount')=='1'
    root=doc.oxml.headers[0].element
    for p in rows:
        pr=root.find('.//'+HH+'paraPr[@id="'+p.get('paraPrIDRef')+'"]')
        tab=root.find('.//'+HH+'tabPr[@id="'+pr.get('tabPrIDRef')+'"]')
        assert all(t.get('leader')=='NONE' for t in tab.iter(HH+'tabItem'))
    assert apply_source_lists(doc,page_lists(bibliography_lines(),1))['source_bibliography_rows']==0
    path=tmp_path/'bibliography.hwpx';doc.save_to_path(path);doc.close()
    reopened=HwpxDocument.open(path)
    assert not list(reopened.validate().issues)
    reopened.close()


def test_unmatched_bibliography_is_not_reordered():
    doc=HwpxDocument.new()
    for n in (1,5,2,6):doc.add_paragraph(f'{n}.『別書』');doc.add_paragraph('作者')
    before=[paragraph_text(p.element) for p in doc.paragraphs]
    assert apply_source_lists(doc,page_lists(bibliography_lines(),1))['source_bibliography_rows']==0
    assert before==[paragraph_text(p.element) for p in doc.paragraphs]
    doc.close()


def test_source_single_column_and_conflicting_evidence_are_conservative():
    lines=[(f'Entry{i}……({i})',.1,.2+i*.04,.8,.22+i*.04) for i in range(4)]
    rows=page_lists(lines,1)
    assert len(rows)==4 and all(r['columns']==1 for r in rows)
    doc=HwpxDocument.new()
    for text,*_ in lines:doc.add_paragraph(text)
    result=apply_source_lists(doc,rows)
    assert result['source_list_columns']==1
    conflicting=[dict(r,columns=2) for r in rows]
    before=[paragraph_text(p.element) for p in doc.paragraphs]
    assert apply_source_lists(doc,rows+conflicting)['source_list_columns']==0
    assert before==[paragraph_text(p.element) for p in doc.paragraphs]
    doc.close()


def test_image_folder_can_supply_ocr_evidence(tmp_path):
    from PIL import Image
    folder=tmp_path/'scans';folder.mkdir()
    Image.new('RGB',(200,300),'white').save(folder/'1.png')
    assert pdf_lists(folder)==[]


def test_writer_uses_source_pdf_two_columns(tmp_path):
    pdf=pymupdf.open();page=pdf.new_page(width=600,height=800);texts=[]
    for col,x in enumerate((60,330)):
        for i in range(4):
            text=f'Entry{col*4+i}........({i+1})';texts.append(text)
            page.insert_text((x,160+i*24),text)
    path=tmp_path/'source.pdf';pdf.save(path);pdf.close()
    evidence=pdf_lists(path)
    assert len(evidence)==8 and all(r['columns']==2 for r in evidence)
    output=tmp_path/'output.hwpx'
    stats=build_hwpx([Block(type='para',text=t) for t in texts],str(output),source_pdf=path)
    assert stats['source_list_columns']==1 and stats['toc_aligned']==8
    doc=HwpxDocument.open(output)
    assert any(c.get('colCount')=='2' for c in doc.sections[0].element.iter(HP+'colPr'))
    doc.close()


def test_attached_index_numbers_are_isolated_reordered_and_tabbed_before_prose(tmp_path):
    from yeone_ocr.blocks import assemble_blocks
    from yeone_ocr.document_editing import prepare_records

    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    regions = []
    for i in range(4):
        for column, x in enumerate((60, 330)):
            number = i + 1 + column * 4
            text = f'Entry{number}({number})'
            page.insert_text((x, 160 + 24*i), text)
            regions.append(dict(id=f'local-line-{number}', kind='본문', bbox=[0, 0, 1000, 1000],
                                paragraphs=[dict(cont=True, columns=[dict(text=text)])]))
    source = tmp_path/'index.pdf'
    pdf.save(source)
    records = [dict(file=source.name, pdf_page=1, merged=dict(pdf_page=1, regions=regions))]
    prepared, _ = prepare_records(records, source_pdf=pdf)
    pdf.close()
    assert all(r['paragraphs'][0]['cont'] for r in regions)
    blocks = assemble_blocks(prepared, layout='flow', edit_paragraphs=True)
    output = tmp_path/'index.hwpx'
    stats = build_hwpx(blocks, str(output), source_pdf=source)
    assert stats['source_index_pages'] == 1 and stats['toc_aligned'] == 8
    with HwpxDocument.open(output) as doc:
        rows = [p.element for p in doc.paragraphs if paragraph_text(p.element).startswith('Entry')]
        assert [paragraph_text(p) for p in rows] == [f'Entry{n}\t({n})' for n in range(1, 9)]
        assert rows[0].find('.//'+HP+'colPr').get('colCount') == '2'
        assert rows[4].get('columnBreak') == '1'
        header = doc.oxml.headers[0].element
        for p in rows:
            assert p.find('.//'+HP+'tab').get('leader') == '0'
            pr = header.find('.//'+HH+'paraPr[@id="'+p.get('paraPrIDRef')+'"]')
            tab = header.find('.//'+HH+'tabPr[@id="'+pr.get('tabPrIDRef')+'"]')
            assert tab.get('autoTabRight') == '1'
            assert {t.get('leader') for t in tab.iter(HH+'tabItem')} == {'NONE'}


def test_scanned_pdf_uses_existing_ocr_boxes(tmp_path):
    pdf=pymupdf.open();pdf.new_page();path=tmp_path/'scan.pdf';pdf.save(path);pdf.close()
    regions=[]
    for col,x in enumerate((100,550)):
        for i in range(4):
            regions.append(dict(id=f'r{col}{i}',kind='본문',bbox=[200+i*40,x,220+i*40,x+300],
                                paragraphs=[dict(columns=[dict(text=f'藥{col}{i}……(1)')])]))
    records=[dict(merged=dict(pdf_page=1,regions=regions))]
    rows=pdf_lists(path,records)
    assert len(rows)==8 and all(r['columns']==2 for r in rows)


def test_pdf_visible_gaps_choose_mixed_leaders_independently_of_list_kind(tmp_path):
    pdf=pymupdf.open();page=pdf.new_page(width=600,height=800)
    for n in range(4):
        y=160+n*30
        page.insert_text((60,y),f'Entry{n}')
        page.insert_text((220,y),f'({n+1})')
        if n<2:
            # Vector dots are deliberately absent from the PDF text layer.
            for x in range(105,214,7):page.draw_circle((x,y-2),.65,color=(0,0,0),fill=(0,0,0))
    source=tmp_path/'mixed.pdf';pdf.save(source);pdf.close()
    evidence=pdf_lists(source)
    assert [r['leader'] for r in evidence]==['DASH','DASH','NONE','NONE']
    target=tmp_path/'mixed.hwpx'
    build_hwpx([Block(type='para',text=f'Entry{n}……({n+1})') for n in range(4)],str(target),source_pdf=source)
    doc=HwpxDocument.open(target)
    ps=[p.element for p in doc.paragraphs if paragraph_text(p.element).startswith('Entry')]
    assert [p.find('.//'+HP+'tab').get('leader') for p in ps]==['3','3','0','0']
    root=doc.oxml.headers[0].element
    for p,expected in zip(ps,('DASH','DASH','NONE','NONE')):
        pr=root.find('.//'+HH+'paraPr[@id="'+p.get('paraPrIDRef')+'"]')
        tab=root.find('.//'+HH+'tabPr[@id="'+pr.get('tabPrIDRef')+'"]')
        assert {t.get('leader') for t in tab.iter(HH+'tabItem')}=={expected}
    # Saving later without PDF evidence must not convert invisible tabs back.
    align_toc_tabs(doc)
    assert [p.find('.//'+HP+'tab').get('leader') for p in ps]==['3','3','0','0']
    doc.close()


def test_bibliography_can_use_dotted_or_plain_tabs_from_its_separator():
    lines=bibliography_lines()
    lines=[(text+'……' if text.startswith(('1.','2.','3.','4.')) else text,x,y,r,b)
           for text,x,y,r,b in lines]
    evidence=page_lists(lines,1)
    assert [r['leader'] for r in evidence]==['DASH']*4+['NONE']*4
    doc=HwpxDocument.new()
    for n in range(1,9):doc.add_paragraph(f'{n}.『本草{n}』');doc.add_paragraph('淸·作者')
    assert apply_source_lists(doc,evidence)['source_bibliography_rows']==8
    tabs=[t for p in doc.paragraphs for t in p.element.iter(HP+'tab')]
    assert [t.get('leader') for t in tabs]==['3']*4+['0']*4
    doc.close()


def test_confirmed_rows_with_unknown_leader_use_reported_dot_default():
    doc=HwpxDocument.new()
    for n in range(4):doc.add_paragraph(f'Entry{n}……({n})')
    evidence=page_lists([(f'Entry{n}……({n})',.1,.2+n*.04,.8,.22+n*.04) for n in range(4)],1)
    for row in evidence:row['leader']=None
    stats=align_toc_tabs(doc,evidence)
    assert stats['toc_aligned']==4
    assert {r['reason'] for r in stats['toc_findings']}=={'default-leader'}
    assert all(p.element.find('.//'+HP+'tab').get('leader')=='3' for p in doc.paragraphs if p.text)
    doc.close()


def test_visible_solid_rule_is_not_mistaken_for_dots():
    from yeone_ocr.source_lists import visible_gap_shape
    pdf=pymupdf.open();page=pdf.new_page(width=600,height=800)
    page.draw_line((100,170),(200,170))
    assert visible_gap_shape(page,(.15,.2,.35,.23)) is None
    assert visible_gap_shape(page,(.15,.3,.35,.33))=='NONE'
    pdf.close()
