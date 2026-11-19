from collections import Counter
import json
from pathlib import Path

from hwpx import HwpxDocument
from yeone_ocr.source_lists import page_lists, apply_source_lists, HP, HH
from yeone_ocr.collate.hwpx_edit import paragraph_text
from yeone_ocr.list_matching import match_toc_paragraphs
from yeone_ocr.list_review import write_list_review


def test_fused_columns_use_separate_right_auto_tabs_and_preserve_reading_order():
    from yeone_ocr.toc_tabs import align_toc_tabs

    rows = page_lists([(f'{label}{i}……({num + i} )', x, .2+i*.05, x+.29, .22+i*.05)
                      for label, num, x in [('山', 1, .1), ('石', 10, .55)] for i in range(4)], 9)
    doc = HwpxDocument.new()
    for i in range(4):
        doc.add_paragraph(f'山{i}……({1+i}) 石{i}……({10+i})')
    apply_source_lists(doc, rows)
    assert align_toc_tabs(doc, rows)['toc_aligned'] == 8
    found = [p.element for p in doc.paragraphs if p.text]
    assert [paragraph_text(p).split('\t')[0] for p in found] == [f'{label}{i}' for label in '山石' for i in range(4)]
    header = doc.oxml.headers[0].element
    for p in found:
        inline = p.find('.//'+HP+'tab')
        assert int(inline.get('width')) > 0
        pp = header.find('.//'+HH+'paraPr[@id="'+p.get('paraPrIDRef')+'"]')
        assert header.find('.//'+HH+'tabPr[@id="'+pp.get('tabPrIDRef')+'"]').get('autoTabRight') == '1'
    doc.close()


def bibliography():
    lines=[]
    for col,x in enumerate((.10,.55)):
        for i in range(4):
            n=col*4+i+1;y=.2+i*.05
            lines.extend([(f'{n}.《本草藥書{n}》',x,y,x+.13,y+.02),('清·作者',x+.22,y,x+.36,y+.02)])
    return page_lists(lines,20)


def document(numbers):
    doc=HwpxDocument.new()
    for n in numbers:doc.add_paragraph(f'{n}.『本草藥書{n}』');doc.add_paragraph('淸·作者')
    doc.add_paragraph('後記')
    return doc


def test_one_unresolved_row_does_not_hold_other_rows():
    rows=bibliography();rows[2]['leader']=None
    doc=document((1,5,2,6,3,7,4,8))
    before=Counter(c for p in doc.paragraphs for c in p.text if not c.isspace())
    stats=apply_source_lists(doc,rows)
    assert stats['source_bibliography_rows']==7
    assert stats['source_list_unresolved']==1
    assert stats['source_list_missing']==0
    assert any(p.text=='3.『本草藥書3』' for p in doc.paragraphs)
    assert Counter(c for p in doc.paragraphs for c in p.text if not c.isspace())==before
    assert stats['source_list_findings'][0]['source_page']==20
    doc.close()


def test_missing_entry_is_reported_without_inserting_source_ocr(tmp_path):
    doc=document((1,2,4,5,6,7,8))
    stats=apply_source_lists(doc,bibliography())
    assert stats['source_bibliography_rows']==7
    missing=[r for r in stats['source_list_findings'] if r['reason']=='missing-source-entry']
    assert [r['source_number'] for r in missing]==[3]
    assert not any(p.text.startswith('3.') for p in doc.paragraphs)
    html,report=write_list_review(tmp_path/'result.hwpx',stats)
    assert json.loads(Path(report).read_text(encoding='utf-8'))['findings']==missing
    assert 'PDF' in Path(html).read_text(encoding='utf-8')
    doc.close()


