"""Real control/SQLite resume authority with a strictly doubled human writer."""

import asyncio
import importlib.util
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from backend.task_contracts import (
    AssistanceRequest, Interaction, InteractionResponse, PollRequest, Principal,
    TaskFault, TaskStart, TaskState,
)
from backend.task_ports import AssistanceBinding
from backend.task_settings import TaskSettings
from backend.tasks.control import create_control
from backend.tasks.repository import now

spec = importlib.util.spec_from_file_location('human_release_doubles', Path(__file__).parent / 'fixtures/browser_tasks/doubles.py')
doubles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doubles)


@pytest.fixture
async def paused(tmp_path, monkeypatch):
    from backend import auth
    monkeypatch.setenv('JWT_SECRET', 'synthetic-test-secret-' * 3)
    monkeypatch.setattr(auth, 'JWT_SECRET', 'synthetic-test-secret-' * 3)
    settings = TaskSettings(state_dir=tmp_path, cancellation_seconds=1)
    control = create_control(settings)
    peers = doubles.bindings_double()
    peers.executor.recover.side_effect = None
    peers.executor.recover.return_value = None
    peers.executor.release_human.side_effect = None
    peers.executor.release_human.return_value = None
    principal = Principal(account_id='owner')
    entered = asyncio.Event()
    resumed = asyncio.Event()
    executions = []
    responses = []
    interaction = Interaction(interaction_id='browser_pause', generation=7, kind='browser', message='Synthetic human pause', expires_at=now() + timedelta(seconds=60))
    async def execute(execution):
        executions.append(execution)
        entered.set()
        response = await control.wait_for_interaction(execution.lease, interaction)
        responses.append(response)
        resumed.set()
        await asyncio.Event().wait()
    peers.executor.execute.side_effect = execute
    async def cancel(lease):
        view = await control.get(principal, lease.task_id, PollRequest())
        await control.finish(lease, view.revision, TaskState.CANCELLED)
        await control.release_browser(lease, profile_saved=True)
    peers.executor.cancel.side_effect = cancel
    control.bind(replace(peers, control=control))
    await control.startup()
    admission = await control.start(principal, TaskStart(task='Synthetic no-network task', idempotency_key='human-task-001'))
    await asyncio.wait_for(entered.wait(), 2)
    async with asyncio.timeout(2):
        while (await control.get(principal, admission.task_id, PollRequest())).state != TaskState.WAITING_FOR_BROWSER:
            await asyncio.sleep(.01)
    lease = await control.assistance_lease(principal, admission.task_id, AssistanceRequest(interaction_id=interaction.interaction_id, generation=7))
    response = InteractionResponse(generation=7, idempotency_key='human-response-001', kind='browser', browser_resolved=True)
    binding = AssistanceBinding(lease=lease, interaction_id=interaction.interaction_id, generation=7, digest='e' * 64, expires_at=interaction.expires_at)
    await control.save_capability(binding)
    yield control, peers.executor, principal, lease, interaction, response, resumed, responses, binding
    await control.shutdown()


@pytest.mark.asyncio
async def test_resume_waits_for_blocked_human_write_and_revokes_before_agent_wakes(paused):
    control, executor, owner, lease, interaction, response, resumed, responses, binding = paused
    releasing = asyncio.Event()
    write_done = asyncio.Event()
    held_controls_released = asyncio.Event()
    async def drain(received_lease, interaction_id, generation):
        assert (received_lease, interaction_id, generation) == (lease, interaction.interaction_id, 7)
        releasing.set()
        await write_done.wait()
        held_controls_released.set()
    executor.release_human.side_effect = drain
    reply = asyncio.create_task(control.respond(owner, lease.task_id, interaction.interaction_id, response))
    try:
        await asyncio.wait_for(releasing.wait(), 2)
        assert not reply.done() and not resumed.is_set()
        assert (await control.get(owner, lease.task_id, PollRequest())).state == 'waiting_for_browser'
        assert await control.capability(binding.digest, now()) == binding
        # A real writer transaction can commit while release is blocked: no
        # database transaction is being held across human/browser I/O.
        with sqlite3.connect(control.repository.path, timeout=.1) as database:
            database.execute('BEGIN IMMEDIATE')
            assert database.execute('SELECT count(*) FROM bt_tasks').fetchone()[0] == 1
        write_done.set()
        view = await reply
        await asyncio.wait_for(resumed.wait(), 2)
        assert held_controls_released.is_set() and view.state == 'running'
        assert responses == [response]
        with pytest.raises(TaskFault):
            await control.capability(binding.digest, now())
        assert await control.respond(owner, lease.task_id, interaction.interaction_id, response) == view
        executor.release_human.assert_awaited_once_with(lease, interaction.interaction_id, 7)
        executor.browser.assert_not_awaited()
    finally:
        write_done.set()
        await asyncio.gather(reply, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['generation', 'kind', 'interaction', 'owner', 'expired', 'waiter', 'idempotency'])
