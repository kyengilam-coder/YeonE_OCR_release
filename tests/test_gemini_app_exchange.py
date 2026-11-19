import json
import re
from zipfile import ZipFile

import pymupdf
import pytest

from yeone_ocr import cli
from yeone_ocr.gemini_app_exchange import export_batch, import_batch
from yeone_ocr.state import Work, book_counts


@pytest.fixture
def task(tmp_path):
    source = tmp_path / 'source.pdf'
    with pymupdf.open() as pdf:
        for number in range(1, 7):
            with pymupdf.open() as vector:
                original = vector.new_page(width=200, height=250)
                original.insert_text((20, 40), f'Original {number}')
                page = pdf.new_page(width=200, height=250)
                page.insert_image(page.rect, pixmap=original.get_pixmap())
        pdf.save(source)
    return Work(str(source), str(tmp_path / 'job'), initialize=False), tmp_path


def _export(work, folder, scope='', selection='2,4,6'):
    path, pages, skipped = export_batch(work, folder, scope, selection)
    prompt = (path / 'Gemini-요청문.txt').read_text(encoding='utf-8')
    key = re.search(r'"batch_id": "([a-f0-9]+)"', prompt)[1]
    return path, pages, skipped, key


def _reading(number, text):
    return {
        'pdf_page': number, 'blank': False,
        'regions': [{'id': 'r1', 'k': '본문', 'bbox': [100, 100, 300, 800], 'dir': '가로',
                     'paras': [{'cols': [{'n': 1, 't': text}]}]}],
    }


def _response(key, *pages):
    return json.dumps({'batch_id': key, 'pages': pages}, ensure_ascii=False)


def test_selected_original_pages_and_incremental_import(task):
    work, folder = task
    work.ensure_source()
    work.save_record(2, {'pdf_page': 2, 'merged': {'regions': []}, 'manual': '보존'})
    path, pages, skipped, key = _export(work, folder, scope='2-5', selection='1,2,3,5,6')
    assert pages == [3, 5]
    assert skipped == [1, 2, 6]
    with pymupdf.open(path / 'Gemini-업로드.pdf') as upload:
        assert [page.get_text().strip() for page in upload] == ['SOURCE PAGE 3', 'SOURCE PAGE 5']
        assert all(page.get_images() for page in upload)
    assert book_counts(str(work.pdf_path), work.dir)['records'] == 1

    assert import_batch(work, '```json\n' + _response(key, _reading(5, '第五頁')) + '\n```') == ([5], [], [3])
    assert import_batch(work, _response(key, _reading(5, '다시 받은 답변'))) == ([], [5], [3])
    assert import_batch(work, _response(key, {'pdf_page': 3, 'blank': True, 'regions': []})) == ([3], [], [5])
    assert work.load_record(2)['manual'] == '보존'
    assert work.load_record(5)['merged']['regions'][0]['paragraphs'][0]['columns'][0]['text'] == '第五頁'
    assert '판독 확인 필요' in work.statuses(5)
    assert book_counts(str(work.pdf_path), work.dir)['records'] == 3


def test_invalid_batch_rejected_before_writing_and_existing_record_wins(task):
    work, folder = task
    _, _, _, key = _export(work, folder)
    for invalid in (
        _response(key, _reading(2, '二'), _reading(3, '범위 밖')),
        _response(key, _reading(2, '二'), _reading(2, '중복')),
        _response(key, {'pdf_page': 2, 'blank': False, 'regions': []}),
        _response(key, _reading(2, '二'))[:-1],
        _response('0' * 32, _reading(2, '다른 작업')),
    ):
        with pytest.raises(ValueError):
            import_batch(work, invalid)
        assert not work.record_path(2).exists()

    work.ensure_source()
    work.save_record(4, {'pdf_page': 4, 'merged': {}, 'manual': '작업 중 먼저 등록됨'})
    assert import_batch(work, _response(key, _reading(2, '二'), _reading(4, '四'))) == ([2], [4], [6])
    assert work.load_record(4)['manual'] == '작업 중 먼저 등록됨'


def test_import_used_by_normal_pdf_output_and_rejects_changed_source(task):
    work, folder = task
    _, _, _, key = _export(work, folder, selection='4')
    assert import_batch(work, _response(key, _reading(4, 'Imported text'))) == ([4], [], [])
    output = folder / 'searchable.pdf'
    assert cli.main(['textlayer', str(work.pdf_path), '--work', str(work.dir),
                     '--out', str(output), '--pages', '4']) == 0
    with pymupdf.open(output) as pdf:
        assert 'Imported text' in pdf[3].get_text()
        assert 'Imported text' not in pdf[2].get_text()
    stem = folder / 'mixed-readings'
    assert cli.main(['build', str(work.pdf_path), '--work', str(work.dir), '--pages', '4',
                     '--out', str(stem), '--reuse-records', '--no-context', '--no-normalize']) == 0
    with ZipFile(stem.with_suffix('.hwpx')) as document:
        assert b'Imported text' in document.read('Contents/section0.xml')
    assert cli.main(['read', str(work.pdf_path), '--work', str(work.dir), '--reader', 'mock',
                     '--pages', '4-5']) == 0
    assert work.load_record(5)
    assert work.load_record(4)['reader'] == 'gemini-app-manual'

    with pymupdf.open(work.pdf_path) as pdf:
        pdf.new_page()
        pdf.saveIncr()
    with pytest.raises(ValueError, match='원본'):
        import_batch(work, _response(key, _reading(4, '다른 원본')))
    assert work.load_record(4)['reader'] == 'gemini-app-manual'
