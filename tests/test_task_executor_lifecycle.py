import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
async def test_cancelled_cleanup_still_settles_recovery_failure(tmp_path):
    class CancelledClose(Runtime):
        def __init__(self, *args):
            super().__init__(*args)
            self.close.side_effect = asyncio.CancelledError
    executor, bindings, _, _ = await setup_executor(tmp_path, CancelledClose)
    await executor.execute(execution())
    assert bindings.control.finish.call_args.args[2] == TaskState.FAILED
    assert not executor.active


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['stale_generation', 'readiness_timeout', 'no_progress', 'interrupted'])
async def test_observation_fault_keeps_typed_reason_and_stops_before_settlement(tmp_path, code):
    from backend.task_contracts import ErrorCode, TaskFault, task_error
    class Guarded(Runtime):
        async def run(self):
            raise TaskFault(task_error(ErrorCode(code), 'Synthetic observation guard blocked task'))
    executor, bindings, runtimes, _ = await setup_executor(tmp_path, Guarded)
    async def finish(lease, revision, state, **kwargs):
        runtimes[0].stop_writers.assert_awaited()
        runtimes[0].close.assert_awaited()
    bindings.control.finish.side_effect = finish
    await executor.execute(execution())
    call = bindings.control.finish.call_args
    assert call.kwargs['error'].code == code
    assert call.args[2] == (TaskState.INTERRUPTED if code == 'interrupted' else TaskState.FAILED)
    assert len(runtimes) == 1


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


@pytest.mark.asyncio
async def test_recover_never_replays_and_only_stops_task_profiles(tmp_path):
    from backend.tasks.memory import storage_key
    from backend.tasks.browser_adapter import host_key
    request = execution()
    profile = storage_key(request.lease.principal, request.lease.profile_id)
    task = MagicMock(labels={'flowpilot-task': '1', 'profile-key': profile,
                            'task-host': host_key(TaskSettings(state_dir=tmp_path))})
    invalid = MagicMock(labels={'flowpilot-task': '1', 'profile-key': '../escape'})
    manager = MagicMock()
    manager.client.containers.list.return_value = [task, invalid]
    bindings = bindings_double()
    bindings.control.release_browser.side_effect = None
    executor = Executor(TaskSettings(state_dir=tmp_path), manager_factory=lambda *args: manager)
    executor.bind(bindings)
    await executor.startup()
    await executor.recover((request.lease,))
    task.stop.assert_called_once_with(timeout=5)
    task.remove.assert_not_called()
    invalid.stop.assert_not_called()
    assert (tmp_path / profile / 'recovery-required').exists()
    assert not executor.active
    bindings.control.release_browser.assert_awaited_once_with(request.lease, profile_saved=False)


@pytest.mark.asyncio
async def test_human_resume_drains_inflight_input_and_releases_keys(tmp_path):
    from backend.task_contracts import BrowserInput, Interaction
    from backend.tasks.runtime import HumanBrowser
    runtime = ConversationRuntime(TaskSettings(state_dir=tmp_path), execution(), bindings_double())
    now = datetime.now(timezone.utc)
    interaction = Interaction(interaction_id='help', generation=1, kind='browser',
                              message='Help', expires_at=now + timedelta(seconds=60))
    runtime.pause = interaction
    runtime.bindings.control.get.side_effect = None
    runtime.bindings.control.get.return_value = SimpleNamespace(state=TaskState.WAITING_FOR_BROWSER,
                                                               interaction=interaction)
    entered, release = asyncio.Event(), asyncio.Event()
    async def keydown(key):
        entered.set()
        await release.wait()
    page = SimpleNamespace(keyboard=SimpleNamespace(down=AsyncMock(side_effect=keydown), up=AsyncMock()))
    runtime.page = lambda: page
    handle = HumanBrowser(runtime, interaction)
    runtime.handles.append(handle)
    command = asyncio.create_task(handle.send_input(BrowserInput(kind='keydown', key='Shift')))
    await entered.wait()
    resume = asyncio.create_task(runtime.resume())
    await asyncio.sleep(0)
    assert not resume.done()
    release.set()
    await asyncio.gather(command, resume)
    page.keyboard.up.assert_awaited_once_with('Shift')
    assert handle.released and runtime.pause is None


