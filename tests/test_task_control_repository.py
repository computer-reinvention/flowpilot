import asyncio
import sqlite3

import pytest

from backend.task_contracts import Principal, TaskStart, TaskFault, TaskState, TaskResult
from backend.task_settings import TaskSettings


@pytest.mark.asyncio
async def test_atomic_admission_claim_and_restart(tmp_path):
    from backend.tasks.repository import Repository
    settings = TaskSettings(state_dir=tmp_path)
    repo = Repository(settings, b'x' * 32)
    await repo.initialize()
    await repo.initialize()
    owner = Principal(account_id='owner')
    request = TaskStart(task='synthetic task', idempotency_key='admission-001')
    admissions = await asyncio.gather(*(repo.admit(owner, request) for _ in range(12)))
    assert len({a.task_id for a in admissions}) == 1
    claims = await asyncio.gather(*(repo.claim(owner, admissions[0].task_id) for _ in range(12)))
    assert sum(c is not None for c in claims) == 1
    with pytest.raises(TaskFault):
        await repo.admit(owner, request.model_copy(update={'task': 'changed'}))
    with pytest.raises(TaskFault) as error:
        await repo.get(Principal(account_id='foreign'), admissions[0].task_id)
    assert error.value.error.code == 'not_found'
    restarted = Repository(settings, b'x' * 32)
    await restarted.initialize()
    leases = await restarted.interrupt_active()
    assert len(leases) == 1
    assert (await restarted.get(owner, admissions[0].task_id)).state == TaskState.INTERRUPTED
    assert await restarted.claim(owner, admissions[0].task_id) is None
    with sqlite3.connect(restarted.path) as db:
        assert db.execute('select count(*) from bt_tasks').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_additive_upgrade_of_legacy_platform_database(tmp_path, monkeypatch):
    from backend import db
    from backend.tasks.repository import Repository
    path = tmp_path / 'tasks.sqlite3'
    monkeypatch.setattr(db, 'DB_PATH', path)
    await db.init_db()
    with sqlite3.connect(path) as database:
        database.execute("INSERT INTO users VALUES('legacy','legacy@example.test','synthetic_hash','Legacy','2026-01-01')")
    repository = Repository(TaskSettings(state_dir=tmp_path), b'x' * 32)
    await repository.initialize()
    await repository.initialize()
    with sqlite3.connect(path) as database:
        assert database.execute('SELECT id FROM users').fetchall() == [('legacy',)]
        assert database.execute('SELECT version FROM bt_schema').fetchall() == [(1,)]


def test_factories_do_not_touch_disk(tmp_path):
    from backend.tasks.control import create_control
    settings = TaskSettings(state_dir=tmp_path / 'absent')
    control = create_control(settings)
    assert not settings.state_dir.exists()
    assert not control.accepting and control.repository is None


@pytest.mark.asyncio
async def test_old_duplicate_release_cannot_quarantine_new_profile_lease(tmp_path):
    from backend.tasks.repository import Repository
    repo = Repository(TaskSettings(state_dir=tmp_path), b'x' * 32)
    await repo.initialize()
    owner = Principal(account_id='owner')
    first = await repo.admit(owner, TaskStart(task='first', idempotency_key='release-first-001'))
    execution = await repo.claim(owner, first.task_id)
    await repo.transition(execution.lease, execution.revision, TaskState.COMPLETED, result=TaskResult(answer='done'))
    await repo.release_browser(execution.lease, profile_saved=True)
    second = await repo.admit(owner, TaskStart(task='second', session_id=first.session_id, idempotency_key='release-second-001'))
    await repo.release_browser(execution.lease, profile_saved=False)
    assert (await repo.list_profiles(owner)).items[0].state == 'leased'
    second_execution = await repo.claim(owner, second.task_id)
    assert second_execution.lease.generation > execution.lease.generation


@pytest.mark.asyncio
async def test_recover_can_attest_saved_profile_through_release_port(tmp_path, monkeypatch):
    from backend.tasks.repository import Repository
    from backend.tasks.control import create_control
    from backend import auth
    from dataclasses import replace
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('release_doubles', Path(__file__).parent / 'fixtures/browser_tasks/doubles.py')
    doubles = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doubles)
    monkeypatch.setenv('JWT_SECRET', 'synthetic-test-secret-' * 3)
    monkeypatch.setattr(auth, 'JWT_SECRET', 'synthetic-test-secret-' * 3)
    settings = TaskSettings(state_dir=tmp_path)
    repo = Repository(settings, b'x' * 32)
    await repo.initialize()
    owner = Principal(account_id='owner')
    first = await repo.admit(owner, TaskStart(task='first', idempotency_key='recovery-first-001'))
    await repo.claim(owner, first.task_id)
    control = create_control(settings)
    peers = doubles.bindings_double()
    async def recover(leases):
        for lease in leases:
            await control.release_browser(lease, profile_saved=True)
    peers.executor.recover.side_effect = recover
    control.bind(replace(peers, control=control))
    await control.startup()
    assert (await control.list_profiles(owner)).items[0].state == 'available'
    await control.shutdown()
