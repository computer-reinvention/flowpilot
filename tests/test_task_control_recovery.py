"""Hard process termination at durable execution checkpoints; no website replay."""

import asyncio
import json
import importlib.util
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from backend.task_contracts import PollRequest, Principal, TaskFault
from backend.task_settings import TaskSettings
from backend.tasks.control import create_control

spec = importlib.util.spec_from_file_location('lane001_recovery_doubles', Path(__file__).parent / 'fixtures/browser_tasks/doubles.py')
doubles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doubles)


CHILD = """
import asyncio,json,sys,time
from datetime import timedelta
from pathlib import Path
from backend.task_settings import TaskSettings
from backend.task_contracts import Principal,TaskStart,Interaction,TaskState
from backend.tasks.repository import Repository,now
async def run():
    repo=Repository(TaskSettings(state_dir=Path(sys.argv[1])),b'x'*32)
    await repo.initialize()
    owner=Principal(account_id='crash_owner')
    receipt=await repo.admit(owner,TaskStart(task='synthetic no-network task',idempotency_key='crash-admit-001'))
    lease=None
    if sys.argv[2]!='queued':
        execution=await repo.claim(owner,receipt.task_id)
        lease=execution.lease
        if sys.argv[2]=='paused':
            interaction=Interaction(interaction_id='crash_pause',generation=1,kind='browser',message='Synthetic pause',expires_at=now()+timedelta(seconds=60))
            await repo.transition(lease,execution.revision,TaskState.WAITING_FOR_BROWSER,interaction=interaction)
    print(json.dumps({'receipt':receipt.model_dump(),'lease':lease.model_dump() if lease else None}),flush=True)
asyncio.run(run())
time.sleep(60)
"""


@pytest.mark.asyncio
@pytest.mark.parametrize('checkpoint', ['queued', 'running', 'paused'])
async def test_process_crash_fences_and_never_redispatches(tmp_path, monkeypatch, checkpoint):
    environment = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    process = subprocess.Popen([sys.executable, '-c', CHILD, str(tmp_path), checkpoint], cwd=Path(__file__).resolve().parents[1], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 10)
        assert ready, process.stderr.read()
        snapshot = json.loads(ready)
    finally:
        process.kill()
        await asyncio.to_thread(process.wait)
        process.stdout.close()
        process.stderr.close()
    monkeypatch.setenv('JWT_SECRET', 'synthetic-test-secret-' * 3)
    from backend import auth
    monkeypatch.setattr(auth, 'JWT_SECRET', 'synthetic-test-secret-' * 3)
    control = create_control(TaskSettings(enabled=True, state_dir=tmp_path))
    peers = doubles.bindings_double()
    peers.executor.recover.side_effect = None
    peers.executor.recover.return_value = None
    control.bind(replace(peers, control=control))
    await control.startup()
    receipt = snapshot['receipt']
    view = await control.get(Principal(account_id='crash_owner'), receipt['task_id'], PollRequest())
    assert view.state == 'interrupted' and view.interaction is None
    assert view.error.code == 'interrupted'
    peers.executor.execute.assert_not_awaited()
    leases = peers.executor.recover.await_args.args[0]
    assert len(leases) == 1
    with pytest.raises(TaskFault):
        await control.progress(leases[0], view.revision, 'late worker')
    assert await control.repository.claim(leases[0].principal, view.task_id) is None
    assert (await control.list_profiles(leases[0].principal)).items[0].state == 'recovery_required'
    await control.shutdown()
