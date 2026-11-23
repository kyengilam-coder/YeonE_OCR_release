from zipfile import ZipFile

from lxml import etree
from PIL import Image, ImageDraw
import pymupdf

from yeone_ocr import cli, source_review
from yeone_ocr.blocks import Block
from yeone_ocr.common_layout import apply_common_layout, audit_note_transitions, note_transition_evidence, verify_written_note_cues
from yeone_ocr.hwpx_builder import HP, build_hwpx
from yeone_ocr.schema import Column, PageReading, Paragraph, Region
from yeone_ocr.state import Work


def _record(text):
    paragraph = Paragraph(columns=[Column(col_from_right=1, text=text)])
    region = Region(id='body', kind='본문', bbox=[260, 190, 390, 900], paragraphs=[paragraph])
    return dict(pdf_page=1, mode='horizontal_zh', file='source.pdf',
                merged=PageReading(pdf_page=1, regions=[region]).model_dump())


def _witness(text, y=300):
    return dict(text=text, image_width=1000, image_height=1000,
                words=[dict(text=text, bbox=[200, y, 890, y + 50])])


def test_source_review_requires_two_independent_positioned_readings():
    before = 'Original jext is clear.'
    after = 'Original text is clear.'
    original = _record(before)
    paddle = _witness(after)
    tesseract = _witness(after)
    updated, report = source_review.compare_page(original, {'rapidocr': paddle, 'tesseract': tesseract})
    assert report['findings'][0]['applied']
    assert updated['merged'] == _record(after)['merged']
    assert updated['local_grades'] == [dict(region_id='body', para=0, start=9, end=10, grade='green')]
    assert original == _record(before)
    updated, report = source_review.compare_page(original, {'rapidocr': paddle, 'ppocrv6': paddle})
    assert updated == original and not report['findings'][0]['applied']
    updated, report = source_review.compare_page(original, {'rapidocr': paddle, 'tesseract': _witness(after, 800)})
    assert updated == original and not report['findings'][0]['applied']
    updated, report = source_review.compare_page(original, {'rapidocr': paddle, 'tesseract': tesseract}, baseline='paddle_server')
    assert updated == original and not report['findings'][0]['applied']
    original = _record('寒者，春之误也；微温者，亦春之温也。')
    updated, report = source_review.compare_page(original, {
        'rapidocr': _witness('寒者，春之寒也；微温者，亦春之温也。'),
        'tesseract': _witness('寒者,春之寒也;微温者,亦春之温也.')})
    assert updated['merged'] == _record('寒者，春之寒也；微温者，亦春之温也。')['merged']
    assert [f['after'] for f in report['findings'] if f['applied']] == ['寒']


def _paragraphs(path):
    with ZipFile(path) as zip_file:
        section = etree.fromstring(zip_file.read('Contents/section0.xml'))
    return [(''.join(el.text or '' for el in p.iter(HP + 't')),
             [el.get('name') for el in p.iter(HP + 'bookmark')]) for p in section.findall(HP + 'p')]


def test_heading_bookmarks_and_note_boundary_are_independent(tmp_path):
    blocks, _ = apply_common_layout([
        Block(type='heading', text='장 제목', page=1, anchors=[(0, 'PDF 1쪽')]),
        Block(type='para', text='① 첫 쪽 각주', page=1, layout={'source_layout_para': 'footnote'}),
        Block(type='para', text='다음 본문', page=2, anchors=[(0, 'PDF 2쪽 · 인쇄 184쪽')]),
        Block(type='heading', text='항목', page=2, level=3),
        Block(type='heading', text='항목', page=3, level=3, anchors=[(0, 'PDF 3쪽')]),
    ], mark_note_transitions=True)
    pages = [{'pdf_page': n} for n in (1, 2, 3)]
    for policy in ('headings', 'none'):
        path = tmp_path / f'{policy}.hwpx'
        build_hwpx(blocks, str(path), edited=True, preset='basic', bookmark_policy=policy)
        evidence = note_transition_evidence(blocks, pages)
        verify_written_note_cues(path, evidence)
        assert evidence[1]['cue_verified'] is True
        assert audit_note_transitions(evidence) == [
            dict(page=1, next_page=2, status='회색 쪽 안내', tail='① 첫 쪽 각주', head='다음 본문')]
        paragraphs = _paragraphs(path)
        assert ('원본 184쪽 · 본문 시작', []) in paragraphs
        named = [(text, marks) for text, marks in paragraphs if marks]
        assert named == ([('장 제목', ['장 제목']), ('항목', ['항목']), ('항목', ['항목 (2)'])]
                         if policy == 'headings' else [])


