"""Read-only Antigravity inventory. Quotas are shared pools, never prompt counts."""
from __future__ import annotations

from datetime import datetime, timezone
import re
import subprocess


def parse_quotas(text):
    rows = []
    for line in text.splitlines():
        parts = line.strip().split('\t')
        if len(parts) != 4 or not re.fullmatch(r'\d+(?:\.\d+)?%', parts[2]):
            continue
        percent = float(parts[2][:-1])
        if not 0 <= percent <= 100:
            continue
        try:
            reset = datetime.fromisoformat(parts[3].replace('Z', '+00:00'))
            if reset.tzinfo is None:
                continue
        except ValueError:
            continue
        rows.append(dict(group=parts[0], window=parts[1], remaining=percent,
                         reset=reset.isoformat(), reset_local=reset.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')))
    return rows


def model_quotas(slug, title, quotas):
    # Argon completed a subscription OCR request while Hydrogen was at its
    # five-hour limit. Only use windows explicitly reported for each pool.
    # Unknown families have no inferred balance.
    family = ('Gemini 4 Argon' if slug.startswith('gemini-4-argon-') or slug == 'gemini-pro-orchestrator-g1-pro'
              else 'Gemini Models' if slug.startswith(('gemini-4-hydrogen-', 'gemini-3.8-flash-')) else None)
    return [q for q in quotas if q['group'] == family] if family else []


def inventory(runner=subprocess.run):
    from . import antigravity_subscription as agy
    state = agy.status(refresh=True, runner=runner)
    result = dict(models=[], current='', quotas=state.get('quotas', []), reason=state.get('reason'))
    if not state.get('logged_in'):
        return result
    try:
        listing = agy._quiet(runner, [state['path'], 'models'], 60)
        current = agy._quiet(runner, [state['path'], '-p', '/model'], 60)
        if current.returncode == 0:
            result['current'] = (current.stdout or '').strip().split('\t')[0]
        if listing.returncode != 0:
            result['reason'] = 'Google 모델 목록을 가져오지 못했습니다. 새로고침해 주세요.'
            return result
        for line in (listing.stdout or '').splitlines():
            fields = line.strip().split('\t', 1)
            if len(fields) != 2 or not re.fullmatch(r'gemini-[a-z0-9-]+', fields[0]):
                continue
            slug, title = fields
            quotas = model_quotas(slug, title, result['quotas'])
            result['models'].append(dict(id=slug, title=title, quotas=quotas,
                                         blocked=any(q['remaining'] == 0 for q in quotas)))
    except (OSError, subprocess.SubprocessError) as exc:
        result['reason'] = f'Google 모델·한도 조회 실패: {type(exc).__name__}'
    return result


def describe(row):
    quotas = row['quotas']
    if not quotas:
        return '잔여 한도 확인 안 됨'
    return ' / '.join(f"{q['group']} · {q['window']}: {q['remaining']:g}% · 초기화 {q['reset_local']}" for q in quotas)


def pool_key(row):
    """Models sharing the same ceilings must not each receive a rejected call."""
    return tuple(sorted((q['group'], q['window']) for q in row['quotas']))


def reset_delay(row, now=None):
    """Wait until all reported exhausted windows for this model pool reopen."""
    now = now or datetime.now(timezone.utc)
    limits = [datetime.fromisoformat(q['reset']) for q in row['quotas'] if q['remaining'] == 0]
    return max((max(60, (reset - now).total_seconds()) for reset in limits), default=0)


def fallback_rank(row):
    slug = row['id']
    family = (0 if slug.startswith('gemini-4-argon-') else 1 if slug.startswith('gemini-4-hydrogen-')
              else 2 if 'flash' in slug else 3)
    effort = next((i for i, suffix in enumerate(('-high', '-xhigh', '-medium', '-low')) if slug.endswith(suffix)), 4)
    return family, effort, slug
