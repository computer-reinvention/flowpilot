"""Single-host task execution. Claimed executions are never replayed on restart."""
import asyncio
import fcntl
import json
import logging
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..sandbox import SandboxManager
from ..task_contracts import (
    ErrorCode, PollRequest, TaskFault, TaskMetrics, TaskResult, TaskState, TERMINAL_STATES, task_error,
)
from .memory import QuarantineLedger, delete_memory, storage_key
from .browser_adapter import host_key
from .runtime import create_runtime, stale


DISPATCH_RECEIPT_LIMIT = 256
MEASURE_SECONDS = 5
logger = logging.getLogger(__name__)


@dataclass
class Active:
    execution: object
    runtime: object
    worker: object = None
    cancelled: bool = False
    settled: asyncio.Event = field(default_factory=asyncio.Event)


class Executor:
    def __init__(self, settings, *, runtime_factory=create_runtime, manager_factory=SandboxManager):
        self.settings = settings
        self.runtime_factory, self.manager_factory = runtime_factory, manager_factory
        self.bindings = None
        self.active = {}
        # Optimization only. Durable claim-revision CAS, not cache membership,
        # is the authority to dispatch. Active/preflight entries are pinned apart.
        self.seen = OrderedDict()
        self.ready = False
        # Fail-closed recovery-artifact quota accounting (no I/O at construction).
        self.ledger = QuarantineLedger(settings)
        self.held = Counter()

    def bind(self, bindings):
        if self.bindings is not None:
            raise RuntimeError('Executor already bound')
        self.bindings = bindings

    async def startup(self):
        if self.bindings is None:
            raise RuntimeError('Executor must be bound')
        from ..trace import ENABLED
        if ENABLED:
            raise RuntimeError('Raw reflex tracing must be disabled for hosted tasks')
        self.settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ready = True

    async def shutdown(self):
        self.ready = False
        await asyncio.gather(*(self.cancel(a.execution.lease) for a in tuple(self.active.values())))

    def _key(self, lease):
        return lease.principal.account_id, lease.task_id, lease.generation

    async def execute(self, execution):
        if not self.ready:
            raise RuntimeError('Executor is not ready')
        lease = execution.lease
        key = self._key(lease)
        if key in self.seen:
            self.seen.move_to_end(key)
            return
        if key in self.active:
            return
        # Pin before the first await, including the DB preflight. Cache eviction
        # must never lose an active writer or allow a concurrent second dispatch.
        active = Active(execution, None)
        self.active[key] = active
        authorized = False
        try:
            view = await self.bindings.control.get(lease.principal, lease.task_id, PollRequest())
            if (view.state != TaskState.RUNNING or view.revision != execution.revision
                    or (view.task_id, view.session_id, view.profile_id) != (lease.task_id, lease.session_id, lease.profile_id)):
                return
            # The frozen mutation port atomically validates the FULL lease,
            # including execution generation, AND consumes the original claim
            # revision. Never substitute the latest fetched revision: that would
            # authorize replay of an evicted, already-dispatched Execution.
            await self.bindings.control.progress(lease, execution.revision, 'Starting private browser')
            authorized = True
            await self._execute_authorized(execution, active)
        finally:
            self.active.pop(key, None)
            active.settled.set()
            if authorized:
                self.seen[key] = None
                while len(self.seen) > DISPATCH_RECEIPT_LIMIT:
                    self.seen.popitem(last=False)

    async def _execute_authorized(self, execution, active):
        lease = execution.lease
        runtime = reservation = None
        holding = False
        key = storage_key(lease.principal, lease.profile_id)
        state, result, error = TaskState.FAILED, None, None
        saved, stopped = False, False
        try:
            if not self.ready:
                raise TaskFault(task_error(ErrorCode.INTERRUPTED, 'Executor stopped during dispatch preflight'))
            if active.cancelled:
                raise asyncio.CancelledError
            remaining = (execution.deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                raise TimeoutError
            # Fail closed BEFORE any container exists: reserve a worst-case
            # recovery-artifact slot or refuse this work with a typed error.
            # Refusal never deletes or reuses existing recovery data.
            # Pin the key first so concurrent recovery never drops a live slot.
            self.held[key] += 1
            holding = True
            reserving = asyncio.create_task(asyncio.to_thread(self.ledger.reserve, lease, key))
            try:
                reservation = await asyncio.shield(reserving)
            except asyncio.CancelledError:
                # The thread may still commit; account for whatever it wrote.
                try:
                    reservation = await reserving
                except Exception:
                    pass
                raise
            remaining = (execution.deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                raise TimeoutError
            runtime = self.runtime_factory(self.settings, execution, self.bindings)
            active.runtime = runtime
            active.worker = asyncio.create_task(runtime.run())
            done, _ = await asyncio.wait({active.worker}, timeout=remaining)
            if not done:
                raise TimeoutError
            answer = active.worker.result()
            if active.cancelled:
                raise asyncio.CancelledError
            result = TaskResult(answer=answer['answer'], metrics=TaskMetrics(**{
                k: v for k, v in answer.get('metrics', {}).items() if k in TaskMetrics.model_fields}))
            state = TaskState.COMPLETED
        except asyncio.CancelledError:
            state = TaskState.CANCELLED if active.cancelled else TaskState.INTERRUPTED
            error = None if active.cancelled else task_error(ErrorCode.INTERRUPTED, 'Execution interrupted; inspect website state before a new task')
            if runtime is not None and runtime.declined:
                state = TaskState.FAILED
                error = task_error(ErrorCode.CONFLICT, 'Confirmation declined; no further actions authorized')
        except TimeoutError:
            state = TaskState.INTERRUPTED
            error = task_error(ErrorCode.INTERRUPTED, 'Execution deadline exceeded; website effects may be partial')
        except TaskFault as exc:
            state = TaskState.INTERRUPTED if exc.error.code == ErrorCode.INTERRUPTED else TaskState.FAILED
            error = exc.error
        except Exception:
            state = TaskState.FAILED
            error = task_error(ErrorCode.PROVIDER_FAILURE, 'Execution failed; inspect website state before a new task')
        finally:
            # Cancellation is not terminal until browser writes and model children
            # have stopped. Bounded asyncio.wait does not wait forever on a task
            # that suppresses CancelledError (unlike wait_for cancellation).
            async def cleanup():
                nonlocal stopped, saved
                if runtime is None:
                    # No runtime was constructed: no model/browser writer exists.
                    stopped = saved = True
                    return
                runtime.fenced = True
                if active.worker and not active.worker.done():
                    active.worker.cancel()
                    await runtime.stop_writers()
                    done, _ = await asyncio.wait({active.worker}, timeout=self.settings.cancellation_seconds / 2)
                    if not done:
                        raise TimeoutError
                if state != TaskState.COMPLETED:
                    await runtime.stop_writers()
                stopped = True
                await runtime.close()
                saved = True
                if error and error.code == ErrorCode.PROFILE_RECOVERY_REQUIRED:
                    saved = False

            cleanup_task = asyncio.create_task(cleanup())
            done, _ = await asyncio.wait({cleanup_task}, timeout=self.settings.cancellation_seconds)
            if not done:
                cleanup_task.cancel()
                if hasattr(runtime, 'quarantine'):
                    runtime.quarantine()
            elif cleanup_task.cancelled() or cleanup_task.exception() is not None:
                pass
            if not stopped:
                state, result = TaskState.INTERRUPTED, None
                error = task_error(ErrorCode.INTERRUPTED, 'Could not prove execution stopped; profile requires recovery')
            elif not saved:
                state, result = TaskState.FAILED, None
                error = task_error(ErrorCode.PROFILE_RECOVERY_REQUIRED, 'Profile save failed; retained state requires recovery')
            if not saved and hasattr(runtime, 'quarantine'):
                runtime.quarantine()
            if runtime is None and error is not None and error.code == ErrorCode.PROFILE_RECOVERY_REQUIRED:
                saved = False  # refused before launch: the profile already holds a quarantine
            try:
                if reservation is not None:
                    # Before settlement: the slot stays charged as a quarantine
                    # unless the save was proven. Failures keep it reserved.
                    await self._settle_reservation(reservation, lease, saved)
            finally:
                if holding:
                    self.held[key] -= 1
                    if self.held[key] <= 0:
                        del self.held[key]
            view = await self.bindings.control.get(lease.principal, lease.task_id, PollRequest())
            if view.state not in TERMINAL_STATES:
                if view.state == TaskState.CANCELLING and stopped and saved:
                    state, result, error = TaskState.CANCELLED, None, None
                elif view.state == TaskState.CANCELLING:
                    state, result = TaskState.INTERRUPTED, None
                await self.bindings.control.finish(lease, view.revision, state, result=result, error=error)
            await self.bindings.control.release_browser(lease, profile_saved=saved)

    async def cancel(self, lease):
        active = self.active.get(self._key(lease))
        if active is None or active.execution.lease != lease:
            return
        active.cancelled = True
        if active.runtime is not None:
            active.runtime.fenced = True
        deadline = asyncio.get_running_loop().time() + self.settings.cancellation_seconds
        if active.worker:
            active.worker.cancel()
            # execute owns settlement and save. Stopping the container also stops
            # browser-side work that outlives a cancelled client evaluate call.
            stop = asyncio.create_task(active.runtime.stop_writers())
            done, _ = await asyncio.wait({stop}, timeout=self.settings.cancellation_seconds)
            if not done:
                stop.cancel()
            else:
                stop.result()
        waiter = asyncio.create_task(active.settled.wait())
        done, _ = await asyncio.wait({waiter}, timeout=max(0, deadline - asyncio.get_running_loop().time()))
        if not done:
            waiter.cancel()

    async def browser(self, lease, interaction_id, generation):
        active = self.active.get(self._key(lease))
        if active is None or active.execution.lease != lease or active.runtime is None:
            raise stale()
        return await active.runtime.browser(interaction_id, generation)

    async def release_human(self, lease, interaction_id, generation):
        active = self.active.get(self._key(lease))
        if active is None or active.execution.lease != lease or active.runtime is None:
            raise stale()
        await active.runtime.release_human(interaction_id, generation)

    async def recover(self, leases):
        # Never restore an agent stack or launch a model. Stop only task-labelled
        # resources. Preserve archives and quarantine uncertain live profiles.
        def reconcile():
            manager = self.manager_factory(str(self.settings.state_dir))
            for container in manager.client.containers.list(all=True, filters={
                    'label': ['flowpilot-task=1', f'task-host={host_key(self.settings)}']}):
                labels = container.labels
                if labels.get('flowpilot-task') != '1' or labels.get('task-host') != host_key(self.settings):
                    continue
                profile = labels.get('profile-key', '')
                if len(profile) != 64 or any(c not in '0123456789abcdef' for c in profile):
                    continue
                directory = self.settings.state_dir / profile
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                # Recovery artifacts are stopped containers: at most one writer
                # per profile exists by the filesystem lock, never auto-replayed.
                with (directory / 'session.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    (directory / 'recovery-required').touch(mode=0o600)
                    container.stop(timeout=5)
        await asyncio.to_thread(reconcile)
        # Account for every retained artifact before admission reopens: dead
        # reservations become quarantines (marker present) or are dropped
        # (nothing retained); unledgered markers are adopted. Never deletes.
        attribution = {storage_key(lease.principal, lease.profile_id): lease.principal.account_id for lease in leases}
        await asyncio.to_thread(self.ledger.reconcile, frozenset(self.held), attribution)
        for lease in leases:
            await self.bindings.control.release_browser(lease, profile_saved=False)

    async def delete_profile(self, principal, profile_id):
        if any(a.execution.lease.principal == principal and a.execution.lease.profile_id == profile_id for a in self.active.values()):
            raise TaskFault(task_error(ErrorCode.CONFLICT, 'Profile is active'))
        def remove_artifacts(profile_key):
            manager = self.manager_factory(str(self.settings.state_dir))
            containers = manager.client.containers.list(all=True, filters={
                'label': ['flowpilot-task=1', f'profile-key={profile_key}', f'task-host={host_key(self.settings)}']})
            for container in containers:
                if (container.labels.get('flowpilot-task') == '1'
                        and container.labels.get('task-host') == host_key(self.settings)
                        and container.labels.get('profile-key') == profile_key):
                    container.stop(timeout=5)
                    container.remove(force=True)
        # Explicit owner deletion is the only path that frees a quarantine slot,
        # and only after its containers, archive and marker are really gone.
        await asyncio.to_thread(delete_memory, self.settings, principal, profile_id, remove_artifacts,
                                self.ledger.forget)

    async def _settle_reservation(self, key, lease, saved):
        """Release a proven-saved slot or convert it to a quarantine. Never raises."""
        try:
            if saved:
                await asyncio.to_thread(self.ledger.release, key, lease)
                return
            measured = None
            try:
                measured = await asyncio.wait_for(asyncio.to_thread(self._measure, key), MEASURE_SECONDS)
            except Exception:
                pass  # unknown size keeps the full worst-case reservation
            await asyncio.to_thread(self.ledger.quarantine, key, lease, measured)
        except Exception:
            # The slot stays reserved and charged; recovery reconciles it later.
            logger.warning('Quarantine ledger update failed; reservation retained task_id=%s generation=%d',
                           lease.task_id, lease.generation)

    def _measure(self, key):
        """Observed bytes of a retained artifact: preserved archive + writable layer."""
        directory = self.settings.state_dir / key
        total = sum(path.stat().st_size for path in directory.glob('sandbox-profile.tar*')
                    if path.is_file() and not path.is_symlink())
        try:
            container_id = json.loads((directory / 'recovery-required').read_text()).get('container_id')
        except (OSError, ValueError, AttributeError):
            container_id = None
        if container_id:
            manager = self.manager_factory(str(self.settings.state_dir))
            rows = manager.client.api.containers(all=True, filters={'id': container_id}, size=True)
            if len(rows) != 1 or not isinstance(rows[0].get('SizeRw'), int):
                return None
            total += rows[0]['SizeRw']
        return total


def create_executor(settings):
    return Executor(settings)
