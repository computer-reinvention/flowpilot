"""Opt-in synthetic Docker smoke, executed INSIDE a backend-network container.

Run this file as a module (not pytest) using the lane's handoff command. It never
calls a model/provider or a real website. A unique SANDBOX_NETWORK is required.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import sys
import os
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.task_contracts import Principal
from backend.task_ports import Lease
from backend.task_settings import TaskSettings
from backend.tasks.browser_adapter import TaskSandbox
from backend.tasks.memory import ProfileStore


async def conversation_smoke(settings):
    from backend.task_contracts import TaskStart
    from backend.task_ports import Execution
    from backend.tasks.runtime import ConversationRuntime
    from tests.fixtures.browser_tasks.doubles import bindings_double, observation_fixture
    bindings = bindings_double()
    for method in ('observe', 'require_fresh', 'after_action'):
        getattr(bindings.observation, method).side_effect = None
    bindings.observation.observe.return_value = observation_fixture()
    script = 'async () => { let n=Number(localStorage.getItem("counter")||0)+1; localStorage.setItem("counter",String(n)); return n; }'
    metrics = []
    for turn in (1, 2):
        async def inference(task, system, definitions, dispatch, **kwargs):
            await dispatch('navigate', {'url': 'http://lane002-smoke:8765'})
            await dispatch('observe', {})
            if turn == 1:
                answer = await dispatch('execute', {'script': script,
                    'precondition': 'async () => localStorage.getItem("counter") === null',
                    'postcondition': 'async () => localStorage.getItem("counter") === "1"'})
                assert answer['success'] and answer['result'] == '1'
                await dispatch('save_reflex', {'name': 'increment', 'description': 'Synthetic mutation', 'script': script})
                await dispatch('human_help', {'reason': 'Synthetic viewport metadata check'})
                await dispatch('observe', {})
                await dispatch('human_help', {'reason': 'Synthetic next-pause drain check'})
                await dispatch('observe', {})
            else:
                answer = await dispatch('run_reflex', {'name': 'increment'})
                assert not answer.get('error')
                assert answer['extracted_content'] == '2'
            await dispatch('complete', {'verify': f'async () => localStorage.getItem("counter") === "{turn}"',
                                        'summary': f'Verified counter {turn}'})
        now = datetime.now(timezone.utc)
        lease = Lease(principal=Principal(account_id='conversation'), task_id=f't{turn}',
                      session_id='followup', profile_id='conversation', generation=turn)
        execution = Execution(lease=lease, revision=2, request=TaskStart(task=f'Explicit mutation {turn}',
                              idempotency_key=f'conversation-{turn}'), admitted_at=now,
                              deadline=now + timedelta(seconds=120))
        runtime = ConversationRuntime(settings, execution, bindings)
        prior_handles = []
        async def assist(lease, interaction):
            from backend.task_contracts import BrowserInput, InteractionResponse, TaskFault, TaskState
            bindings.control.get.side_effect = None
            bindings.control.get.return_value = SimpleNamespace(state=TaskState.WAITING_FOR_BROWSER,
                                                                 interaction=interaction)
            await runtime._human_page.set_viewport_size({'width': 831, 'height': 617})
            handle = await runtime.browser(interaction.interaction_id, interaction.generation)
            metadata = await handle.metadata()
            assert str(metadata.url) == 'http://lane002-smoke:8765/'
            assert (metadata.viewport_width, metadata.viewport_height) == (831, 617)
            assert await handle.screenshot()
            frames = handle.frames()
            frame = await anext(frames)
            assert frame.startswith(b'\xff\xd8')
            await frames.aclose()
            page = runtime._human_page
            await page.evaluate('window.syntheticKeyUps=[]; document.addEventListener("keyup", e => syntheticKeyUps.push(e.key))')
            if not prior_handles:
                entered, proceed = asyncio.Event(), asyncio.Event()
                original_down = page.keyboard.down
                async def delayed_down(key):
                    entered.set()
                    await proceed.wait()
                    await original_down(key)
                with patch.object(page.keyboard, 'down', AsyncMock(side_effect=delayed_down)):
                    writing = asyncio.create_task(handle.send_input(BrowserInput(kind='keydown', key='Shift')))
                    await entered.wait()
                    draining = asyncio.create_task(runtime.release_human(interaction.interaction_id, interaction.generation))
                    await asyncio.sleep(0)
                    assert not draining.done()
                    try:
                        await runtime.browser(interaction.interaction_id, interaction.generation)
                    except TaskFault:
                        pass
                    else:
                        raise AssertionError('API drain allowed a future human handle')
                    proceed.set()
                    await asyncio.gather(writing, draining)
            else:
                await handle.send_input(BrowserInput(kind='keydown', key='Shift'))
                await prior_handles[-1].release()
                assert runtime.keys == {'Shift'}  # Old task/pause cannot release this writer.
                await handle.release()  # Viewer's own release before direct API response.
            await runtime.release_human(interaction.interaction_id, interaction.generation)
            await runtime.release_human(interaction.interaction_id, interaction.generation)
            await handle.release()
            assert not runtime.keys
            assert 'Shift' in await page.evaluate('window.syntheticKeyUps')
            prior_handles.append(handle)
            print('LANE002_METADATA_EVIDENCE=' + metadata.model_dump_json(), flush=True)
            print(f'LANE002_HUMAN_DRAIN_EVIDENCE=pause-{interaction.generation},fenced,inflight-drained,Shift-released,idempotent', flush=True)
            return InteractionResponse(kind='browser', generation=interaction.generation,
                                       idempotency_key='synthetic-help1', browser_resolved=True)
        bindings.control.wait_for_interaction.side_effect = assist
        try:
            with patch('backend.pi_browser.run_pi', inference):
                result = await runtime.run()
            assert result['answer'] == f'Verified counter {turn}'
            metrics.append(result['metrics'])
        finally:
            await runtime.close()
    assert metrics[0]['saved'] == 1 and metrics[1]['reused'] == 1
    assert bindings.observation.require_fresh.await_count >= 4
    print('LANE002_CONVERSATION_EVIDENCE=' + json.dumps(metrics), flush=True)


async def cancellation_smoke(settings):
    from backend.tasks.executor import Executor
    from backend.task_ports import Execution
    from backend.task_contracts import TaskStart, TaskState
    from tests.fixtures.browser_tasks.doubles import bindings_double, observation_fixture
    bindings = bindings_double()
    view = SimpleNamespace(state=TaskState.RUNNING, revision=2, task_id='cancel', session_id='s', profile_id='p')
    for method in ('get', 'progress', 'finish', 'release_browser'):
        getattr(bindings.control, method).side_effect = None
        getattr(bindings.control, method).return_value = view
    for method in ('observe', 'require_fresh', 'after_action'):
        getattr(bindings.observation, method).side_effect = None
    bindings.observation.observe.return_value = observation_fixture()
    started = asyncio.Event()
    async def inference(task, system, definitions, dispatch, **kwargs):
        await dispatch('navigate', {'url': 'http://lane002-smoke:8765'})
        await dispatch('observe', {})
        started.set()
        await dispatch('execute', {'script': 'async () => { localStorage.setItem("cancelCounter","1"); await new Promise(r => setTimeout(r,60000)); localStorage.setItem("cancelCounter","2"); }',
            'precondition': 'async () => localStorage.getItem("cancelCounter") === null',
            'postcondition': 'async () => localStorage.getItem("cancelCounter") === "2"'})
        raise AssertionError('Cancelled model/script continued')
    now = datetime.now(timezone.utc)
    lease = Lease(principal=Principal(account_id='cancel'), task_id='cancel', session_id='s', profile_id='p', generation=1)
    execution = Execution(lease=lease, revision=2, admitted_at=now, deadline=now+timedelta(seconds=90),
                          request=TaskStart(task='Synthetic cancellation', idempotency_key='cancel123'))
    executor = Executor(settings)
    executor.bind(bindings)
    await executor.startup()
    with patch('backend.pi_browser.run_pi', inference):
        task = asyncio.create_task(executor.execute(execution))
        await asyncio.wait_for(started.wait(), 30)
        runtime = next(iter(executor.active.values())).runtime
        page = next(p for p in runtime.session.context.pages if p.url.startswith('http://lane002-smoke:8765'))
        for _ in range(100):
            if await page.evaluate('localStorage.getItem("cancelCounter")') == '1':
                break
            await asyncio.sleep(.01)
        else:
            raise AssertionError('Synthetic script did not start')
        view.state = TaskState.CANCELLING
        cancelled_at = asyncio.get_running_loop().time()
        await executor.cancel(lease)
        await asyncio.wait_for(task, 40)
        elapsed = asyncio.get_running_loop().time() - cancelled_at
    state = bindings.control.finish.call_args.args[2]
    assert state == TaskState.CANCELLED, bindings.control.finish.call_args
    assert bindings.control.release_browser.call_args.kwargs['profile_saved']
    assert not executor.active
    assert elapsed <= settings.cancellation_seconds + 1
    from playwright.async_api import async_playwright
    store = ProfileStore(settings, lease.principal, lease.profile_id)
    sandbox = TaskSandbox(store.path, store.path.name, settings=settings, lease=lease)
    try:
        _, websocket = await sandbox.start()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(websocket)
            page = await browser.contexts[0].new_page()
            await page.goto('http://lane002-smoke:8765')
            assert await page.evaluate('localStorage.getItem("cancelCounter")') == '1'
            client = await browser.new_browser_cdp_session()
            try:
                await client.send('Browser.close')
            except Exception:
                assert not browser.is_connected()
    finally:
        try:
            await sandbox.close()
        finally:
            store.close()
    print(f'LANE002_CANCEL_EVIDENCE=script-terminated,container-stopped,profile-saved,cancelled,counter-restored-1,seconds={elapsed:.2f}', flush=True)


async def pid_benchmark():
    from playwright.async_api import async_playwright
    records = []
    with tempfile.TemporaryDirectory(prefix='lane002-pids-') as root:
        async with async_playwright() as playwright:
            for limit in (256, 1024):
                settings = TaskSettings(state_dir=Path(root), sandbox_pids=limit)
                lease = Lease(principal=Principal(account_id='benchmark'), task_id='pids', session_id='s',
                              profile_id=f'p{limit}', generation=1)
                store = ProfileStore(settings, lease.principal, lease.profile_id)
                sandbox = TaskSandbox(store.path, store.path.name, settings=settings, lease=lease)
                count, failure = 0, None
                try:
                    _, websocket = await sandbox.start()
                    browser = await playwright.chromium.connect_over_cdp(websocket)
                    context = browser.contexts[0]
                    container = sandbox.manager.client.containers.get(sandbox.info['container_id'])
                    for number in range(24):
                        try:
                            page = await asyncio.wait_for(context.new_page(), 5)
                            await page.goto(f'data:text/html,<title>synthetic-{number}</title>', timeout=5000)
                            count += 1
                        except Exception as exc:
                            failure = type(exc).__name__
                            break
                    stats = container.exec_run(['sh', '-c', 'cat /sys/fs/cgroup/pids.current /sys/fs/cgroup/pids.events']).output.decode()
                    records.append({'limit': limit, 'opened_tabs': count, 'failure': failure, 'cgroup': stats})
                    client = await browser.new_browser_cdp_session()
                    try:
                        await asyncio.wait_for(client.send('Browser.close'), 5)
                    except Exception:
                        pass
                finally:
                    try:
                        await sandbox.close()
                    finally:
                        store.close()
    print('LANE002_PID_BENCHMARK=' + json.dumps(records), flush=True)


async def crash_smoke(recover=False):
    """Two separate containers/processes share ONLY the dedicated /state mount."""
    if not recover and os.getpid() == 1:
        raise RuntimeError('Crash harness requires docker run --init so SIGKILL is not ignored by namespace PID 1')
    from playwright.async_api import async_playwright
    from backend.task_contracts import TaskFault
    from backend.tasks.executor import Executor
    from backend.tasks.runtime import ConversationRuntime
    from backend.task_ports import Execution
    from backend.task_contracts import TaskStart
    from tests.fixtures.browser_tasks.doubles import bindings_double, observation_fixture
    settings = TaskSettings(state_dir=Path('/state'))
    lease = Lease(principal=Principal(account_id='crash'), profile_id='crash', task_id='crash', session_id='s', generation=1)
    metadata = settings.state_dir / 'crash.json'
    async with async_playwright() as playwright:
        if not recover:
            site = ThreadingHTTPServer(('0.0.0.0', 8765), Site)
            threading.Thread(target=site.serve_forever, daemon=True).start()
            bindings = bindings_double()
            for method in ('get', 'progress'):
                getattr(bindings.control, method).side_effect = None
                from backend.task_contracts import TaskState
                getattr(bindings.control, method).return_value = SimpleNamespace(state=TaskState.RUNNING, revision=2,
                    task_id=lease.task_id, session_id=lease.session_id, profile_id=lease.profile_id)
            for method in ('observe', 'require_fresh', 'after_action'):
                getattr(bindings.observation, method).side_effect = None
            bindings.observation.observe.return_value = observation_fixture()
            executor = Executor(settings)
            executor.bind(bindings)
            await executor.startup()
            async def inference(task, system, definitions, dispatch, **kwargs):
                await dispatch('navigate', {'url': 'http://lane002-smoke:8765'})
                await dispatch('observe', {})
                result = await dispatch('execute', {'script': 'async () => { localStorage.setItem("mutationCount", "1"); return 1; }',
                    'precondition': 'async () => localStorage.getItem("mutationCount") === null',
                    'postcondition': 'async () => localStorage.getItem("mutationCount") === "1"'})
                assert result['success']
                runtime = next(iter(executor.active.values())).runtime
                sandbox = runtime.session.sandbox
                metadata.write_text(json.dumps({'websocket': runtime.session.cdp_websocket_url,
                                                 'container_id': sandbox.info['container_id'],
                                                 'profile': runtime.session.store.path.name}))
                print('LANE002_CRASH_MUTATION=1; SIGKILL actual executor before completion/cleanup', flush=True)
                os.kill(os.getpid(), signal.SIGKILL)
            now = datetime.now(timezone.utc)
            execution = Execution(lease=lease, revision=2, admitted_at=now, deadline=now+timedelta(seconds=60),
                                  request=TaskStart(task='Synthetic crash mutation', idempotency_key='crash123'))
            with patch('backend.pi_browser.run_pi', inference):
                await executor.execute(execution)
        else:
            info = json.loads(metadata.read_text())
            browser = await playwright.chromium.connect_over_cdp(info['websocket'])
            page = next(p for p in browser.contexts[0].pages if p.url.startswith('http://lane002-smoke:8765'))
            assert await page.evaluate('localStorage.getItem("mutationCount")') == '1'
            def forbidden_engine(*args):
                raise AssertionError('Recovery must not replay an engine')
            bindings = bindings_double()
            executor = Executor(settings, runtime_factory=forbidden_engine)
            executor.bind(bindings)
            await executor.startup()
            await executor.recover(())
            assert (settings.state_dir / info['profile'] / 'recovery-required').exists()
            manager = executor.manager_factory(str(settings.state_dir))
            container = manager.client.containers.get(info['container_id'])
            container.reload()
            assert container.status == 'exited'
            now = datetime.now(timezone.utc)
            execution = Execution(lease=lease, revision=2, admitted_at=now, deadline=now+timedelta(seconds=30),
                                  request=TaskStart(task='Never replay', idempotency_key='no-replay1'))
            runtime = ConversationRuntime(settings, execution, bindings)
            try:
                await runtime.open()
            except TaskFault as exc:
                assert exc.error.code == 'profile_recovery_required'
            else:
                raise AssertionError('Recovery-required profile reopened')
            await executor.delete_profile(lease.principal, lease.profile_id)
            print('LANE002_CRASH_RECOVERY=stopped,quarantined,no-engine-replay,profile-deletion-cleaned-artifact', flush=True)


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'<title>Synthetic lane002</title><button>synthetic</button>')

    def log_message(self, *args):
        pass


async def main():
    from playwright.async_api import async_playwright
    site = ThreadingHTTPServer(('0.0.0.0', 8765), Site)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    results = []
    with tempfile.TemporaryDirectory(prefix='lane002-') as root:
        settings = TaskSettings(state_dir=Path(root))
        async with async_playwright() as playwright:
            for account, profile, expected in [('alice', 'p', None), ('alice', 'p', 'private'),
                                               ('alice', 'q', None), ('bob', 'p', None)]:
                principal = Principal(account_id=account)
                store = ProfileStore(settings, principal, profile)
                lease = Lease(principal=principal, task_id='smoke', session_id='s', profile_id=profile, generation=1)
                sandbox = TaskSandbox(store.path, store.path.name, settings=settings, lease=lease)
                browser = None
                try:
                    url, websocket = await sandbox.start()
                    container = sandbox.manager.client.containers.get(sandbox.info['container_id'])
                    container.reload()
                    assert not container.attrs['HostConfig']['PortBindings']
                    assert container.attrs['HostConfig']['PidsLimit'] == settings.sandbox_pids
                    assert container.attrs['HostConfig']['LogConfig'] == {
                        'Type': 'json-file', 'Config': {'max-size': '10m', 'max-file': '3'}}
                    browser = await playwright.chromium.connect_over_cdp(websocket)
                    context = browser.contexts[0]
                    page = await context.new_page()
                    await page.goto('http://lane002-smoke:8765')
                    assert await page.evaluate('localStorage.getItem("marker")') == expected
                    cookies = await context.cookies()
                    assert next((c['value'] for c in cookies if c['name'] == 'marker'), None) == expected
                    if account == 'alice' and profile == 'p' and expected is None:
                        await page.evaluate('localStorage.setItem("marker", "private"); document.cookie="marker=private;max-age=3600"')
                        store.record_turn('synthetic first task', {}, 'verified counter 1', {})
                        store.db.execute("INSERT INTO reflexes(domain,name,description,script) VALUES(?,?,?,?)",
                                         ('lane002-smoke:8765', 'count', 'Synthetic script', 'async () => true'))
                        store.db.commit()
                    if expected:
                        assert len(store.history()) == 1
                        assert len(store.scripts()) == 1
                    elif account != 'alice' or profile != 'p':
                        assert not store.history() and not store.scripts()
                    client = await browser.new_browser_cdp_session()
                    try:
                        await client.send('Browser.close')
                    except Exception:
                        assert not browser.is_connected()
                    results.append({'account': account, 'profile': profile, 'restored': bool(expected),
                                    'host_ports': False, 'pids_limit': 256})
                finally:
                    try:
                        await sandbox.close()
                    finally:
                        store.close()
            await conversation_smoke(settings)
            await cancellation_smoke(settings)
    site.shutdown()
    print('LANE002_DOCKER_EVIDENCE=' + json.dumps(results), flush=True)


if __name__ == '__main__':
    if '--crash' in sys.argv or '--recover' in sys.argv:
        asyncio.run(crash_smoke(recover='--recover' in sys.argv))
    else:
        asyncio.run(pid_benchmark() if '--pids' in sys.argv else main())
