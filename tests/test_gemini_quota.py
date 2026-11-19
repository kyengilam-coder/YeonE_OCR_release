import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

from yeone_ocr import gemini_quota as quota, antigravity_subscription as agy, subscriptions, local_first

REPORT = ('Gemini Models\tWeekly Limit Remaining\t100%\t2026-11-23T10:25:05Z\n'
          'Gemini Models\tFive Hour Limit Remaining\t83%\t2026-11-16T15:25:05Z\n'
          'Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t0%\t2026-11-21T23:37:10Z\n')


def test_real_report_shared_pools_specific_exhaustion_and_unknowns():
    rows = quota.parse_quotas(REPORT)
    assert len(rows) == 3 and rows[2]['remaining'] == 0
    assert rows[2]['reset'] == '2026-11-21T23:37:10+00:00'
    argon = quota.model_quotas('gemini-4-argon-high', '', rows)
    hydrogen = quota.model_quotas('gemini-4-hydrogen-high', '', rows)
    assert [q['remaining'] for q in argon] == [0]
    assert [q['remaining'] for q in hydrogen] == [100, 83]
    assert quota.model_quotas('gemini-future-new', '', rows) == []
    assert quota.model_quotas('gemini-3.8-flash-high', '', rows) == hydrogen
    assert quota.parse_quotas('bad\tdata\t101%\t2026-11-23T10:25:05Z\n') == []
    assert quota.parse_quotas('bad\tdata\t50%\twrong\n') == []


def test_argon_only_uses_windows_reported_for_its_own_pool():
    rows = quota.parse_quotas(REPORT.replace('83%', '0%').replace('Weekly Limit\t0%', 'Weekly Limit\t100%'))
    assert [q['remaining'] for q in quota.model_quotas('gemini-4-argon-low', '', rows)] == [100]
    assert [q['remaining'] for q in quota.model_quotas('gemini-pro-orchestrator-g1-pro', '', rows)] == [100]
    assert quota.model_quotas('gemini-4-hydrogen-low', '', rows)[-1]['remaining'] == 0
    assert quota.model_quotas('gemini-4-argon-low', '', rows[:2]) == []


def test_inventory_uses_only_read_only_commands(monkeypatch):
    monkeypatch.setattr(agy, 'find_cli', lambda: 'agy')
    calls = []
    def run(args, **kw):
        calls.append(args[1:])
        output = {'--version': '1.5.9', '/quota': REPORT,
                  'models': 'gemini-4-argon-medium\tGemini 4 Argon (Medium)\n'
                            'gemini-4-hydrogen-high\tGemini 4 Hydrogen (High)\nclaude-opus\tClaude\n',
                  '/model': 'gemini-4-argon-medium\tGemini 4 Argon (Medium)'}[args[-1]]
        return subprocess.CompletedProcess(args, 0, output, '')
    result = quota.inventory(run)
    assert calls == [['--version'], ['-p', '/quota'], ['models'], ['-p', '/model']]
    assert len(result['models']) == 2 and result['models'][0]['blocked']
    assert not result['models'][1]['blocked']
    assert '초기화' in quota.describe(result['models'][1])
    agy.reset_status_cache()


def test_legacy_paid_fallback_flag_cannot_construct_api_or_read_keys():
    keys = Mock(side_effect=AssertionError('must not read API keys'))
    assert local_first._api_fallback(SimpleNamespace(local_ai_api_fallback=True, reader='gemini'), 99, keys, keys) is None
    keys.assert_not_called()
    assert agy.RUN_SETTINGS['useG1Credits'] is False


def test_session_pins_gemini_choice_on_actual_request(monkeypatch):
    monkeypatch.setattr(subscriptions, 'module', lambda plan: agy)
    client = Mock()
    client.request.return_value = ('{}', True, 'STOP', {}, 'gemini-4-hydrogen-high')
    session = subscriptions.Session('gemini', factory=lambda plan: client, gemini_model='gemini-4-hydrogen-high')
    result = session.request(model_name='gemini-api-preview', system='s', prompt='p', images=[], schema={})
    assert client.request.call_args.kwargs['model_name'] == 'gemini-4-hydrogen-high'
    assert 'gemini-4-hydrogen-high' in str(result)
