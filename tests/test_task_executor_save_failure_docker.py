"""Acceptance gate ``profile_save_failure`` with a REAL save failure (lane002 R1).

The pytest entry point is an explicit local-Docker opt-in. It creates a uniquely
labelled disposable network, then runs this same module INSIDE the existing
backend image on that network (the hosted topology: Docker-internal CDP, no host
port publication). The in-container scenario uses the real Control/SQLite
repository, Executor, BrowserSession/Pi dispatch (injected deterministic
inference, no provider), TaskSandbox and Chromium sandbox, against a synthetic
local site that counts server-side effects.

Failure injection is test-only instrumentation, not a failpoint endpoint: after
the task's synthetic website effect, the test writes an oversized (sparse) file
into the live Chromium profile through ``docker exec``. The production save path
then genuinely fails its 256 MiB/272 MiB archive budget. Production code is not
patched, wrapped or configured differently for the failure.
"""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest

LABEL = 'flowpilot-r1-quota'
EVIDENCE = 'R1_PROFILE_SAVE_FAILURE_EVIDENCE='
WORKTREE = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.getenv('FLOWPILOT_R1_SAVE_FAILURE_DOCKER') != '1', reason='Explicit local Docker opt-in')
def test_profile_save_failure_gate_in_real_docker():
    import docker
    client = docker.from_env()
    run_id = uuid4().hex[:12]
    name = f'r1-save-{run_id}'
    # Existing local images only: no pull/build, no daemon configuration change.
    client.images.get('flowpilot-backend:latest')
    client.images.get('flowpilot-sandbox:latest')
    network = client.networks.create(f'r1-save-net-{run_id}', driver='bridge', labels={LABEL: run_id})
    try:
        result = subprocess.run([
            'docker', 'run', '--rm', '--init', '--name', name, '--label', f'{LABEL}={run_id}',
            '--network', network.name, '-e', f'SANDBOX_NETWORK={network.name}', '-e', f'R1_SITE_HOST={name}',
            '-e', 'PYTHONPATH=/lane', '-v', '/var/run/docker.sock:/var/run/docker.sock',
            '-v', f'{WORKTREE}:/lane:ro', '-w', '/lane', 'flowpilot-backend:latest',
            '/app/.venv/bin/python', '-m', 'tests.test_task_executor_save_failure_docker',
        ], capture_output=True, text=True, timeout=600)
        sys.stdout.write(result.stdout[-6000:])
        log = os.getenv('FLOWPILOT_R1_SAVE_FAILURE_LOG')
        if log:
            Path(log).write_text(result.stdout + '\n--- stderr ---\n' + result.stderr)
        assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
        line = next(line for line in result.stdout.splitlines() if line.startswith(EVIDENCE))
        evidence = json.loads(line[len(EVIDENCE):])
        assert evidence['gate'] == 'profile_save_failure' and evidence['passed'] is True
    finally:
        # Only this run's disposable containers: attached to (or, once exited,
        # configured for) the unique network. Never a broad label sweep.
        for row in client.api.containers(all=True, filters={'label': 'flowpilot-task=1'}):
            if row.get('HostConfig', {}).get('NetworkMode') == network.name:
                client.api.remove_container(row['Id'], force=True)
        network.reload()
        for container in network.containers:
            container.remove(force=True)
        network.remove()
        client.close()


# ---------------------------------------------------------------------------
# In-container scenario (python -m tests.test_task_executor_save_failure_docker)
# ---------------------------------------------------------------------------

