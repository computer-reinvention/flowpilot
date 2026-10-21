"""Real HTTP + SQLite; only the frozen executor/assistance peers are doubles."""

import asyncio
import hashlib
import importlib.util
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from backend import auth, db
from backend.task_bootstrap import install_task_api
from backend.task_contracts import (
    API_PREFIX, AssistanceRequest, Interaction, InteractionKind, PollRequest, Principal, TaskState,
    TaskResult, TaskStart, InputField, InteractionResponse, TaskFault,
)
from backend.task_ports import AssistanceBinding
from backend.task_settings import TaskSettings
from backend.tasks.control import create_control
from backend.tasks.repository import now
from backend.tasks.routes import create_router

spec = importlib.util.spec_from_file_location('lane001_doubles', Path(__file__).parent / 'fixtures/browser_tasks/doubles.py')
doubles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doubles)


@pytest.fixture
async def system(tmp_path, monkeypatch):
    from backend import config
    monkeypatch.setattr(config, 'PROFILES_DIR', tmp_path)
    monkeypatch.setenv('JWT_SECRET', 'synthetic-test-secret-' * 3)
    monkeypatch.setattr(auth, 'JWT_SECRET', 'synthetic-test-secret-' * 3)
    monkeypatch.setattr(auth, 'DB_PATH', tmp_path / 'platform.sqlite3')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'platform.sqlite3')
    auth._rate_buckets.clear()
    await db.init_db()
    # Upgrade existing platform database using the additive schema too.
    settings = TaskSettings(enabled=True, state_dir=tmp_path / 'task-state')
    control = create_control(settings)
    peers = doubles.bindings_double()
    bindings = replace(peers, control=control)
    for method in ('recover', 'delete_profile'):
        getattr(peers.executor, method).side_effect = None
        getattr(peers.executor, method).return_value = None
    starts = []
    parked = asyncio.Event()
    async def execute(execution):
        starts.append(execution)
        await parked.wait()
    peers.executor.execute.side_effect = execute
    async def cancel(lease):
        view = await control.get(lease.principal, lease.task_id, PollRequest())
        await control.finish(lease, view.revision, TaskState.CANCELLED)
        await control.release_browser(lease, profile_saved=True)
        parked.set()
    peers.executor.cancel.side_effect = cancel
    app = FastAPI()
    from backend.main import account_router
    app.include_router(account_router)
    host = install_task_api(app, settings, bindings=bindings, routers=(create_router(bindings),))
    await host.startup()
    assert host.readiness.ready
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        yield client, control, bindings, starts, host
    await host.shutdown()


async def key_for(client, email='owner@example.test'):
    response = await client.post('/api/auth/signup', json={'email': email, 'password': 'synthetic-password', 'name': 'Owner'})
    assert response.status_code == 200, response.text
    jwt = {'Authorization': 'Bearer ' + response.json()['token']}
    response = await client.post('/api/v1/api-keys', headers=jwt, json={'name': 'test key'})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['key']}, jwt, response.json()['id']


async def admit(client, headers, **kwargs):
    response = await client.post(API_PREFIX, headers=headers, json={'task': 'Inspect synthetic page', 'idempotency_key': 'admission-001', **kwargs})
    assert response.status_code == 202, response.text
    return response.json()


async def wait_started(starts, count=1):
    async with asyncio.timeout(2):
        while len(starts) < count:
            await asyncio.sleep(.01)


