"""Bounded terminal-only maintenance, real SQLite and managed lifecycle evidence."""

import asyncio
import importlib.util
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from backend.task_contracts import (
    ErrorCode, Interaction, Principal, TaskFault, TaskResult, TaskStart, TaskState, task_error,
)
from backend.task_settings import TaskSettings
from backend.tasks.control import create_control
from backend.tasks.repository import Repository, now

spec = importlib.util.spec_from_file_location('retention_doubles', Path(__file__).parent / 'fixtures/browser_tasks/doubles.py')
doubles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doubles)


async def seed(repository, key, *, state='completed', released=True, profile_id=None, session_id=None):
    owner = Principal(account_id='owner')
    request = TaskStart(task='Synthetic retention fixture', idempotency_key=key, profile_id=profile_id, session_id=session_id)
    admission = await repository.admit(owner, request)
    execution = await repository.claim(owner, admission.task_id)
    if state == 'completed':
        await repository.transition(execution.lease, execution.revision, TaskState.COMPLETED, result=TaskResult(answer='Synthetic old answer'))
    elif state in {'failed', 'interrupted'}:
        code = ErrorCode.PROVIDER_FAILURE if state == 'failed' else ErrorCode.INTERRUPTED
        await repository.transition(execution.lease, execution.revision, TaskState(state), error=task_error(code, 'Synthetic safe error'))
    elif state == 'cancelled':
        cancelling = await repository.request_cancel(owner, admission.task_id)
        await repository.transition(execution.lease, cancelling.revision, TaskState.CANCELLED)
    elif state == 'paused':
        await repository.transition(execution.lease, execution.revision, TaskState.WAITING_FOR_BROWSER, interaction=Interaction(interaction_id='pause_' + key, generation=1, kind='browser', message='Synthetic pause', expires_at=now() + timedelta(seconds=60)))
    elif state == 'cancelling':
        await repository.request_cancel(owner, admission.task_id)
    if state in {'completed', 'failed', 'interrupted', 'cancelled'} and released:
        await repository.release_browser(execution.lease, profile_saved=True)
    # Age durable history without relying on sleeps or touching real user data.
    with sqlite3.connect(repository.path) as database:
        raw = json.loads(database.execute('SELECT view FROM bt_tasks WHERE id=?', (admission.task_id,)).fetchone()[0])
        timestamp = (now() - timedelta(days=31)).isoformat()
        raw['created_at'] = raw['updated_at'] = timestamp
        database.execute('UPDATE bt_tasks SET view=? WHERE id=?', (json.dumps(raw), admission.task_id))
    return owner, request, admission, execution.lease


@pytest.fixture
async def repository(tmp_path):
    repository = Repository(TaskSettings(state_dir=tmp_path, max_active_browsers=10, max_account_browsers=10), b'x' * 32)
    await repository.initialize()
    return repository


@pytest.mark.asyncio
async def test_terminal_pruning_removes_history_but_preserves_continuity_and_admission_barrier(repository):
    owner, request, admission, lease = await seed(repository, 'retention-old-001')
    with sqlite3.connect(repository.path) as database:
        database.execute('INSERT INTO bt_interactions(task,id,generation,descriptor) VALUES(?,?,?,?)', (admission.task_id, 'old_pause', 1, '{}'))
        database.execute('INSERT INTO bt_capabilities VALUES(?,?,?)', ('a' * 64, admission.task_id, '{}'))
        database.execute('INSERT INTO bt_idempotency VALUES(?,?,?,?,?)', (owner.account_id, 'respond', 'old-response-001', 'b' * 64, json.dumps({'task_id': admission.task_id})))
    archive = repository.path.parent / 'synthetic-profile-archive'
    archive.write_text('synthetic archive marker')
    assert await repository.prune_history(batch_size=10) == 1
    with sqlite3.connect(repository.path) as database:
        for table in ('bt_tasks', 'bt_interactions', 'bt_capabilities'):
            assert database.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
        assert database.execute("SELECT count(*) FROM bt_idempotency WHERE operation='respond'").fetchone()[0] == 0
        assert database.execute("SELECT count(*) FROM bt_idempotency WHERE operation='admit'").fetchone()[0] == 1
        assert database.execute('SELECT id,owner,profile FROM bt_sessions').fetchall() == [(admission.session_id, owner.account_id, admission.profile_id)]
        assert database.execute('SELECT id FROM bt_profiles').fetchall() == [(admission.profile_id,)]
    assert archive.read_text() == 'synthetic archive marker'
    with pytest.raises(TaskFault) as missing:
        await repository.admit(owner, request)
    assert missing.value.error.code == 'not_found'
    followup = await repository.admit(owner, TaskStart(task='Explicit followup', session_id=admission.session_id, profile_id=admission.profile_id, idempotency_key='retention-followup-001'))
    assert followup.session_id == admission.session_id and followup.profile_id == admission.profile_id
    with pytest.raises(TaskFault):
        await repository.claim(owner, admission.task_id)


