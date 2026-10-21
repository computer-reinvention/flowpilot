import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.task_contracts import (
    InteractionKind, InteractionResponse, Principal, TaskStart, TaskState,
)
from backend.task_ports import Execution, Lease
from backend.task_settings import TaskSettings
from backend.tasks.executor import Executor
from backend.tasks.runtime import ConversationRuntime
from tests.fixtures.browser_tasks.doubles import bindings_double, observation_fixture


def execution():
    now = datetime.now(timezone.utc)
    return Execution(lease=Lease(principal=Principal(account_id='alice'), task_id='t',
                                session_id='s', profile_id='p', generation=1),
                     revision=2, request=TaskStart(task='synthetic task', idempotency_key='test-key1'),
                     admitted_at=now, deadline=now + timedelta(seconds=60))


class Runtime:
    def __init__(self, *args):
        self.declined = self.fenced = False
        self.started = asyncio.Event()
        self.close = AsyncMock()
        self.stop_writers = AsyncMock()

    async def run(self):
        self.started.set()
        return {'answer': 'Verified', 'metrics': {'tool_calls': 2}}


async def setup_executor(tmp_path, runtime_type=Runtime):
    bindings = bindings_double()
    view = SimpleNamespace(state=TaskState.RUNNING, revision=2)
    for name in ('get', 'progress', 'finish', 'release_browser'):
        method = getattr(bindings.control, name)
        method.side_effect = None
        method.return_value = view
    runtimes = []
    def factory(*args):
        runtime = runtime_type(*args)
        runtimes.append(runtime)
        return runtime
    executor = Executor(TaskSettings(state_dir=tmp_path), runtime_factory=factory)
    executor.bind(bindings)
    await executor.startup()
    return executor, bindings, runtimes, view


@pytest.mark.asyncio
async def test_execute_duplicate_dispatch_does_not_replay(tmp_path):
    executor, bindings, runtimes, view = await setup_executor(tmp_path)
    request = execution()
    await asyncio.gather(executor.execute(request), executor.execute(request))
    assert len(runtimes) == 1
    runtimes[0].close.assert_awaited_once()
    assert bindings.control.finish.call_args.args[2] == TaskState.COMPLETED
    assert bindings.control.finish.call_args.kwargs['result'].metrics.tool_calls == 2
    bindings.control.release_browser.assert_awaited_once_with(request.lease, profile_saved=True)


@pytest.mark.asyncio
async def test_cancel_stops_writers_before_terminal(tmp_path):
    class Waiting(Runtime):
        async def run(self):
            self.started.set()
            await asyncio.Event().wait()
    executor, bindings, runtimes, view = await setup_executor(tmp_path, Waiting)
    request = execution()
    running = asyncio.create_task(executor.execute(request))
    while not runtimes:
        await asyncio.sleep(0)
    await runtimes[0].started.wait()
    view.state = TaskState.CANCELLING
    await executor.cancel(request.lease)
    await running
    assert runtimes[0].fenced
    runtimes[0].stop_writers.assert_awaited()
    assert bindings.control.finish.call_args.args[2] == TaskState.CANCELLED


@pytest.mark.asyncio
async def test_failed_save_never_reports_completed(tmp_path):
    class BrokenSave(Runtime):
        def __init__(self, *args):
            super().__init__(*args)
            self.close.side_effect = RuntimeError('secret raw error')
    executor, bindings, _, _ = await setup_executor(tmp_path, BrokenSave)
    await executor.execute(execution())
    call = bindings.control.finish.call_args
    assert call.args[2] == TaskState.FAILED
    assert call.kwargs['error'].code == 'profile_recovery_required'
    assert 'secret' not in str(call)
    assert bindings.control.release_browser.call_args.kwargs == {'profile_saved': False}


@pytest.mark.asyncio
async def test_interaction_uses_frozen_ports_and_fresh_inspection(tmp_path):
    bindings = bindings_double()
    runtime = ConversationRuntime(TaskSettings(state_dir=tmp_path), execution(), bindings)
    runtime.session = SimpleNamespace(browser=object())
    runtime._select_page = AsyncMock()
    async def respond(lease, interaction):
        return InteractionResponse(kind='browser', generation=interaction.generation,
                                   idempotency_key='response1', browser_resolved=True)
    bindings.control.wait_for_interaction.side_effect = respond
    bindings.observation.observe.side_effect = None
    bindings.observation.observe.return_value = observation_fixture()
    assert await runtime.interact('Please help') == {'browser_resolved': True}
    assert runtime.pause is None
    bindings.observation.observe.assert_awaited_once_with(runtime.session.browser)


@pytest.mark.asyncio
async def test_decline_fences_execution(tmp_path):
    bindings = bindings_double()
    runtime = ConversationRuntime(TaskSettings(state_dir=tmp_path), execution(), bindings)
    async def respond(lease, interaction):
        return InteractionResponse(kind='confirmation', generation=interaction.generation,
                                   idempotency_key='response1', confirmed=False)
    bindings.control.wait_for_interaction.side_effect = respond
    with pytest.raises(asyncio.CancelledError):
        await runtime.interact('Submit?', 'confirmation')
    assert runtime.fenced and runtime.declined
    bindings.control.require_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_browser_pause_is_rejected(tmp_path):
    runtime = ConversationRuntime(TaskSettings(state_dir=tmp_path), execution(), bindings_double())
    with pytest.raises(Exception, match='stale_generation'):
        await runtime.browser('old', 1)