@pytest.mark.asyncio
async def test_http_admission_ownership_keys_profiles_and_cancel(system):
    client, control, bindings, starts, host = system
    headers, jwt, key_id = await key_for(client)
    receipts = await asyncio.gather(*(admit(client, headers) for _ in range(8)))
    assert len({r['task_id'] for r in receipts}) == 1
    await wait_started(starts)
    assert len(starts) == 1
    receipt = receipts[0]
    path = receipt['status_url']
    assert (await client.post(API_PREFIX, headers=headers, json={'task': 'changed', 'idempotency_key': 'admission-001'})).status_code == 409
    foreign, _, _ = await key_for(client, 'foreign@example.test')
    assert (await client.get(path, headers={**foreign, 'X-User-ID': starts[0].lease.principal.account_id})).status_code == 404
    assert (await client.post(API_PREFIX, headers=foreign, json={'task': 'x', 'idempotency_key': 'foreign-001', 'profile_id': receipt['profile_id']})).status_code == 404
    assert (await client.post(API_PREFIX, headers=headers, json={'task': 'x', 'idempotency_key': 'another-001', 'session_id': receipt['session_id']})).status_code == 409
    assert (await client.delete(API_PREFIX + '/profiles/' + receipt['profile_id'], headers=headers)).status_code == 409
    second = await admit(client, headers, idempotency_key='admission-002')
    assert second['profile_id'] != receipt['profile_id']
    assert (await client.post(API_PREFIX, headers=headers, json={'task': 'overload', 'idempotency_key': 'overload-001'})).status_code == 429
    listed = await client.get('/api/v1/api-keys', headers=jwt)
    assert listed.status_code == 200 and 'key_hash' not in listed.text
    assert headers['Authorization'][7:] not in listed.text
    response = await client.post('/api/v1/api-keys', headers=jwt, json={'name': 'rotation'})
    rotated = {'Authorization': 'Bearer ' + response.json()['key']}
    assert (await client.delete('/api/v1/api-keys/' + key_id, headers=jwt)).status_code == 200
    for suffix in ('', '/cancel', '/assistance'):
        result = await (client.get(path, headers=headers) if not suffix else client.post(path + suffix, headers=headers, json={'interaction_id': 'x', 'generation': 1}) if suffix == '/assistance' else client.post(path + suffix, headers=headers))
        assert result.status_code == 401
    assert (await client.get(path, headers=rotated)).status_code == 200
    response = await client.post(path + '/cancel', headers=rotated)
    assert response.json()['state'] == 'cancelling'
    async with asyncio.timeout(2):
        while (await client.get(path, headers=rotated)).json()['state'] != 'cancelled':
            await asyncio.sleep(.01)
    assert (await client.post(path + '/cancel', headers=rotated)).json()['state'] == 'cancelled'
    assert (await client.delete(API_PREFIX + '/profiles/' + receipt['profile_id'], headers=rotated)).status_code == 204
    bindings.executor.delete_profile.assert_awaited_once()


