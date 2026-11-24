from pathlib import Path
from zipfile import ZipFile

import pymupdf
from lxml import etree

from yeone_ocr.source_layout_hwpx import HP, measured_rows, write_source_hwpx


def test_native_source_layout_preserves_tabs_blank_pages_and_paper_size(tmp_path):
    target = tmp_path / 'source-layout.hwpx'
    with pymupdf.open() as source:
        page = source.new_page(width=420, height=595)
        page.insert_text((40, 80), 'Left column')
        page.insert_text((170, 80), 'Middle column')
        page.insert_text((295, 80), 'Right column')
        page.draw_line((35, 94), (385, 94), width=.8)
        source.new_page(width=420, height=595)
        page = source.new_page(width=595, height=420)
        page.insert_text((40, 80), 'Landscape body')

        assert [f.text for f in measured_rows(source[0])[0][1]] == ['Left column', 'Middle column', 'Right column']
        records = [{'pdf_page': number, 'merged': {'regions': []}} for number in (1, 2, 3)]
        assert write_source_hwpx(source, source, records, target) == 3

    with ZipFile(target) as z:
        assert z.testzip() is None
        first = etree.fromstring(z.read('Contents/section0.xml'))
        second = etree.fromstring(z.read('Contents/section1.xml'))
        assert len(first.findall('.//' + HP + 'tab')) == 2
        assert len(first.findall('.//' + HP + 'line')) == 1
        assert len(first.xpath('./hp:p[@pageBreak="1"]', namespaces={'hp': HP[1:-1]})) == 1
        assert first.find('.//' + HP + 'pagePr').get('width') != second.find('.//' + HP + 'pagePr').get('width')
        assert any(t.text == 'Landscape body' for t in second.iter(HP + 't'))
