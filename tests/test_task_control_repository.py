import asyncio
import sqlite3

import pytest

from backend.task_contracts import Principal, TaskStart, TaskFault, TaskState
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
