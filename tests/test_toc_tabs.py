from hwpx import HwpxDocument
from yeone_ocr.toc_tabs import align_toc_tabs, HP, HH, paragraph_text
from yeone_ocr.hwpx_builder import build_hwpx
from yeone_ocr.blocks import Block
import pytest


@pytest.mark.parametrize('columns',[1,2])
def test_toc_column_width_and_runs_roundtrip(tmp_path,columns):
    doc=HwpxDocument.new()
    section=doc.sections[0]
    col=section.element.find('.//'+HP+'colPr')
    col.set('colCount',str(columns));col.set('sameGap','1200')
    a=doc.add_paragraph('一·藥論……')
    a.add_run('(12)',char_pr_id_ref='0')
    b=doc.add_paragraph('二·治法····(13)')
    prose=doc.add_paragraph('原文……未完')
    isolated=doc.add_paragraph('正文…(123)')
    runs=[r.get('charPrIDRef') for r in a.element.findall(HP+'run')]
    stats=align_toc_tabs(doc)
    assert stats=={'toc_aligned':2,'toc_alignment_skipped':0}
    assert paragraph_text(a.element)=='一·藥論\t(12)'
    assert paragraph_text(b.element)=='二·治法\t(13)'
    assert paragraph_text(prose.element)=='原文……未完'
    assert paragraph_text(isolated.element)=='正文…(123)'
    assert runs==[r.get('charPrIDRef') for r in a.element.findall(HP+'run')]
    header=doc.oxml.headers[0].element
    prop=header.find('.//'+HH+'paraPr[@id="'+a.element.get('paraPrIDRef')+'"]')
    tab=header.find('.//'+HH+'tabPr[@id="'+prop.get('tabPrIDRef')+'"]')
    page=section.element.find('.//'+HP+'pagePr');margin=page.find(HP+'margin')
    width=int(page.get('width'))-sum(int(margin.get(k,'0')) for k in ('left','right','gutter'))
    assert int(tab.find('.//'+HH+'tabItem').get('pos'))==(width-1200*(columns-1))//columns
    before=(len(header.find('.//'+HH+'paraProperties')),len(header.find('.//'+HH+'tabProperties')))
    align_toc_tabs(doc)
    assert before==(len(header.find('.//'+HH+'paraProperties')),len(header.find('.//'+HH+'tabProperties')))
    path=tmp_path/'toc.hwpx';doc.save_to_path(path);doc.close()
    reopened=HwpxDocument.open(path)
    assert any(paragraph_text(p.element)=='一·藥論\t(12)' for p in reopened.paragraphs)
    reopened.close()


def test_unequal_columns_are_not_guessed():
    doc=HwpxDocument.new()
    col=doc.sections[0].element.find('.//'+HP+'colPr')
    col.set('colCount','2');col.set('sameSz','0')
    a=doc.add_paragraph('甲……(1)');doc.add_paragraph('乙……(2)')
    assert align_toc_tabs(doc)=={'toc_aligned':0,'toc_alignment_skipped':2}
    assert paragraph_text(a.element)=='甲……(1)'
    doc.close()


def test_reuse_existing_right_tab_and_split_leader_runs():
    doc=HwpxDocument.new()
    a=doc.add_paragraph('甲……(1)');doc.add_paragraph('乙……(2)')
    align_toc_tabs(doc)
    root=doc.oxml.headers[0].element
    prop=root.find('.//'+HH+'paraPr[@id="'+a.element.get('paraPrIDRef')+'"]')
    tabid=prop.get('tabPrIDRef')
    tab=root.find('.//'+HH+'tabPr[@id="'+tabid+'"]')
    for item in tab.iter(HH+'tabItem'):
        item.set('pos',str(int(item.get('pos'))-20))
    b=doc.add_paragraph('丙…',para_pr_id_ref=prop.get('id'),inherit_style=False)
    b.add_run('…');b.add_run('(3)')
    c=doc.add_paragraph('丁……(4)',para_pr_id_ref=prop.get('id'),inherit_style=False)
    align_toc_tabs(doc)
    assert paragraph_text(b.element)=='丙\t(3)'
    assert b.element.get('paraPrIDRef')==prop.get('id')
    assert c.element.get('paraPrIDRef')==prop.get('id')
    doc.close()


def test_writer_applies_toc_alignment(tmp_path):
    path=tmp_path/'output.hwpx'
    stats=build_hwpx([Block(type='para',text='甲……(1)'),Block(type='para',text='乙……(2)')],str(path))
    assert stats['toc_aligned']==2
    doc=HwpxDocument.open(path)
    assert any(paragraph_text(p.element)=='甲\t(1)' for p in doc.paragraphs)
    doc.close()
