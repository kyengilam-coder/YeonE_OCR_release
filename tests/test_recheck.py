import json
from unittest.mock import patch

import pymupdf
import pytest

from yeone_ocr import cli, recheck
from yeone_ocr.state import Work
from yeone_ocr.schema import PageReading, Region, Paragraph, Column


def record(n, text):
    reading = PageReading(pdf_page=n, regions=[Region(id='body', kind='본문', paragraphs=[
        Paragraph(columns=[Column(text=text)])])])
    return {'pdf_page': n, 'merged': reading.model_dump(), 'human_history': ['사용자 교정']}


def setup(tmp_path):
    pdf = tmp_path / 'original.pdf'
    with pymupdf.open() as doc:
        for _ in range(3):
            doc.new_page()
        doc.save(pdf)
    work = Work(pdf, tmp_path / 'old' / '작업자료')
    work.save_record(1, record(1, '정상 본문'))
    work.save_record(2, record(2, '의심 〔入/人〕'))
    (work.dir / 'corrections.json').write_text('{"history":"보존"}', encoding='utf-8')
    return pdf, work


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def args_for(pdf, source, target, mode='suspect'):
    return cli.build_parser().parse_args(['run', str(pdf), '--work', str(target), '--recheck-from', str(source),
                                         '--recheck-mode', mode, '--reader', 'mock', '--no-context-model', '--no-adjudicate'])


def test_selection_rename_and_readonly_validation(tmp_path):
    pdf, work = setup(tmp_path)
    original = files(work.dir)
    renamed = tmp_path / 'renamed.pdf'; pdf.rename(renamed)
    assert set(recheck.inspect(renamed, work.dir)[2]) == {2, 3}
    assert set(recheck.inspect(renamed, work.dir, 'all')[2]) == {1, 2, 3}
    renamed.write_bytes(b'changed')
    with pytest.raises(ValueError, match='일치'):
        recheck.inspect(renamed, work.dir)
    assert files(work.dir) == original


def test_fork_preserves_history_and_builds_all_pages(tmp_path):
    pdf, source = setup(tmp_path)
    original = files(source.dir)
    target = tmp_path / 'new' / '작업자료'
    args = args_for(pdf, source.dir, target)
    def run(trial, work):
        assert trial.force and trial.no_cache and trial.recheck_from is None
        assert trial.pages == '2,3'
        assert not work.record_path(2).exists()  # no old answer supplied to independent reading
        for n in (2, 3):
            work.save_record(n, record(n, '새 판독 <script>'))
        return 0
    with patch.object(cli, 'cmd_run', side_effect=run), patch.object(cli, 'cmd_build', return_value=0) as build:
        assert recheck.command(args) == 0
        output = build.call_args.args[0]
        assert output.pages == '1,2,3' and output.reuse_records and output.no_context_model
    assert files(source.dir) == original
    assert files(target.parent / '기존작업_보존') == original
    fresh = Work(pdf, target, initialize=False)
    assert recheck.read_record(fresh, 1) == recheck.read_record(source, 1)
    report = (target.parent / '재판독_비교.html').read_text('utf-8')
    assert '&lt;script&gt;' in report and '<script>' not in report
    assert json.loads((target.parent / '재판독_실행.json').read_text('utf-8'))['status'] == '완료·원본 대조 필요'


def test_failure_keeps_original_and_reports_partial(tmp_path):
    pdf, source = setup(tmp_path)
    original = files(source.dir)
    target = tmp_path / 'failed' / '작업자료'
    with patch.object(cli, 'cmd_run', side_effect=RuntimeError('offline')), patch.object(cli, 'cmd_build') as build:
        with pytest.raises(RuntimeError):
            recheck.command(args_for(pdf, source.dir, target))
        build.assert_not_called()
    assert files(source.dir) == original
    assert json.loads((target.parent / '재판독_비교.json').read_text('utf-8'))['status'] == '중지·실패'


def test_existing_or_nested_destination_rejected(tmp_path):
    pdf, source = setup(tmp_path)
    original = files(source.dir)
    for target in (source.dir, source.dir / 'nested' / '작업자료'):
        with pytest.raises(ValueError, match='새 결과'):
            recheck.command(args_for(pdf, source.dir, target))
    assert files(source.dir) == original


def test_actual_mock_pipeline_creates_new_document(tmp_path):
    pdf, source = setup(tmp_path)
    original = files(source.dir)
    target = tmp_path / 'real' / '작업자료'
    assert cli.cmd_run(args_for(pdf, source.dir, target, 'all')) == 0
    assert files(source.dir) == original
    assert len(list((target.parent / 'out').glob('*.hwpx'))) == 1
    assert (target.parent / '재판독_비교.html').is_file()


def test_comparison_history_references_are_bound_to_preserved_copy(tmp_path):
    source = tmp_path / 'old'
    baseline = tmp_path / 'snapshot'
    rec = record(1, '원문')
    rec['local_first'] = {'project_path': str(source / 'comparison' / 'project.json')}
    rebound = recheck.snapshot_records({1: rec}, source, baseline)
    assert rebound[1]['local_first']['project_path'] == str(baseline / 'comparison' / 'project.json')
    assert rec['local_first']['project_path'] == str(source / 'comparison' / 'project.json')
    rec['local_first']['project_path'] = str(tmp_path / 'external.json')
    with pytest.raises(ValueError, match='밖'):
        recheck.snapshot_records({1: rec}, source, baseline)