def test_footnote_boundary_cue_precedes_next_pages_entry(tmp_path):
    blocks, _ = apply_common_layout([
        Block(type='para', text='〔4〕마지막 각주', page=39, layout={'source_layout_para': 'footnote'}),
        Block(type='heading', text='艾火', level=3, page=40, anchors=[(0, 'PDF 40쪽 · 인쇄 6쪽')]),
        Block(type='para', text='［主治］ 본문', page=40),
    ], mark_note_transitions=True)
    dest = tmp_path / 'entry.hwpx'
    build_hwpx(blocks, str(dest), edited=True, preset='basic', bookmark_policy='headings')
    assert [('원본 6쪽 · 본문 시작', []), ('艾火', ['艾火'])] == [
        item for item in _paragraphs(dest) if item[0].startswith('원본') or item[1]]
    evidence = note_transition_evidence(blocks, [{'pdf_page': n} for n in (39, 40)])
    verify_written_note_cues(dest, evidence)
    assert audit_note_transitions(evidence)[0]['status'] == '회색 쪽 안내'


def test_contents_heading_gets_bookmark_and_footnotes_keep_next_page_break(tmp_path):
    def contents(page, row, heading=False):
        return dict(front_section=heading, source_contents=dict(row=(page, row), mode='title', slot=0,
                    columns=[], center=True, leader='NONE', before=0, start=row == 0))

    blocks, _ = apply_common_layout([
        Block(type='heading', text='목차', page=1, layout=contents(1, 0, heading=True)),
        Block(type='para', text='제1장', page=1, layout=contents(1, 1)),
        Block(type='para', text='〔1〕목차 각주', page=1, layout={'source_layout_para': 'footnote'}),
        Block(type='para', text='제2장', page=2, layout=contents(2, 0)),
    ], mark_note_transitions=True)
    path = tmp_path / 'contents.hwpx'
    build_hwpx(blocks, str(path), edited=True, preset='basic', bookmark_policy='headings')
    assert [('목차', ['목차'])] == [(text, marks) for text, marks in _paragraphs(path) if marks]
    evidence = note_transition_evidence(blocks, [{'pdf_page': 1}, {'pdf_page': 2}])
    assert audit_note_transitions(evidence)[0]['status'] == '제목 또는 쪽 나눔'
    with ZipFile(path) as archive:
        section = etree.fromstring(archive.read('Contents/section0.xml'))
    next_page = next(p for p in section.findall(HP + 'p') if '제2장' in ''.join(p.itertext()))
    assert next_page.get('pageBreak') == '1'


def test_reviewed_scan_rule_recovers_short_note_wrap_and_bad_note_number():
    from yeone_ocr.source_typography import _scanned_notes

    regions = [Region(id=name, kind='본문', bbox=bbox, paragraphs=[
        Paragraph(columns=[Column(col_from_right=1, text=text)])]) for name, bbox, text in (
            ('body', [850, 90, 890, 890], '본문'),
            ('note', [920, 126, 937, 730], '5C1] 원본의 번호가 오독된 주석'),
            ('wrap', [938, 101, 949, 143], '끝.”'),
            ('folio', [951, 90, 970, 145], '。720。'))]
    hints = {}
    _scanned_notes(regions, hints, 731, reviewed=True, rule=906)
    assert set(hints) == {'note', 'wrap'}
    assert all(hint['source_layout_para'] == 'footnote' for hint in hints.values())


