"""Acceptance-only instrumentation for the frozen executor constructor seam.

No HTTP failpoint endpoint. InputOperationGate must be injected UNDER the real
executor's tracked input-operation boundary, never wrapped outside BrowserHandle.
Otherwise the executor cannot know it must drain the held operation and the test
does not establish executor correctness. ReleaseAudit observes/delegates the real
release_human; it never drains inputs or repairs a missing production fence.
"""

import asyncio
import threading
from dataclasses import dataclass
from typing import Callable


async def wait_event(event, timeout=10):
    assert await asyncio.to_thread(event.wait, timeout), 'owned fixture barrier was not reached'


class InputOperationGate:
    def __init__(self, run_label, location, max_hold=30):
        assert run_label.startswith('task-acceptance-')
        assert location in {'inside_executor_input_registry', 'consumer_peer'}
        self.run_label = run_label
        self.location = location
        self.max_hold = max_hold
        self.entered = threading.Event()
        self.permit = threading.Event()
        self.finished = threading.Event()

    async def invoke(self, operation):
        """Call from the owned injected low-level input transport, while tracked."""
        self.entered.set()
        try:
            await wait_event(self.permit, self.max_hold)
            return await operation()
        finally:
            self.finished.set()


def pause_key(lease, interaction_id, generation):
    return (lease.principal.account_id, lease.task_id, lease.generation, interaction_id, generation)


class ReleaseAudit:
    """Transparent executor proxy, apart from explicit failure/timeout injection.

    All ordinary methods go to the REAL injected executor. This adapter does not
    implement the success-path fence, input drain or capability revocation.
    """
    def __init__(self, executor, run_label):
        assert run_label.startswith('task-acceptance-')
        self.inner = executor
        self.run_label = run_label
        self.current_pause = None
        self.calls = []
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.abort_timeout = threading.Event()
        self.mode = 'success'

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def browser(self, lease, interaction_id, generation):
        handle = await self.inner.browser(lease, interaction_id, generation)
        self.current_pause = pause_key(lease, interaction_id, generation)
        return handle

    def arm(self, mode):
        assert mode in {'success', 'failure', 'timeout'}
        assert self.current_pause is not None, 'open real human stream before arming audit'
        self.mode = mode
        self.calls.clear()
        self.entered.clear()
        self.finished.clear()
        self.abort_timeout.clear()

    async def release_human(self, lease, interaction_id, generation):
        key = pause_key(lease, interaction_id, generation)
        self.calls.append(key)
        self.entered.set()
        # Do not reject wrong generations on behalf of the implementation.
        # The public-target assertions require that control never calls us for one.
        if key == self.current_pause:
            if self.mode == 'failure':
                raise RuntimeError('synthetic owned release failure')
            if self.mode == 'timeout':
                # Control must cancel this await within its configured budget.
                while not self.abort_timeout.is_set():
                    await asyncio.sleep(.01)
                raise RuntimeError('synthetic timeout barrier abandoned')
        result = await self.inner.release_human(lease, interaction_id, generation)
        self.finished.set()
        return result


@dataclass
class DirectResumeTarget:
    owner: object
    receipt: dict
    interaction: dict
    grant: dict
    gate: InputOperationGate
    audit: ReleaseAudit
    counters: Callable[[], dict]
    timeout: float = 10
    observation_window: float = .15
    proof_kind: str = 'assembled'


