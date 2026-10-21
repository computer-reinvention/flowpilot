"""Dispatch receipts are bounded hints; real SQLite/control CAS is authority."""
import asyncio
from datetime import timedelta
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from backend.task_contracts import ErrorCode, Principal, TaskFault, TaskStart, TaskState, task_error
from backend.task_settings import TaskSettings
from backend.tasks.control import Control
from backend.tasks.executor import Executor
from backend.tasks.repository import Repository
from tests.fixtures.browser_tasks.doubles import bindings_double


RECEIPT_LIMIT = 256


async def setup(tmp_path):
    settings = TaskSettings(state_dir=tmp_path)
    repository = Repository(settings, b'synthetic-test-receipt-key-0000000')
    await repository.initialize()
    control = Control(settings)
    control.repository = repository
    counts = {'constructed': 0, 'effects': 0, 'closed': 0}
    holds = {}
    started = {}

    class Runtime:
        declined = fenced = False

        def __init__(self, settings, execution, bindings):
            counts['constructed'] += 1
            self.execution = execution

        async def run(self):
            counts['effects'] += 1
            task_id = self.execution.lease.task_id
            if task_id in holds:
                started[task_id].set()
                await holds[task_id].wait()
            return {'answer': 'Synthetic effect verified', 'metrics': {}}

        async def close(self):
            counts['closed'] += 1

        async def stop_writers(self):
            pass

    executor = Executor(settings, runtime_factory=Runtime)
    executor.bind(replace(bindings_double(), control=control, executor=executor))
    await executor.startup()
    return executor, control, repository, counts, holds, started


async def claim(repository, number, session_id=None):
    owner = Principal(account_id='synthetic-owner')
    request = TaskStart(task=f'Explicit synthetic effect {number}', session_id=session_id,
                        idempotency_key=f'synthetic-receipts-{number}')
    admission = await repository.admit(owner, request)
    execution = await repository.claim(owner, admission.task_id)
    assert execution is not None
    return execution


@pytest.mark.asyncio
async def test_receipts_bound_terminal_eviction_and_active_pin(tmp_path, monkeypatch):
    executor, control, repository, counts, holds, started = await setup(tmp_path)
    active = await claim(repository, 'held')
    task_id = active.lease.task_id
    holds[task_id], started[task_id] = asyncio.Event(), asyncio.Event()
    job = asyncio.create_task(executor.execute(active))
    await started[task_id].wait()
    oldest = None
    session = None
    total = RECEIPT_LIMIT * 2 + 5
    try:
        for number in range(total):
            execution = await claim(repository, number, session)
            session = execution.lease.session_id
            oldest = oldest or execution
            await executor.execute(execution)
            assert len(executor.seen) <= RECEIPT_LIMIT
            assert executor._key(active.lease) in executor.active
        assert executor._key(oldest.lease) not in executor.seen
        before = dict(counts)
        # Cache hit or miss must never construct a second runtime for an active
        # execution; terminal eviction must not even construct a runtime.
        await asyncio.gather(executor.execute(oldest), executor.execute(active), executor.execute(active))
        assert counts == before
        assert (await repository.get(oldest.lease.principal, oldest.lease.task_id)).state == TaskState.COMPLETED
    finally:
        holds[task_id].set()
        await job
    assert counts['constructed'] == counts['effects'] == total + 1
    assert not executor.active and len(executor.seen) <= RECEIPT_LIMIT
    # Existing peer-owned retention may remove the row; cache eviction/restart
    # must not reinterpret that absence as authority to reconstruct old work.
    from backend.tasks.repository import now
    future = now() + timedelta(days=31)
    monkeypatch.setattr('backend.tasks.repository.now', lambda: future)
    assert await repository.prune_history() == 100
    before = dict(counts)
    with pytest.raises(TaskFault) as raised:
        await executor.execute(oldest)
    assert raised.value.error.code == ErrorCode.NOT_FOUND
    assert counts == before and not executor.active


@pytest.mark.asyncio
async def test_wrong_generation_with_matching_owned_view_never_constructs_runtime(tmp_path):
    executor, control, repository, counts, _, _ = await setup(tmp_path)
    first = await claim(repository, 'first')
    await executor.execute(first)
    current = await claim(repository, 'current', first.lease.session_id)
    assert current.lease.generation > first.lease.generation
    stale = current.model_copy(update={'lease': current.lease.model_copy(update={'generation': first.lease.generation})})
    before = dict(counts)
    with pytest.raises(TaskFault) as raised:
        await executor.execute(stale)
    assert raised.value.error.code == ErrorCode.STALE_GENERATION
    assert counts == before
    view = await repository.get(current.lease.principal, current.lease.task_id)
    assert view.state == TaskState.RUNNING and view.revision == current.revision
    assert not executor.active
    await executor.execute(current)
    assert counts['constructed'] == counts['effects'] == 2


