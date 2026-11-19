from zipfile import ZipFile
from xml.etree import ElementTree as ET

import pymupdf

from tests._hwpx_fixtures import HH, HP, rec, text_region
from yeone_ocr import cli
from yeone_ocr.collate.hwpx_edit import paragraph_text
from yeone_ocr.rule_generation import save_generated
from yeone_ocr.state import Work
from ab_ocr.ocr import stable_file_sha256


def test_saved_contents_rule_preserves_outline_and_three_cells_without_replacing_ocr(tmp_path):
    pdf = tmp_path / 'book.pdf'
    with pymupdf.open() as doc:
        doc.new_page()
        doc.new_page()
        doc.new_page()
        doc.save(pdf)
    work = Work(pdf, tmp_path / 'work')
    summary = ['書名', '總 目', '序文 ····· 3', '校訂 ····· 7', '凡例 ····· 9']
    positions = [('目錄', [100, 120, 130, 300]), ('卷第三 ···· 103', [170, 120, 198, 890])]
    cells = [('玉泉123', '丹砂124', '雲母124'), ('石膽125', '朴消126', '白青126'),
             ('紫石127', '赤石127', '滑石128'), ('黃石129', '青石129')]
    # Stored readings may arrive column-major despite a row-major printed page.
    for c, x in enumerate((160, 410, 660)):
        for r, row in enumerate(cells):
            if c < len(row):
                y = 240 + r * 40
                positions.append((row[c], [y, x, y + 28, x + 135]))
    positions.extend([('卷第四', [493, 120, 527, 449]), ('130', [505, 835, 528, 890])])
    detailed = []
    for text, bbox in positions:
        region = text_region('body', [text])
        region['bbox'] = bbox
        detailed.append(region)
    packed = ['次卷目錄', '金屑 130 銀屑 130 水銀 131', '雄黃 131 雌黃 132 礬石 132',
              '石腦 133 石膽 133 紫石 133']
    for n, regions in [(1, [text_region('body', [text]) for text in summary]), (2, detailed),
                       (3, [text_region('body', [text]) for text in packed])]:
        for i, region in enumerate(regions):
            region['id'] = f'local-line-{i}'
        work.save_record(n, rec(n, regions))
    existing = {p: p.read_bytes() for p in work.readings_dir.glob('*.json')}
    sha = stable_file_sha256(pdf)
    rule = save_generated({'inputs': {'pdf': str(pdf), 'pdf_sha256': sha}, 'rule': {
        'schema': 1, 'page_types': {'source_sha256': sha, 'pages': {'목차': [1, 2, 3]}}}}, tmp_path / 'rules')
    rc = cli.main(['build', str(pdf), '--work', str(work.dir), '--layout', 'flow', '--reuse-records',
                   '--no-normalize', '--preset', str(rule), '--out', '목차'])
    assert rc == 0
    with ZipFile(work.out_dir / '목차.hwpx') as z:
        section = ET.fromstring(z.read('Contents/section0.xml'))
        header = ET.fromstring(z.read('Contents/header.xml'))
    ps = {paragraph_text(p): p for p in section.findall(HP + 'p')}
    assert '序文\t3' in ps
    assert '目錄' in ps and ps['目錄'].get('pageBreak') == '1'
    assert '玉泉123\t丹砂124\t雲母124' in ps
    assert '黃石129\t青石129' in ps
    assert '卷第四\t130' in ps
    assert '金屑 130\t銀屑 130\t水銀 131' in ps

    def stops(text):
        para_id = ps[text].get('paraPrIDRef')
        fmt = next(p for p in header.iter(HH + 'paraPr') if p.get('id') == para_id)
        tab = next(t for t in header.iter(HH + 'tabPr') if t.get('id') == fmt.get('tabPrIDRef'))
        return {(t.get('type'), t.get('leader')) for t in tab.iter(HH + 'tabItem')}

    assert stops('序文\t3') == {('RIGHT', 'DASH')}
    assert stops('玉泉123\t丹砂124\t雲母124') == {('LEFT', 'NONE')}
    assert {p: p.read_bytes() for p in existing} == existing
