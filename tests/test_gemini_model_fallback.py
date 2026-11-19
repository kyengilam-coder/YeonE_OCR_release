import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from yeone_ocr import antigravity_subscription as agy, gemini_quota, subscriptions
from yeone_ocr.claude_subscription import SubscriptionError


def limited():
    error = SubscriptionError('Individual quota reached')
    error.kind = 'limit'
    return error


def row(name, group, remaining=100):
    return dict(id=name, title=name, blocked=remaining == 0,
                quotas=[dict(group=group, window='Weekly Limit', remaining=remaining, reset='2026-12-01T00:00:00+00:00')])


def request(client, name='gemini-4-argon-high'):
    return client.request(model_name=name, system='s', prompt='p', images=[], schema={})


def answer(name):
    return '{}', True, 'STOP', {}, name


def test_models_exhaust_before_chatgpt_and_claude_and_shared_variants_are_skipped(monkeypatch):
    rows = [row('gemini-4-argon-high', 'Argon'), row('gemini-4-argon-medium', 'Argon'),
            row('gemini-4-hydrogen-high', 'Gemini'), row('gemini-4-hydrogen-low', 'Gemini'),
            row('gemini-3.8-flash-high', 'Gemini'), row('gemini-future-unknown', 'unknown')]
    rows[-1]['quotas'] = []
    monkeypatch.setattr(gemini_quota, 'inventory', lambda **kw: dict(models=rows, current=rows[0]['id']))
    seen = []
    google = agy.SubscriptionClient(cli='not-executed')
    def fail(**kwargs):
        seen.append(('gemini', kwargs['model_name']))
        raise limited()
    google._request_once = fail
    class Other:
        def __init__(self, plan): self.plan = plan
        def request(self, **kw):
            seen.append((self.plan, kw['model_name']))
            if self.plan == 'chatgpt': raise limited()
            return '{}', True, 'end_turn', {}, 'opus'
    monkeypatch.setattr(subscriptions, 'status', lambda *a, **kw: dict(usable=True))
    subscriptions.reset_exhausted()
    try:
        session = subscriptions.Session('gemini', gemini_model=rows[0]['id'], log=lambda *a: None,
                                        factory=lambda plan: google if plan == 'gemini' else Other(plan))
        result = request(session)
        assert [plan for plan, name in seen] == ['gemini', 'gemini', 'chatgpt', 'claude']
        assert [name for plan, name in seen if plan == 'gemini'] == [rows[0]['id'], rows[2]['id']]
        assert session.plan == 'claude' and 'opus' in str(result)
        assert session.api is None and agy.RUN_SETTINGS['useG1Credits'] is False
    finally:
        subscriptions.reset_exhausted()


def test_successful_fallback_is_reused_and_actual_model_is_recorded(monkeypatch):
    rows = [row('gemini-4-argon-high', 'Argon', 0), row('gemini-4-hydrogen-high', 'Gemini')]
    catalog = Mock(return_value=dict(models=rows, current=rows[0]['id']))
    monkeypatch.setattr(gemini_quota, 'inventory', catalog)
    client = agy.SubscriptionClient(cli='not-executed')
    seen = []
    def send(**kw):
        seen.append(kw['model_name'])
        if kw['model_name'] == rows[0]['id']: raise limited()
        return answer(kw['model_name'])
    client._request_once = send
    assert request(client)[-1] == rows[1]['id']
    assert request(client)[-1] == rows[1]['id']
    assert seen == [rows[0]['id'], rows[1]['id'], rows[1]['id']]
    assert catalog.call_count == 1 and len(client.model_switches) == 1


