"""Public assistance checks for amendment 2201b5a; no internal lease access.

The second pause of the same uninterrupted execution must remain usable. Pause
generations increase without creating a new task or restarting execution. This
exercises the distinction without requiring a forbidden public execution epoch.
Concurrent HTTP stress does not replace a deterministic save_capability commit
failpoint; that stronger atomicity requirement remains explicitly blocked.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .client import Client
from .wire import assistance_state, ordinary_safe, view


def current_pause(owner, task_id, after_generation=0, timeout=15):
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        current = owner.get(task_id)
        view(current, previous)
        interaction = current.get('interaction')
        if current['state'] == 'waiting_for_browser' and interaction['generation'] > after_generation:
            return current
        assert current['state'] not in {'completed', 'failed', 'cancelled', 'interrupted'}, 'task terminated before expected browser pause'
        previous = current
        time.sleep(.05)
    raise TimeoutError('next distinct browser pause unavailable')


def mint(owner, receipt, interaction):
    request = {'interaction_id': interaction['interaction_id'], 'generation': interaction['generation']}
    grant = owner.request('POST', receipt['status_url'] + '/assistance', request)
    assert grant.status == 201
    assert grant.body['interaction_id'] == interaction['interaction_id']
    assert grant.body['generation'] == interaction['generation']
    path = grant.body['assistance_url']
    prefix = '/api/v1/browser-tasks/help/'
    assert path.startswith(prefix)
    token = path[len(prefix):]
    assert 32 <= len(token) <= 128 and all(c.isalnum() or c in '_-' for c in token)
    ordinary_safe(owner.get(receipt['task_id']))
    return grant.body


def metadata(owner, grant, interaction, expected_url):
    capability_client = Client(owner.base_url, ssl_context=getattr(owner, 'ssl_context', None))  # No API key on capability requests.
    state = capability_client.request('GET', grant['assistance_url'] + '/state')
    assert state.status == 200
    assistance_state(state.body, interaction, expected_url)
    headers = {name.lower(): value for name, value in state.headers.items()}
    assert headers.get('cache-control') == 'no-store'
    assert headers.get('referrer-policy') == 'no-referrer'
    assert "frame-ancestors 'none'" in headers.get('content-security-policy', '')
    return {'viewport_width': state.body['viewport_width'], 'viewport_height': state.body['viewport_height']}


def wallclock_expiry(evidence, owner, grant, max_wait=10):
    expires = datetime.fromisoformat(grant['expires_at'].replace('Z', '+00:00'))
    remaining = (expires - datetime.now(timezone.utc)).total_seconds()
    if remaining > max_wait:
        evidence.record('assistance_wallclock_expiry', 'blocked', 'configure disposable service assistance TTL <= 10 seconds to exercise actual expiry')
        return
    time.sleep(max(0, remaining) + .1)
    evidence.check('assistance_wallclock_expiry', lambda: expired(owner, grant), 'assistance_wallclock_expiry')


def expired(owner, grant):
    context = getattr(owner, 'ssl_context', None)
    Client(owner.base_url, ssl_context=context).expect_error('GET', grant['assistance_url'] + '/state', None, 'assistance_expired')
    Client(owner.base_url, ssl_context=context).expect_error('POST', grant['assistance_url'] + '/input', {'kind': 'release'},
                                        'assistance_expired', {'Origin': owner.base_url})


def public_assistance_targets(evidence, owner, foreign, profile_id, run_id, site):
    receipt = owner.start({'task': f'Open {site.browser_url}/ . Request browser assistance. After I resolve it, request browser assistance a second time. After the second resolution, request a third browser pause and wait. Never click a mutation button or finish without me.',
                           'profile_id': profile_id, 'idempotency_key': run_id + ':generations'})
    first = current_pause(owner, receipt['task_id'])['interaction']
    request = {'interaction_id': first['interaction_id'], 'generation': first['generation']}
    path = receipt['status_url'] + '/assistance'
    foreign.expect_error('POST', path, request, 'not_found')
    owner.expect_error('POST', path, {**request, 'generation': first['generation'] + 1}, 'stale_generation')
    grant1 = mint(owner, receipt, first)
    metadata(owner, grant1, first, site.browser_url)
    rotated = mint(owner, receipt, first)
    assert grant1['assistance_url'] != rotated['assistance_url']
    expired(owner, grant1)
    metadata(owner, rotated, first, site.browser_url)
    response = {'generation': first['generation'], 'idempotency_key': run_id + ':resume-first',
                'kind': 'browser', 'browser_resolved': True}
    interaction_path = receipt['status_url'] + '/interactions/' + first['interaction_id'] + '/respond'
    assert owner.request('POST', interaction_path, response).status == 202
    second = current_pause(owner, receipt['task_id'], first['generation'])['interaction']
    assert second['generation'] > first['generation']
    assert second['interaction_id'] != first['interaction_id']
    expired(owner, rotated)
    grant2 = mint(owner, receipt, second)
    viewport = metadata(owner, grant2, second, site.browser_url)
    evidence.record('assistance_generation_metadata', 'passed', 'two public mint/state successes in one uninterrupted task; second pause has distinct generation',
                    pause_generations=[first['generation'], second['generation']], **viewport)
    evidence.requirements['assistance_generation_metadata']['outcome'] = 'passed'
    wallclock_expiry(evidence, owner, grant2)
    # Remint so the race uses a current capability even after the TTL check.
    current = mint(owner, receipt, second)
    interaction_path = receipt['status_url'] + '/interactions/' + second['interaction_id'] + '/respond'
    response = {**response, 'generation': second['generation'], 'idempotency_key': run_id + ':resume-race'}
    rendezvous = threading.Barrier(3)
    calls = [('cancel', receipt['status_url'] + '/cancel', None),
             ('resolve', interaction_path, response),
             ('mint', path, {'interaction_id': second['interaction_id'], 'generation': second['generation']})]
    def race(call):
        name, target, body = call
        rendezvous.wait(timeout=5)
        return name, Client(owner.base_url, owner.token).request('POST', target, body)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(race, calls))
    grants = [current]
    statuses = {}
    for name, reply in results:
        statuses[name] = reply.status
        if name == 'cancel':
            assert reply.status == 202
        elif name == 'resolve':
            assert reply.status in {202, 409}
            if reply.status == 409:
                assert reply.body['error']['code'] in {'stale_generation', 'conflict'}
        else:
            assert reply.status in {201, 409}
            if reply.status == 201:
                grants.append(reply.body)
            else:
                assert reply.body['error']['code'] in {'stale_generation', 'conflict'}
    terminal = owner.wait(receipt['task_id'])
    assert terminal['state'] in {'cancelled', 'interrupted'}
    view(owner.get(receipt['task_id']), terminal)
    for grant in grants:
        expired(owner, grant)
    evidence.record('assistance_race_fencing', 'passed', 'concurrent public mint/resolve/cancel followed by terminal capability rejection',
                    http_statuses=statuses, limitation='HTTP scheduling stress only; deterministic lookup-to-save commit failpoint remains unverified')
    return {'distinct_pauses': 2, 'race_requests': 3}