@pytest.mark.asyncio
async def test_live_paused_cancelling_idle_reserved_and_recent_terminal_are_preserved(repository):
    records = []
    for state, released in (('running', False), ('paused', False), ('cancelling', False), ('completed', False)):
        records.append(await seed(repository, 'retention-' + state + '-001', state=state, released=released))
    recent = await seed(repository, 'retention-recent-001')
    with sqlite3.connect(repository.path) as database:
        raw = json.loads(database.execute('SELECT view FROM bt_tasks WHERE id=?', (recent[2].task_id,)).fetchone()[0])
        raw['updated_at'] = now().isoformat()
        database.execute('UPDATE bt_tasks SET view=? WHERE id=?', (json.dumps(raw), recent[2].task_id))
        before = tuple(database.iterdump())
    assert await repository.prune_history(batch_size=100) == 0
    with sqlite3.connect(repository.path) as database:
        assert tuple(database.iterdump()) == before


@pytest.mark.asyncio
async def test_pruning_is_batched_and_respects_configured_retention(repository):
    for number in range(5):
        await seed(repository, f'retention-batch-{number:03d}')
    assert await repository.prune_history(batch_size=2) == 2
    assert await repository.prune_history(batch_size=2) == 2
    assert await repository.prune_history(batch_size=2) == 1
    assert await repository.prune_history(batch_size=2) == 0
    with pytest.raises(ValueError):
        await repository.prune_history(batch_size=101)
    await seed(repository, 'retention-long-policy-001')
    repository.settings = repository.settings.model_copy(update={'history_retention_days': 60})
    assert await repository.prune_history(batch_size=2) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['completed', 'failed', 'interrupted', 'cancelled'])
async def test_all_settled_terminal_states_are_eligible(repository, state):
    old = await seed(repository, 'retention-terminal-' + state, state=state)
    assert await repository.prune_history(batch_size=1) == 1
    with pytest.raises(TaskFault):
        await repository.get(old[0], old[2].task_id)


@pytest.mark.asyncio
async def test_batch_failure_rolls_back_all_deletes_and_age_lookup_is_indexed(repository):
    old = await seed(repository, 'retention-rollback-001')
    with sqlite3.connect(repository.path) as database:
        database.execute('INSERT INTO bt_interactions(task,id,generation,descriptor) VALUES(?,?,?,?)', (old[2].task_id, 'rollback_pause', 1, '{}'))
        database.execute("CREATE TRIGGER fail_history_delete BEFORE DELETE ON bt_tasks BEGIN SELECT RAISE(ABORT,'synthetic deletion failure'); END")
        before = tuple(database.iterdump())
        query_plan = database.execute("EXPLAIN QUERY PLAN SELECT id,profile FROM bt_tasks WHERE reserved=0 AND state IN ('completed','failed','cancelled','interrupted') AND julianday(json_extract(view,'$.updated_at')) < julianday(?) ORDER BY julianday(json_extract(view,'$.updated_at')),id LIMIT ?", (now().isoformat(), 100)).fetchall()
        assert any('bt_terminal_history_age' in row[-1] for row in query_plan)
    with pytest.raises(sqlite3.IntegrityError):
        await repository.prune_history(batch_size=100)
    with sqlite3.connect(repository.path) as database:
        assert tuple(database.iterdump()) == before


@pytest.mark.asyncio
async def test_release_and_pruning_race_never_deletes_reserved_browser_history(repository):
    for number in range(8):
        old = await seed(repository, f'retention-release-race-{number:03d}', released=False)
        count, _ = await asyncio.gather(repository.prune_history(batch_size=100), repository.release_browser(old[3], profile_saved=True))
        assert count in {0, 1}
        if count == 0:
            assert (await repository.get(old[0], old[2].task_id)).state == 'completed'
            assert await repository.prune_history(batch_size=100) == 1
        with pytest.raises(TaskFault):
            await repository.admit(old[0], old[1])