@pytest.mark.asyncio
async def test_interaction_secret_receipts_confirmation_and_capabilities(system):
    client, control, bindings, starts, host = system
    headers, _, _ = await key_for(client)
    receipt = await admit(client, headers)
    await wait_started(starts)
    lease = starts[0].lease
    interaction = Interaction(interaction_id='otp', generation=1, kind=InteractionKind.INPUT, message='Enter test code', fields=(InputField(name='code', label='Code', kind='secret'),), expires_at=now() + timedelta(seconds=30))
    waiting = asyncio.create_task(control.wait_for_interaction(lease, interaction))
    async with asyncio.timeout(2):
        while (await client.get(receipt['status_url'], headers=headers)).json()['state'] != 'waiting_for_input':
            await asyncio.sleep(.01)
    path = receipt['status_url'] + '/interactions/otp/respond'
    body = {'generation': 1, 'idempotency_key': 'response-001', 'kind': 'input', 'values': {'code': 'CANARY-OTP-DO-NOT-STORE'}}
    response = await client.post(path, headers=headers, json=body)
    assert response.status_code == 202, response.text
    assert (await waiting).values['code'] == body['values']['code']
    assert (await client.post(path, headers=headers, json=body)).json() == response.json()
    assert (await client.post(path, headers=headers, json={**body, 'values': {'code': 'changed'}})).status_code == 409
    assert b'CANARY-OTP' not in control.repository.path.read_bytes()
    with sqlite3.connect(control.repository.path) as database:
        assert 'CANARY-OTP' not in '\n'.join(database.iterdump())
    confirmation = Interaction(interaction_id='confirm', generation=2, kind='confirmation', message='Publish?', action_id='publish', expires_at=now() + timedelta(seconds=30))
    waiting = asyncio.create_task(control.wait_for_interaction(lease, confirmation))
    async with asyncio.timeout(2):
        while (await client.get(receipt['status_url'], headers=headers)).json()['state'] != 'waiting_for_confirmation':
            await asyncio.sleep(.01)
    with pytest.raises(TaskFault):
        await control.require_approval(lease, 'confirm', 2, 'publish')
    response = await client.post(receipt['status_url'] + '/interactions/confirm/respond', headers=headers, json={'generation': 2, 'idempotency_key': 'decision-001', 'kind': 'confirmation', 'confirmed': False})
    assert response.status_code == 202
    assert (await waiting).confirmed is False
    with pytest.raises(TaskFault):
        await control.require_approval(lease, 'confirm', 2, 'publish')
    browser = Interaction(interaction_id='browser', generation=3, kind='browser', message='Sign in privately', expires_at=now() + timedelta(seconds=30))
    waiting = asyncio.create_task(control.wait_for_interaction(lease, browser))
    async with asyncio.timeout(2):
        while (await client.get(receipt['status_url'], headers=headers)).json()['state'] != 'waiting_for_browser':
            await asyncio.sleep(.01)
    assistance_lease = await control.assistance_lease(lease.principal, lease.task_id, AssistanceRequest(interaction_id='browser', generation=3))
    assert assistance_lease == lease and assistance_lease.generation != 3
    binding = AssistanceBinding(lease=assistance_lease, interaction_id='browser', generation=3, digest=hashlib.sha256(b'synthetic-token').hexdigest(), expires_at=browser.expires_at)
    await control.save_capability(binding)
    assert await control.capability(binding.digest, now()) == binding
    await client.post(receipt['status_url'] + '/cancel', headers=headers)
    with pytest.raises(TaskFault):
        await control.capability(binding.digest, now())
    await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_bounded_validation_and_prefix_collisions(system):
    client, control, bindings, starts, host = system
    headers, jwt, _ = await key_for(client)
    malformed = await client.post('/api/auth/login', json={'email': 'x', 'password': {'secret': 'CANARY-PASSWORD'}})
    assert malformed.status_code == 422 and 'CANARY' not in malformed.text
    assert (await client.post('/api/auth/login', content=b'x' * 9000)).status_code == 422
    wrong = await client.post(API_PREFIX, headers=headers, json={'task': {'secret': 'CANARY-TASK'}, 'idempotency_key': 'invalid-001'})
    assert wrong.status_code == 422 and 'CANARY' not in wrong.text
    assert (await client.post(API_PREFIX, headers=headers, content=b'x' * 131073)).status_code == 422
    first = 'fp_live_abcd' + '1' * 28
    second = 'fp_live_abcd' + '2' * 28
    user = auth.decode_token(jwt['Authorization'][7:])
    await db.create_api_key(user, 'collision one', auth.hash_api_key(first), first[:12])
    await db.create_api_key(user, 'collision two', auth.hash_api_key(second), second[:12])
    result = await client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + second})
    assert result.status_code == 200, result.text
    assert result.json()['user']['id'] == user
    receipt = await admit(client, headers)
    invalid_poll = await client.get(receipt['status_url'] + '?wait_seconds=1', headers=headers)
    assert invalid_poll.status_code == 422
    invalid_poll = await client.get(receipt['status_url'] + '?after_revision=999', headers=headers)
    assert invalid_poll.status_code == 422


@pytest.mark.asyncio
async def test_completion_cannot_override_cancel_intent(system):
    client, control, bindings, starts, host = system
    headers, _, _ = await key_for(client)
    receipt = await admit(client, headers)
    await wait_started(starts)
    lease = starts[0].lease
    view = await control.repository.request_cancel(lease.principal, lease.task_id)
    with pytest.raises(TaskFault):
        await control.finish(lease, view.revision, TaskState.COMPLETED, result=TaskResult(answer='late result'))
    assert (await client.get(receipt['status_url'], headers=headers)).json()['state'] == 'cancelling'


@pytest.mark.asyncio
async def test_disconnect_during_commit_still_dispatches_once(system, monkeypatch):
    client, control, bindings, starts, host = system
    headers, jwt, _ = await key_for(client)
    principal = Principal(account_id=auth.decode_token(jwt['Authorization'][7:]))
    admitted = asyncio.Event()
    release = asyncio.Event()
    original = control.repository.admit
    async def delayed(*args):
        result = await original(*args)
        admitted.set()
        await release.wait()
        return result
    monkeypatch.setattr(control.repository, 'admit', delayed)
    request = TaskStart(task='No network synthetic task', idempotency_key='disconnect-001')
    caller = asyncio.create_task(control.start(principal, request))
    await admitted.wait()
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    release.set()
    await wait_started(starts)
    receipt = await control.start(principal, request)
    assert receipt.task_id == starts[0].lease.task_id and len(starts) == 1


