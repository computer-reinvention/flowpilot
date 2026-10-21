"""Host-side launcher. Cleanup is limited to the uniquely labelled network."""

import json
from pathlib import Path
import secrets
import re
import tempfile

from .wire import ROOT
from .security import canary_variants


def run_assembled_docker(evidence):
    import docker
    client = docker.from_env()
    run = evidence.run_id
    labels = {'task-acceptance-run': run}
    image = client.images.get('flowpilot-backend:latest')
    sandbox = client.images.get('flowpilot-sandbox:latest')
    network = client.networks.create(run, labels=labels, check_duplicate=True)
    backend = None
    canary = 'synthetic-canary-' + secrets.token_hex(16)
    with tempfile.TemporaryDirectory(prefix=run + '-', dir=ROOT / 'reports/task-acceptance') as directory:
        try:
            backend = client.containers.run(image.id,
                ['/app/.venv/bin/python', '-m', 'tests.task_acceptance_support.assembled_run'],
                name=run + '-backend', labels=labels, network=network.name, init=True, detach=True,
                environment={'PYTHONPATH': '/lane', 'PROFILES_DIR': '/state', 'SANDBOX_NETWORK': network.name,
                             'JWT_SECRET': secrets.token_hex(32), 'FLOWPILOT_TRACE': '0', 'TASK_ACCEPTANCE_RUN_ID': run,
                             'TASK_ACCEPTANCE_OTP_CANARY': canary,
                             'FLOWPILOT_TASKS_API_REQUESTS_PER_MINUTE': '10000', 'FLOWPILOT_TASKS_LOGIN_REQUESTS_PER_MINUTE': '100'},
                volumes={str(ROOT): {'bind': '/lane', 'mode': 'ro'}, directory: {'bind': '/state', 'mode': 'rw'},
                         '/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'}},
                working_dir='/lane', pids_limit=1024, mem_limit='2g')
            result = backend.wait(timeout=900)
            # Logs remain private temporary data; never echo raw exceptions/DOM.
            logs = backend.logs()
            (Path(directory) / 'backend.log').write_bytes(logs)
            exposed = any(value in logs for value in canary_variants(canary))
            key_patterns = len(re.findall(rb'fp_live_[a-f0-9]{32}', logs))
            evidence.record('real_backend_log_canaries', 'failed' if exposed or key_patterns else 'passed',
                            'actual backend stdout/stderr scanned without retaining raw logs', otp_canary_found=exposed,
                            plaintext_key_patterns=key_patterns)
            result_path = Path(directory) / 'assembled_evidence.json'
            if result_path.exists():
                result_data = json.loads(result_path.read_text())
                evidence.checks.extend(result_data['checks'])
                evidence.requirements.update(result_data['requirements'])
            else:
                text = logs.decode(errors='replace')
                frames = [{'path': path, 'line': int(line)} for path, line in re.findall(r'File "([^"\n]+)", line (\d+)', text)]
                errors = re.findall(r'^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)):', text, re.M)
                evidence.record('assembled_child', 'failed', 'backend exited without sanitized result; only traceback locations retained',
                                exit_code=result['StatusCode'], traceback_frames=frames[-12:], exception_types=errors[-5:])
            evidence.record('assembled_topology', 'passed' if result['StatusCode'] == 0 else 'failed',
                            'actual backend/browser on owned Docker bridge; no provider credentials; PID input 1024; legacy cleanup never invoked',
                            backend_image_id=image.id, sandbox_image_id=sandbox.id,
                            network_label=run, sandbox_pids=1024, backend_exit=result['StatusCode'],
                            api_requests_per_minute=10000, login_requests_per_minute=100,
                            limitations='Existing local images, not proof of lane005 packaged network policy or live-provider quality')
        finally:
            network.reload()
            containers = set(network.attrs.get('Containers', {}))
            containers.update(container.id for container in client.containers.list(all=True, filters={'network': network.name}))
            for container_id in containers:
                container = client.containers.get(container_id)
                container.reload()
                assert network.name in container.attrs['NetworkSettings']['Networks']
                assert (container.labels.get('task-acceptance-run') == run
                        or container.labels.get('flowpilot-task') == '1'), 'foreign resource on owned network; refuse broad cleanup'
                container.remove(force=True)
            # Stopped task containers remain network members; include only this
            # unique network, never a global flowpilot label sweep.
            network.remove()
            evidence.record('owned_docker_cleanup', 'passed', 'only containers attached to this run-labelled network removed, then that exact network removed',
                            owned_containers_removed=len(containers), unique_network_removed=True)