def test_column_gap_and_tab_position_come_from_source_geometry():
    rows=bibliography();doc=document(range(1,9))
    stats=apply_source_lists(doc,rows)
    assert stats['source_bibliography_rows']==8
    section=doc.sections[0].element
    page=section.find('.//'+HP+'pagePr');margin=page.find(HP+'margin')
    body=int(page.get('width'))-sum(int(margin.get(k,'0')) for k in ('left','right','gutter'))
    first=next(p.element for p in doc.paragraphs if p.text.startswith('1.'))
    gap=int(first.find('.//'+HP+'colPr').get('sameGap'))
    assert gap==round(body*rows[0]['gap_fraction']) and gap!=1200
    root=doc.oxml.headers[0].element
    pr=root.find('.//'+HH+'paraPr[@id="'+first.get('paraPrIDRef')+'"]')
    tab=root.find('.//'+HH+'tabPr[@id="'+pr.get('tabPrIDRef')+'"]')
    pos=int(tab.find('.//'+HH+'tabItem').get('pos'))
    assert pos==round(((body-gap)//2)*rows[0]['tab_fraction'])
    doc.close()


def test_neighbour_match_tolerates_ocr_error_without_rewriting_text():
    labels=['甲乙丙丁戊己','庚辛壬癸甲乙','丙丁戊己庚辛','壬癸甲乙丙丁']
    rows=page_lists([(t+'……(1)',.1,.2+i*.05,.8,.22+i*.05) for i,t in enumerate(labels)],1)
    doc=HwpxDocument.new();ps=[]
    for i,t in enumerate(labels):ps.append(doc.add_paragraph((t if i!=1 else '庚辛壬癸甲申')+'……(1)').element)
    matches=match_toc_paragraphs(ps,rows)
    assert len(matches)==4 and matches[ps[1]]['method']=='bracketed-label'
    assert paragraph_text(ps[1])=='庚辛壬癸甲申……(1)'
    doc.close()


def test_ambiguous_source_neighbours_are_not_guessed():
    rows=page_lists([(t+'……(1)',.1,.2+i*.05,.8,.22+i*.05)
                     for i,t in enumerate(['甲乙丙丁戊己','甲乙丙丁戊庚','壬癸甲乙丙丁','庚辛壬癸甲乙'])],1)
    doc=HwpxDocument.new();p=doc.add_paragraph('甲乙丙丁戊辛……(1)').element
    assert not match_toc_paragraphs([p],rows)
    doc.close()


def test_mixed_one_and_two_column_source_page_is_not_discarded():
    lines=[(f'全文{i}……(1)',.1,.1+i*.05,.9,.12+i*.05) for i in range(4)]
    for col,x in enumerate((.1,.55)):
        lines.extend((f'條目{col}{i}……(2)',x,.5+i*.05,x+.35,.52+i*.05) for i in range(4))
    rows=page_lists(lines,1)
    assert len(rows)==12
    assert [r['columns'] for r in rows]==[1]*4+[2]*8


def test_short_label_needs_both_neighbours_and_same_page_target():
    labels=['甲乙','木瓜','丙丁','戊己']
    rows=page_lists([(t+'……(1)',.1,.2+i*.05,.8,.22+i*.05) for i,t in enumerate(labels)],1)
    doc=HwpxDocument.new()
    ps=[doc.add_paragraph(t+'……(1)').element for t in ['甲乙','本瓜','丙丁','戊己']]
    assert match_toc_paragraphs(ps,rows)[ps[1]]['method']=='bracketed-label'
    assert ps[1] not in match_toc_paragraphs(ps[1:],rows)
    wrong=doc.add_paragraph('本瓜……(2)').element
    assert wrong not in match_toc_paragraphs([ps[0],wrong,ps[2],ps[3]],rows)
    doc.close()


def test_body_citations_do_not_become_author_lists():
    doc=HwpxDocument.new()
    for n in range(1,9):
        doc.add_paragraph(f'{n}.『本草藥書{n}』')
        doc.add_paragraph('此藥治病當審其證')
    before=[p.text for p in doc.paragraphs]
    stats=apply_source_lists(doc,bibliography())
    assert stats['source_bibliography_rows']==0
    assert [p.text for p in doc.paragraphs]==before
    doc.close()


def test_separate_dotted_page_cells_are_linked_to_labels():
    lines=[]
    for i in range(4):
        y=.2+i*.05
        lines.extend([(f'條目{i}',.1,y,.3,y+.02),('……(3)',.5,y,.8,y+.02)])
    rows=page_lists(lines,1)
    assert len(rows)==4
    assert all(r['target']=='3' and r['target_offset']==2 for r in rows)


def test_original_column_settings_are_restored_after_list():
    from yeone_ocr.source_lists import _columns
    doc=HwpxDocument.new()
    first=doc.add_paragraph('前文').element
    _columns(first,2,777)
    for n in range(1,9):
        doc.add_paragraph(f'{n}.『本草藥書{n}』')
        doc.add_paragraph('淸·作者')
    after=doc.add_paragraph('後記').element
    apply_source_lists(doc,bibliography())
    restored=after.find('.//'+HP+'colPr')
    assert restored.get('colCount')=='2' and restored.get('sameGap')=='777'
    doc.close()
