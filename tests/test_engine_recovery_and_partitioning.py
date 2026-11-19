import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from ab_ocr import glmocr_worker as worker
from yeone_ocr.comparison_ai import AIReviewError, ReviewPolicy, prepare_plan, review_window
from yeone_ocr.engine_diagnostics import save_failure
from yeone_ocr.state import review_page_mark
from yeone_ocr.gui_progress import cell_kind


def test_large_plan_windows_resume_without_duplicate_calls_or_losing_human_history():
    import test_comparison_ai
    fixture = test_comparison_ai.PipelineTests()
    fixture.setUp()
    try:
        fixture.project = fixture.new_project({'tesseract':'甲乙丙丁'*100, 'glmocr':'甲戊丙丁'*100})
        fixture.project.set_text(1, '甲乙丙丁'*100+'校', reason='human correction fixture')
        before = fixture.project.to_dict()
        policy = ReviewPolicy(max_targets=2, chunk_chars=50, batch_size=1)
        with pytest.raises(AIReviewError):
            prepare_plan(fixture.project,[1],policy,settings=fixture.settings)
        plan = prepare_plan(fixture.project,[1],policy,settings=fixture.settings,auto_split_targets=True)
        assert plan.summary['targets'] > 2 and plan.summary['automatic_partitions'] > 1
        window = review_window(plan,1)
        assert window.fingerprint == plan.fingerprint and window.batches[0] == plan.batches[0]
        result,calls = fixture.run_fake(window)
        assert result['calls']==len(calls)==1
        after = fixture.project.to_dict()
        assert after['pages']['1']['runs']==before['pages']['1']['runs']
        assert after['pages']['1']['edits']==before['pages']['1']['edits']
        remaining = prepare_plan(fixture.project,[1],policy,settings=fixture.settings,auto_split_targets=True)
        assert remaining.summary['resumed_batches'] >= 1
        assert window.batches[0]['batch_id'] not in [b['batch_id'] for b in remaining.batches]
    finally:
        fixture.doCleanups()


@pytest.mark.parametrize('review,expected', [
    ({'status':'reviewed'},'done'),
    ({'status':'pending'},'review_pending'),
    ({'status':'reviewed','engine_errors':{'glmocr':'failed'}},'engine_issue'),
    ({'status':'pending','engine_errors':{'glmocr':'failed'}},'engine_and_review'),
])
def test_engine_and_review_states_remain_distinct_in_grouped_page_strip(review,expected):
    assert review_page_mark(review)==expected
    assert cell_kind([expected])[0]==expected


def test_diagnostics_are_private_sidecars_not_replacement_ocr():
    with tempfile.TemporaryDirectory() as folder:
        error=RuntimeError('worker failed')
        error.diagnostics={'message':'prediction aborted, token repeat limit reached','server_exit_code':None}
        path=save_failure(Path(folder)/'book.yeone-compare.json',5,'glmocr',error)
        result=json.loads(path.read_text(encoding='utf-8'))
        assert result['page']==5 and 'token repeat limit' in result['details']['message']
        assert path.parent.name=='engine-diagnostics'
