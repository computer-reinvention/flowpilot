"""Strict local protocol peers challenge the target driver, NOT service proof.

No producer implementation is imported. Deliberately broken peers must fail the
same assertions that will later run against assembled constructor-injected peers.
"""

import asyncio
import base64
import hashlib
import json
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from tests.task_acceptance_support.client import Client
from tests.task_acceptance_support.evidence import Evidence
from tests.task_acceptance_support.release_fence import (
    DirectResumeTarget, InputOperationGate, ReleaseAudit, run_constructor_resume_targets,
    verify_direct_api_resume, wait_event,
)
from tests.task_acceptance_support.site import synthetic_site
from tests.task_acceptance_support.wire import PREFIX


@contextmanager
def resume_consumer_peer(site, defect=None):
    """Local disposable HTTP/WebSocket provider; cannot establish service acceptance."""
    run_label = site.run_id
    gate = InputOperationGate(run_label, 'consumer_peer', max_hold=5)
    lease = SimpleNamespace(principal=SimpleNamespace(account_id='owner'), task_id='task_resume', generation=2)
    interaction = {'interaction_id': 'pause_resume', 'generation': 9, 'kind': 'browser',
                   'message': 'Synthetic pause', 'expires_at': '2030-01-01T00:10:00Z'}
    status_path = PREFIX + '/task_resume'
    capability = PREFIX + '/help/' + 'synthetic_consumer_capability_00000001'
    state = {'task_id': 'task_resume', 'session_id': 'session_resume', 'profile_id': 'profile_resume',
             'state': 'waiting_for_browser', 'revision': 3, 'interaction': interaction,
             'created_at': '2030-01-01T00:00:00Z', 'updated_at': '2030-01-01T00:00:00Z'}
    stopped = threading.Event()
    released = threading.Event()
    failures = []

    class PeerExecutor:
        async def browser(self, lease, interaction_id, generation):
            return object()

        async def release_human(self, lease, interaction_id, generation):
            if generation != 9:
                raise ValueError('wrong pause')
            if defect != 'ignore_input_drain':
                await wait_event(gate.finished, 5)
            released.set()

    audit = ReleaseAudit(PeerExecutor(), run_label)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def fault(self, code, status, retryable=False):
            self.reply(status, {'error': {'code': code, 'message': 'Synthetic protocol boundary', 'retryable': retryable}})

        def do_GET(self):
            if self.path == status_path:
                assert self.headers.get('Authorization') == 'Bearer owner-token'
                return self.reply(200, state)
            if self.path == capability + '/state':
                if state['state'] != 'waiting_for_browser':
                    return self.fault('assistance_expired', 410)
                return self.reply(200, {'interaction': interaction, 'url': site.url,
                                        'viewport_width': 800, 'viewport_height': 600})
            assert self.path == capability + '/stream'
            assert self.headers.get('Authorization') is None
            assert self.headers.get('Origin') == self.server.base_url
            asyncio.run(audit.browser(lease, interaction['interaction_id'], 9))
            accept = base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key'] +
                                                   '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', accept)
            self.end_headers()
            frame = b'\xff\xd8synthetic-peer-frame\xff\xd9'
            try:
                while not stopped.is_set() and state['state'] == 'waiting_for_browser':
                    self.wfile.write(bytes([0x82, len(frame)]) + frame)
                    self.wfile.flush()
                    stopped.wait(.02)
                self.wfile.write(b'\x88\x00')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        def do_POST(self):
            try:
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path == capability + '/input':
                    assert self.headers.get('Authorization') is None
                    if state['state'] != 'waiting_for_browser':
                        return self.fault('assistance_expired', 410)
                    assert data == {'kind': 'move', 'x': 3, 'y': 3}
                    async def operation():
                        return None
                    asyncio.run(gate.invoke(operation))
                    return self.reply(200, {'accepted': True})
                assert self.path == status_path + '/interactions/pause_resume/respond'
                assert self.headers.get('Authorization') == 'Bearer owner-token'
                if data['generation'] != 9:
                    if defect == 'wrong_generation_releases':
                        try:
                            asyncio.run(audit.release_human(lease, 'pause_resume', data['generation']))
                        except ValueError:
                            pass
                    return self.fault('stale_generation', 409)
                try:
                    if defect == 'wake_before_drain':
                        site.effect('agent')
                    if defect != 'no_release_call':
                        asyncio.run(asyncio.wait_for(audit.release_human(lease, 'pause_resume', 9), .1 if audit.mode == 'timeout' else 5))
                except (RuntimeError, TimeoutError):
                    if defect != 'resolve_after_failure':
                        return self.fault('unavailable', 503, True)
                state.update(state='running', revision=4)
                state.pop('interaction', None)
                site.effect('agent')  # Consumer peer's durable continuation oracle, NOT a real agent.
                return self.reply(202, state)
            except Exception as exc:
                failures.append(type(exc).__name__)
                self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.base_url = f'http://127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield DirectResumeTarget(Client(server.base_url, 'owner-token'), {'task_id': 'task_resume', 'status_url': status_path},
                                 interaction, {'assistance_url': capability}, gate, audit, site.counters,
                                 timeout=1.5, observation_window=.05, proof_kind='consumer_peer')
        assert not failures, 'protocol peer failed outside asserted boundary'
    finally:
        gate.permit.set()
        audit.abort_timeout.set()
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('mode', ['success', 'failure', 'timeout'])
async def test_consumer_driver_requires_direct_api_fence_with_active_stream_and_held_input(tmp_path, mode):
    with synthetic_site(tmp_path) as site, resume_consumer_peer(site) as target:
        observed = await verify_direct_api_resume(target, mode)
        assert observed['proof_kind'] == 'consumer_peer'
        assert observed['wrong_generation_releases'] == 0
        assert observed['agent_effects'] == (1 if mode == 'success' else 0)


@pytest.mark.parametrize('defect,mode', [('ignore_input_drain', 'success'), ('no_release_call', 'success'),
                                       ('wrong_generation_releases', 'success'), ('resolve_after_failure', 'failure'),
                                       ('wake_before_drain', 'success')])
async def test_consumer_driver_rejects_producer_regressions_instead_of_mock_passing(tmp_path, defect, mode):
    with synthetic_site(tmp_path) as site, resume_consumer_peer(site, defect) as target:
        with pytest.raises((AssertionError, KeyError)):
            await verify_direct_api_resume(target, mode)


async def test_input_gate_holds_the_actual_delegated_operation_until_owned_permit():
    gate = InputOperationGate('task-acceptance-gate-unit', 'consumer_peer')
    calls = []
    async def actual_operation():
        calls.append('operation')
        return 7
    pending = asyncio.create_task(gate.invoke(actual_operation))
    try:
        await wait_event(gate.entered)
        assert calls == [] and not pending.done()
        gate.permit.set()
        assert await pending == 7 and gate.finished.is_set()
    finally:
        gate.permit.set()
        await pending


async def test_missing_assembled_constructor_adapter_is_explicitly_blocked(monkeypatch):
    # Simulate absence even after the parent eventually supplies a real adapter;
    # offline tests must never accidentally launch the assembled service.
    monkeypatch.setitem(sys.modules, 'tests.task_acceptance_support.assembled_resume', None)
    evidence = Evidence('consumer-test')
    await run_constructor_resume_targets(evidence)
    assert len(evidence.checks) == 4
    assert all(check['outcome'] == 'blocked' for check in evidence.checks)
    assert evidence.exit_code(require_complete=True) == 2
