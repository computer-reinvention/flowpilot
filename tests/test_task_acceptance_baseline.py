"""Real HTTP fail-closed boundary only; lifecycle intentionally disabled.

Legacy app startup sweeps unlabelled global Docker resources, so do not invoke it
from this independent harness until 005 supplies a disposable topology. These
tests do not establish application startup or assembled service readiness.
"""

import socket
import sys
import time
import uuid

import pytest

from tests.task_acceptance_support.client import Client
from tests.task_acceptance_support.evidence import Evidence
from tests.task_acceptance_support.peers import scripted_peer
from tests.task_acceptance_support.resources import OwnedProcess
from tests.task_acceptance_support.system import run_service
from tests.task_acceptance_support.wire import PREFIX, ROOT, error


@pytest.mark.parametrize('enabled,expected', [(False, 404), (True, 503)])
def test_real_uvicorn_http_boundary_without_lifecycle(tmp_path, enabled, expected):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    directory = tmp_path / ('task-acceptance-' + uuid.uuid4().hex)
    process = OwnedProcess([sys.executable, '-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1',
                            '--port', str(port), '--lifespan', 'off', '--no-access-log'], directory, ROOT,
                           {'PROFILES_DIR': str(directory / 'state'),
                            'FLOWPILOT_TASKS_ENABLED': str(enabled).lower()})
    try:
        client = Client(f'http://127.0.0.1:{port}', timeout=1)
        deadline = time.monotonic() + 15
        while True:
            try:
                readiness = client.request('GET', '/health/browser-tasks')
                break
            except Exception:
                assert process.process.poll() is None, 'application import/start failed'
                assert time.monotonic() < deadline, 'application HTTP listener did not start'
                time.sleep(.05)
        assert readiness.body['enabled'] is enabled and readiness.body['ready'] is False
        denied = client.request('POST', PREFIX, {'task': 'No effects', 'idempotency_key': 'baseline-only'})
        assert denied.status == expected
        error(denied.status, denied.body, denied.headers)
        evidence = Evidence('baseline')
        run_service(evidence, client.base_url, site=None)
        assert evidence.checks == [{'name': 'service_readiness', 'outcome': 'blocked',
                                   'detail': 'task peers disabled or unavailable',
                                   'http_status': readiness.status}]
    finally:
        process.close()


def test_disabled_health_200_never_counts_as_ready():
    with scripted_peer([{'method': 'GET', 'path': '/health/browser-tasks', 'status': 200,
                         'response': {'enabled': False, 'ready': False, 'protocol_version': '1', 'reason': 'disabled'}}]) as base:
        evidence = Evidence('consumer-test')
        run_service(evidence, base, site=None, allow_tasks=True)
        assert [check['outcome'] for check in evidence.checks] == ['blocked']