@pytest.mark.asyncio
async def test_active_profile_lease_blocks_pruning_older_terminal_history(repository):
    old = await seed(repository, 'retention-profile-old-001')
    current = await seed(repository, 'retention-profile-live-001', state='paused', session_id=old[2].session_id, profile_id=old[2].profile_id)
    assert await repository.prune_history(batch_size=100) == 0
    assert (await repository.get(old[0], old[2].task_id)).state == 'completed'
    assert (await repository.get(current[0], current[2].task_id)).state == 'waiting_for_browser'


@pytest.mark.asyncio
async def test_busy_oldest_profile_does_not_starve_later_batches(repository):
    old = await seed(repository, 'retention-busy-oldest-001')
    await seed(repository, 'retention-busy-live-001', state='paused', session_id=old[2].session_id)
    eligible = await seed(repository, 'retention-eligible-next-001')
    assert await repository.prune_history(batch_size=1) == 0
    assert await repository.prune_history(batch_size=1) == 1
    with pytest.raises(TaskFault):
        await repository.get(eligible[0], eligible[2].task_id)
    assert (await repository.get(old[0], old[2].task_id)).state == 'completed'


@pytest.mark.asyncio
async def test_admission_pruning_race_is_transactionally_profile_lease_aware(repository):
    for number in range(8):
        old = await seed(repository, f'retention-race-old-{number:03d}')
        request = TaskStart(task='New explicit followup', session_id=old[2].session_id, profile_id=old[2].profile_id, idempotency_key=f'retention-race-new-{number:03d}')
        pruned, admitted = await asyncio.gather(repository.prune_history(batch_size=100), repository.admit(old[0], request))
        assert pruned in {0, 1}
        assert admitted.session_id == old[2].session_id
        assert await repository.claim(old[0], admitted.task_id) is not None
        if pruned:
            with pytest.raises(TaskFault):
                await repository.admit(old[0], old[1])
        else:
            assert (await repository.get(old[0], old[2].task_id)).state == 'completed'
            assert await repository.admit(old[0], old[1]) == old[2]


@pytest.mark.asyncio
async def test_managed_maintenance_starts_periodically_drains_on_shutdown_and_sanitizes_failure(tmp_path, monkeypatch, caplog):
    from backend import auth
    import backend.tasks.control as module
    monkeypatch.setenv('JWT_SECRET', 'synthetic-test-secret-' * 3)
    monkeypatch.setattr(auth, 'JWT_SECRET', 'synthetic-test-secret-' * 3)
    monkeypatch.setattr(module, 'MAINTENANCE_INTERVAL_SECONDS', .01)
    settings = TaskSettings(state_dir=tmp_path)
    repository = Repository(settings, b'x' * 32)
    await repository.initialize()
    old = await seed(repository, 'retention-startup-001')
    control = create_control(settings)
    peers = doubles.bindings_double()
    peers.executor.recover.side_effect = None
    peers.executor.recover.return_value = None
    control.bind(replace(peers, control=control))
    await control.startup()
    with pytest.raises(TaskFault):
        await control.repository.get(old[0], old[2].task_id)
    entered = asyncio.Event()
    unblock = asyncio.Event()
    calls = []
    async def bounded_prune(*, batch_size):
        calls.append(batch_size)
        if len(calls) == 1:
            raise RuntimeError('CANARY-maintenance-credential')
        entered.set()
        await unblock.wait()
        return 0
    monkeypatch.setattr(control.repository, 'prune_history', bounded_prune)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert calls == [100, 100]
        assert 'Task history maintenance failed' in caplog.text
        assert 'CANARY' not in caplog.text
        stopping = asyncio.create_task(control.shutdown())
        await asyncio.sleep(.02)
        assert not stopping.done() and not control.accepting
        unblock.set()
        await asyncio.wait_for(stopping, 2)
        count = len(calls)
        await asyncio.sleep(.03)
        assert len(calls) == count
        assert control._maintenance_task is None
    finally:
        unblock.set()
        await control.shutdown()
