"""Opt-in, disposable log-config inspection; no browser/provider/daemon changes."""
import os
from uuid import uuid4

import pytest

from backend import sandbox
from backend.task_settings import TaskSettings


@pytest.mark.skipif(os.getenv('FLOWPILOT_LANE002_REPAIR_DOCKER') != '1', reason='Explicit local Docker opt-in')
def test_task_container_has_explicit_rotating_json_logs(tmp_path, monkeypatch):
    import docker
    client = docker.from_env()
    label = 'flowpilot-lane002-repair'
    run_id = uuid4().hex
    network = client.networks.create('lane002-logs-' + run_id, labels={label: run_id})
    container = None
    try:
        # Existing lightweight local image; no build, pull or daemon mutation.
        client.images.get('python:3.13-slim')
        monkeypatch.setattr(sandbox, 'SANDBOX_IMAGE', 'python:3.13-slim')
        monkeypatch.setattr(sandbox, 'SANDBOX_NETWORK', network.name)
        manager = sandbox.SandboxManager(str(tmp_path))
        manager._client = client
        info = manager.start(task_labels={'flowpilot-task': '1', label: run_id},
                             settings=TaskSettings(state_dir=tmp_path))
        container = client.containers.get(info['container_id'])
        container.reload()
        assert container.labels[label] == run_id
        actual = container.attrs['HostConfig']['LogConfig']
        assert actual == {'Type': 'json-file', 'Config': {'max-size': '10m', 'max-file': '3'}}
        print('LANE002_REPAIR_LOG_CONFIG=' + str(actual))
        assert not container.attrs['HostConfig']['PortBindings']
    finally:
        if container is not None:
            assert container.labels[label] == run_id
            container.remove(force=True)
        assert network.attrs['Labels'][label] == run_id
        network.remove()
        client.close()