def test_book_preserve_text_flag_disables_output_normalization():
    from yeone_ocr.hwpx_output_rules import cli_flags

    parser = cli.build_parser()
    book_opts = cli_flags({'hwpx_original_text': True, 'hwpx_entry_paragraphs': True})
    assert book_opts == ['--hwpx-original-text']
    assert cli.normalization_options(parser.parse_args(['build', 'source.pdf'])).enabled
    assert not cli.normalization_options(parser.parse_args(['build', 'source.pdf', *book_opts])).enabled


def test_book_conversion_settings_override_defaults_for_one_book(tmp_path):
    from types import SimpleNamespace
    from yeone_ocr.hwpx_output_rules import conversion_flags
    from yeone_ocr.state import load_book_settings, save_book_settings

    values = {'hwpx_convert_traditional': 'on', 'hwpx_convert_punctuation': 'off', 'hwpx_review_colors': False}
    work = tmp_path / 'first'
    save_book_settings(work, values, create=True)
    app = SimpleNamespace(v_traditional=SimpleNamespace(get=lambda: False))
    assert load_book_settings(work) == values
    assert conversion_flags(app, load_book_settings(work)) == ['--no-punctuation']
    assert conversion_flags(app, load_book_settings(tmp_path / 'other')) == ['--no-traditional']


def test_printed_note_numbers_are_text_and_consecutive_volumes_are_distinct():
    from yeone_ocr.blocks import is_running_head
    from yeone_ocr.marks import fold, split_marks
    from yeone_ocr.textlayer import tokens

    assert not is_running_head('《本草述》卷之二', ['本草述卷之一'])
    assert not is_running_head('《本草述》原序', ['重刻本草述序'])
    assert split_marks('注〔1〕字') == [('注〔1〕字', 'body')]
    assert fold('注〔1〕字') == '注〔1〕字'
    assert ''.join(tokens('注〔1〕字')) == '注〔1〕字'


def test_verified_source_line_break_keeps_inline_note_in_one_line(tmp_path):
    from dataclasses import replace
    from yeone_ocr.presets import resolve_preset

    preset = replace(resolve_preset('basic'), source_sha256='a' * 64)
    lines = ('連續正文的第一行。', '第二行附原注〔4〕，後接正文。')
    path = tmp_path / 'source-lines.hwpx'
    blocks = [Block(type='para', role='body', text=''.join(lines), page=39, layout={'source_lines': lines})]
    build_hwpx(blocks, str(path), edited=True, preset=preset, bookmark_policy='headings')
    with ZipFile(path) as file:
        section = etree.fromstring(file.read('Contents/section0.xml'))
    br = section.find('.//' + HP + 'lineBreak')
    assert br is not None and br.getparent().text == lines[0] and br.tail == lines[1]


def test_cli_publishes_one_correction_to_hwpx_and_searchable_pdf(tmp_path, monkeypatch):
    from ab_ocr.ocr import RecognitionResult
    from yeone_ocr import automatic_ocr, engine_pool

    source = tmp_path / 'source.pdf'
    im = Image.new('RGB', (1000, 1000), 'white')
    ImageDraw.Draw(im).text((204, 310), 'Original text is clear.', fill='black', font_size=42)
    im.save(source, 'PDF')
    work = Work(str(source), str(tmp_path / 'work'))
    work.ensure_source()
    record = _record('Original jext is clear.')
    work.save_record(1, record)
    monkeypatch.setattr(automatic_ocr, 'available_engines', lambda *a, **kw: (['tesseract', 'rapidocr'], {}))

    def recognize(self, path, *, engine, **kw):
        raw = _witness('Original text is clear.')
        return RecognitionResult(raw['text'], raw['words'], str(path), 'a' * 64, 1,
                                 1000, 1000, 'test', 'chi_tra', None, .01, engine=engine)

    monkeypatch.setattr(engine_pool.EnginePool, 'recognize', recognize)
    assert cli.main(['build', str(source), '--work', str(work.dir), '--out', 'checked',
                     '--hwpx-bookmarks', 'headings', '--source-review', '--textlayer-pdf',
                     '--no-normalize', '--no-context', '--no-highlight']) == 0
    assert any('Original text is clear.' in line for line, _ in _paragraphs(work.out_dir / 'checked.hwpx'))
    with pymupdf.open(work.out_dir / 'checked.pdf') as search:
        assert 'Original text is clear.' in ' '.join(search[0].get_text().split())
    assert '자동 반영 1건' in (work.out_dir / 'checked_원본대조.html').read_text(encoding='utf-8')
    assert work.load_record(1)['merged'] == record['merged']


