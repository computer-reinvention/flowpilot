"""Host-side launcher. Cleanup is limited to this run's unique network and label."""

import hashlib
import json
import os
import sys
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import threading

from .wire import ROOT
from .security import scan_blobs

TOOLS_IMAGE = 'flowpilot-lane005-tools:verify'  # Existing local lane005 image: caddy + age.


def synthetic_secret():
    # Transform-sensitive characters make URL/JSON/HTML/UTF-16 variants distinct.
    return 'synthetic-canary-' + secrets.token_hex(12) + '-"/+ é\\'


def build_frontend(directory):
    """Build THIS revision's SPA offline with the checkout's existing node_modules."""
    common = Path(subprocess.check_output(['git', 'rev-parse', '--git-common-dir'], cwd=ROOT, text=True).strip())
    main_root = (common if common.is_absolute() else ROOT / common).resolve().parent
    modules = main_root / 'frontend' / 'node_modules'
    lock = (ROOT / 'frontend/package-lock.json').read_bytes()
    assert (main_root / 'frontend/package-lock.json').read_bytes() == lock, 'installed frontend dependencies differ from this lockfile'
    source = Path(directory) / 'src'
    shutil.copytree(ROOT / 'frontend', source, ignore=shutil.ignore_patterns('node_modules', 'dist'))
    (source / 'node_modules').symlink_to(modules)
    output = Path(directory) / 'dist'
    subprocess.run([str(modules / '.bin' / 'vite'), 'build', '--outDir', str(output), '--emptyOutDir'], cwd=source, check=True,
                   env={'PATH': os.environ.get('PATH', ''), 'HOME': os.environ.get('HOME', '')}, capture_output=True, timeout=300)
    return output, hashlib.sha256(lock).hexdigest()


def run_local_chat(evidence, network, run, results, marker, backend):
    """./chat REPL path on the host, concurrently with the assembled service's
    compatibility phase (released by a marker the backend writes)."""
    import time
    from .site import synthetic_site
    while not marker.exists():
        backend.reload()
        if backend.status not in {'created', 'running'}:
            evidence.record('real_local_chat_repl', 'blocked', 'assembled compatibility phase never started; REPL not run')
            return
        time.sleep(1)
    with tempfile.TemporaryDirectory(prefix=run + '-chat-', dir=ROOT / 'reports/task-acceptance') as directory:
        state = Path(directory) / 'repl-state'
        cwd = Path(directory) / 'cwd'
        cwd.mkdir()
        env = {name: os.environ[name] for name in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'DOCKER_HOST') if name in os.environ}
        env.update({'SANDBOX_NETWORK': network, 'PYTHONPATH': str(ROOT), 'TASK_ACCEPTANCE_RUN_ID': run})
        with synthetic_site(directory, 'host.docker.internal') as site:
            def ask(scenario):
                request = 'acceptance:' + json.dumps({'scenario': scenario, 'url': site.browser_url})
                result = subprocess.run([sys.executable, '-m', 'tests.task_acceptance_support.chat_repl', '--user', run + '-chat',
                                         '--state-dir', str(state), '--ask', request], cwd=cwd, env=env,
                                        capture_output=True, timeout=300)
                results['output'] = results.get('output', b'') + result.stdout + result.stderr
                assert result.returncode == 0, 'local REPL --ask failed'
                return json.loads(result.stdout.decode().strip().splitlines()[-1])
            try:
                first = ask('seed')
                assert site.counters() == {'prerequisite': 1, 'cart': 1}
                second = ask('follow')
                assert second['metrics']['reused'] == 1 and site.counters() == {'prerequisite': 1, 'cart': 2}
                evidence.record('real_local_chat_repl', 'passed', '`./chat` entry (backend.repl main) with real Docker browser, persisted cookies/localStorage and saved script across two REPL processes, concurrent with the task service; deterministic model only',
                                repl_processes=2, script_reused=second['metrics']['reused'], prerequisite_repeated=0,
                                first_saved=first['metrics'].get('saved', 0))
            except Exception as exc:
                evidence.record('real_local_chat_repl', 'failed', 'local REPL path failed; rerun exact launcher',
                                exception_type=type(exc).__name__)