async def verify_direct_api_resume(target, mode):
    """Real HTTP/WebSocket consumer. Caller owns the disposable service/fixture.

    Every mode starts a live stream and holds a human input. The resume request
    goes to the account-authenticated interaction endpoint, NOT viewer /respond.
    """
    from websockets.asyncio.client import connect
    from .client import Client
    from .wire import error, ordinary_safe

    assert target.proof_kind in {'assembled', 'consumer_peer'}
    assert target.audit.run_label == target.gate.run_label
    if target.proof_kind == 'assembled':
        assert target.gate.location == 'inside_executor_input_registry'
    owner = target.owner
    capability = target.grant['assistance_url']
    path = target.receipt['status_url'] + '/interactions/' + target.interaction['interaction_id'] + '/respond'
    response = {'generation': target.interaction['generation'], 'kind': 'browser', 'browser_resolved': True,
                'idempotency_key': target.gate.run_label + ':direct:' + mode}
    websocket_url = owner.base_url.replace('http://', 'ws://', 1).replace('https://', 'wss://', 1) + capability + '/stream'
    input_pending = resume_pending = None
    baseline = target.counters()

    async def request(client, method, route, body=None, headers=None):
        return await asyncio.to_thread(client.request, method, route, body, headers)

    def still_paused(current):
        assert current['state'] == 'waiting_for_browser'
        assert current['interaction'] == target.interaction
        assert target.counters() == baseline, 'agent website effects started before successful drain'
        ordinary_safe(current)

    try:
        async with connect(websocket_url, origin=owner.base_url, proxy=None,
                           open_timeout=target.timeout, close_timeout=1, max_size=2 * 1024 * 1024) as stream:
            first_frame = await asyncio.wait_for(stream.recv(), target.timeout)
            assert isinstance(first_frame, bytes) and first_frame.startswith(b'\xff\xd8')
            target.audit.arm(mode)
            input_pending = asyncio.create_task(request(Client(owner.base_url), 'POST', capability + '/input',
                                                        {'kind': 'move', 'x': 3, 'y': 3}, {'Origin': owner.base_url}))
            await wait_event(target.gate.entered, target.timeout)
            assert not input_pending.done(), 'input fixture did not actually hold an operation'
            # Invalid pause generation must not drain/fence the legitimate handle.
            wrong = await request(owner, 'POST', path, {**response, 'generation': response['generation'] + 1})
            error(wrong.status, wrong.body, wrong.headers)
            assert wrong.body['error']['code'] == 'stale_generation'
            assert target.audit.calls == [], 'wrong generation invoked release_human'
            assert not target.gate.finished.is_set()
            still_paused(await asyncio.to_thread(owner.get, target.receipt['task_id']))
            second_frame = await asyncio.wait_for(stream.recv(), target.timeout)
            assert isinstance(second_frame, bytes), 'wrong generation fenced legitimate stream'

            resume_pending = asyncio.create_task(request(owner, 'POST', path, response))
            await wait_event(target.audit.entered, target.timeout)
            assert target.audit.calls == [target.audit.current_pause], 'release used the wrong execution/pause binding'
            if mode == 'success':
                await asyncio.sleep(target.observation_window)
                assert not resume_pending.done(), 'direct API resolved before in-flight input drained'
                assert not target.audit.finished.is_set(), 'executor drain ignored held input'
                assert not input_pending.done()
                still_paused(await asyncio.to_thread(owner.get, target.receipt['task_id']))
                target.gate.permit.set()
                input_reply = await asyncio.wait_for(input_pending, target.timeout)
                assert input_reply.status == 200 and input_reply.body == {'accepted': True}
                resumed = await asyncio.wait_for(resume_pending, target.timeout)
                assert resumed.status == 202 and target.audit.finished.is_set()
                deadline = asyncio.get_running_loop().time() + target.timeout
                while target.counters().get('agent', 0) != baseline.get('agent', 0) + 1:
                    assert asyncio.get_running_loop().time() < deadline, 'no real agent continuation effect observed'
                    await asyncio.sleep(.02)
                # Former capability must no longer grant state or late input.
                for method, suffix, body in [('GET', '/state', None), ('POST', '/input', {'kind': 'release'})]:
                    late = await request(Client(owner.base_url), method, capability + suffix, body,
                                         {'Origin': owner.base_url} if method == 'POST' else None)
                    error(late.status, late.body, late.headers)
                    assert late.body['error']['code'] == 'assistance_expired'
            else:
                rejected = await asyncio.wait_for(resume_pending, target.timeout)
                assert rejected.status >= 400, 'release failure/timeout was accepted'
                error(rejected.status, rejected.body, rejected.headers)
                assert not target.audit.finished.is_set()
                still_paused(await asyncio.to_thread(owner.get, target.receipt['task_id']))
                state = await request(Client(owner.base_url), 'GET', capability + '/state')
                assert state.status == 200 and state.body['interaction'] == target.interaction
                assert not input_pending.done(), 'failed release unexpectedly completed held input'
            return {'proof_kind': target.proof_kind, 'mode': mode, 'active_stream': True,
                    'held_input': True, 'direct_authenticated_response': True,
                    'wrong_generation_releases': 0, 'agent_effects': target.counters().get('agent', 0) - baseline.get('agent', 0)}
    finally:
        # Unblock only this run's input/fault tasks, including on failed assertions.
        target.gate.permit.set()
        target.audit.abort_timeout.set()
        for task in (input_pending, resume_pending):
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), 5)
                except (Exception, asyncio.CancelledError):
                    task.cancel()


async def run_constructor_resume_targets(evidence):
    """Optional fixed local factory; absence is BLOCKED, never a fake pass.

    Parent supplies acceptance-only assembled_resume.create_target(mode), an async
    context manager that composes actual services via their documented constructor
    factories, inserts ReleaseAudit at TaskBindings.executor and InputOperationGate
    below real input tracking, and owns all cleanup. No public failpoint API.
    """
    import importlib
    module_name = 'tests.task_acceptance_support.assembled_resume'
    requirements = {'success': 'direct_api_resume_drain', 'failure': 'resume_release_failure',
                    'timeout': 'resume_release_timeout'}
    try:
        factory = importlib.import_module(module_name).create_target
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        for requirement in [*requirements.values(), 'resume_wrong_generation']:
            evidence.record(requirement, 'blocked', 'owned assembled executor/input constructor adapter not yet supplied; no public failpoint substitute')
        return
    for mode, requirement in requirements.items():
        try:
            async with factory(mode) as target:
                assert target.proof_kind == 'assembled', 'consumer peers cannot satisfy integrated requirements'
                observations = await verify_direct_api_resume(target, mode)
        except Exception as exc:
            evidence.record(requirement, 'failed', 'direct API human-writer fence target failed', exception_type=type(exc).__name__)
            evidence.requirements[requirement]['outcome'] = 'failed'
        else:
            evidence.record(requirement, 'passed', 'real services with owned constructor fault instrumentation', **observations)
            evidence.requirements[requirement]['outcome'] = 'passed'
    if all(evidence.requirements[key]['outcome'] == 'passed' for key in requirements.values()):
        evidence.requirements['resume_wrong_generation']['outcome'] = 'passed'