@pytest.mark.asyncio
async def test_recover_refuses_a_profile_opened_by_another_process(tmp_path):
    from backend.tasks.memory import ProfileStore
    from backend.tasks.browser_adapter import host_key
    request = execution()
    settings = TaskSettings(state_dir=tmp_path)
    store = ProfileStore(settings, request.lease.principal, request.lease.profile_id)
    task = MagicMock(labels={'flowpilot-task': '1', 'profile-key': store.path.name, 'task-host': host_key(settings)})
    manager = MagicMock()
    manager.client.containers.list.return_value = [task]
    executor = Executor(settings, manager_factory=lambda *args: manager)
    executor.bind(bindings_double())
    try:
        with pytest.raises(BlockingIOError):
            await executor.recover(())
        task.stop.assert_not_called()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_metadata_reads_live_authorized_document_and_rejects_resume(tmp_path):
    from backend.task_contracts import Interaction, TaskFault
    from backend.tasks.runtime import HumanBrowser
    runtime = ConversationRuntime(TaskSettings(state_dir=tmp_path), execution(), bindings_double())
    now = datetime.now(timezone.utc)
    interaction = Interaction(interaction_id='help', generation=1, kind='browser', message='Help',
                              expires_at=now + timedelta(seconds=60))
    runtime.pause = interaction
    runtime.control.get.side_effect = None
    runtime.control.get.return_value = SimpleNamespace(state=TaskState.WAITING_FOR_BROWSER, interaction=interaction)
    page = SimpleNamespace(evaluate=AsyncMock(return_value={
        'url': 'https://example.test/live', 'viewport_width': 731, 'viewport_height': 419}))
    runtime.page = lambda: page
    handle = HumanBrowser(runtime, interaction)
    metadata = await handle.metadata()
    assert str(metadata.url) == 'https://example.test/live'
    assert (metadata.viewport_width, metadata.viewport_height) == (731, 419)
    await runtime.resume()
    with pytest.raises(TaskFault):
        await handle.metadata()
    page.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_deadline_interrupts_without_replay(tmp_path):
    class Waiting(Runtime):
        async def run(self):
            await asyncio.Event().wait()
    executor, bindings, runtimes, _ = await setup_executor(tmp_path, Waiting)
    request = execution().model_copy(update={'deadline': datetime.now(timezone.utc) + timedelta(seconds=.03)})
    await asyncio.wait_for(executor.execute(request), 1)
    assert len(runtimes) == 1
    runtimes[0].stop_writers.assert_awaited()
    assert bindings.control.finish.call_args.args[2] == TaskState.INTERRUPTED


@pytest.mark.asyncio
async def test_failed_stop_never_claims_cancelled_or_releases_saved_profile(tmp_path):
    class BrokenStop(Runtime):
        def __init__(self, *args):
            super().__init__(*args)
            self.stop_writers.side_effect = RuntimeError('Docker unavailable')
        async def run(self):
            raise asyncio.CancelledError
    executor, bindings, _, view = await setup_executor(tmp_path, BrokenStop)
    request = execution()
    # The run starts under a running view; subsequent settlement sees cancel intent.
    calls = 0
    async def get(*args):
        nonlocal calls
        calls += 1
        if calls > 1:
            view.state = TaskState.CANCELLING
        return view
    bindings.control.get.side_effect = get
    await executor.execute(request)
    assert bindings.control.finish.call_args.args[2] == TaskState.INTERRUPTED
    assert bindings.control.release_browser.call_args.kwargs == {'profile_saved': False}