def run_assembled_docker(evidence):
    import docker
    client = docker.from_env()
    run = evidence.run_id
    labels = {'task-acceptance-run': run}
    image = client.images.get('flowpilot-backend:latest')
    sandbox = client.images.get('flowpilot-sandbox:latest')
    tools = client.images.get(TOOLS_IMAGE)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    values = {name: synthetic_secret() for name in ('otp_main', 'otp_mismatch', 'otp_pending', 'otp_held', 'otp_expired',
                                                    'pw_owner', 'pw_foreign', 'pw_ui', 'pw_expiry')}
    scenarios = os.environ.get('TASK_ACCEPTANCE_SCENARIOS', '')
    network = client.networks.create(run, labels=labels, check_duplicate=True)
    backend = None
    chat = {}
    with tempfile.TemporaryDirectory(prefix=run + '-', dir=ROOT / 'reports/task-acceptance') as directory, \
            tempfile.TemporaryDirectory(prefix=run + '-ops-', dir=ROOT / 'reports/task-acceptance') as ops, \
            tempfile.TemporaryDirectory(prefix=run + '-ui-', dir=ROOT / 'reports/task-acceptance') as ui:
        try:
            frontend, lock_hash = build_frontend(ui)
            backend = client.containers.run(image.id,
                ['/app/.venv/bin/python', '-m', 'tests.task_acceptance_support.assembled_run'],
                name=run + '-backend', labels=labels, network=network.name, init=True, detach=True,
                environment={'PYTHONPATH': '/lane', 'PROFILES_DIR': '/state/service', 'SANDBOX_NETWORK': network.name,
                             'JWT_SECRET': secrets.token_hex(32), 'FLOWPILOT_TRACE': '0', 'TASK_ACCEPTANCE_RUN_ID': run,
                             'TASK_ACCEPTANCE_SECRETS_JSON': json.dumps(values), 'TASK_ACCEPTANCE_SCENARIOS': scenarios,
                             'TASK_ACCEPTANCE_HOST_ROOT': str(ROOT), 'TASK_ACCEPTANCE_HOST_STATE': directory,
                             'TASK_ACCEPTANCE_HOST_OPS': ops, 'TASK_ACCEPTANCE_TOOLS_IMAGE': tools.id,
                             'TASK_ACCEPTANCE_REVISION': revision, 'TASK_ACCEPTANCE_FRONTEND_DIR': '/ui',
                             'FLOWPILOT_TASKS_API_REQUESTS_PER_MINUTE': '10000', 'FLOWPILOT_TASKS_LOGIN_REQUESTS_PER_MINUTE': '100'},
                volumes={str(ROOT): {'bind': '/lane', 'mode': 'ro'}, directory: {'bind': '/state', 'mode': 'rw'},
                         ops: {'bind': '/ops', 'mode': 'rw'}, str(frontend): {'bind': '/ui', 'mode': 'ro'},
                         '/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'}},
                working_dir='/lane', pids_limit=1024, mem_limit='2g')
            chat_thread = None
            if not scenarios or 'compat' in scenarios.split(','):
                chat_thread = threading.Thread(target=run_local_chat, args=(evidence, network.name, run, chat, Path(directory) / 'compat-started', backend), daemon=True)
                chat_thread.start()
            result = backend.wait(timeout=2400)
            if chat_thread is not None:
                chat_thread.join(timeout=600)
                if chat_thread.is_alive():
                    evidence.record('real_local_chat_repl', 'failed', 'local REPL path did not finish within bound')
            # Logs remain private temporary data; never echo raw exceptions/DOM.
            logs = backend.logs()
            (Path(directory) / 'backend.log').write_bytes(logs)
            if os.environ.get('TASK_ACCEPTANCE_DEBUG') == '1':
                # Development only: traceback locations/types, never values.
                for line in logs.decode(errors='replace').splitlines():
                    if 'File "' in line or line.startswith(('Traceback', 'Thread', 'Current thread')) or 'Error:' in line[:60]:
                        print('debug:', line[:240])
            log_scan = scan_blobs([('backend', logs), ('local-chat', chat.get('output', b''))], list(values.values()))
            key_patterns = len(re.findall(rb'fp_live_[a-f0-9]{32}', logs))
            evidence.record('real_backend_log_canaries', 'failed' if log_scan['findings'] or key_patterns else 'passed',
                            'actual backend and local REPL stdout/stderr scanned for every OTP/password canary in raw and transformed variants without retaining raw logs',
                            canaries=len(values), findings=len(log_scan['findings']), plaintext_key_patterns=key_patterns)
            result_path = Path(directory) / 'assembled_evidence.json'
            if result_path.exists():
                result_data = json.loads(result_path.read_text())
                evidence.checks.extend(result_data['checks'])
                evidence.requirements.update(result_data['requirements'])
                evidence.scenario_selection = result_data.get('scenario_selection')
            else:
                text = logs.decode(errors='replace')
                frames = [{'path': path, 'line': int(line)} for path, line in re.findall(r'File "([^"\n]+)", line (\d+)', text)]
                errors = re.findall(r'^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)):', text, re.M)
                evidence.record('assembled_child', 'failed', 'backend exited without sanitized result; only traceback locations retained',
                                exit_code=result['StatusCode'], traceback_frames=frames[-12:], exception_types=errors[-5:])
            from .assembled_gates import settle_requirement
            settle_requirement(evidence, 'compatibility', ['real_legacy_flow_api', 'real_ui_boundary_browser', 'real_local_chat_repl'])
            evidence.record('assembled_topology', 'passed' if result['StatusCode'] == 0 else 'failed',
                            'actual backend/browser on owned Docker bridge; no provider credentials; PID input 1024; legacy cleanup never invoked',
                            backend_image_id=image.id, sandbox_image_id=sandbox.id, tools_image_id=tools.id,
                            frontend_lock_sha256=lock_hash, network_label=run, sandbox_pids=1024, backend_exit=result['StatusCode'],
                            api_requests_per_minute=10000, login_requests_per_minute=100,
                            limitations='Existing local images, not proof of lane005 packaged network policy or live-provider quality')
        finally:
            network.reload()
            containers = set(network.attrs.get('Containers', {}))
            containers.update(container.id for container in client.containers.list(all=True, filters={'network': network.name}))
            # Proxy/ops siblings share the backend namespace or no network; select by exact run label only.
            containers.update(container.id for container in client.containers.list(all=True, filters={'label': 'task-acceptance-run=' + run}))
            for container_id in containers:
                container = client.containers.get(container_id)
                container.reload()
                attached = network.name in container.attrs['NetworkSettings']['Networks']
                assert (container.labels.get('task-acceptance-run') == run
                        or (attached and container.labels.get('flowpilot-task') == '1')
                        or (attached and container.labels.get('flowpilot') == 'sandbox' and container.labels.get('user-id') == run + '-chat')), \
                    'foreign resource on owned network; refuse broad cleanup'
                container.remove(force=True)
            # Stopped task containers remain network members; include only this
            # unique network, never a global flowpilot label sweep.
            network.remove()
            evidence.record('owned_docker_cleanup', 'passed', 'only this run-labelled network members and exact run-label siblings removed, then that exact network removed',
                            owned_containers_removed=len(containers), unique_network_removed=True)