async def test_invalid_response_does_not_release_legitimate_writer(paused, monkeypatch, invalid):
    control, executor, owner, lease, interaction, response, resumed, responses, binding = paused
    interaction_id = interaction.interaction_id
    if invalid == 'generation':
        response = response.model_copy(update={'generation': 8})
    elif invalid == 'kind':
        response = InteractionResponse(generation=7, idempotency_key='wrong-kind-001', kind='confirmation', confirmed=True)
    elif invalid == 'interaction':
        interaction_id = 'different_pause'
    elif invalid == 'owner':
        owner = Principal(account_id='foreign')
    elif invalid == 'expired':
        import backend.tasks.repository as repository_module
        import backend.tasks.control as control_module
        future = lambda: interaction.expires_at + timedelta(seconds=1)
        monkeypatch.setattr(repository_module, 'now', future)
        monkeypatch.setattr(control_module, 'now', future)
    elif invalid == 'waiter':
        control._waiters.pop((lease.task_id, interaction.interaction_id, 7))
    elif invalid == 'idempotency':
        # Existing receipt for another operation target must be rejected before
        # disturbing the current viewer, not only at the final resolve commit.
        with sqlite3.connect(control.repository.path) as database:
            database.execute('INSERT INTO bt_idempotency VALUES(?,?,?,?,?)', (owner.account_id, 'respond', response.idempotency_key, 'f' * 64, '{}'))
    with pytest.raises(TaskFault):
        await control.respond(owner, lease.task_id, interaction_id, response)
    executor.release_human.assert_not_awaited()
    assert not resumed.is_set() and not responses
    assert (await control.get(lease.principal, lease.task_id, PollRequest())).state == 'waiting_for_browser'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['error', 'timeout'])
async def test_release_failure_or_timeout_never_resolves_or_wakes(paused, failure):
    control, executor, owner, lease, interaction, response, resumed, responses, binding = paused
    async def fail_release(*args):
        if failure == 'timeout':
            await asyncio.Event().wait()
        raise RuntimeError('CANARY-human-writer-failure')
    executor.release_human.side_effect = fail_release
    with pytest.raises(TaskFault) as error:
        await asyncio.wait_for(control.respond(owner, lease.task_id, interaction.interaction_id, response), 2)
    assert error.value.error.code == 'unavailable'
    assert 'CANARY' not in str(error.value)
    assert not resumed.is_set() and not responses
    assert (await control.get(owner, lease.task_id, PollRequest())).state == 'waiting_for_browser'
    assert await control.capability(binding.digest, now()) == binding
    with sqlite3.connect(control.repository.path) as database:
        assert database.execute('SELECT digest,response_key FROM bt_interactions').fetchone() == (None, None)
        assert database.execute("SELECT count(*) FROM bt_idempotency WHERE operation='respond'").fetchone()[0] == 0
    # A retried valid response can complete after a failed drain, without creating
    # a viewer merely to release it; successful duplicates must not drain again.
    executor.release_human.side_effect = None
    view = await control.respond(owner, lease.task_id, interaction.interaction_id, response)
    assert await control.respond(owner, lease.task_id, interaction.interaction_id, response) == view
    assert executor.release_human.await_count == 2
    executor.browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_viewer_resume_and_consumed_receipt_never_release_new_pause(paused):
    control, executor, owner, lease, interaction, response, resumed, responses, binding = paused
    first = await control.respond(owner, lease.task_id, interaction.interaction_id, response)
    await asyncio.wait_for(resumed.wait(), 2)
    next_interaction = interaction.model_copy(update={'interaction_id': 'next_pause', 'generation': 8})
    waiting = asyncio.create_task(control.wait_for_interaction(lease, next_interaction))
    try:
        async with asyncio.timeout(2):
            while (await control.get(owner, lease.task_id, PollRequest())).interaction != next_interaction:
                await asyncio.sleep(.01)
        assert await control.respond(owner, lease.task_id, interaction.interaction_id, response) == first
        executor.release_human.assert_awaited_once_with(lease, interaction.interaction_id, 7)
        assert not waiting.done()
        with pytest.raises(TaskFault):
            await control.respond(owner, lease.task_id, interaction.interaction_id, response.model_copy(update={'idempotency_key': 'contradiction-001'}))
        assert executor.release_human.await_count == 1
        executor.browser.assert_not_awaited()
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_first', [True, False])
async def test_cancel_response_serialization_never_wakes_cancelled_pause(paused, cancel_first):
    control, executor, owner, lease, interaction, response, resumed, responses, binding = paused
    entered = asyncio.Event()
    unblock = asyncio.Event()
    async def drain(*args):
        entered.set()
        await unblock.wait()
    executor.release_human.side_effect = drain
    if cancel_first:
        await control.cancel(owner, lease.task_id)
        with pytest.raises(TaskFault):
            await control.respond(owner, lease.task_id, interaction.interaction_id, response)
        executor.release_human.assert_not_awaited()
        assert not resumed.is_set()
    else:
        replying = asyncio.create_task(control.respond(owner, lease.task_id, interaction.interaction_id, response))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            cancelling = asyncio.create_task(control.cancel(owner, lease.task_id))
            await asyncio.sleep(.02)
            assert not cancelling.done() and not resumed.is_set()
            unblock.set()
            assert (await replying).state == 'running'
            assert (await cancelling).state == 'cancelling'
            executor.release_human.assert_awaited_once()
        finally:
            unblock.set()
            await asyncio.gather(replying, return_exceptions=True)
    async with asyncio.timeout(2):
        while (await control.get(owner, lease.task_id, PollRequest())).state != 'cancelled':
            await asyncio.sleep(.01)