EFFECTS = {'count': 0}
EFFECT_LOCK = threading.Lock()


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'<title>Synthetic R1 site</title><button>synthetic</button>')

    def do_POST(self):
        if self.path != '/effect':
            self.send_response(404)
            self.end_headers()
            return
        with EFFECT_LOCK:
            EFFECTS['count'] += 1
            count = EFFECTS['count']
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'count': count}).encode())

    def log_message(self, *args):
        pass


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def scenario(root):
    from unittest.mock import patch
    from backend.task_contracts import ErrorCode, Principal, ProfileCreate, TaskFault, TaskStart, TaskState, TERMINAL_STATES
    from backend.task_ports import TaskBindings
    from backend.task_settings import TaskSettings
    from backend.tasks.browser_adapter import host_key
    from backend.tasks.control import Control
    from backend.tasks.executor import Executor
    from backend.tasks.memory import storage_key
    from backend.tasks.runtime import create_runtime
    from tests.fixtures.browser_tasks.doubles import bindings_double, observation_fixture

    site_url = f"http://{os.environ['R1_SITE_HOST']}:8765"
    gib = 1024 ** 3
    # One quarantine fills both the global and the per-account quota.
    settings = TaskSettings(state_dir=root, quarantine_max_count=1, quarantine_max_account_count=1,
                            quarantine_max_bytes=gib, quarantine_max_account_bytes=gib,
                            quarantine_reserve_bytes=gib, cancellation_seconds=60, shutdown_seconds=90)
    owner = Principal(account_id='r1-synthetic-owner')
    evidence = {'gate': 'profile_save_failure', 'passed': False}
    constructed = []

    def counting_runtime(*args):
        constructed.append(args[1].lease.task_id)
        return create_runtime(*args)

    def boot(runtime_factory):
        doubles = bindings_double()
        for method in ('observe', 'require_fresh', 'after_action'):
            getattr(doubles.observation, method).side_effect = None
        doubles.observation.observe.return_value = observation_fixture()
        control, executor = Control(settings), Executor(settings, runtime_factory=runtime_factory)
        bindings = TaskBindings(control=control, executor=executor, assistance=doubles.assistance,
                                observation=doubles.observation)
        executor.bind(bindings)
        control.bind(bindings)
        return control, executor

    async def settle(control, task_id):
        for _ in range(1800):
            view = await control.repository.get(owner, task_id)
            if view.state in TERMINAL_STATES and task_id not in control._executions:
                return view
            await asyncio.sleep(.1)
        raise AssertionError('Task did not settle')

    state = {'executor': None, 'inject': False, 'container': None}
    script = ('async () => { const r = await fetch("/effect", {method: "POST"}); const body = await r.json();'
              ' localStorage.setItem("effects", String(body.count)); return body.count; }')

    async def inference(task, system, definitions, dispatch, **kwargs):
        await dispatch('navigate', {'url': site_url})
        await dispatch('observe', {})
        before = EFFECTS['count']
        answer = await dispatch('execute', {'script': script,
            'precondition': 'async () => true',
            'postcondition': f'async () => localStorage.getItem("effects") === "{before + 1}"'})
        assert answer['success'], answer
        if state['inject']:
            # TEST-ONLY instrumentation: make the live profile genuinely exceed
            # the production archive budget. The real save path then fails.
            runtime = next(iter(state['executor'].active.values())).runtime
            sandbox = runtime.session.sandbox
            container = sandbox.manager.client.containers.get(sandbox.info['container_id'])
            code, output = container.exec_run(
                ['sh', '-c', 'truncate -s 300M /tmp/chrome-profile/r1-oversize.bin && ls -l /tmp/chrome-profile/r1-oversize.bin'],
                user='root')
            assert code == 0, output
            state['container'] = container.id
        await dispatch('complete', {'verify': f'async () => localStorage.getItem("effects") === "{before + 1}"',
                                    'summary': f'Verified synthetic effect {before + 1}'})

    control, executor = boot(counting_runtime)
    state['executor'] = executor
    await executor.startup()
    await control.startup()
    profile = await control.create_profile(owner, ProfileCreate(label='R1 synthetic', idempotency_key='r1-profile-1'))
    key = storage_key(owner, profile.profile_id)
    directory = root / key
    archive = directory / 'sandbox-profile.tar'
    try:
        with patch('backend.pi_browser.run_pi', inference):
            # 1. Clean task: proves a known-good previous archive exists.
            first = await control.start(owner, TaskStart(task='Synthetic effect one', profile_id=profile.profile_id,
                                                         idempotency_key='r1-task-1'))
            view = await settle(control, first.task_id)
            assert view.state == TaskState.COMPLETED, view
            assert EFFECTS['count'] == 1 and archive.is_file()
            good_digest, good_size = sha256(archive), archive.stat().st_size
            assert executor.ledger.entries() == {}

            # 2. Real effect, then a real save failure on close.
            state['inject'] = True
            second = await control.start(owner, TaskStart(task='Synthetic effect two', profile_id=profile.profile_id,
                                                          idempotency_key='r1-task-2'))
            view = await settle(control, second.task_id)
            state['inject'] = False
        assert view.state == TaskState.FAILED, view
        assert view.error.code == ErrorCode.PROFILE_RECOVERY_REQUIRED and view.result is None
        assert EFFECTS['count'] == 2
        assert sha256(archive) == good_digest, 'previous archive must be preserved byte-for-byte'
        assert not (directory / 'sandbox-profile.tar.tmp').exists()
        marker = json.loads((directory / 'recovery-required').read_text())
        assert marker['task_id'] == second.task_id and marker['account_id'] == owner.account_id
        assert marker['container_id'] == state['container'] and marker['task_host'] == host_key(settings)
        retained = executor.manager_factory(str(root)).client.containers.get(state['container'])
        retained.reload()
        assert retained.status == 'exited' and retained.labels['profile-key'] == key
        assert retained.labels['task-id'] == second.task_id
        entry = executor.ledger.entries()[key]
        assert entry['state'] == 'quarantined' and entry['task_id'] == second.task_id
        listed = (await control.list_profiles(owner)).items
        assert next(p for p in listed if p.profile_id == profile.profile_id).state == 'recovery_required'
        evidence.update(task_state=view.state, error=view.error.code, previous_archive_bytes=good_size,
                        previous_archive_preserved=True, container_status=retained.status,
                        ledger=entry['state'], measured_bytes=entry.get('measured_bytes'))

        # 3. No replay: same profile refused at durable admission; a fresh
        # profile is refused by the full quota before any browser exists.
        try:
            await control.start(owner, TaskStart(task='Synthetic effect two', profile_id=profile.profile_id,
                                                 idempotency_key='r1-task-3'))
        except TaskFault as fault:
            assert fault.error.code == ErrorCode.PROFILE_RECOVERY_REQUIRED
        else:
            raise AssertionError('Quarantined profile was readmitted')
        duplicate = await control.start(owner, TaskStart(task='Synthetic effect two', profile_id=profile.profile_id,
                                                         idempotency_key='r1-task-2'))
        assert duplicate.task_id == second.task_id
        fresh = await control.start(owner, TaskStart(task='Synthetic fresh profile', idempotency_key='r1-task-4'))
        view = await settle(control, fresh.task_id)
        assert view.state == TaskState.FAILED and view.error.code == ErrorCode.CAPACITY_EXHAUSTED
        fresh_key = storage_key(owner, fresh.profile_id)
        assert not executor.manager_factory(str(root)).client.containers.list(
            all=True, filters={'label': [f'profile-key={fresh_key}']})
        assert constructed == [first.task_id, second.task_id]
        assert EFFECTS['count'] == 2
        evidence.update(readmission='profile_recovery_required', quota_refusal='capacity_exhausted')
        await control.shutdown()
        await executor.shutdown()

        # 4. Restart: real startup reconciliation never constructs a runtime.
        def forbidden(*args):
            raise AssertionError('Recovery must never replay a browser task')
        control, executor = boot(forbidden)
        await executor.startup()
        await control.startup()
        await asyncio.sleep(.5)
        assert EFFECTS['count'] == 2 and constructed == [first.task_id, second.task_id]
        retained.reload()
        assert retained.status == 'exited' and sha256(archive) == good_digest
        assert executor.ledger.entries()[key]['state'] == 'quarantined'
        evidence.update(restart='no-replay', effects_after_restart=EFFECTS['count'])
        await control.shutdown()
        await executor.shutdown()

        # 5. Explicit deletion is the only cleanup; it frees the quota slot.
        control, executor = boot(counting_runtime)
        state['executor'] = executor
        await executor.startup()
        await control.startup()
        await control.delete_profile(owner, profile.profile_id)
        try:
            retained.reload()
        except Exception as exc:
            assert type(exc).__name__ == 'NotFound'
        else:
            raise AssertionError('Deleted profile kept its quarantined container')
        assert not archive.exists() and not (directory / 'recovery-required').exists()
        assert executor.ledger.entries() == {}
        with patch('backend.pi_browser.run_pi', inference):
            after = await control.start(owner, TaskStart(task='Synthetic after deletion', idempotency_key='r1-task-5'))
            view = await settle(control, after.task_id)
        assert view.state == TaskState.COMPLETED, view
        assert EFFECTS['count'] == 3 and executor.ledger.entries() == {}
        evidence.update(deletion='container-archive-marker-ledger-removed', admission_after_deletion='completed',
                        effects=EFFECTS['count'], runtimes=len(constructed), passed=True)
    finally:
        await control.shutdown()
        await executor.shutdown()
        manager = executor.manager_factory(str(root))
        for container in manager.client.containers.list(all=True, filters={'label': [f'task-host={host_key(settings)}']}):
            container.remove(force=True)
    print(EVIDENCE + json.dumps(evidence), flush=True)


def main():
    site = ThreadingHTTPServer(('0.0.0.0', 8765), Site)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='r1-save-') as root:
            asyncio.run(scenario(Path(root)))
    finally:
        site.shutdown()


if __name__ == '__main__':
    main()