@pytest.mark.asyncio
async def test_success_followup_and_profile_delete_history(system):
    client, control, bindings, starts, host = system
    headers, _, _ = await key_for(client)
    receipt = await admit(client, headers)
    await wait_started(starts)
    lease = starts[0].lease
    view = await control.get(lease.principal, lease.task_id, PollRequest())
    finished = await control.finish(lease, view.revision, TaskState.COMPLETED, result=TaskResult(answer='Synthetic answer'))
    assert finished.result.answer == 'Synthetic answer'
    # An idle browser reservation is not released merely by completion.
    assert (await client.delete(API_PREFIX + '/profiles/' + lease.profile_id, headers=headers)).status_code == 409
    await control.release_browser(lease, profile_saved=True)
    followup = await admit(client, headers, idempotency_key='followup-001', session_id=lease.session_id, profile_id=lease.profile_id)
    assert followup['profile_id'] == receipt['profile_id'] and followup['task_id'] != receipt['task_id']
    await wait_started(starts, 2)
    await client.post(followup['status_url'] + '/cancel', headers=headers)
    async with asyncio.timeout(2):
        while (await client.get(followup['status_url'], headers=headers)).json()['state'] != 'cancelled':
            await asyncio.sleep(.01)
    assert (await client.delete(API_PREFIX + '/profiles/' + lease.profile_id, headers=headers)).status_code == 204
    assert (await client.get(receipt['status_url'], headers=headers)).status_code == 404
    retry = await client.post(API_PREFIX, headers=headers, json={'task': 'Inspect synthetic page', 'idempotency_key': 'admission-001'})
    assert retry.status_code == 404  # retained tombstone never re-executes


@pytest.mark.asyncio
async def test_poll_wait_is_bounded_and_cancelled_poll_does_not_cancel_task(system):
    client, control, bindings, starts, host = system
    headers, _, _ = await key_for(client)
    receipt = await admit(client, headers)
    await wait_started(starts)
    lease = starts[0].lease
    view = await control.get(lease.principal, lease.task_id, PollRequest())
    read = asyncio.create_task(control.get(lease.principal, lease.task_id, PollRequest(after_revision=view.revision, wait_seconds=25)))
    await asyncio.sleep(.02)
    read.cancel()
    await asyncio.gather(read, return_exceptions=True)
    assert (await control.get(lease.principal, lease.task_id, PollRequest())).state == 'running'
    loop = asyncio.get_running_loop()
    begin = loop.time()
    unchanged = await control.get(lease.principal, lease.task_id, PollRequest(after_revision=view.revision, wait_seconds=.1))
    assert unchanged.revision == view.revision and .08 <= loop.time() - begin < .5


@pytest.mark.asyncio
async def test_auth_bounds_login_rate_and_hosted_secret(system, monkeypatch):
    client, control, bindings, starts, host = system
    for _ in range(10):
        response = await client.post('/api/auth/login', json={'email': 'absent@example.test', 'password': 'synthetic'})
        assert response.status_code == 401
    response = await client.post('/api/auth/login', json={'email': 'absent@example.test', 'password': 'synthetic'})
    assert response.status_code == 429 and response.headers['Retry-After'] == '60'
    monkeypatch.delenv('JWT_SECRET')
    with pytest.raises(RuntimeError, match='configured JWT_SECRET'):
        auth.validate_hosted_secret(True)


@pytest.mark.asyncio
async def test_loopback_tcp_client_signup_key_task_and_cancel(system):
    """No ASGI transport shortcuts: a fresh TCP HTTP client against uvicorn."""
    import socket
    import uvicorn
    client, control, bindings, starts, host = system
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    address = sock.getsockname()
    server = uvicorn.Server(uvicorn.Config(client._transport.app, lifespan='off', access_log=False, log_level='critical'))
    job = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                await asyncio.sleep(.01)
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{address[1]}', trust_env=False) as external:
            headers, _, _ = await key_for(external)
            receipt = await admit(external, headers)
            await wait_started(starts)
            assert (await external.get(receipt['status_url'], headers=headers)).json()['state'] == 'running'
            assert (await external.post(receipt['status_url'] + '/cancel', headers=headers)).status_code == 202
            async with asyncio.timeout(3):
                while (await external.get(receipt['status_url'], headers=headers)).json()['state'] != 'cancelled':
                    await asyncio.sleep(.01)
    finally:
        server.should_exit = True
        await asyncio.wait_for(job, 5)
        sock.close()


