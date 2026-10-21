"""Short SQLite transactions are the authority; no browser I/O occurs here.

Executable requests and response values are deliberately never stored. A crash
interrupts even unclaimed admissions, so there is no durable replay queue.
"""

import asyncio
import hmac
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from ..task_contracts import (
    API_PREFIX, INTERACTION_STATE, TERMINAL_STATES, ErrorCode, Interaction,
    Principal, ProfileList, ProfileView, TaskAdmission, TaskFault, TaskState,
    TaskView, require_transition, task_error,
)
from ..task_ports import AssistanceBinding, Execution, Lease
from .schema import SCHEMA


def now():
    return datetime.now(timezone.utc)


def fail(code, message='Operation conflicts with current state'):
    raise TaskFault(task_error(code, message, 5 if code in {ErrorCode.CAPACITY_EXHAUSTED, ErrorCode.UNAVAILABLE} else None))


def identifier(prefix):
    return prefix + '_' + uuid.uuid4().hex


class Repository:
    def __init__(self, settings, receipt_secret):
        self.settings = settings
        self.path = settings.state_dir / 'tasks.sqlite3'
        self.secret = receipt_secret
        self._requests = {}

    async def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        def initialize():
            with sqlite3.connect(self.path) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.executescript(SCHEMA)
        await asyncio.to_thread(initialize)
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    async def _run(self, operation):
        return await asyncio.to_thread(operation)

    def _row(self, db, principal, task_id):
        row = db.execute('SELECT * FROM bt_tasks WHERE id=? AND owner=?', (task_id, principal.account_id)).fetchone()
        if row is None:
            fail(ErrorCode.NOT_FOUND, 'Not found')
        return row

    def _lease(self, row):
        return Lease(principal=Principal(account_id=row['owner']), task_id=row['id'], session_id=row['session'], profile_id=row['profile'], generation=row['generation'])

    def _fenced(self, db, lease):
        row = self._row(db, lease.principal, lease.task_id)
        if self._lease(row) != lease or not row['claimed']:
            fail(ErrorCode.STALE_GENERATION, 'Execution lease is no longer current')
        return row

    def _write(self, db, row, state, *, interaction=None, result=None, error=None, progress=''):
        before = TaskView.model_validate_json(row['view'])
        view = TaskView(task_id=before.task_id, session_id=before.session_id, profile_id=before.profile_id,
                        state=state, revision=before.revision + 1, progress=progress,
                        interaction=interaction, result=result, error=error,
                        created_at=before.created_at, updated_at=now())
        db.execute('UPDATE bt_tasks SET state=?,revision=?,view=? WHERE id=? AND revision=?',
                   (str(state), view.revision, view.model_dump_json(), row['id'], before.revision))
        if state not in {TaskState.WAITING_FOR_BROWSER}:
            db.execute('DELETE FROM bt_capabilities WHERE task=?', (row['id'],))
        return view

    def _profile(self, db, principal, profile_id):
        row = db.execute('SELECT * FROM bt_profiles WHERE id=? AND owner=?', (profile_id, principal.account_id)).fetchone()
        if row is None:
            fail(ErrorCode.NOT_FOUND, 'Not found')
        return row

    async def admit(self, principal, request):
        def operation():
            with self.transaction() as db:
                prior = db.execute('SELECT * FROM bt_idempotency WHERE owner=? AND operation=? AND key=?',
                                   (principal.account_id, 'admit', request.idempotency_key)).fetchone()
                if prior:
                    if not hmac.compare_digest(prior['fingerprint'], request.fingerprint()):
                        fail(ErrorCode.CONFLICT, 'Idempotency key is already bound to another request')
                    receipt = TaskAdmission.model_validate_json(prior['receipt'])
                    self._row(db, principal, receipt.task_id)
                    return receipt
                profile_id = request.profile_id
                session_id = request.session_id
                if session_id:
                    session = db.execute('SELECT * FROM bt_sessions WHERE id=? AND owner=?', (session_id, principal.account_id)).fetchone()
                    if session is None:
                        fail(ErrorCode.NOT_FOUND, 'Not found')
                    if profile_id and profile_id != session['profile']:
                        fail(ErrorCode.CONFLICT)
                    profile_id = session['profile']
                    if db.execute("SELECT 1 FROM bt_tasks WHERE session=? AND state NOT IN ('completed','failed','cancelled','interrupted')", (session_id,)).fetchone():
                        fail(ErrorCode.CONFLICT, 'Session already has an active task')
                if profile_id:
                    profile = self._profile(db, principal, profile_id)
                    if profile['recovery'] or profile['deleting']:
                        fail(ErrorCode.PROFILE_RECOVERY_REQUIRED, 'Profile requires recovery')
                    if db.execute('SELECT 1 FROM bt_tasks WHERE profile=? AND reserved=1', (profile_id,)).fetchone():
                        fail(ErrorCode.CONFLICT, 'Profile is leased')
                total, owned = db.execute('SELECT count(*),coalesce(sum(owner=?),0) FROM bt_tasks WHERE reserved=1', (principal.account_id,)).fetchone()
                if total >= self.settings.max_active_browsers or owned >= self.settings.max_account_browsers:
                    fail(ErrorCode.CAPACITY_EXHAUSTED, 'Browser capacity is reserved')
                timestamp = now()
                if not profile_id:
                    profile_id = identifier('profile')
                    db.execute('INSERT INTO bt_profiles(id,owner,label,created) VALUES(?,?,?,?)', (profile_id, principal.account_id, 'Private profile', timestamp.isoformat()))
                if not session_id:
                    session_id = identifier('session')
                    db.execute('INSERT INTO bt_sessions VALUES(?,?,?)', (session_id, principal.account_id, profile_id))
                db.execute('UPDATE bt_profiles SET generation=generation+1 WHERE id=?', (profile_id,))
                generation = self._profile(db, principal, profile_id)['generation']
                task_id = identifier('task')
                view = TaskView(task_id=task_id, session_id=session_id, profile_id=profile_id, state=TaskState.QUEUED, revision=1, created_at=timestamp, updated_at=timestamp)
                db.execute('INSERT INTO bt_tasks(id,owner,session,profile,state,revision,generation,view) VALUES(?,?,?,?,?,?,?,?)',
                           (task_id, principal.account_id, session_id, profile_id, 'queued', 1, generation, view.model_dump_json()))
                admission = TaskAdmission(task_id=task_id, session_id=session_id, profile_id=profile_id, status_url=f'{API_PREFIX}/{task_id}', revision=1)
                db.execute('INSERT INTO bt_idempotency VALUES(?,?,?,?,?)', (principal.account_id, 'admit', request.idempotency_key, request.fingerprint(), admission.model_dump_json()))
                self._requests[task_id] = request
                return admission
        return await self._run(operation)

    async def get(self, principal, task_id):
        def operation():
            with self.transaction() as db:
                return TaskView.model_validate_json(self._row(db, principal, task_id)['view'])
        return await self._run(operation)

    async def lease(self, principal, task_id):
        def operation():
            with self.transaction() as db:
                return self._lease(self._row(db, principal, task_id))
        return await self._run(operation)

    async def claim(self, principal, task_id):
        def operation():
            with self.transaction() as db:
                row = self._row(db, principal, task_id)
                if row['state'] != 'queued' or row['claimed'] or task_id not in self._requests:
                    return None
                view = self._write(db, row, TaskState.RUNNING)
                db.execute('UPDATE bt_tasks SET claimed=1 WHERE id=? AND claimed=0', (task_id,))
                return Execution(lease=self._lease(row), revision=view.revision, request=self._requests.pop(task_id),
                                 admitted_at=view.created_at, deadline=view.created_at + timedelta(seconds=self.settings.task_timeout_seconds))
        return await self._run(operation)

    async def transition(self, lease, expected_revision, state, *, interaction=None, result=None, error=None, progress=''):
        def operation():
            with self.transaction() as db:
                row = self._fenced(db, lease)
                if row['revision'] != expected_revision:
                    fail(ErrorCode.CONFLICT, 'Revision is no longer current')
                try:
                    require_transition(TaskState(row['state']), state)
                except ValueError:
                    fail(ErrorCode.CONFLICT)
                if interaction:
                    if INTERACTION_STATE[interaction.kind] != state or interaction.expires_at <= now():
                        fail(ErrorCode.INVALID_INPUT, 'Invalid interaction')
                    previous = db.execute('SELECT max(generation) FROM bt_interactions WHERE task=?', (lease.task_id,)).fetchone()[0] or 0
                    if interaction.generation <= previous:
                        fail(ErrorCode.STALE_GENERATION, 'Interaction generation is no longer current')
                    db.execute('INSERT INTO bt_interactions(task,id,generation,descriptor) VALUES(?,?,?,?)',
                               (lease.task_id, interaction.interaction_id, interaction.generation, interaction.model_dump_json()))
                return self._write(db, row, state, interaction=interaction, result=result, error=error, progress=progress)
        return await self._run(operation)

    async def set_progress(self, lease, expected_revision, summary):
        def operation():
            with self.transaction() as db:
                row = self._fenced(db, lease)
                before = TaskView.model_validate_json(row['view'])
                if before.revision != expected_revision or before.state in TERMINAL_STATES or before.state == TaskState.CANCELLING:
                    fail(ErrorCode.CONFLICT)
                return self._write(db, row, before.state, interaction=before.interaction, progress=summary)
        return await self._run(operation)

    async def request_cancel(self, principal, task_id):
        def operation():
            with self.transaction() as db:
                row = self._row(db, principal, task_id)
                view = TaskView.model_validate_json(row['view'])
                if view.state in TERMINAL_STATES or view.state == TaskState.CANCELLING:
                    return view
                return self._write(db, row, TaskState.CANCELLING)
        return await self._run(operation)

    async def resolve(self, principal, task_id, interaction_id, response):
        def operation():
            with self.transaction() as db:
                row = self._row(db, principal, task_id)
                decision = db.execute('SELECT * FROM bt_interactions WHERE task=? AND id=?', (task_id, interaction_id)).fetchone()
                if decision is None:
                    fail(ErrorCode.NOT_FOUND, 'Not found')
                digest = response.receipt_digest(self.secret, principal, task_id, interaction_id)
                prior = db.execute('SELECT * FROM bt_idempotency WHERE owner=? AND operation=? AND key=?',
                                   (principal.account_id, 'respond', response.idempotency_key)).fetchone()
                if prior and not hmac.compare_digest(prior['fingerprint'], digest):
                    fail(ErrorCode.CONFLICT, 'Idempotency key is already bound to another response')
                if decision['digest']:
                    if decision['response_key'] != response.idempotency_key or not hmac.compare_digest(decision['digest'], digest):
                        fail(ErrorCode.CONFLICT, 'Interaction already has a different decision')
                    return TaskView.model_validate_json(decision['receipt'])
                before = TaskView.model_validate_json(row['view'])
                if not before.interaction or before.interaction.interaction_id != interaction_id:
                    fail(ErrorCode.STALE_GENERATION, 'Interaction is no longer active')
                response.validate_for(before.interaction, now())
                progress = 'Decision received; proposed submission was not authorized' if response.confirmed is False else 'Response received'
                view = self._write(db, row, TaskState.RUNNING, progress=progress)
                db.execute('UPDATE bt_interactions SET response_key=?,digest=?,confirmed=?,receipt=? WHERE task=? AND id=?',
                           (response.idempotency_key, digest, response.confirmed, view.model_dump_json(), task_id, interaction_id))
                db.execute('INSERT INTO bt_idempotency VALUES(?,?,?,?,?)',
                           (principal.account_id, 'respond', response.idempotency_key, digest, view.model_dump_json()))
                return view
        return await self._run(operation)

    async def approval(self, lease, interaction_id, generation, action_id):
        def operation():
            with self.transaction() as db:
                row = self._fenced(db, lease)
                decision = db.execute('SELECT * FROM bt_interactions WHERE task=? AND id=?', (lease.task_id, interaction_id)).fetchone()
                if row['state'] != 'running' or not decision or decision['confirmed'] != 1:
                    fail(ErrorCode.CONFLICT, 'Action was not approved')
                descriptor = Interaction.model_validate_json(decision['descriptor'])
                if (descriptor.generation, descriptor.action_id) != (generation, action_id) or descriptor.expires_at <= now():
                    fail(ErrorCode.STALE_GENERATION, 'Approval is no longer current')
                latest = db.execute('SELECT max(generation) FROM bt_interactions WHERE task=?', (lease.task_id,)).fetchone()[0]
                if latest != generation:
                    fail(ErrorCode.STALE_GENERATION, 'Approval is no longer current')
        return await self._run(operation)

    async def release_browser(self, lease, *, profile_saved):
        def operation():
            with self.transaction() as db:
                row = self._row(db, lease.principal, lease.task_id)
                if self._lease(row) != lease:
                    fail(ErrorCode.STALE_GENERATION)
                if row['state'] not in {str(s) for s in TERMINAL_STATES}:
                    fail(ErrorCode.CONFLICT, 'Active tasks retain their reservation')
                db.execute('UPDATE bt_tasks SET reserved=0 WHERE id=?', (lease.task_id,))
                if not profile_saved:
                    db.execute('UPDATE bt_profiles SET recovery=1 WHERE id=?', (lease.profile_id,))
        return await self._run(operation)

    async def interrupt_active(self):
        def operation():
            with self.transaction() as db:
                rows = db.execute("SELECT * FROM bt_tasks WHERE reserved=1 OR state NOT IN ('completed','failed','cancelled','interrupted')").fetchall()
                for row in rows:
                    if row['state'] not in {str(s) for s in TERMINAL_STATES}:
                        self._write(db, row, TaskState.INTERRUPTED, error=task_error(ErrorCode.INTERRUPTED, 'Execution interrupted; website effects may be partial'))
                    db.execute('UPDATE bt_tasks SET claimed=0 WHERE id=?', (row['id'],))
                    db.execute('DELETE FROM bt_capabilities WHERE task=?', (row['id'],))
                self._requests.clear()
                return tuple(self._lease(row) for row in rows)
        return await self._run(operation)

    async def interrupt_one(self, lease):
        """Fence unknown dispatch/stop outcomes, including unclaimed admission."""
        def operation():
            with self.transaction() as db:
                row = self._row(db, lease.principal, lease.task_id)
                if self._lease(row) != lease:
                    fail(ErrorCode.STALE_GENERATION)
                view = TaskView.model_validate_json(row['view'])
                if view.state not in TERMINAL_STATES:
                    view = self._write(db, row, TaskState.INTERRUPTED, error=task_error(ErrorCode.INTERRUPTED, 'Execution interrupted; website effects may be partial'))
                db.execute('UPDATE bt_tasks SET claimed=0 WHERE id=?', (lease.task_id,))
                self._requests.pop(lease.task_id, None)
                return view
        return await self._run(operation)

    async def create_profile(self, principal, request):
        def operation():
            with self.transaction() as db:
                prior = db.execute('SELECT * FROM bt_idempotency WHERE owner=? AND operation=? AND key=?', (principal.account_id, 'profile', request.idempotency_key)).fetchone()
                if prior:
                    if prior['fingerprint'] != request.label:
                        fail(ErrorCode.CONFLICT)
                    profile = ProfileView.model_validate_json(prior['receipt'])
                    self._profile(db, principal, profile.profile_id)
                    return profile
                profile = ProfileView(profile_id=identifier('profile'), label=request.label, state='available', created_at=now())
                db.execute('INSERT INTO bt_profiles(id,owner,label,created) VALUES(?,?,?,?)', (profile.profile_id, principal.account_id, profile.label, profile.created_at.isoformat()))
                db.execute('INSERT INTO bt_idempotency VALUES(?,?,?,?,?)', (principal.account_id, 'profile', request.idempotency_key, request.label, profile.model_dump_json()))
                return profile
        return await self._run(operation)

    async def list_profiles(self, principal, cursor=None):
        def operation():
            with self.transaction() as db:
                rows = db.execute('SELECT *,EXISTS(SELECT 1 FROM bt_tasks t WHERE t.profile=p.id AND reserved=1) AS leased FROM bt_profiles p WHERE owner=? AND id>? ORDER BY id LIMIT 101', (principal.account_id, cursor or '')).fetchall()
                items = tuple(ProfileView(profile_id=r['id'], label=r['label'], created_at=r['created'], state='recovery_required' if r['recovery'] or r['deleting'] else 'leased' if r['leased'] else 'available') for r in rows[:100])
                return ProfileList(items=items, next_cursor=items[-1].profile_id if len(rows) > 100 else None)
        return await self._run(operation)

    async def begin_delete(self, principal, profile_id):
        def operation():
            with self.transaction() as db:
                self._profile(db, principal, profile_id)
                if db.execute('SELECT 1 FROM bt_tasks WHERE profile=? AND reserved=1', (profile_id,)).fetchone():
                    fail(ErrorCode.CONFLICT, 'Profile is leased')
                db.execute('UPDATE bt_profiles SET deleting=1 WHERE id=?', (profile_id,))
        return await self._run(operation)

    async def delete_profile(self, principal, profile_id):
        def operation():
            with self.transaction() as db:
                profile = self._profile(db, principal, profile_id)
                if not profile['deleting'] or db.execute('SELECT 1 FROM bt_tasks WHERE profile=? AND reserved=1', (profile_id,)).fetchone():
                    fail(ErrorCode.CONFLICT)
                # Runtime has removed archives/scripts/context before we erase
                # durable interaction/result history. Idempotency tombstones
                # remain so retrying a deleted task cannot execute it again.
                db.execute('DELETE FROM bt_capabilities WHERE task IN (SELECT id FROM bt_tasks WHERE profile=?)', (profile_id,))
                db.execute('DELETE FROM bt_interactions WHERE task IN (SELECT id FROM bt_tasks WHERE profile=?)', (profile_id,))
                db.execute('DELETE FROM bt_tasks WHERE profile=?', (profile_id,))
                db.execute('DELETE FROM bt_sessions WHERE profile=?', (profile_id,))
                db.execute('DELETE FROM bt_profiles WHERE id=?', (profile_id,))
        return await self._run(operation)

    def _active_binding(self, db, binding, timestamp):
        row = self._fenced(db, binding.lease)
        view = TaskView.model_validate_json(row['view'])
        if view.state != TaskState.WAITING_FOR_BROWSER or not view.interaction or (view.interaction.interaction_id, view.interaction.generation) != (binding.interaction_id, binding.generation) or min(view.interaction.expires_at, binding.expires_at) <= timestamp:
            fail(ErrorCode.ASSISTANCE_EXPIRED, 'Assistance is no longer available')

    async def save_capability(self, binding):
        def operation():
            with self.transaction() as db:
                self._active_binding(db, binding, now())
                db.execute('DELETE FROM bt_capabilities WHERE task=?', (binding.lease.task_id,))
                db.execute('INSERT INTO bt_capabilities VALUES(?,?,?)', (binding.digest, binding.lease.task_id, binding.model_dump_json()))
        return await self._run(operation)

    async def capability(self, digest, now):
        def operation():
            with self.transaction() as db:
                row = db.execute('SELECT binding FROM bt_capabilities WHERE digest=?', (digest,)).fetchone()
                if not row:
                    fail(ErrorCode.ASSISTANCE_EXPIRED, 'Assistance is no longer available')
                binding = AssistanceBinding.model_validate_json(row['binding'])
                self._active_binding(db, binding, now)
                return binding
        return await self._run(operation)

    async def revoke_capabilities(self, lease):
        def operation():
            with self.transaction() as db:
                row = self._row(db, lease.principal, lease.task_id)
                if self._lease(row) != lease:
                    fail(ErrorCode.STALE_GENERATION)
                db.execute('DELETE FROM bt_capabilities WHERE task=?', (lease.task_id,))
        return await self._run(operation)