def test_cli_uses_same_scan_ocr_copy_alongside_an_independent_engine(tmp_path, monkeypatch):
    from contextlib import ExitStack
    from ab_ocr.ocr import RecognitionResult
    from yeone_ocr import automatic_ocr, engine_pool
    from yeone_ocr.source_review_pdf import find_scan_copy

    source = tmp_path / 'book.pdf'
    copy = tmp_path / 'book (인식).pdf'
    wrong = tmp_path / 'book (OCR) - different.pdf'
    with Image.new('L', (1000, 1000), 'white') as image:
        draw = ImageDraw.Draw(image)
        for i in range(8):
            draw.rectangle((140 + i * 20, 170 + i * 69, 730 - i * 21, 180 + i * 69), fill='black')
        draw.text((202, 310), 'Original text is clear.', fill='black', font_size=42)
        image.save(source, 'PDF')
        image.transpose(Image.Transpose.ROTATE_90).save(wrong, 'PDF')
    for target in (copy, wrong):
        with pymupdf.open(source if target == copy else wrong) as doc:
            doc.set_metadata({'producer': 'ABBYY FineReader'})
            doc[0].insert_text((200, 340), 'Original text is clear.', fontsize=34, render_mode=3)
            doc.save(target) if target == copy else doc.saveIncr()

    with ExitStack() as stack, pymupdf.open(source) as doc:
        assert find_scan_copy(source, doc, stack)[:2] == (copy.name, 'abbyy')
    work = Work(str(source), str(tmp_path / 'work'))
    work.ensure_source()
    original = _record('Original jext is clear.')
    original['reader'] = 'paddle_server'
    work.save_record(1, original)
    monkeypatch.setattr(automatic_ocr, 'available_engines', lambda *a, **kw: (['tesseract'], {}))

    def recognize(self, path, *, engine, **kwargs):
        raw = _witness('Original text is clear.')
        return RecognitionResult(raw['text'], raw['words'], str(path), 'a' * 64, 1,
                                 1000, 1000, 'test', 'eng', None, .01, engine=engine)

    monkeypatch.setattr(engine_pool.EnginePool, 'recognize', recognize)
    assert cli.main(['build', str(source), '--work', str(work.dir), '--out', 'checked', '--source-review',
                     '--textlayer-pdf', '--no-normalize', '--no-context', '--hwpx-review-letters']) == 0
    assert ('Original text is clear.', []) in _paragraphs(work.out_dir / 'checked.hwpx')
    with pymupdf.open(work.out_dir / 'checked.pdf') as result:
        assert 'Original text is clear.' in ' '.join(result[0].get_text().split())
    report = (work.out_dir / 'checked_원본대조.html').read_text(encoding='utf-8')
    assert '자동 반영 1건' in report and copy.name in report
    copy.unlink()
    with ExitStack() as stack, pymupdf.open(source) as doc:
        assert find_scan_copy(source, doc, stack) is None


def test_letter_review_preserves_red_text_and_drops_generated_highlighting(tmp_path):
    from yeone_ocr.hwpx_writer import HH

    blocks = [Block(type='para', text='검수 후보', spans=[(3, 5, 'red')])]
    path = tmp_path / 'letters.hwpx'
    build_hwpx(blocks, str(path), edited=True, preset='basic', review_letters=True)
    with ZipFile(path) as zip_file:
        section = etree.fromstring(zip_file.read('Contents/section0.xml'))
        header = etree.fromstring(zip_file.read('Contents/header.xml'))
    runs = {r.get('charPrIDRef') for r in section.iter(HP + 'run') if '후보' in ''.join(r.itertext())}
    painted = [c for c in header.iter(HH + 'charPr') if c.get('id') in runs]
    assert painted and all(c.get('textColor') == '#C00000' and c.get('shadeColor') == 'none' for c in painted)
