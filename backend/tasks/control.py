"""Durable task orchestration. Factories/bind perform no filesystem or task I/O."""

import asyncio
import weakref
from datetime import timedelta

from ..task_contracts import (
    INTERACTION_STATE, TERMINAL_STATES, AssistanceRequest, ErrorCode, InteractionKind, PollRequest, TaskFault,
    TaskState, task_error,
)
from .repository import Repository, fail, now


class Control:
    def __init__(self, settings):
        self.settings = settings
        self.bindings = None
        self.repository = None
        self.accepting = False
        self._executions = {}
        self._stops = {}
        self._waiters = {}
        self._locks = weakref.WeakValueDictionary()
        self._admissions = set()

    def bind(self, bindings):
        self.bindings = bindings

    def _lock(self, task_id):
        return self._locks.setdefault(task_id, asyncio.Lock())

    async def _durable(self, operation):
        """Own commit/callback work even when the HTTP request disconnects."""
        job = asyncio.create_task(operation)
        self._admissions.add(job)
        def finished(task):
            self._admissions.discard(task)
            if not task.cancelled():
                task.exception()  # retrieve errors even if the caller went away
        job.add_done_callback(finished)
        return await asyncio.shield(job)

    async def startup(self):
        from ..auth import JWT_SECRET, validate_hosted_secret
        validate_hosted_secret(self.settings.enabled)
        if not self.bindings:
            raise RuntimeError('Bind all peer services before startup')
        secret = JWT_SECRET.encode()
        if len(secret) < 32:
            raise RuntimeError('Task response receipts require a 32-byte JWT secret')
        self.repository = Repository(self.settings, secret)
        await self.repository.initialize()
        leases = await self.repository.interrupt_active()
        await asyncio.wait_for(self.bindings.executor.recover(leases), self.settings.shutdown_seconds)
        for lease in leases:
            # If recover attested a saved profile via release_browser, this is
            # an idempotent no-op. Otherwise conservatively quarantine it.
            await self.repository.release_browser(lease, profile_saved=False)
        self.accepting = True

    async def shutdown(self):
        self.accepting = False
        if self.repository is None:
            return
        # Disconnect-safe admission jobs own the commit/dispatch window. Drain
        # them before taking the execution snapshot so shutdown cannot miss one.
        if self._admissions:
            await asyncio.gather(*tuple(self._admissions), return_exceptions=True)
        for task_id, (lease, job) in tuple(self._executions.items()):
            if not job.done():
                await self.cancel(lease.principal, task_id)
        pending = [job for _, job in self._executions.values()] + list(self._stops.values())
        if pending:
            done, unfinished = await asyncio.wait(pending, timeout=self.settings.shutdown_seconds)
            for job in unfinished:
                job.cancel()
            await asyncio.gather(*unfinished, return_exceptions=True)
        # Fence and recover even admitted-but-not-yet-dispatched work.
        leases = await self.repository.interrupt_active()
        if leases:
            await asyncio.wait_for(self.bindings.executor.recover(leases), self.settings.cancellation_seconds)
            for lease in leases:
                await self.repository.release_browser(lease, profile_saved=False)

    async def start(self, principal, request):
        if not self.accepting:
            raise TaskFault(task_error(ErrorCode.SHUTDOWN, 'Admission is closed', 5))
        return await self._durable(self._admit_dispatch(principal, request))

    async def _admit_dispatch(self, principal, request):
        # Caller cancellation cannot strand a committed queued record.
        admission = await self.repository.admit(principal, request)
        execution = await self.repository.claim(principal, admission.task_id)
        if execution:
            try:
                job = asyncio.create_task(self._execute(execution))
                self._executions[admission.task_id] = (execution.lease, job)
            except Exception:
                await self._unknown(execution.lease)
        return admission

    async def _unknown(self, lease):
        try:
            await self.repository.interrupt_one(lease)
        except TaskFault as fault:
            if fault.error.code == ErrorCode.NOT_FOUND:
                return  # a released terminal profile/history was deleted
            raise
        # Keep the reservation when cleanup fails; recovery evidence is durable.
        try:
            await asyncio.wait_for(self.bindings.executor.recover((lease,)), self.settings.cancellation_seconds)
        except Exception:
            return
        await self.repository.release_browser(lease, profile_saved=False)

    async def _execute(self, execution):
        try:
            remaining = max(0.01, (execution.deadline - now()).total_seconds())
            await asyncio.wait_for(self.bindings.executor.execute(execution), remaining)
            view = await self.repository.get(execution.lease.principal, execution.lease.task_id)
            if view.state not in TERMINAL_STATES:
                await self._unknown(execution.lease)
        except asyncio.CancelledError:
            try:
                view = await self.repository.get(execution.lease.principal, execution.lease.task_id)
                if view.state not in TERMINAL_STATES:
                    await self._unknown(execution.lease)
            except TaskFault as fault:
                if fault.error.code != ErrorCode.NOT_FOUND:
                    raise
            raise
        except Exception:
            await self._unknown(execution.lease)
        finally:
            self._executions.pop(execution.lease.task_id, None)

    async def get(self, principal, task_id, poll=PollRequest()):
        view = await self.repository.get(principal, task_id)
        if poll.after_revision is not None and poll.after_revision > view.revision:
            fail(ErrorCode.INVALID_INPUT, 'Revision is ahead of current task')
        deadline = asyncio.get_running_loop().time() + min(poll.wait_seconds, self.settings.poll_wait_seconds)
        while poll.after_revision == view.revision and view.state not in TERMINAL_STATES and asyncio.get_running_loop().time() < deadline:
            # DB readback also observes other handles; no waiter registration leaks.
            await asyncio.sleep(min(.05, max(0, deadline - asyncio.get_running_loop().time())))
            view = await self.repository.get(principal, task_id)
        return view

    async def cancel(self, principal, task_id):
        return await self._durable(self._cancel(principal, task_id))

    async def _cancel(self, principal, task_id):
        async with self._lock(task_id):
            view = await self.repository.request_cancel(principal, task_id)
            if view.state == TaskState.CANCELLING and task_id not in self._stops:
                lease = await self.repository.lease(principal, task_id)
                self._stops[task_id] = asyncio.create_task(self._stop(lease))
            return view

    async def _stop(self, lease):
        try:
            await asyncio.wait_for(self.bindings.executor.cancel(lease), self.settings.cancellation_seconds)
            view = await self.repository.get(lease.principal, lease.task_id)
            if view.state == TaskState.CANCELLING:
                # A returning cancel alone is not proof of terminal settlement.
                await self._unknown(lease)
        except Exception:
            await self._unknown(lease)
        finally:
            for key, waiter in tuple(self._waiters.items()):
                if key[0] == lease.task_id and not waiter.done():
                    waiter.cancel()
            self._stops.pop(lease.task_id, None)
            execution = self._executions.get(lease.task_id)
            if execution and not execution[1].done():
                execution[1].cancel()

    async def respond(self, principal, task_id, interaction_id, response):
        return await self._durable(self._respond(principal, task_id, interaction_id, response))

    async def _respond(self, principal, task_id, interaction_id, response):
        async with self._lock(task_id):
            before = await self.repository.get(principal, task_id)
            waiter = self._waiters.get((task_id, interaction_id, response.generation))
            # A live unresolved interaction must have its current claimed waiter.
            if before.interaction and before.interaction.interaction_id == interaction_id and (waiter is None or waiter.done()):
                fail(ErrorCode.STALE_GENERATION, 'Interaction executor is no longer waiting')
            if before.interaction and before.interaction.interaction_id == interaction_id and before.interaction.kind == InteractionKind.BROWSER:
                response.validate_for(before.interaction, now())
                await self.repository.validate_pending_response(principal, task_id, interaction_id, response)
                lease = await self.repository.assistance_lease(principal, task_id, AssistanceRequest(interaction_id=interaction_id, generation=response.generation))
                try:
                    # No DB transaction spans this browser I/O. The executor
                    # fences new human handles, drains writes and held controls.
                    await asyncio.wait_for(self.bindings.executor.release_human(lease, interaction_id, response.generation), self.settings.cancellation_seconds)
                except TaskFault:
                    raise
                except Exception:
                    raise TaskFault(task_error(ErrorCode.UNAVAILABLE, 'Human browser input could not be safely released', 5)) from None
                if waiter is None or waiter.done():
                    fail(ErrorCode.STALE_GENERATION, 'Interaction executor is no longer waiting')
            # Final transaction revalidates current pause/expiry and atomically
            # revokes capabilities before any transient response reaches agent.
            view = await self.repository.resolve(principal, task_id, interaction_id, response)
            if waiter and not waiter.done() and before.interaction and view.state == TaskState.RUNNING:
                waiter.set_result(response)
            return view

    async def wait_for_interaction(self, lease, interaction):
        key = (lease.task_id, interaction.interaction_id, interaction.generation)
        async with self._lock(lease.task_id):
            if key in self._waiters:
                fail(ErrorCode.CONFLICT)
            view = await self.repository.get(lease.principal, lease.task_id)
            deadline = min(interaction.expires_at, now() + timedelta(seconds=self.settings.human_wait_seconds), view.created_at + timedelta(seconds=self.settings.task_timeout_seconds))
            interaction = interaction.model_copy(update={'expires_at': deadline})
            waiter = asyncio.get_running_loop().create_future()
            self._waiters[key] = waiter
            try:
                await self.repository.transition(lease, view.revision, INTERACTION_STATE[interaction.kind], interaction=interaction)
            except BaseException:
                self._waiters.pop(key, None)
                raise
        try:
            return await asyncio.wait_for(waiter, max(0.01, (deadline - now()).total_seconds()))
        except TimeoutError:
            await self.cancel(lease.principal, lease.task_id)
            raise TaskFault(task_error(ErrorCode.STALE_GENERATION, 'Interaction expired')) from None
        finally:
            self._waiters.pop(key, None)

    async def require_approval(self, lease, interaction_id, generation, action_id):
        await self.repository.approval(lease, interaction_id, generation, action_id)

    async def progress(self, lease, expected_revision, summary):
        return await self.repository.set_progress(lease, expected_revision, summary)

    async def finish(self, lease, expected_revision, state, *, result=None, error=None):
        if state not in TERMINAL_STATES:
            fail(ErrorCode.INVALID_INPUT, 'Finish requires a terminal state')
        return await self.repository.transition(lease, expected_revision, state, result=result, error=error)

    async def release_browser(self, lease, *, profile_saved):
        await self.repository.release_browser(lease, profile_saved=profile_saved)

    async def create_profile(self, principal, request):
        if not self.accepting:
            raise TaskFault(task_error(ErrorCode.SHUTDOWN, 'Admission is closed', 5))
        return await self.repository.create_profile(principal, request)

    async def list_profiles(self, principal, cursor=None):
        return await self.repository.list_profiles(principal, cursor)

    async def delete_profile(self, principal, profile_id):
        # Tombstone first prevents new admissions during runtime/archive deletion.
        async with self._lock('profile:' + profile_id):
            await self.repository.begin_delete(principal, profile_id)
            await asyncio.wait_for(self.bindings.executor.delete_profile(principal, profile_id), self.settings.cancellation_seconds)
            await self.repository.delete_profile(principal, profile_id)

    async def save_capability(self, binding):
        await self.repository.save_capability(binding)

    async def assistance_lease(self, principal, task_id, request):
        return await self.repository.assistance_lease(principal, task_id, request)

    async def capability(self, digest, now):
        return await self.repository.capability(digest, now)

    async def revoke_capabilities(self, lease):
        await self.repository.revoke_capabilities(lease)


def create_control(settings):
    return Control(settings)