def test_ocr_session_uses_cli_inventory_to_fall_back_from_hydrogen_to_argon(monkeypatch):
    import subprocess

    hydrogen = 'gemini-4-hydrogen-low'
    argon = 'gemini-4-argon-low'
    monkeypatch.setattr(agy, 'find_cli', lambda: 'agy')
    commands = []

    def popen(args, *, stdin, **kwargs):
        assert stdin == subprocess.DEVNULL
        commands.append(args[-1])
        responses = {
            '--version': '2.0.0',
            '/quota': ('Gemini Models\tWeekly Limit Remaining\t14%\t2026-11-25T01:14:45Z\n'
                       'Gemini Models\tFive Hour Limit Remaining\t0%\t2026-11-19T07:08:16Z\n'
                       'Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t1%\t2026-11-25T04:27:07Z\n'),
            'models': f'{hydrogen}\tHydrogen\n{argon}\tArgon',
            '/model': hydrogen,
        }
        proc = Mock(returncode=0)
        proc.communicate.return_value = (responses[args[-1]], '')
        return proc

    monkeypatch.setattr(subprocess, 'Popen', popen)
    session = subscriptions.Session('gemini', gemini_model=hydrogen, log=lambda _: None)
    client = agy.SubscriptionClient(cli='agy', runner=session._run)
    session._factory = {'gemini': client}.__getitem__
    def send(**kwargs):
        if kwargs['model_name'] == hydrogen:
            raise limited()
        return answer(kwargs['model_name'])
    client._request_once = Mock(side_effect=send)
    try:
        assert request(session, hydrogen)[-1] == argon
        assert request(session, hydrogen)[-1] == argon
        assert [c.kwargs['model_name'] for c in client._request_once.call_args_list] == [hydrogen, argon, argon]
        assert commands == ['--version', '/quota', 'models', '/model']
        assert session.plan == 'gemini' and not session.switches
    finally:
        agy.reset_status_cache()


def test_concurrent_quota_failures_share_one_routing_decision(monkeypatch):
    rows = [row('gemini-4-argon-high', 'Argon'), row('gemini-4-hydrogen-high', 'Gemini')]
    catalog = Mock(return_value=dict(models=rows, current=rows[0]['id']))
    monkeypatch.setattr(gemini_quota, 'inventory', catalog)
    barrier = threading.Barrier(2)
    client = agy.SubscriptionClient(cli='not-executed')
    def send(**kw):
        if kw['model_name'] == rows[0]['id']:
            barrier.wait(timeout=5)
            raise limited()
        return answer(kw['model_name'])
    client._request_once = send
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: request(client), range(2)))
    assert [r[-1] for r in results] == [rows[1]['id']] * 2
    assert catalog.call_count == 1 and len(client.model_switches) == 1


def test_all_google_pools_wait_for_first_pool_to_reset(monkeypatch):
    hydrogen = 'gemini-4-hydrogen-low'
    argon = 'gemini-4-argon-low'
    rows = [row(hydrogen, 'Gemini Models'), row(argon, 'Gemini 4 Argon')]
    monkeypatch.setattr(gemini_quota, 'inventory', lambda **_kw: dict(models=rows, current=hydrogen))
    monkeypatch.setattr(agy.time, 'monotonic', lambda: 1000)
    client = agy.SubscriptionClient(cli='agy')
    def send(**kw):
        error = limited()
        error.retry_after = 600 if kw['model_name'] == hydrogen else 86400
        raise error
    client._request_once = Mock(side_effect=send)
    with pytest.raises(SubscriptionError) as error:
        request(client, hydrogen)
    assert error.value.retry_after == 600
    assert [c.kwargs['model_name'] for c in client._request_once.call_args_list] == [hydrogen, argon]


def test_stopping_during_quota_refresh_does_not_send_to_another_model(monkeypatch):
    stop = threading.Event()
    client = agy.SubscriptionClient(cli='not-executed')
    client.should_stop = stop.is_set
    client._request_once = Mock(side_effect=limited())
    def catalog(**kw):
        stop.set()
        return dict(models=[row('gemini-4-hydrogen-high', 'Gemini')], current='gemini-4-argon-high')
    monkeypatch.setattr(gemini_quota, 'inventory', catalog)
    with pytest.raises(SubscriptionError): request(client)
    assert client._request_once.call_count == 1


@pytest.mark.parametrize('error', [TimeoutError('timeout'), SubscriptionError('sign in')])
def test_non_quota_errors_never_switch_gemini_models(monkeypatch, error):
    catalog = Mock()
    monkeypatch.setattr(gemini_quota, 'inventory', catalog)
    client = agy.SubscriptionClient(cli='not-executed')
    client._request_once = Mock(side_effect=error)
    with pytest.raises(type(error)): request(client)
    catalog.assert_not_called()
