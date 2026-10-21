"""Single-host task execution. Claimed executions are never replayed on restart."""
import asyncio
import fcntl
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..sandbox import SandboxManager
from ..task_contracts import (
    ErrorCode, PollRequest, TaskFault, TaskMetrics, TaskResult, TaskState, TERMINAL_STATES, task_error,
)
from .memory import delete_memory
from .browser_adapter import host_key
from .runtime import create_runtime, stale


DISPATCH_RECEIPT_LIMIT = 256


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
        runtime = None
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
        await asyncio.to_thread(delete_memory, self.settings, principal, profile_id, remove_artifacts)


def create_executor(settings):
    return Executor(settings)
