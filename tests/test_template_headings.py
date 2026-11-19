import copy
from zipfile import ZipFile
from xml.etree import ElementTree as ET

from hwpx import HwpxDocument
from yeone_ocr.presets import DATA, load_preset
from yeone_ocr.template_headings import read_template_headings, HH, HP
from yeone_ocr.citation_layout import heading_key
from yeone_ocr.document_editing import prepare_records, clean_output_blocks
from yeone_ocr.blocks import assemble_blocks
from yeone_ocr.structure import apply_structure
from yeone_ocr.common_layout import apply_common_layout
from yeone_ocr.hwpx_template import build_hwpx_template
from tests._hwpx_fixtures import rec, text_region


def template(path):
    doc=HwpxDocument.new(); h=doc.oxml.headers[0]
    center=h.ensure_paragraph_format(base_para_pr_id=0,alignment='CENTER')
    for name,size,color,font,texts in [
        ('제목-본초명',16,'#0000FF','Source Han Serif Heavy',['桂(肉桂·桂枝)','牛膝']),
        ('제목-본초명(소)',14,'#6666FF','Source Han Sans Heavy',['桂','桂枝'])]:
        c=doc.oxml.ensure_run_style(size=size,color=color,font=font)
        st=h.ensure_style(name,style_type='PARA',para_pr_id_ref=center,char_pr_id_ref=c)
        for text in texts[:1]:doc.add_paragraph(text,style_id_ref=st,char_pr_id_ref=c,para_pr_id_ref=center)
        if name=='제목-본초명': main=(st,c)
        else:
            doc.add_paragraph(texts[1],style_id_ref=st,char_pr_id_ref=c,para_pr_id_ref=center)
    # Place a distinct main title after both subtitles.
    doc.add_paragraph('牛膝',style_id_ref=main[0],char_pr_id_ref=main[1],para_pr_id_ref=center)
    doc.save_to_path(str(path));doc.close()


def test_template_subtitle_hierarchy_fonts_and_history(tmp_path):
    p=tmp_path/'template.hwpx';template(p);rule=load_preset(DATA/'book_rule_yaolun.json')
    titles=read_template_headings(p,rule)
    assert titles[heading_key('桂')]==('subsection',heading_key('桂(肉桂·桂枝)'))
    texts=['桂(肉桂·桂枝)','桂','1.《本草衍義》：原文。','桂枝','2.《本經》：次文。','牛膝','桂']
    records=[rec(i+5,[text_region(f'local-line-{i}',[t])]) for i,t in enumerate(texts)]
    before=copy.deepcopy(records)
    prepared,_=prepare_records(records,common_layout=True,citation=rule.citation,template_headings=titles)
    blocks=assemble_blocks(prepared,layout='flow',grades=False,legend=False,memo_mode='none',edit_paragraphs=True)
    blocks,_=apply_structure(blocks,rule);blocks=clean_output_blocks(blocks);blocks,_=apply_common_layout(blocks)
    assert records==before
    heads=[b for b in blocks if b.type=='heading']
    assert [(b.text,b.role) for b in heads]==[(texts[0],'section'),('桂','subsection'),('桂枝','subsection'),('牛膝','section')]
    assert not any(b.layout.get('page_break_before') for b in heads)
    assert sum(b.role=='citation_source' for b in blocks)==2
    out=tmp_path/'out.hwpx';build_hwpx_template(blocks,str(out),str(p),preset=rule,edited=True,simple=True)
    with ZipFile(p) as z: original=ET.fromstring(z.read('Contents/header.xml'))
    with ZipFile(out) as z:
        header=ET.fromstring(z.read('Contents/header.xml'));section=ET.fromstring(z.read('Contents/section0.xml'))
    original_styles={s.get('name'):s for s in original.iter(HH+'style')}
    output_styles={s.get('name'):s for s in header.iter(HH+'style')}
    original_chars={s.get('id'):s for s in original.iter(HH+'charPr')}
    chars={s.get('id'):s for s in header.iter(HH+'charPr')}
    for name in ['제목-본초명','제목-본초명(소)']:
        a=original_styles[name].get('charPrIDRef');b=output_styles[name].get('charPrIDRef')
        assert ET.tostring(original_chars[a])==ET.tostring(chars[b])
    nonempty=[p for p in section.findall(HP+'p') if ''.join(t.text or '' for t in p.iter(HP+'t'))]
    small=output_styles['제목-본초명(소)']
    assert nonempty[1].get('styleIDRef')==small.get('id')
    assert nonempty[1].find(HP+'run').get('charPrIDRef')==small.get('charPrIDRef')


