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
from unittest.mock import patch

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
                answer = await dispatch('execute', {'script': script})
                assert answer['success'] and answer['result'] == '1'
                await dispatch('save_reflex', {'name': 'increment', 'description': 'Synthetic mutation', 'script': script})
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
                    assert container.attrs['HostConfig']['PidsLimit'] == 256
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
    site.shutdown()
    print('LANE002_DOCKER_EVIDENCE=' + json.dumps(results), flush=True)


if __name__ == '__main__':
    asyncio.run(pid_benchmark() if '--pids' in sys.argv else main())
