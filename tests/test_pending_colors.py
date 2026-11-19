from copy import deepcopy
from zipfile import ZipFile
from xml.etree import ElementTree as ET
from yeone_ocr.blocks import Block
from yeone_ocr.normalization import normalize_blocks,NormalizationOptions
from yeone_ocr.document_editing import format_blocks
from yeone_ocr.hwpx_builder import build_hwpx
from yeone_ocr.common_layout import apply_common_layout

HP='{http://www.hancom.co.kr/hwpml/2011/paragraph}'
HH='{http://www.hancom.co.kr/hwpml/2011/head}'


def test_pending_conversion_targets_red_without_changing_source_or_adding_markers(tmp_path):
    original=[Block(type='para',text='陶云，次句。',spans=[(0,2,'green')])];before=deepcopy(original)
    options=NormalizationOptions(traditional=False,yun=True,korean_forms=False)
    blocks,report=normalize_blocks(original,options=options)
    blocks,_=format_blocks(blocks)
    assert original==before
    assert blocks[0].text=='陶云, 次句. '
    marked=[blocks[0].text[a:b] for a,b,k in blocks[0].spans if k=='pending']
    assert marked and all('云' in s for s in marked)
    target=tmp_path/'pending.hwpx';build_hwpx(blocks,str(target),edited=True)
    with ZipFile(target) as z:
        header=ET.fromstring(z.read('Contents/header.xml'));sec=ET.fromstring(z.read('Contents/section0.xml'))
        colors={n.get('id'):n.get('textColor') for n in header.iter(HH+'charPr')}
        runs=[r for r in sec.iter(HP+'run') if '云' in ''.join(n.text or '' for n in r.iter(HP+'t'))]
        assert runs and all(colors[r.get('charPrIDRef')]=='#C00000' for r in runs)


def test_explicit_black_export_disables_pending_marking():
    blocks,report=normalize_blocks([Block(type='para',text='云')],options=NormalizationOptions(mark_pending=False))
    assert report['review'] and not blocks[0].spans


def test_table_pending_conversion_is_colored(tmp_path):
    blocks,_=normalize_blocks([Block(type='table',rows=[['云']])])
    assert blocks[0].cell_spans[(0,0)]==[(0,1,'pending')]
    target=tmp_path/'table.hwpx';build_hwpx(blocks,str(target),edited=True)
    with ZipFile(target) as z:
        sec=ET.fromstring(z.read('Contents/section0.xml'));header=ET.fromstring(z.read('Contents/header.xml'))
        colors={p.get('id'):p.get('textColor') for p in header.iter(HH+'charPr')}
        run=next(r for r in sec.iter(HP+'run') if ''.join(n.text or '' for n in r.findall(HP+'t'))=='云')
        assert colors[run.get('charPrIDRef')]=='#C00000'


def test_pending_heading_keeps_heading_size_and_bold(tmp_path):
    blocks,_=normalize_blocks([Block(type='heading',text='云',level=2)])
    blocks,_=apply_common_layout(blocks)
    target=tmp_path/'title.hwpx';build_hwpx(blocks,str(target),edited=True)
    with ZipFile(target) as z:
        root=ET.fromstring(z.read('Contents/section0.xml'));header=ET.fromstring(z.read('Contents/header.xml'))
        styles={s.get('id'):s for s in header.iter(HH+'charPr')}
        run=next(r for r in root.iter(HP+'run') if ''.join(n.text or '' for n in r.iter(HP+'t'))=='云')
        style=styles[run.get('charPrIDRef')]
        assert style.get('height')=='1400' and style.get('textColor')=='#C00000'
        assert style.find(HH+'bold') is not None