def test_unknown_subtitle_parent_is_not_guessed(tmp_path):
    p=tmp_path/'template.hwpx';template(p);rule=load_preset(DATA/'book_rule_yaolun.json')
    records=[rec(7,[text_region('local-line-1',['桂'])])]
    prepared,_=prepare_records(records,headings={'section':['桂']},template_headings=read_template_headings(p,rule))
    assert not prepared[0].get('_common_layout_regions')


def test_catalog_only_herb_requires_source_layout_and_first_citation(tmp_path):
    p=tmp_path/'catalog.hwpx';doc=HwpxDocument.new();h=doc.oxml.headers[0]
    st=h.ensure_style('목차-2',style_type='PARA',para_pr_id_ref=0,char_pr_id_ref=0)
    doc.add_paragraph('百部(百部)',style_id_ref=st);doc.save_to_path(str(p));doc.close()
    rule=load_preset(DATA/'book_rule_yaolun.json');titles=read_template_headings(p,rule)
    assert titles[heading_key('百部')]==('catalog','')
    regions=[text_region('local-line-1',['各论'],box=(.696,.065,.737,.081)),
             text_region('local-line-2',['一、植物药类'],box=(.735,.065,.849,.080)),
             text_region('local-line-3',['149'],box=(.876,.067,.907,.081)),
             text_region('local-line-4',['百部'],box=(.458,.120,.572,.144)),
             text_region('local-line-5',['1.《纲目》：百部亦天门冬之类。'],box=(.144,.166,.918,.181)),
             text_region('local-line-6',['正文。'],box=(.1,.2,.9,.23))]
    original=rec(165,regions);before=copy.deepcopy(original)
    out,_=prepare_records([original,rec(167,regions),rec(169,regions)],common_layout=True,citation=rule.citation,template_headings=titles)
    assert original==before
    assert len(out[0]['_document_excluded'])==3
    assert out[0]['_common_layout_regions']['local-line-4']['kind']=='section'
    assert out[0]['merged']['regions'][0]['id']=='local-line-4'
    body=rec(166,[text_region('local-line-1',['百部'],box=(.1,.12,.2,.14)),regions[-2],regions[-1]])
    result,_=prepare_records([body],common_layout=True,citation=rule.citation,template_headings=titles)
    assert 'local-line-1' not in result[0].get('_common_layout_regions',{})


def test_parenthetical_attached_herb_restores_left_aligned_leaf_title(tmp_path):
    p=tmp_path/'ginseng.hwpx';doc=HwpxDocument.new();h=doc.oxml.headers[0]
    style=h.ensure_style('제목-본초명',style_type='PARA',para_pr_id_ref=0,char_pr_id_ref=0)
    doc.add_paragraph('人蔘(附：參須, 人蔘葉)',style_id_ref=style);doc.save_to_path(str(p));doc.close()
    rule=load_preset(DATA/'book_rule_yaolun.json');titles=read_template_headings(p,rule)
    records=[rec(69,[text_region('local-line-1',['人参（附：参须、人参叶）'],box=(.36,.6,.64,.64))]),
             rec(75,[text_region('local-line-20',['相宜。'],box=(.104,.431,.156,.445)),
                     text_region('local-line-21',['人参叶'],box=(.140,.453,.203,.469)),
                     text_region('local-line-22',['1.《本草从新》：大苦大寒。'],box=(.139,.475,.852,.491))])]
    before=copy.deepcopy(records)
    out,_=prepare_records(records,common_layout=True,citation=rule.citation,template_headings=titles)
    assert records==before and out[1]['_common_layout_regions']['local-line-21']['kind']=='subsection'
    assert out[1]['_common_layout_regions']['local-line-21']['evidence']=='template-member-and-source-citation'
    assert 'local-line-21' not in out[1]['_document_continuations']
    isolated,_=prepare_records(records[1:],common_layout=True,citation=rule.citation,template_headings=titles)
    assert 'local-line-21' not in isolated[0].get('_common_layout_regions',{})
