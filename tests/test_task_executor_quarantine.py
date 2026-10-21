"""Fail-closed aggregate quarantine quotas (lane002 R1)."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from backend.task_contracts import (
    ErrorCode, Principal, ProfileCreate, TaskFault, TaskStart, TaskState, TERMINAL_STATES,
)
from backend.task_ports import Execution, Lease
from backend.task_settings import TaskSettings
from backend.tasks.browser_adapter import host_key, mark_recovery
from backend.tasks.executor import Executor
from backend.tasks.memory import QuarantineLedger, storage_key
from tests.fixtures.browser_tasks.doubles import bindings_double


GIB = 1024 ** 3
MIB = 1024 ** 2


def test_quarantine_quota_settings_have_safe_defaults_and_env_names():
    settings = TaskSettings.from_env({})
    assert (settings.quarantine_max_count, settings.quarantine_max_account_count) == (16, 4)
    assert (settings.quarantine_max_bytes, settings.quarantine_max_account_bytes) == (16 * GIB, 4 * GIB)
    assert settings.quarantine_reserve_bytes == GIB
    configured = TaskSettings.from_env({
        'FLOWPILOT_TASKS_QUARANTINE_MAX_COUNT': '3',
        'FLOWPILOT_TASKS_QUARANTINE_MAX_ACCOUNT_COUNT': '1',
        'FLOWPILOT_TASKS_QUARANTINE_MAX_BYTES': str(2 * GIB),
        'FLOWPILOT_TASKS_QUARANTINE_MAX_ACCOUNT_BYTES': str(GIB),
        'FLOWPILOT_TASKS_QUARANTINE_RESERVE_BYTES': str(512 * MIB),
    })
    assert (configured.quarantine_max_count, configured.quarantine_max_account_count) == (3, 1)
    assert configured.quarantine_reserve_bytes == 512 * MIB


@pytest.mark.parametrize('overrides', [
    {'quarantine_max_count': 0},
    {'quarantine_max_count': 2, 'quarantine_max_account_count': 3},
    {'quarantine_max_bytes': GIB, 'quarantine_max_account_bytes': 2 * GIB, 'quarantine_reserve_bytes': GIB},
    {'quarantine_max_account_bytes': GIB, 'quarantine_reserve_bytes': 2 * GIB},
    {'quarantine_reserve_bytes': 1024},
])
def test_incoherent_quarantine_quotas_are_rejected(overrides):
    with pytest.raises(ValidationError):
        TaskSettings(**overrides)


# ---------------------------------------------------------------------------
# Ledger and executor admission (offline; strict doubles + real Control/SQLite)
# ---------------------------------------------------------------------------

def quota(tmp_path, **overrides):
    values = dict(state_dir=tmp_path, quarantine_max_count=2, quarantine_max_account_count=1,
                  quarantine_max_bytes=2 * GIB, quarantine_max_account_bytes=GIB,
                  quarantine_reserve_bytes=GIB)
    values.update(overrides)
    return TaskSettings(**values)


def lease_for(account='alice', profile='p', task='t', generation=1):
    return Lease(principal=Principal(account_id=account), task_id=task, session_id='s',
                 profile_id=profile, generation=generation)


def execution_for(lease):
    now = datetime.now(timezone.utc)
    return Execution(lease=lease, revision=2, admitted_at=now, deadline=now + timedelta(seconds=60),
                     request=TaskStart(task='synthetic task', idempotency_key='quota-key1'))


def key_of(lease):
    return storage_key(lease.principal, lease.profile_id)


def test_ledger_reserves_fail_closed_and_never_overwrites_a_quarantine(tmp_path):
    ledger = QuarantineLedger(quota(tmp_path))
    alice, bob, carol = lease_for('alice'), lease_for('bob'), lease_for('carol')
    ledger.reserve(alice, key_of(alice))
    # Per-account count: alice's in-flight slot already counts as a quarantine.
    with pytest.raises(TaskFault) as raised:
        ledger.reserve(lease_for('alice', 'q'), key_of(lease_for('alice', 'q')))
    assert raised.value.error.code == ErrorCode.CAPACITY_EXHAUSTED
    ledger.reserve(bob, key_of(bob))
    # Global count (2) is now full for every account.
    with pytest.raises(TaskFault) as raised:
        ledger.reserve(carol, key_of(carol))
    assert raised.value.error.code == ErrorCode.CAPACITY_EXHAUSTED
    assert ledger.quarantine(key_of(alice), alice, measured_bytes=5)
    # An existing quarantine is never overwritten or released by any writer.
    assert not ledger.quarantine(key_of(alice), lease_for('alice', generation=9))
    assert not ledger.release(key_of(alice), alice)
    with pytest.raises(TaskFault) as raised:
        ledger.reserve(alice, key_of(alice))
    assert raised.value.error.code == ErrorCode.PROFILE_RECOVERY_REQUIRED
    with pytest.raises(TaskFault) as raised:
        ledger.reserve(lease_for('bob', task='other'), key_of(bob))
    assert raised.value.error.code == ErrorCode.CONFLICT
    assert not ledger.release(key_of(bob), lease_for('bob', task='other'))
    assert ledger.release(key_of(bob), bob)
    ledger.reserve(carol, key_of(carol))
    entries = ledger.entries()
    assert entries[key_of(alice)]['state'] == 'quarantined'
    assert set(entries) == {key_of(alice), key_of(carol)}


def test_ledger_byte_quota_charges_measured_and_unreadable_entries(tmp_path):
    settings = quota(tmp_path, quarantine_max_count=10, quarantine_max_account_count=10,
                     quarantine_max_bytes=3 * GIB, quarantine_max_account_bytes=3 * GIB,
                     quarantine_reserve_bytes=512 * MIB)
    ledger = QuarantineLedger(settings)
    big = lease_for('alice', 'big')
    ledger.reserve(big, key_of(big))
    ledger.quarantine(key_of(big), big, measured_bytes=2 * GIB)  # larger than its reservation
    (ledger.root / ('f' * 64 + '.json')).write_text('{corrupt')  # charged, never free capacity
    usage = ledger.usage('bob')
    assert usage['count'] == 2 and usage['bytes'] == 2 * GIB + 512 * MIB
    assert usage['account_count'] == 1  # unreadable owner counts against every account
    small = lease_for('bob', 'small')
    ledger.reserve(small, key_of(small))  # 2.5 GiB + 0.5 GiB == 3 GiB still fits
    with pytest.raises(TaskFault) as raised:
        ledger.reserve(lease_for('bob', 'next'), key_of(lease_for('bob', 'next')))
    assert raised.value.error.code == ErrorCode.CAPACITY_EXHAUSTED


def _race_reserve(state_dir, number, start):
    settings = quota(Path(state_dir), quarantine_max_count=3, quarantine_max_account_count=3,
                     quarantine_max_bytes=3 * GIB, quarantine_max_account_bytes=3 * GIB)
    lease = lease_for('racer', f'profile{number}', f'task{number}')
    start.wait()
    try:
        QuarantineLedger(settings).reserve(lease, key_of(lease))
        return 'reserved'
    except TaskFault as fault:
        return fault.error.code


def test_cross_process_reservations_are_atomic(tmp_path):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    context = multiprocessing.get_context('spawn')
    with context.Manager() as manager, ProcessPoolExecutor(8, mp_context=context) as pool:
        start = manager.Event()
        futures = [pool.submit(_race_reserve, str(tmp_path), number, start) for number in range(8)]
        start.set()
        outcomes = sorted(future.result() for future in futures)
    assert outcomes == ['capacity_exhausted'] * 5 + ['reserved'] * 3
    assert len(QuarantineLedger(quota(tmp_path)).entries()) == 3


class Runtime:
    declined = fenced = False

    def __init__(self, settings, execution, bindings):
        self.settings, self.execution = settings, execution
        self.close = AsyncMock()
        self.stop_writers = AsyncMock()
        self.directory = settings.state_dir / key_of(execution.lease)

    async def run(self):
        return {'answer': 'Verified', 'metrics': {}}

    def quarantine(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        mark_recovery(self.directory, self.settings, self.execution.lease, None)


class FailedSave(Runtime):
    def __init__(self, *args):
        super().__init__(*args)
        self.close.side_effect = RuntimeError('Profile save failed; synthetic')


async def executor_with(tmp_path, settings, runtime_type, manager=None):
    bindings = bindings_double()
    views = {}

    async def get(principal, task_id, poll):
        return views[task_id]
    for name in ('progress', 'finish', 'release_browser'):
        getattr(bindings.control, name).side_effect = None
    bindings.control.get.side_effect = get
    constructed = []

    def factory(*args):
        runtime = runtime_type(*args)
        constructed.append(runtime)
        return runtime
    executor = Executor(settings, runtime_factory=factory,
                        **({'manager_factory': lambda *args: manager} if manager else {}))
    executor.bind(bindings)
    await executor.startup()

    async def run(lease):
        views[lease.task_id] = SimpleNamespace(state=TaskState.RUNNING, revision=2, task_id=lease.task_id,
                                               session_id=lease.session_id, profile_id=lease.profile_id)
        await executor.execute(execution_for(lease))
        return bindings.control.finish.call_args, bindings.control.release_browser.call_args
    return executor, bindings, constructed, run


@pytest.mark.asyncio
async def test_full_quota_refuses_new_work_before_any_browser_and_keeps_recovery_data(tmp_path):
    settings = quota(tmp_path, quarantine_max_count=1, quarantine_max_bytes=GIB)
    manager = MagicMock()
    executor, bindings, constructed, run = await executor_with(tmp_path, settings, FailedSave, manager)
    first = lease_for('alice', 'p', 't1')
    finish, release = await run(first)
    assert finish.args[2] == TaskState.FAILED and finish.kwargs['error'].code == 'profile_recovery_required'
    assert release.kwargs == {'profile_saved': False}
    directory = tmp_path / key_of(first)
    (directory / 'sandbox-profile.tar').write_bytes(b'previous-good-archive')
    snapshot = sorted(p.name for p in directory.iterdir())
    assert executor.ledger.entries()[key_of(first)]['state'] == 'quarantined'
    for lease in (lease_for('alice', 'q', 't2'), lease_for('bob', 'p', 't3')):
        finish, release = await run(lease)
        error = finish.kwargs['error']
        assert finish.args[2] == TaskState.FAILED and error.code == ErrorCode.CAPACITY_EXHAUSTED
        assert 'quota' in error.message and finish.kwargs['result'] is None
        # Refusal before launch does not mark the untouched profile unsaved.
        assert release.kwargs == {'profile_saved': True}
        assert not (tmp_path / key_of(lease)).exists()
    assert len(constructed) == 1  # no runtime/browser for refused work
    manager.client.containers.list.assert_not_called()  # nothing evicted to make room
    manager.client.api.containers.assert_not_called()
    assert sorted(p.name for p in directory.iterdir()) == snapshot
    assert (directory / 'sandbox-profile.tar').read_bytes() == b'previous-good-archive'
    assert set(executor.ledger.entries()) == {key_of(first)}


@pytest.mark.asyncio
async def test_quarantined_profile_is_refused_without_replay_or_overwrite(tmp_path):
    executor, bindings, constructed, run = await executor_with(tmp_path, quota(tmp_path), FailedSave)
    first = lease_for('alice', 'p', 't1')
    await run(first)
    marker = (tmp_path / key_of(first) / 'recovery-required').read_text()
    finish, release = await run(lease_for('alice', 'p', 't2', generation=2))
    assert finish.kwargs['error'].code == ErrorCode.PROFILE_RECOVERY_REQUIRED
    assert finish.kwargs['error'].message == 'Profile requires recovery'
    assert release.kwargs == {'profile_saved': False}
    assert len(constructed) == 1
    assert (tmp_path / key_of(first) / 'recovery-required').read_text() == marker
    assert executor.ledger.entries()[key_of(first)]['task_id'] == 't1'


@pytest.mark.asyncio
async def test_in_flight_reservation_counts_and_proven_save_frees_it(tmp_path):
    settings = quota(tmp_path, quarantine_max_count=1, quarantine_max_bytes=GIB)
    gate, entered = asyncio.Event(), asyncio.Event()

    class Held(Runtime):
        async def run(self):
            entered.set()
            await gate.wait()
            return await super().run()
    executor, bindings, constructed, run = await executor_with(tmp_path, settings, Held)
    first = asyncio.create_task(run(lease_for('alice', 'p', 't1')))
    await entered.wait()
    assert executor.ledger.entries()[key_of(lease_for('alice', 'p'))]['state'] == 'reserved'
    finish, _ = await run(lease_for('bob', 'p', 't2'))
    assert finish.kwargs['error'].code == ErrorCode.CAPACITY_EXHAUSTED
    gate.set()
    finish, release = await first
    assert finish.args[2] == TaskState.COMPLETED and release.kwargs == {'profile_saved': True}
    assert executor.ledger.entries() == {} and not executor.held
    finish, _ = await run(lease_for('bob', 'p', 't3'))
    assert finish.args[2] == TaskState.COMPLETED
    assert len(constructed) == 2


@pytest.mark.asyncio
async def test_ledger_write_failure_keeps_slot_charged(tmp_path, monkeypatch):
    settings = quota(tmp_path, quarantine_max_count=1, quarantine_max_bytes=GIB)
    executor, bindings, constructed, run = await executor_with(tmp_path, settings, FailedSave)

    def broken(*args, **kwargs):
        raise OSError('synthetic disk failure')
    monkeypatch.setattr(executor.ledger, 'quarantine', broken)
    finish, release = await run(lease_for('alice', 'p', 't1'))
    assert finish.kwargs['error'].code == 'profile_recovery_required'
    assert release.kwargs == {'profile_saved': False}
    assert executor.ledger.entries()[key_of(lease_for('alice', 'p'))]['state'] == 'reserved'
    finish, _ = await run(lease_for('bob', 'p', 't2'))
    assert finish.kwargs['error'].code == ErrorCode.CAPACITY_EXHAUSTED
    # Recovery converts the dead reservation into a quarantine (marker present).
    manager = MagicMock()
    manager.client.containers.list.return_value = []
    executor.manager_factory = lambda *args: manager
    await executor.recover(())
    assert executor.ledger.entries()[key_of(lease_for('alice', 'p'))]['state'] == 'quarantined'


@pytest.mark.asyncio
async def test_explicit_profile_deletion_cleans_artifacts_and_frees_the_slot(tmp_path):
    settings = quota(tmp_path, quarantine_max_count=1, quarantine_max_bytes=GIB)
    first = lease_for('alice', 'p', 't1')
    labels = {'flowpilot-task': '1', 'profile-key': key_of(first), 'task-host': host_key(settings)}
    retained = MagicMock(labels=labels)
    manager = MagicMock()
    manager.client.containers.list.return_value = [retained]
    executor, bindings, constructed, run = await executor_with(tmp_path, settings, FailedSave, manager)
    await run(first)
    assert (await run(lease_for('bob', 'p', 't2')))[0].kwargs['error'].code == ErrorCode.CAPACITY_EXHAUSTED
    # Failed artifact removal keeps both the artifact and its charged slot.
    retained.remove.side_effect = RuntimeError('docker unavailable')
    with pytest.raises(RuntimeError):
        await executor.delete_profile(first.principal, first.profile_id)
    assert (tmp_path / key_of(first) / 'recovery-required').exists()
    assert key_of(first) in executor.ledger.entries()
    retained.remove.side_effect = None
    await executor.delete_profile(first.principal, first.profile_id)
    retained.remove.assert_called_with(force=True)
    assert not (tmp_path / key_of(first) / 'recovery-required').exists()
    assert executor.ledger.entries() == {}
    assert (await run(lease_for('bob', 'p', 't3')))[0].kwargs['error'].code == 'profile_recovery_required'
    assert len(constructed) == 2  # slot freed: bob's work ran (and failed its own save)


@pytest.mark.asyncio
async def test_recover_reconciles_dead_reservations_and_adopts_unledgered_markers(tmp_path):
    settings = quota(tmp_path, quarantine_max_count=5, quarantine_max_account_count=5,
                     quarantine_max_bytes=5 * GIB, quarantine_max_account_bytes=5 * GIB)
    ledger = QuarantineLedger(settings)
    clean, crashed = lease_for('alice', 'clean'), lease_for('alice', 'crashed')
    legacy, orphan = lease_for('bob', 'legacy'), lease_for('carol', 'orphan')
    ledger.reserve(clean, key_of(clean))      # dead, nothing retained -> dropped
    ledger.reserve(crashed, key_of(crashed))  # dead, container found -> quarantined
    for lease in (legacy, orphan):            # pre-ledger, empty markers
        (tmp_path / key_of(lease)).mkdir()
        (tmp_path / key_of(lease) / 'recovery-required').touch()
    (tmp_path / ('a' * 64)).mkdir()  # unrelated profile without a marker
    container = MagicMock(labels={'flowpilot-task': '1', 'profile-key': key_of(crashed),
                                  'task-host': host_key(settings)})
    manager = MagicMock()
    manager.client.containers.list.return_value = [container]
    bindings = bindings_double()
    bindings.control.release_browser.side_effect = None

    def forbidden(*args):
        raise AssertionError('Recovery must never construct a runtime')
    executor = Executor(settings, runtime_factory=forbidden, manager_factory=lambda *args: manager)
    executor.bind(bindings)
    await executor.startup()
    await executor.recover((legacy,))
    entries = ledger.entries()
    assert set(entries) == {key_of(crashed), key_of(legacy), key_of(orphan)}
    assert all(entry['state'] == 'quarantined' for entry in entries.values())
    assert entries[key_of(legacy)]['account_id'] == 'bob'   # attributed by lease
    assert entries[key_of(orphan)]['account_id'] is None   # unknown owner: charged to all
    assert ledger.usage('dave')['account_count'] == 1
    container.stop.assert_called_once_with(timeout=5)
    container.remove.assert_not_called()
    for lease in (crashed, legacy, orphan):
        assert (tmp_path / key_of(lease) / 'recovery-required').exists()
    await executor.recover(())  # idempotent
    assert ledger.entries() == entries


@pytest.mark.asyncio
async def test_real_control_reports_save_failure_and_quota_refusal(tmp_path):
    """Real Control/SQLite authority: typed states, recovery flag, no replay."""
    from backend.tasks.control import Control
    from backend.tasks.repository import Repository
    settings = quota(tmp_path, quarantine_max_count=1, quarantine_max_bytes=GIB)
    repository = Repository(settings, b'synthetic-test-receipt-key-0000000')
    await repository.initialize()
    control = Control(settings)
    control.repository, control.accepting = repository, True
    runs = []

    class Recorded(FailedSave):
        async def run(self):
            runs.append(self.execution.lease.task_id)
            return await super().run()
    manager = MagicMock()
    manager.client.containers.list.return_value = []
    executor = Executor(settings, runtime_factory=Recorded, manager_factory=lambda *args: manager)
    bindings = replace(bindings_double(), control=control, executor=executor)
    executor.bind(bindings)
    control.bind(bindings)
    await executor.startup()
    owner = Principal(account_id='alice')
    profile = await control.create_profile(owner, ProfileCreate(label='Synthetic', idempotency_key='profile-key1'))

    async def settle(task_id):
        for _ in range(500):
            view = await repository.get(owner, task_id)
            if view.state in TERMINAL_STATES and not control._executions:
                return view
            await asyncio.sleep(.01)
        raise AssertionError('task did not settle')

    admitted = await control.start(owner, TaskStart(task='Synthetic effect', profile_id=profile.profile_id,
                                                    idempotency_key='quota-real-1'))
    view = await settle(admitted.task_id)
    assert view.state == TaskState.FAILED and view.error.code == ErrorCode.PROFILE_RECOVERY_REQUIRED
    assert view.result is None
    listed = (await control.list_profiles(owner)).items
    assert next(p for p in listed if p.profile_id == profile.profile_id).state == 'recovery_required'
    # Same profile: refused at durable admission. Fresh profile: refused by the
    # quota before any browser. Duplicate admission returns the original receipt.
    with pytest.raises(TaskFault) as raised:
        await control.start(owner, TaskStart(task='Synthetic effect', profile_id=profile.profile_id,
                                             idempotency_key='quota-real-2'))
    assert raised.value.error.code == ErrorCode.PROFILE_RECOVERY_REQUIRED
    fresh = await control.start(owner, TaskStart(task='Synthetic effect', idempotency_key='quota-real-3'))
    view = await settle(fresh.task_id)
    assert view.state == TaskState.FAILED and view.error.code == ErrorCode.CAPACITY_EXHAUSTED
    again = await control.start(owner, TaskStart(task='Synthetic effect', profile_id=profile.profile_id,
                                                 idempotency_key='quota-real-1'))
    assert again.task_id == admitted.task_id
    await asyncio.sleep(.05)
    assert runs == [admitted.task_id]
    # Restart-style recovery never replays; explicit deletion frees the slot.
    await executor.recover(())
    assert runs == [admitted.task_id]
    await control.delete_profile(owner, profile.profile_id)
    assert executor.ledger.entries() == {}
    after = await control.start(owner, TaskStart(task='Synthetic effect', idempotency_key='quota-real-4'))
    assert (await settle(after.task_id)).error.code == ErrorCode.PROFILE_RECOVERY_REQUIRED
    assert runs == [admitted.task_id, after.task_id]


def test_budget_save_failure_closes_the_daemon_archive_stream(tmp_path):
    """An abandoned get_archive stream keeps Docker's container lock: later
    inspect/stop/remove of the retained artifact hangs (reproduced in real
    Docker). A failed save must release the stream before reporting failure."""
    from backend.tasks.browser_adapter import TaskSandbox
    settings = TaskSettings(state_dir=tmp_path)
    sandbox = TaskSandbox(tmp_path, 'key', settings=settings, lease=lease_for())
    sandbox.archive.write_bytes(b'previous-good-archive')
    sandbox.info = {'container_id': 'container', 'sandbox_id': 'sandbox'}
    sandbox.manager._client = MagicMock()
    stream = {'open': False}

    def oversized():
        stream['open'] = True
        try:
            while True:
                yield b'\0' * (8 * MIB)
        finally:
            stream['open'] = False
    container = sandbox.manager.client.containers.get.return_value
    container.get_archive.return_value = (oversized(), {})
    with pytest.raises(RuntimeError, match='Profile save failed'):
        sandbox._close()
    assert not stream['open']  # closed although the raised traceback is still alive
    assert sandbox.archive.read_bytes() == b'previous-good-archive'
    assert (tmp_path / 'recovery-required').exists()
    container.remove.assert_not_called()
