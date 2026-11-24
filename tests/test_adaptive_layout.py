import pymupdf
import pytest

from yeone_ocr.adaptive_layout import analyze_records, apply_index_geometry
from yeone_ocr.schema import Column, PageReading, Paragraph, Region


def line(ident, text, bbox):
    return Region(id=f'local-line-{ident}', kind='본문', bbox=bbox, reading_direction='가로',
                  paragraphs=[Paragraph(columns=[Column(text=text)])])


def record(number, regions):
    return {'pdf_page': number, 'mode': 'horizontal_zh',
            'merged': PageReading(pdf_page=number, regions=regions).model_dump()}


def test_infers_heading_and_notes_and_reads_article_columns_in_order():
    rows = [line('heading', 'The field survey', [120, 260, 165, 740])]
    rows += [line(f'{side}-{i}', f'{side} column sentence number {i}.',
                  [230+65*i, x, 252+65*i, x+340])
             for i in range(6) for side, x in [('left', 100), ('right', 560)]]
    rows += [line('note1', '① Field note', [765, 100, 780, 390]),
             line('note2', 'A continuation of the note', [785, 100, 799, 400])]
    item = record(1, rows)
    with pymupdf.open() as doc:
        doc.new_page()
        doc.set_toc([[1, 'The field survey', 1]])
        types = analyze_records([item], doc)
    assert types['각주'] == [1]
    reading = PageReading.model_validate(item['merged'])
    assert reading.regions[0].title_text == 'The field survey'
    ordered = [r.id for r in reading.regions if r.id.startswith(('local-line-left', 'local-line-right'))]
    assert ordered == [f'local-line-{side}-{i}' for side in ('left', 'right') for i in range(6)]
    assert sum(r.kind == '소주' for r in reading.regions) == 2


def test_scan_only_index_uses_three_columns_carried_by_ocr_geometry():
    rows = [line('index', '索引', [107, 460, 126, 540])]
    rows += [line(f'{col}-{i}', f'題目{col}{i}({10*col+i})', [185+62*i, x, 207+62*i, x+118])
             for i in range(8) for col, x in enumerate([95, 392, 683])]
    rows.append(line('wrapped-label', 'A longer index label whose number is on the next line', [380, 392, 400, 662]))
    r = record(1, rows)
    with pymupdf.open() as doc:
        doc.new_page()
        types = analyze_records([r], doc)
    assert types['색인'] == [1]
    apply_index_geometry([r], types)
    assert r['_source_index_page']
    assert r['_automatic_layout']['column_count'] == 3
    assert r['_common_layout_regions']['local-line-index']['source_index_title']
    assert r['_common_layout_regions']['local-line-wrapped-label']['source_index']['column'] == 1
    assert [r['_common_layout_regions'][f'local-line-{col}-0']['source_index']['column'] for col in range(3)] == [0, 1, 2]


@pytest.mark.parametrize('scanned', [False, True])
def test_confirmed_grid_becomes_an_editable_table_without_using_unreviewed_source_text(tmp_path, scanned):
    import json
    from zipfile import ZipFile
    from yeone_ocr import cli
    from yeone_ocr.state import Work

    path = tmp_path / 'report.pdf'
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=400)
        for x in (40, 200, 360):
            page.draw_line((x, 80), (x, 240))
        for y in (80, 160, 240):
            page.draw_line((40, y), (360, y))
        rows = []
        for i, y in enumerate((110, 190)):
            for j, x in enumerate((55, 215)):
                page.insert_text((x, y), 'unreviewed text')
                rows.append(line(f'{i}-{j}', f'Corrected {i}-{j}', [
                    int(2.5*(y-10)), int(2.5*x), int(2.5*(y+3)), int(2.5*(x+100))]))
        r = record(1, rows)
        saved = record(1, rows)
        saved['file'] = path.name
        if scanned:
            image = page.get_pixmap(dpi=144).tobytes('png')
            doc.delete_page(0)
            page = doc.new_page(width=400, height=400)
            page.insert_image(page.rect, stream=image)
        doc.save(path)
        analyze_records([r], doc)
        table, = PageReading.model_validate(r['merged']).regions
        assert table.kind == '단순표'
        assert [row.cells for row in table.table_rows] == [['Corrected 0-0', 'Corrected 0-1'],
                                                           ['Corrected 1-0', 'Corrected 1-1']]
    work = Work(str(path), str(tmp_path / 'work'))
    work.save_record(1, saved)
    assert cli.main(['build', str(path), '--work', str(work.dir), '--reuse-records', '--layout', 'auto',
                     '--no-normalize', '--out', 'adapted']) == 0
    with ZipFile(work.out_dir / 'adapted.hwpx') as z:
        xml = z.read('Contents/section0.xml')
        assert b'<hp:tbl ' in xml and b'Corrected 1-1' in xml and b'unreviewed text' not in xml
    report = json.loads((work.out_dir / 'adapted_quality.json').read_text(encoding='utf-8'))
    assert report['automatic_layout'][0]['tables'] == 1