@pytest.mark.asyncio
async def test_evicted_running_receipt_cannot_reconsume_original_claim_revision(tmp_path):
    executor, control, repository, counts, _, _ = await setup(tmp_path)
    spent = await claim(repository, 'spent')
    finish = control.finish
    control.finish = AsyncMock(side_effect=TaskFault(task_error(ErrorCode.UNAVAILABLE, 'Synthetic lost terminal write')))
    with pytest.raises(TaskFault):
        await executor.execute(spent)
    control.finish = finish
    view = await repository.get(spent.lease.principal, spent.lease.task_id)
    assert view.state == TaskState.RUNNING and view.revision > spent.revision
    session = None
    for number in range(RECEIPT_LIMIT + 1):
        execution = await claim(repository, number, session)
        session = execution.lease.session_id
        await executor.execute(execution)
    assert executor._key(spent.lease) not in executor.seen
    before = dict(counts)
    await executor.execute(spent)
    assert counts == before
    assert await repository.get(spent.lease.principal, spent.lease.task_id) == view


@pytest.mark.asyncio
async def test_preflight_is_pinned_and_precedes_runtime_construction(tmp_path):
    executor, control, repository, counts, _, _ = await setup(tmp_path)
    execution = await claim(repository, 'concurrent')
    entered, proceed = asyncio.Event(), asyncio.Event()
    get = control.get
    async def blocked_get(*args):
        entered.set()
        await proceed.wait()
        return await get(*args)
    control.get = blocked_get
    job = asyncio.create_task(executor.execute(execution))
    await entered.wait()
    try:
        await asyncio.gather(*(executor.execute(execution) for _ in range(20)))
        assert counts['constructed'] == counts['effects'] == 0
        assert len(executor.active) == 1
    finally:
        proceed.set()
        await job
    assert counts['constructed'] == counts['effects'] == 1


@pytest.mark.asyncio
async def test_two_executor_instances_share_durable_dispatch_cas(tmp_path):
    executor, control, repository, counts, _, _ = await setup(tmp_path)
    execution = await claim(repository, 'two-instances')
    other = Executor(executor.settings, runtime_factory=executor.runtime_factory)
    other.bind(replace(executor.bindings, executor=other))
    await other.startup()
    get = control.get
    readers = 0
    barrier = asyncio.Event()
    async def simultaneous_read(*args):
        nonlocal readers
        view = await get(*args)
        if view.revision == execution.revision:
            readers += 1
            if readers == 2:
                barrier.set()
            await barrier.wait()
        return view
    control.get = simultaneous_read
    results = await asyncio.gather(executor.execute(execution), other.execute(execution), return_exceptions=True)
    assert all(result is None or isinstance(result, TaskFault) for result in results)
    assert sum(isinstance(result, TaskFault) for result in results) == 1
    assert counts['constructed'] == counts['effects'] == 1
    assert not executor.active and not other.active


@pytest.mark.asyncio
async def test_missing_pruned_or_foreign_task_never_constructs_runtime(tmp_path):
    executor, control, repository, counts, _, _ = await setup(tmp_path)
    execution = await claim(repository, 'owned')
    foreign = execution.model_copy(update={'lease': execution.lease.model_copy(update={
        'principal': Principal(account_id='foreign')})})
    missing = execution.model_copy(update={'lease': execution.lease.model_copy(update={'task_id': 'missing-pruned'})})
    for rejected in (foreign, missing):
        with pytest.raises(TaskFault) as raised:
            await executor.execute(rejected)
        assert raised.value.error.code == ErrorCode.NOT_FOUND
    assert counts['constructed'] == counts['effects'] == 0
    assert not executor.active


@pytest.mark.asyncio
async def test_runtime_constructor_failure_settles_and_remains_non_replayable(tmp_path):
    executor, control, repository, counts, _, _ = await setup(tmp_path)
    execution = await claim(repository, 'broken-constructor')
    def broken(*args):
        raise RuntimeError('Synthetic constructor failure')
    executor.runtime_factory = broken
    await executor.execute(execution)
    view = await repository.get(execution.lease.principal, execution.lease.task_id)
    assert view.state == TaskState.FAILED
    assert not executor.active
    executor.seen.clear()  # Process restart/cache loss is not execution authority.
    await executor.execute(execution)
    assert counts['constructed'] == counts['effects'] == 0