@pytest.mark.asyncio
async def test_profiles_create_list_cross_owner_pairs_and_failed_delete(system):
    client, control, bindings, starts, host = system
    headers, _, _ = await key_for(client)
    body = {'label': 'Explicit private profile', 'idempotency_key': 'profile-create-001'}
    created = await client.post(API_PREFIX + '/profiles', headers=headers, json=body)
    assert created.status_code == 201
    assert (await client.post(API_PREFIX + '/profiles', headers=headers, json=body)).json() == created.json()
    assert (await client.post(API_PREFIX + '/profiles', headers=headers, json={**body, 'label': 'Changed'})).status_code == 409
    profile_id = created.json()['profile_id']
    foreign, _, _ = await key_for(client, 'other@example.test')
    assert (await client.get(API_PREFIX + '/profiles', headers=foreign)).json()['items'] == []
    assert (await client.delete(API_PREFIX + '/profiles/' + profile_id, headers=foreign)).status_code == 404
    first = await admit(client, headers)
    mismatch = await client.post(API_PREFIX, headers=headers, json={'task': 'x', 'idempotency_key': 'pair-mismatch-001', 'session_id': first['session_id'], 'profile_id': profile_id})
    assert mismatch.status_code == 409
    bindings.executor.delete_profile.side_effect = RuntimeError('CANARY-RAW-FAILURE')
    # TaskHost masks arbitrary runtime exceptions; retrying deletion remains safe.
    transport = httpx.ASGITransport(app=client._transport.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as caller:
        failure = await caller.delete(API_PREFIX + '/profiles/' + profile_id, headers=headers)
    assert failure.status_code == 503 and 'CANARY' not in failure.text
    profiles = (await client.get(API_PREFIX + '/profiles', headers=headers)).json()['items']
    assert next(p for p in profiles if p['profile_id'] == profile_id)['state'] == 'recovery_required'
    blocked = await client.post(API_PREFIX, headers=headers, json={'task': 'x', 'idempotency_key': 'deleted-profile-001', 'profile_id': profile_id})
    assert blocked.status_code == 409
    bindings.executor.delete_profile.side_effect = None
    assert (await client.delete(API_PREFIX + '/profiles/' + profile_id, headers=headers)).status_code == 204


@pytest.mark.asyncio
async def test_dispatch_and_stop_failures_are_interrupted_not_replayed(system):
    client, control, bindings, starts, host = system
    headers, _, _ = await key_for(client)
    bindings.executor.execute.side_effect = RuntimeError('CANARY-PROVIDER-CREDENTIAL')
    receipt = await admit(client, headers)
    async with asyncio.timeout(3):
        while (await client.get(receipt['status_url'], headers=headers)).json()['state'] != 'interrupted':
            await asyncio.sleep(.01)
    status = await client.get(receipt['status_url'], headers=headers)
    assert 'CANARY' not in status.text
    assert await admit(client, headers) == receipt
    assert bindings.executor.execute.await_count == 1
    async with asyncio.timeout(3):
        while (await client.get(API_PREFIX + '/profiles', headers=headers)).json()['items'][0]['state'] == 'leased':
            await asyncio.sleep(.01)
    assert (await client.get(API_PREFIX + '/profiles', headers=headers)).json()['items'][0]['state'] == 'recovery_required'
    stop_wait = asyncio.Event()
    async def parked(execution):
        starts.append(execution)
        await stop_wait.wait()
    bindings.executor.execute.side_effect = parked
    second = await admit(client, headers, idempotency_key='stop-failure-001')
    await wait_started(starts)
    bindings.executor.cancel.side_effect = RuntimeError('CANARY-STOP-FAILURE')
    assert (await client.post(second['status_url'] + '/cancel', headers=headers)).json()['state'] == 'cancelling'
    async with asyncio.timeout(3):
        while (await client.get(second['status_url'], headers=headers)).json()['state'] != 'interrupted':
            await asyncio.sleep(.01)


@pytest.mark.asyncio
async def test_current_affirmative_approval_only_and_stale_responses(system):
    client, control, bindings, starts, host = system
    headers, _, _ = await key_for(client)
    receipt = await admit(client, headers)
    await wait_started(starts)
    lease = starts[0].lease
    confirmation = Interaction(interaction_id='approve', generation=1, kind='confirmation', message='Commit synthetic change?', action_id='action_one', expires_at=now() + timedelta(seconds=30))
    wait = asyncio.create_task(control.wait_for_interaction(lease, confirmation))
    async with asyncio.timeout(2):
        while (await client.get(receipt['status_url'], headers=headers)).json()['state'] != 'waiting_for_confirmation':
            await asyncio.sleep(.01)
    path = receipt['status_url'] + '/interactions/approve/respond'
    response = {'generation': 1, 'idempotency_key': 'approve-response-001', 'kind': 'confirmation', 'confirmed': True}
    assert (await client.post(path, headers=headers, json={**response, 'generation': 2})).status_code == 409
    assert (await client.post(path, headers=headers, json=response)).status_code == 202
    assert (await wait).confirmed
    await control.require_approval(lease, 'approve', 1, 'action_one')
    with pytest.raises(TaskFault):
        await control.require_approval(lease, 'approve', 1, 'action_two')
    await client.post(receipt['status_url'] + '/cancel', headers=headers)
    with pytest.raises(TaskFault):
        await control.require_approval(lease, 'approve', 1, 'action_one')


@pytest.mark.asyncio
async def test_shutdown_closes_admission_before_executor_cleanup(system):
    client, control, bindings, starts, host = system
    headers, jwt, _ = await key_for(client)
    principal = Principal(account_id=auth.decode_token(jwt['Authorization'][7:]))
    await host.shutdown()
    with pytest.raises(TaskFault) as failure:
        await control.start(principal, TaskStart(task='x', idempotency_key='shutdown-001'))
    assert failure.value.error.code == 'shutdown'
    assert (await client.post(API_PREFIX, headers=headers, json={'task': 'x', 'idempotency_key': 'shutdown-002'})).status_code == 503


@pytest.mark.asyncio
async def test_paired_session_profile_authorization_errors_are_identical_and_atomic(system):
    client, control, bindings, starts, host = system
    headers, _, _ = await key_for(client)
    foreign_headers, _, _ = await key_for(client, 'pair-foreign@example.test')
    owned = await client.post(API_PREFIX + '/profiles', headers=headers, json={'label': 'Owned alternate', 'idempotency_key': 'http-pair-owned-001'})
    foreign = await client.post(API_PREFIX + '/profiles', headers=foreign_headers, json={'label': 'Foreign profile', 'idempotency_key': 'http-pair-foreign-001'})
    assert owned.status_code == foreign.status_code == 201
    first = await admit(client, headers)
    await wait_started(starts)
    lease = starts[0].lease
    view = await control.get(lease.principal, lease.task_id, PollRequest())
    await control.finish(lease, view.revision, TaskState.COMPLETED, result=TaskResult(answer='done'))
    await control.release_browser(lease, profile_saved=True)
    def snapshot():
        with sqlite3.connect(control.repository.path) as database:
            return tuple(database.iterdump()), dict(control.repository._requests)
    before = snapshot()
    errors = []
    for profile_id, status in ((foreign.json()['profile_id'], 404), ('missing_profile', 404), (owned.json()['profile_id'], 409)):
        response = await client.post(API_PREFIX, headers=headers, json={'task': 'followup', 'session_id': first['session_id'], 'profile_id': profile_id, 'idempotency_key': 'http-pair-retry-001'})
        assert response.status_code == status, response.text
        if status == 404:
            assert response.json() == {'error': {'code': 'not_found', 'message': 'Not found', 'retryable': False, 'retry_after_seconds': None}}
            errors.append(response.content)
        assert snapshot() == before
        assert bindings.executor.execute.await_count == 1
    assert errors[0] == errors[1]
    matching = await client.post(API_PREFIX, headers=headers, json={'task': 'followup', 'session_id': first['session_id'], 'profile_id': first['profile_id'], 'idempotency_key': 'http-pair-retry-001'})
    assert matching.status_code == 202, matching.text
    assert matching.json()['session_id'] == first['session_id']
    assert matching.json()['profile_id'] == first['profile_id']
    await wait_started(starts, 2)
    assert bindings.executor.execute.await_count == 2
