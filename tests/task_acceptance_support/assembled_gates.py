"""Remaining proof gates, executed inside the owned backend container.

Each gate drives the actual composed Control/Repository/Executor/Assistance/
Observation services, real Docker browsers and HTTP/WSS. Instrumentation is
limited to harness-owned seams: the transparent ControlAudit (lease recorder +
one armed save_capability failpoint), the existing low-level input gate and
in-process driver barriers. No public failpoint endpoint and no strict peers.
Evidence carries counts, states, codes and hashes only; never secret values.
"""

import asyncio
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import ssl
import time
import traceback
import uuid

from .assistance import current_pause, expired, metadata, mint
from .client import Client
from .release_fence import InputOperationGate
from .security import canary_variants, scan_artifacts, scan_blobs
from .system import onboard
from .wire import PREFIX, error as wire_error, no_store, ordinary_safe, view

TLS_HOST = 'localhost:18443'
TLS_BASE = 'https://' + TLS_HOST


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def code(reply):
    wire_error(reply.status, reply.body, reply.headers)
    return reply.body['error']['code']


@dataclass
class Ctx:
    evidence: object
    base: str
    run: str
    site: object
    holder: dict
    owner: Client
    dashboard: Client
    foreign: Client
    foreign_dashboard: Client
    owned_profiles: list
    foreign_profiles: list
    secrets: dict
    tokens: list = field(default_factory=list)
    browser_logs: list = field(default_factory=list)
    task_ids: list = field(default_factory=list)
    caddy_logs: bytes = b''
    seed: dict = field(default_factory=dict)
    views: list = field(default_factory=list)

    def snapshot_views(self):
        """Task results/history as an HTTP client sees them, for the final scan."""
        for account, task_id in self.task_ids:
            self.views.append(('task:' + task_id, json.dumps(account.get(task_id)).encode()))
        return len(self.views)

    def program(self, scenario, key, **extra):
        from .assembled_run import program
        return program(None, self.site, scenario, key, **extra)

    def task(self, scenario, profile, account=None, inputs=None, **extra):
        account = account or self.owner
        request = {**self.program(scenario, self.run + ':' + uuid.uuid4().hex, **extra), 'profile_id': profile}
        if inputs:
            request['inputs'] = inputs
        receipt = account.start(request)
        self.task_ids.append((account, receipt['task_id']))
        return receipt

    def new_profile(self, label, account=None):
        account = account or self.owner
        reply = account.request('POST', PREFIX + '/profiles', {'label': label, 'idempotency_key': self.run + ':' + label})
        assert reply.status == 201
        return reply.body['profile_id']

    def on_server(self, coroutine, timeout=30):
        """Run a coroutine on the actual server event loop (same process)."""
        return asyncio.run_coroutine_threadsafe(coroutine, self.holder['loop']).result(timeout)

    def capture_browser_logs(self, label):
        """Read live task-browser container logs while the secret-bearing page is open."""
        import docker
        client = docker.from_env()
        containers = client.containers.list(filters={'network': os.environ['SANDBOX_NETWORK'], 'label': 'flowpilot-task=1'})
        for container in containers:
            self.browser_logs.append((label + ':' + container.id[:12], container.logs(stdout=True, stderr=True)))
        return len(containers)

    def counters(self):
        return self.site.counters()


def check(ctx, name, action):
    """evidence.check plus sanitized traceback locations for failure triage."""
    try:
        safe = action() or {}
    except Exception as exc:
        from .assembled_run import MODEL_EVENTS
        frames = [{'path': frame.filename, 'line': frame.lineno} for frame in traceback.extract_tb(exc.__traceback__)]
        traceback.print_exc()  # Private container stderr only; host never retains it.
        ctx.evidence.record(name, 'failed', 'actual assembled assertion failed; rerun exact launcher',
                            exception_type=type(exc).__name__, traceback_frames=frames[-6:], model_events=MODEL_EVENTS[-12:])
        return False
    ctx.evidence.record(name, 'passed', 'assertions verified against actual services', **safe)
    return True


def settle_requirement(evidence, requirement, names):
    outcomes = {check['name']: check['outcome'] for check in evidence.checks}
    if any(outcomes.get(name) == 'failed' for name in names):
        evidence.requirements[requirement]['outcome'] = 'failed'
    elif all(outcomes.get(name) == 'passed' for name in names):
        evidence.requirements[requirement]['outcome'] = 'passed'
    evidence.requirements[requirement]['proof_checks'] = list(names)


def wait_counter(site, action, expected, timeout=60):
    deadline = time.monotonic() + timeout
    while site.counters().get(action, 0) < expected:
        assert time.monotonic() < deadline, 'expected browser-side effect was not observed'
        time.sleep(.05)


def stable(account, task_id, settled, seconds=1.5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        current = account.get(task_id)
        view(current, settled)
        assert current == settled, 'terminal task mutated'
        time.sleep(.1)


def released(account, profile_id, timeout=45):
    """Wait until the real executor releases a profile lease; return its state."""
    deadline = time.monotonic() + timeout
    while True:
        state = next(p for p in account.request('GET', PREFIX + '/profiles').body['items'] if p['profile_id'] == profile_id)['state']
        if state != 'leased':
            return state
        assert time.monotonic() < deadline, 'executor never released the profile lease'
        time.sleep(.1)


def capability_rows(task_id):
    database = Path(os.environ['PROFILES_DIR']) / 'browser-tasks' / 'tasks.sqlite3'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        return db.execute('SELECT count(*) FROM bt_capabilities WHERE task=?', (task_id,)).fetchone()[0]


def token_of(grant):
    return grant['assistance_url'].rsplit('/', 1)[1]


# --------------------------------------------------------------------------
# questions_secrets: adversarial receipt matrix on the main server
# --------------------------------------------------------------------------

def question_matrix(ctx):
    from .assembled_run import QUESTION_DELIVERIES, hold
    owner, run = ctx.owner, ctx.run
    otp, mismatch = ctx.secrets['otp_main'], ctx.secrets['otp_mismatch']
    ctx.site.otp_digests.add(sha(otp))
    profile = ctx.new_profile('question-matrix')
    reached, release = hold('otp')
    before = ctx.counters()
    delivered_before = len(QUESTION_DELIVERIES)
    receipt = ctx.task('question', profile, use_otp=True, barrier='otp')
    waiting = owner.wait(receipt['task_id'], {'waiting_for_input'}, timeout=120)
    interaction = waiting['interaction']
    assert [item['kind'] for item in interaction['fields']] == ['secret']
    path = receipt['status_url'] + '/interactions/' + interaction['interaction_id'] + '/respond'
    g = interaction['generation']
    good = {'kind': 'input', 'generation': g, 'values': {'otp': otp}, 'idempotency_key': run + ':answer'}
    codes = {}
    codes['foreign_owner'] = code(ctx.foreign.request('POST', path, good))
    codes['wrong_generation'] = code(owner.request('POST', path, {**good, 'generation': g + 1, 'idempotency_key': run + ':answer-gen'}))
    codes['wrong_kind'] = code(owner.request('POST', path, {'kind': 'confirmation', 'generation': g, 'confirmed': True,
                                                            'idempotency_key': run + ':answer-kind'}))
    codes['missing_field'] = code(owner.request('POST', path, {**good, 'values': {}, 'idempotency_key': run + ':answer-missing'}))
    codes['extra_field'] = code(owner.request('POST', path, {**good, 'values': {'otp': otp, 'extra': 'x'},
                                                             'idempotency_key': run + ':answer-extra'}))
    assert codes == {'foreign_owner': 'not_found', 'wrong_generation': 'stale_generation', 'wrong_kind': 'stale_generation',
                     'missing_field': 'invalid_input', 'extra_field': 'invalid_input'}, 'adversarial response accepted'
    pending = owner.get(receipt['task_id'])
    assert pending['state'] == 'waiting_for_input' and pending['interaction'] == interaction
    assert len(QUESTION_DELIVERIES) == delivered_before, 'rejected response reached the executor'
    first = owner.request('POST', path, good)
    assert first.status == 202
    no_store(first.headers)
    ordinary_safe(first.body)
    duplicate = owner.request('POST', path, good)
    assert duplicate.status == 202 and duplicate.body == first.body, 'duplicate receipt differs'
    codes['same_key_mismatched_value'] = code(owner.request('POST', path, {**good, 'values': {'otp': mismatch}}))
    codes['new_key_after_decision'] = code(owner.request('POST', path, {**good, 'idempotency_key': run + ':answer-other'}))
    assert codes['same_key_mismatched_value'] == 'conflict' and codes['new_key_after_decision'] == 'conflict'
    assert reached.wait(90), 'driver did not use the transient OTP'
    wait_counter(ctx.site, 'otp', before.get('otp', 0) + 1)
    browser_logs = ctx.capture_browser_logs('question')
    release.set()
    done = owner.wait(receipt['task_id'], timeout=120)
    assert done['state'] == 'completed'
    replay = owner.request('POST', path, good)
    assert replay.status == 202 and replay.body == first.body, 'post-completion replay differs'
    after = ctx.counters()
    assert after.get('otp', 0) == before.get('otp', 0) + 1 and after.get('otp_mismatch', 0) == before.get('otp_mismatch', 0)
    assert QUESTION_DELIVERIES[delivered_before:] == [sha(otp)], 'secret delivered other than exactly once to current driver'
    results = scan_blobs([('task-view', json.dumps(done).encode()), ('receipt', json.dumps(first.body).encode())], [otp, mismatch])
    assert not results['findings'], 'transient value in task result or receipt'
    return {'rejections': codes, 'deliveries_to_current_driver': 1, 'website_otp_uses': 1,
            'duplicate_receipt_identical': True, 'post_completion_replay_identical': True,
            'browser_log_containers_captured': browser_logs, 'result_and_receipt_scan_findings': 0}


@contextmanager
def owned_child(port, env=None):
    """Owned spawn child running the same composition; never a global process kill."""
    from .assembled_run import serve, wait_ready
    context = multiprocessing.get_context('spawn')
    state = {'process': context.Process(target=serve, args=(port, None, None, env))}
    state['process'].start()
    try:
        wait_ready('http://127.0.0.1:%d' % port, state['process'])
        yield state
    finally:
        process = state['process']
        if process.is_alive():
            process.terminate()
            process.join(timeout=40)
            if process.is_alive():
                process.kill()
                process.join(timeout=10)


@contextmanager
def dump_on_failure(state):
    """On failure, ask the owned child to dump thread stack locations (stderr only)."""
    try:
        yield
    except BaseException:
        import signal
        if state['process'].is_alive():
            os.kill(state['process'].pid, signal.SIGUSR1)
            time.sleep(2)
        raise


def restart(state, port):
    from .assembled_run import serve, wait_ready
    state['process'].kill()  # This exact owned child only.
    state['process'].join(timeout=10)
    assert state['process'].exitcode is not None and state['process'].exitcode < 0
    state['process'] = multiprocessing.get_context('spawn').Process(target=serve, args=(port,))
    state['process'].start()
    wait_ready('http://127.0.0.1:%d' % port, state['process'])


def question_restart(ctx):
    """SIGKILL the owned backend while a secret answer is pending, and while the
    executor holds an accepted secret in memory. Neither may persist or replay."""
    port = 18081
    base = 'http://127.0.0.1:%d' % port
    observed = {}
    with owned_child(port) as state:
        for mode in ('pending', 'held'):
            account = Client(base, ctx.foreign.token)
            secret = ctx.secrets['otp_' + mode]
            ctx.site.otp_digests.add(sha(secret))
            profile = ctx.new_profile('question-' + mode, account)
            before = ctx.counters()
            request = {**ctx.program('question', ctx.run + ':restart-' + mode, use_otp=True, hold=mode == 'held'), 'profile_id': profile}
            receipt = account.start(request)
            waiting = account.wait(receipt['task_id'], {'waiting_for_input'}, timeout=120)
            interaction = waiting['interaction']
            path = receipt['status_url'] + '/interactions/' + interaction['interaction_id'] + '/respond'
            answer = {'kind': 'input', 'generation': interaction['generation'], 'values': {'otp': secret},
                      'idempotency_key': ctx.run + ':restart-answer-' + mode}
            if mode == 'held':
                accepted = account.request('POST', path, answer)
                assert accepted.status == 202
                wait_counter(ctx.site, 'otp_held', before.get('otp_held', 0) + 1)
            restart(state, port)
            fresh = Client(base, ctx.foreign.token)
            recovered = fresh.get(receipt['task_id'])
            assert recovered['state'] == 'interrupted' and recovered['error']['code'] == 'interrupted'
            assert recovered.get('interaction') is None
            outcome = {'recovered_state': recovered['state']}
            if mode == 'pending':
                outcome['late_answer'] = code(fresh.request('POST', path, answer))
                assert outcome['late_answer'] == 'stale_generation'
            else:
                replay = fresh.request('POST', path, answer)
                outcome['same_key_replay_status'] = replay.status
                assert replay.status in {202, 409}
                outcome['different_value_same_key'] = code(fresh.request('POST', path, {**answer, 'values': {'otp': ctx.secrets['otp_mismatch']}}))
                outcome['new_key'] = code(fresh.request('POST', path, {**answer, 'idempotency_key': ctx.run + ':restart-new-' + mode}))
                assert outcome['different_value_same_key'] == 'conflict' and outcome['new_key'] in {'conflict', 'stale_generation'}
            # Admission retry returns the original receipt and never re-executes.
            assert fresh.start(request) == receipt
            time.sleep(1)
            after = ctx.counters()
            assert after.get('otp', 0) == before.get('otp', 0), 'secret reached website after crash'
            assert after.get('otp_mismatch', 0) == before.get('otp_mismatch', 0)
            view(fresh.get(receipt['task_id']), recovered)
            listing = fresh.request('GET', PREFIX + '/profiles')
            assert next(p for p in listing.body['items'] if p['profile_id'] == profile)['state'] == 'recovery_required'
            assert fresh.request('DELETE', PREFIX + '/profiles/' + profile).status == 204
            observed[mode] = outcome
    return {'backend_signal': 'SIGKILL', 'cases': observed, 'website_secret_uses_after_crash': 0, 'automatic_replay': False}


def expiry_server(ctx):
    """Separately owned composition with a short human wait (own state dir)."""
    port = 18082
    base = 'http://127.0.0.1:%d' % port
    env = {'PROFILES_DIR': '/state/expiry', 'TASK_ACCEPTANCE_HUMAN_WAIT': '6', 'TASK_ACCEPTANCE_ASSISTANCE_TTL': '3'}
    observed = {}
    with owned_child(port, env):
        account, _, _ = onboard(base, ctx.run + '-expiry', password=ctx.secrets['pw_expiry'])
        profile = ctx.new_profile('expiry', account)
        secret = ctx.secrets['otp_expired']
        ctx.site.otp_digests.add(sha(secret))
        for scenario, state, response in [
                ('question', 'waiting_for_input', {'kind': 'input', 'values': {'otp': secret}}),
                ('approve', 'waiting_for_confirmation', {'kind': 'confirmation', 'confirmed': True})]:
            before = ctx.counters()
            request = {**ctx.program(scenario, ctx.run + ':expiry-' + scenario, use_otp=True), 'profile_id': profile}
            receipt = account.start(request)
            waiting = account.wait(receipt['task_id'], {state}, timeout=120)
            interaction = waiting['interaction']
            expires = datetime.fromisoformat(interaction['expires_at'].replace('Z', '+00:00'))
            remaining = (expires - datetime.now(timezone.utc)).total_seconds()
            assert 0 < remaining <= 7, 'expiry server did not apply the short human wait'
            time.sleep(remaining + .5)
            settled = account.wait(receipt['task_id'], timeout=60)
            path = receipt['status_url'] + '/interactions/' + interaction['interaction_id'] + '/respond'
            late = account.request('POST', path, {**response, 'generation': interaction['generation'],
                                                  'idempotency_key': ctx.run + ':expiry-late-' + scenario})
            late_code = code(late)
            assert late_code in {'stale_generation', 'conflict'}
            stable(account, receipt['task_id'], settled)
            time.sleep(1)
            after = ctx.counters()
            assert settled['state'] != 'completed'
            assert after.get('otp', 0) == before.get('otp', 0) and after.get('submit', 0) == before.get('submit', 0)
            observed[scenario] = {'terminal_state': settled['state'], 'error_code': (settled.get('error') or {}).get('code'),
                                  'late_response': late_code}
            observed[scenario]['profile_state_after'] = released(account, profile)
            if observed[scenario]['profile_state_after'] == 'recovery_required':
                assert account.request('DELETE', PREFIX + '/profiles/' + profile).status == 204
                profile = ctx.new_profile('expiry-' + scenario, account)
    return {'human_wait_seconds': 6, 'cases': observed, 'secret_delivered': False, 'unapproved_submit_effects': 0}


# --------------------------------------------------------------------------
# confirmation_decline: approve/decline/duplicate/wrong-owner/stale matrix
# --------------------------------------------------------------------------

def confirmation_approve(ctx):
    owner, run = ctx.owner, ctx.run
    profile = ctx.new_profile('approve')
    before = ctx.counters()
    receipt = ctx.task('approve', profile)
    waiting = owner.wait(receipt['task_id'], {'waiting_for_confirmation'}, timeout=120)
    interaction = waiting['interaction']
    assert interaction['action_id']
    g = interaction['generation']
    path = receipt['status_url'] + '/interactions/' + interaction['interaction_id'] + '/respond'
    ok = {'kind': 'confirmation', 'generation': g, 'confirmed': True, 'idempotency_key': run + ':approve'}
    codes = {'foreign_approve': code(ctx.foreign.request('POST', path, ok)),
             'foreign_decline': code(ctx.foreign.request('POST', path, {**ok, 'confirmed': False})),
             'wrong_generation': code(owner.request('POST', path, {**ok, 'generation': g + 1, 'idempotency_key': run + ':approve-gen'})),
             'wrong_kind': code(owner.request('POST', path, {'kind': 'input', 'generation': g, 'values': {'x': 'y'},
                                                            'idempotency_key': run + ':approve-kind'})),
             'key_bound_to_other_interaction': code(owner.request('POST', path, {**ok, 'idempotency_key': run + ':answer'}))}
    assert codes == {'foreign_approve': 'not_found', 'foreign_decline': 'not_found', 'wrong_generation': 'stale_generation',
                     'wrong_kind': 'stale_generation', 'key_bound_to_other_interaction': 'conflict'}
    pending = owner.get(receipt['task_id'])
    assert pending['state'] == 'waiting_for_confirmation' and pending['interaction'] == interaction
    assert ctx.counters().get('submit', 0) == before.get('submit', 0)
    first = owner.request('POST', path, ok)
    assert first.status == 202
    duplicate = owner.request('POST', path, ok)
    assert duplicate.status == 202 and duplicate.body == first.body
    codes['decline_after_approval'] = code(owner.request('POST', path, {**ok, 'confirmed': False, 'idempotency_key': run + ':approve-decline'}))
    assert codes['decline_after_approval'] == 'conflict'
    done = owner.wait(receipt['task_id'], timeout=120)
    assert done['state'] == 'completed'
    assert ctx.counters().get('submit', 0) == before.get('submit', 0) + 1, 'approved action not exactly once'
    replay = owner.request('POST', path, ok)
    assert replay.status == 202 and replay.body == first.body
    return {'rejections': codes, 'approved_submit_effects': 1, 'duplicate_receipt_identical': True}


def confirmation_decline(ctx):
    owner, run = ctx.owner, ctx.run
    before = ctx.counters()
    receipt = ctx.task('decline', ctx.owned_profiles[1])
    waiting = owner.wait(receipt['task_id'], {'waiting_for_confirmation'}, timeout=120)
    interaction = waiting['interaction']
    path = receipt['status_url'] + '/interactions/' + interaction['interaction_id'] + '/respond'
    response = {'kind': 'confirmation', 'generation': interaction['generation'], 'confirmed': False, 'idempotency_key': run + ':decline'}
    assert code(ctx.foreign.request('POST', path, response)) == 'not_found'
    first = owner.request('POST', path, response)
    assert first.status == 202
    approve_after = code(owner.request('POST', path, {**response, 'confirmed': True, 'idempotency_key': run + ':decline-approve'}))
    assert approve_after == 'conflict'
    outcome = owner.wait(receipt['task_id'], timeout=120)
    assert outcome['state'] == 'completed' and outcome['result']['answer'].startswith('Not booked: you declined')
    stable(owner, receipt['task_id'], outcome)
    assert ctx.counters().get('submit', 0) == before.get('submit', 0)
    return {'terminal_state': 'completed_declined', 'approve_after_decline': approve_after, 'submit_effects': 0}


# --------------------------------------------------------------------------
# assistance_atomic_commit: deterministic lookup -> save failpoint
# --------------------------------------------------------------------------

def atomic_commit(ctx):
    owner, run = ctx.owner, ctx.run
    audit = ctx.holder['control']
    cases = {}
    for case in ('control', 'resolve', 'cancel'):
        profile = ctx.new_profile('atomic-' + case)
        before = ctx.counters()
        receipt = ctx.task('pause' if case == 'cancel' else 'drain', profile)
        task_id = receipt['task_id']
        interaction = owner.wait(task_id, {'waiting_for_browser'}, timeout=120)['interaction']
        request = {'interaction_id': interaction['interaction_id'], 'generation': interaction['generation']}
        point = audit.arm(task_id)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(Client(ctx.base, owner.token).request, 'POST', receipt['status_url'] + '/assistance', request)
                assert point.entered.wait(15), 'mint never reached save_capability'
                order = audit.order(task_id)
                assert order[-1] == 'save_capability' and 'assistance_lease' in order[:-1]
                assert order.index('assistance_lease') < len(order) - 1
                time.sleep(.2)
                assert not pending.done(), 'failpoint did not hold the commit'
                path = receipt['status_url'] + '/interactions/' + interaction['interaction_id'] + '/respond'
                if case == 'resolve':
                    resolved = owner.request('POST', path, {'kind': 'browser', 'generation': interaction['generation'],
                                                            'browser_resolved': True, 'idempotency_key': run + ':atomic-resolve'})
                    assert resolved.status == 202
                elif case == 'cancel':
                    assert owner.request('POST', receipt['status_url'] + '/cancel').status == 202
                    assert owner.wait(task_id, timeout=60)['state'] == 'cancelled'
                point.permit.set()
                reply = pending.result(30)
        finally:
            audit.disarm()
        rows = capability_rows(task_id)
        if case == 'control':
            assert reply.status == 201 and rows == 1
            ctx.tokens.append(token_of(reply.body))
            metadata(owner, reply.body, interaction, ctx.site.browser_url)
            resolved = owner.request('POST', receipt['status_url'] + '/interactions/' + interaction['interaction_id'] + '/respond',
                                     {'kind': 'browser', 'generation': interaction['generation'], 'browser_resolved': True,
                                      'idempotency_key': run + ':atomic-control'})
            assert resolved.status == 202
            expired(owner, reply.body)
            outcome = {'mint_status': 201, 'capability_rows': rows}
        else:
            rejection = code(reply)
            assert rejection in {'assistance_expired', 'stale_generation'}, 'stale binding committed'
            assert rows == 0, 'stale capability row persisted'
            outcome = {'mint_status': reply.status, 'mint_error': rejection, 'capability_rows': rows}
        if case != 'cancel':
            done = owner.wait(task_id, timeout=120)
            assert done['state'] == 'completed'
            assert ctx.counters().get('agent', 0) == before.get('agent', 0) + 1
        outcome['call_order'] = audit.order(task_id)[-4:]
        cases[case] = outcome
    return {'failpoint': 'ControlAudit.save_capability after assistance_lease lookup (harness-owned, not HTTP)', 'cases': cases}


# --------------------------------------------------------------------------
# cancel_fencing: long JS, held human writer, stale executor callbacks
# --------------------------------------------------------------------------

def stale_callbacks(ctx, task_id, account):
    """Replay genuine (now stale) execution leases into the REAL Control and
    Executor on the server loop. Every callback must fail closed; the terminal
    view, profile state and website counters must not change."""
    from backend.task_contracts import (
        AssistanceRequest, Interaction, InteractionKind, TaskFault, TaskResult, TaskState,
    )
    from backend.task_ports import AssistanceBinding
    control = ctx.holder['control']
    executor = ctx.holder['audit']
    lease = control.leases[task_id]
    # Wait for the REAL executor to release the reservation first, so the
    # replayed release below can only be an idempotent no-op, never the release.
    released(account, lease.profile_id)
    before = account.get(task_id)
    profiles_before = account.request('GET', PREFIX + '/profiles').body
    counters = ctx.counters()
    later = datetime.now(timezone.utc) + timedelta(seconds=60)
    revision = before['revision']
    older = lease.model_copy(update={'generation': lease.generation - 1}) if lease.generation > 1 else None
    calls = {
        'progress': lambda: control.progress(lease, revision, 'late stale progress'),
        'finish_completed': lambda: control.finish(lease, revision, TaskState.COMPLETED, result=TaskResult(answer='late stale completion')),
        'wait_for_interaction': lambda: asyncio.wait_for(control.wait_for_interaction(lease, Interaction(
            interaction_id=uuid.uuid4().hex, generation=99, kind=InteractionKind.BROWSER, message='late', expires_at=later)), 10),
        'require_approval': lambda: control.require_approval(lease, uuid.uuid4().hex, 1, uuid.uuid4().hex),
        'assistance_lease': lambda: control.assistance_lease(lease.principal, task_id, AssistanceRequest(interaction_id=uuid.uuid4().hex, generation=1)),
        'save_capability': lambda: control.save_capability(AssistanceBinding(lease=lease, interaction_id=uuid.uuid4().hex, generation=1,
                                                                             digest='0' * 64, expires_at=later)),
        'executor_browser': lambda: executor.browser(lease, uuid.uuid4().hex, 1),
        'executor_release_human': lambda: executor.release_human(lease, uuid.uuid4().hex, 1),
    }
    if older is not None:
        calls['older_generation_progress'] = lambda: control.progress(older, revision, 'older lease')
    results = {}
    for name, factory in calls.items():
        try:
            ctx.on_server(factory())
        except TaskFault as fault:
            results[name] = str(fault.error.code)
        else:
            results[name] = 'accepted'
    assert all(value != 'accepted' for value in results.values()), 'stale executor callback accepted'
    # Idempotent release of an already released reservation must not clear quarantine.
    ctx.on_server(control.release_browser(lease, profile_saved=True))
    after = account.get(task_id)
    assert after == before, 'stale callback mutated a terminal task'
    assert account.request('GET', PREFIX + '/profiles').body == profiles_before
    assert ctx.counters() == counters
    return results


def held_writer_cancel(ctx, receipt, interaction, grant, gate):
    """Cancel while a REAL human input operation holds the runtime writer lock."""
    from websockets.asyncio.client import connect
    from backend.task_contracts import TaskFault, TaskResult, TaskState
    owner = ctx.owner
    control = ctx.holder['control']
    capability = grant['assistance_url']
    observations = {}

    async def scenario():
        async with connect(ctx.base.replace('http://', 'ws://') + capability + '/stream', origin=ctx.base, proxy=None,
                           open_timeout=15, close_timeout=1, max_size=2 * 1024 * 1024) as stream:
            assert (await asyncio.wait_for(stream.recv(), 15)).startswith(b'\xff\xd8')
            await asyncio.wait_for(stream.recv(), 15)
            held = asyncio.create_task(asyncio.to_thread(Client(ctx.base).request, 'POST', capability + '/input',
                                                         {'kind': 'move', 'x': 3, 'y': 3}, {'Origin': ctx.base}))
            assert await asyncio.to_thread(gate.entered.wait, 15), 'human input was not held under the writer lock'
            cancelled = await asyncio.to_thread(owner.request, 'POST', receipt['status_url'] + '/cancel')
            assert cancelled.status == 202
            current = await asyncio.to_thread(owner.get, receipt['task_id'])
            observations['state_while_writer_held'] = current['state']
            lease = control.leases[receipt['task_id']]
            late = {}
            for name, factory in {'finish_completed': lambda: control.finish(lease, current['revision'], TaskState.COMPLETED,
                                                                              result=TaskResult(answer='late during cancel')),
                                  'progress': lambda: control.progress(lease, current['revision'], 'late during cancel')}.items():
                try:
                    await asyncio.to_thread(ctx.on_server, factory())
                except TaskFault as fault:
                    late[name] = str(fault.error.code)
                else:
                    late[name] = 'accepted'
            assert all(value != 'accepted' for value in late.values()), 'late callback settled a cancelling task'
            observations['late_callbacks_while_cancelling'] = late
            settled = await asyncio.to_thread(owner.wait, receipt['task_id'], {'cancelled', 'interrupted', 'failed', 'completed'}, 30)
            assert settled['state'] in {'cancelled', 'interrupted'}, 'held-writer cancel settled as success'
            assert not gate.finished.is_set(), 'held operation finished before release'
            observations['terminal_state_with_writer_held'] = settled['state']
            # The viewer route serializes capability calls; probe only after release.
            gate.permit.set()
            reply = await asyncio.wait_for(held, 30)
            observations['held_input_status_after_release'] = reply.status
            for method, suffix, body in [('GET', '/state', None), ('POST', '/input', {'kind': 'release'})]:
                late = await asyncio.to_thread(Client(ctx.base).request, method, capability + suffix, body,
                                               {'Origin': ctx.base} if method == 'POST' else None)
                assert code(late) == 'assistance_expired', 'late human access after cancellation'
            return settled

    settled = asyncio.run(scenario())
    stable(owner, receipt['task_id'], owner.get(receipt['task_id']))
    observations['terminal_immutable_after_release'] = True
    return settled, observations


def cancel_fencing(ctx):
    import docker
    from .assembled_run import GATES
    owner = ctx.owner
    client = docker.from_env()

    def task_containers(task_id):
        return client.containers.list(all=True, filters={'label': ['flowpilot-task=1', 'task-id=' + task_id]})

    observed = {}
    # A bystander pause on another profile must survive the other task's cancel/recovery.
    bystander_profile = ctx.new_profile('bystander')
    bystander = ctx.task('pause', bystander_profile)
    bystander_pause = owner.wait(bystander['task_id'], {'waiting_for_browser'}, timeout=120)

    # 1. Cancellation during long-running page JavaScript.
    profile = ctx.new_profile('longjs')
    before = ctx.counters()
    receipt = ctx.task('longjs', profile, busy_ms=10000)
    wait_counter(ctx.site, 'longjs', before.get('longjs', 0) + 1, timeout=120)
    js_started = time.monotonic()
    assert owner.request('POST', receipt['status_url'] + '/cancel').status == 202
    settled = owner.wait(receipt['task_id'], timeout=60)
    elapsed = round(time.monotonic() - js_started, 2)
    assert settled['state'] in {'cancelled', 'interrupted'}, 'long-running JS cancel reported success'
    running = [c.id for c in task_containers(receipt['task_id']) if c.status == 'running']
    assert not running, 'browser still running after cancellation settled'
    time.sleep(max(0, 10 - (time.monotonic() - js_started)) + 3)
    assert ctx.counters().get('late', 0) == before.get('late', 0), 'page JavaScript continued after cancel'
    stable(owner, receipt['task_id'], settled)
    observed['long_running_js'] = {'terminal_state': settled['state'], 'error_code': (settled.get('error') or {}).get('code'),
                                   'seconds_from_js_start_to_terminal': elapsed, 'browser_running_after_terminal': False,
                                   'late_page_effects': 0}
    observed['long_running_js']['stale_callbacks'] = stale_callbacks(ctx, receipt['task_id'], owner)

    # Bystander still paused, browser alive and its capability usable.
    current = owner.get(bystander['task_id'])
    assert current['state'] == 'waiting_for_browser' and current['interaction'] == bystander_pause['interaction'], 'bystander task disturbed'
    assert [c for c in task_containers(bystander['task_id']) if c.status == 'running'], 'bystander browser stopped'
    grant = mint(owner, bystander, bystander_pause['interaction'])
    ctx.tokens.append(token_of(grant))
    metadata(owner, grant, bystander_pause['interaction'], ctx.site.browser_url)
    observed['bystander_unaffected'] = True

    # 2. Cancellation during a held human writer operation.
    profile = ctx.new_profile('held-writer')
    before = ctx.counters()
    receipt = ctx.task('drain', profile)
    interaction = owner.wait(receipt['task_id'], {'waiting_for_browser'}, timeout=120)['interaction']
    held_grant = mint(owner, receipt, interaction)
    ctx.tokens.append(token_of(held_grant))
    gate = InputOperationGate(ctx.run, 'inside_executor_input_registry')
    GATES['current'] = gate
    try:
        settled, held = held_writer_cancel(ctx, receipt, interaction, held_grant, gate)
    finally:
        gate.permit.set()
        GATES.pop('current', None)
    assert ctx.counters().get('agent', 0) == before.get('agent', 0), 'agent continued after cancelled human pause'
    held['stale_callbacks'] = stale_callbacks(ctx, receipt['task_id'], owner)
    observed['held_human_writer'] = held

    assert owner.request('POST', bystander['status_url'] + '/cancel').status == 202
    assert owner.wait(bystander['task_id'], timeout=60)['state'] == 'cancelled'
    expired(owner, grant)
    return observed


# --------------------------------------------------------------------------
# assistance_tls: repository Caddyfile as a TLS-terminating proxy with WSS
# --------------------------------------------------------------------------

class CaddyProxy:
    """Caddy (lane005 tools image) in the backend network namespace, using the
    unmodified scripts/ops_Caddyfile. Only this run-labelled container is removed."""

    def __init__(self, run):
        self.run = run
        self.container = None
        self.logs = b''

    def __enter__(self):
        import docker
        client = docker.from_env()
        image = client.images.get(os.environ['TASK_ACCEPTANCE_TOOLS_IMAGE'])
        self.image_id = image.id
        Path('/state/caddy-data').mkdir(mode=0o700, exist_ok=True)
        host_root, host_state = os.environ['TASK_ACCEPTANCE_HOST_ROOT'], os.environ['TASK_ACCEPTANCE_HOST_STATE']
        self.container = client.containers.run(
            image.id, ['caddy', 'run', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile'],
            name=self.run + '-caddy', labels={'task-acceptance-run': self.run}, detach=True,
            network_mode='container:' + self.run + '-backend',
            environment={'FLOWPILOT_DOMAIN': TLS_BASE, 'FLOWPILOT_EXTERNAL_HOST': TLS_HOST,
                         'XDG_DATA_HOME': '/data', 'XDG_CONFIG_HOME': '/data/config', 'HOME': '/data'},
            volumes={host_root + '/scripts/ops_Caddyfile': {'bind': '/etc/caddy/Caddyfile', 'mode': 'ro'},
                     host_state + '/caddy-data': {'bind': '/data', 'mode': 'rw'}})
        root = Path('/state/caddy-data/caddy/pki/authorities/local/root.crt')
        deadline = time.monotonic() + 60
        while True:
            self.container.reload()
            assert self.container.status in {'created', 'running'}, 'TLS proxy exited'
            if root.exists():
                try:
                    self.context = ssl.create_default_context(cafile=str(root))
                    if Client(TLS_BASE, timeout=2, ssl_context=self.context).request('GET', '/health/browser-tasks').status == 200:
                        break
                except Exception:
                    pass
            assert time.monotonic() < deadline, 'TLS proxy never became ready'
            time.sleep(.2)
        return self

    def __exit__(self, *exc):
        if self.container is not None:
            try:
                self.container.stop(timeout=5)
                self.logs = self.container.logs(stdout=True, stderr=True)
            finally:
                self.container.remove(force=True)


def tls_assistance(ctx):
    import websockets
    from websockets.asyncio.client import connect
    owner = ctx.owner
    with CaddyProxy(ctx.run) as proxy:
        context = proxy.context
        tls = Client(TLS_BASE, owner.token, ssl_context=context)
        anonymous = Client(TLS_BASE, ssl_context=context)
        # Real certificate chain: an untrusting default client must refuse it.
        untrusted = False
        try:
            Client(TLS_BASE, ssl_context=ssl.create_default_context()).request('GET', '/health/browser-tasks')
        except Exception as exc:
            untrusted = 'CERTIFICATE_VERIFY_FAILED' in repr(exc) or isinstance(getattr(exc, 'reason', None), ssl.SSLError)
        assert untrusted, 'TLS proxy certificate was not verified'
        profile = ctx.new_profile('tls-viewer')
        receipt = tls.start({**ctx.program('viewer', ctx.run + ':tls-viewer', pauses=1), 'profile_id': profile})
        ctx.task_ids.append((owner, receipt['task_id']))
        interaction = current_pause(tls, receipt['task_id'], timeout=120)['interaction']
        grant = mint(tls, receipt, interaction)
        ctx.tokens.append(token_of(grant))
        capability = grant['assistance_url']
        origin = {'Origin': TLS_BASE}
        page = anonymous.request('GET', capability, raw=True)
        assert page.status == 200 and b'<html' in page.body.lower()
        headers = {k.lower(): v for k, v in page.headers.items()}
        assert 'no-store' in headers['cache-control'] and headers['referrer-policy'] == 'no-referrer'
        assert "frame-ancestors 'none'" in headers['content-security-policy'] and 'server' not in headers
        state = anonymous.request('GET', capability + '/state')
        assert state.status == 200 and state.body['interaction'] == interaction
        screen = anonymous.request('GET', capability + '/screen', raw=True)
        assert screen.status == 200 and screen.body.startswith(b'\x89PNG\r\n\x1a\n')
        rejected = {}
        for name, extra in {'http_scheme_origin': {'Origin': 'http://' + TLS_HOST},
                            'foreign_origin': {'Origin': 'https://foreign.invalid'},
                            'missing_origin': {},
                            'cross_site_fetch': {**origin, 'Sec-Fetch-Site': 'cross-site'}}.items():
            rejected[name] = code(anonymous.request('POST', capability + '/input', {'kind': 'release'}, extra))
        assert set(rejected.values()) == {'not_found'}
        # Client-supplied forwarding headers cannot downgrade the proxy-asserted scheme.
        spoofed = anonymous.request('POST', capability + '/input', {'kind': 'release'},
                                    {**origin, 'X-Forwarded-Proto': 'http', 'Forwarded': 'proto=http;host=foreign.invalid'})
        assert spoofed.status == 200
        oversized = anonymous.request('POST', capability + '/input', {'kind': 'type', 'text': 'x' * 140000}, origin, raw=True)
        # Proxy (413) or backend boundary (422) may answer first; never accepted.
        assert oversized.status in {413, 422}, 'oversized capability input accepted'
        commands = [{'kind': 'down', 'x': 100, 'y': 15, 'button': 'left'}, {'kind': 'up', 'x': 100, 'y': 15, 'button': 'left'},
                    {'kind': 'type', 'text': 'synthetic-human'}, {'kind': 'keydown', 'key': 'Shift'}, {'kind': 'keyup', 'key': 'Shift'}]
        for command in commands:
            assert anonymous.request('POST', capability + '/input', command, origin).status == 200

        async def stream_checks(current, wait_expiry):
            url = 'wss://' + TLS_HOST + current['assistance_url'] + '/stream'
            refused = {}
            for name, bad in {'http_scheme_origin': 'http://' + TLS_HOST, 'foreign_origin': 'https://foreign.invalid'}.items():
                try:
                    async with connect(url, origin=bad, ssl=context, proxy=None, open_timeout=10) as socket:
                        await asyncio.wait_for(socket.recv(), 3)
                    refused[name] = False
                except (websockets.exceptions.InvalidStatus, websockets.exceptions.ConnectionClosed,
                        websockets.exceptions.InvalidHandshake, TimeoutError):
                    refused[name] = True
            assert all(refused.values()), 'cross-origin WSS stream accepted'
            frames = 0
            closed_after = None
            async with connect(url, origin=TLS_BASE, ssl=context, proxy=None, open_timeout=15, max_size=2 * 1024 * 1024) as socket:
                for _ in range(2):
                    frame = await asyncio.wait_for(socket.recv(), 15)
                    assert isinstance(frame, bytes) and frame.startswith(b'\xff\xd8')
                    frames += 1
                if wait_expiry:
                    expires = datetime.fromisoformat(current['expires_at'].replace('Z', '+00:00'))
                    try:
                        while True:
                            await asyncio.wait_for(socket.recv(), (expires - datetime.now(timezone.utc)).total_seconds() + 5)
                            frames += 1
                    except websockets.exceptions.ConnectionClosed:
                        closed_after = (datetime.now(timezone.utc) - expires).total_seconds()
                    assert closed_after is not None and -1 <= closed_after <= 3, 'active WSS outlived capability expiry'
            return {'bad_origin_refused': refused, 'frames': frames, 'closed_seconds_after_expiry': closed_after}

        streamed = asyncio.run(stream_checks(grant, False))
        rotated = mint(tls, receipt, interaction)
        ctx.tokens.append(token_of(rotated))
        expired(anonymous, grant)
        old_stream = asyncio.run(_refused_stream(token_url=grant['assistance_url'], context=context))
        assert old_stream, 'rotated capability still streams'
        natural = asyncio.run(stream_checks(rotated, True))
        expired(anonymous, rotated)
        renewed = mint(tls, receipt, interaction)
        ctx.tokens.append(token_of(renewed))
        response = {'kind': 'browser', 'generation': interaction['generation'], 'browser_resolved': True,
                    'idempotency_key': ctx.run + ':tls-viewer'}
        assert anonymous.request('POST', renewed['assistance_url'] + '/respond', response, origin).status == 202
        done = tls.wait(receipt['task_id'], timeout=120)
        assert done['state'] == 'completed'
        expired(anonymous, renewed)
    tokens = [token_of(g) for g in (grant, rotated, renewed)]
    ctx.caddy_logs = proxy.logs
    scan = scan_blobs([('caddy', proxy.logs)], tokens)
    assert not scan['findings'], 'capability path in proxy logs'
    assert b'/browser-tasks/help/' not in proxy.logs
    return {'proxy': 'scripts/ops_Caddyfile unmodified', 'proxy_image_id': proxy.image_id, 'tls_verified': True,
            'viewer_html_png_state': True, 'input_origin_rejections': rejected, 'forwarding_spoof_ignored': True,
            'oversized_body_status': oversized.status, 'wss_initial': streamed, 'rotated_capability_stream_refused': True,
            'wss_natural_expiry': natural, 'dom_effect_via_tls_viewer_completed': True,
            'proxy_log_bytes': len(proxy.logs), 'proxy_log_capability_findings': 0}


async def _refused_stream(token_url, context):
    import websockets
    from websockets.asyncio.client import connect
    try:
        async with connect('wss://' + TLS_HOST + token_url + '/stream', origin=TLS_BASE, ssl=context, proxy=None, open_timeout=10) as socket:
            await asyncio.wait_for(socket.recv(), 3)
        return False
    except (websockets.exceptions.InvalidStatus, websockets.exceptions.ConnectionClosed,
            websockets.exceptions.InvalidHandshake, TimeoutError):
        return True


# --------------------------------------------------------------------------
# compatibility: legacy flow API and UI boundary alongside the task API
# --------------------------------------------------------------------------

def legacy_flow_api(ctx):
    owner, dashboard = ctx.owner, ctx.dashboard
    profile = ctx.new_profile('legacy-alongside')
    paused = ctx.task('pause', profile)
    pause = owner.wait(paused['task_id'], {'waiting_for_browser'}, timeout=120)
    anonymous = Client(ctx.base)
    health = anonymous.request('GET', '/health')
    assert health.status == 200
    assert anonymous.request('POST', '/api/flows', {'name': 'x', 'domain': 'acceptance.invalid', 'task': 'x'}).status == 401
    created = dashboard.request('POST', '/api/flows', {'name': ctx.run + '-flow', 'domain': 'acceptance.invalid',
                                                       'task': 'Synthetic legacy flow; never explored or executed'})
    assert created.status == 200 and created.body['name'] == ctx.run + '-flow'
    flow_id = created.body['id']
    assert any(item['id'] == flow_id for item in dashboard.request('GET', '/api/flows').body)
    assert dashboard.request('GET', '/api/flows/' + flow_id).body['domain'] == 'acceptance.invalid'
    updated = dashboard.request('PUT', '/api/flows/' + flow_id, {'name': ctx.run + '-flow-renamed'})
    assert updated.status == 200 and updated.body['name'] == ctx.run + '-flow-renamed'
    runs = dashboard.request('GET', '/api/runs')
    assert runs.status == 200 and isinstance(runs.body, list)
    assert dashboard.request('GET', '/api/v1/runs/' + uuid.uuid4().hex[:12]).status == 404
    assert owner.request('GET', '/api/v1/runs/' + uuid.uuid4().hex[:12]).status == 404  # API key on legacy v1
    assert dashboard.request('DELETE', '/api/flows/' + flow_id).status == 200
    assert dashboard.request('GET', '/api/flows/' + flow_id).status == 404
    # The task API is unaffected and shares the same account authority.
    current = owner.get(paused['task_id'])
    assert current == pause
    assert dashboard.get(paused['task_id']) == pause
    assert owner.request('POST', paused['status_url'] + '/cancel').status == 202
    assert owner.wait(paused['task_id'], timeout=60)['state'] == 'cancelled'
    return {'legacy_flow_crud': True, 'legacy_runs_list': True, 'unauthenticated_flow_write': 401,
            'task_paused_alongside_unchanged': True}


def ui_boundary(ctx):
    # Release the host-side ./chat REPL now, so it runs alongside this live UI task.
    Path('/state/compat-started').touch()
    frontend = Path(os.environ.get('TASK_ACCEPTANCE_FRONTEND_DIR', ''))
    assert (frontend / 'index.html').is_file(), 'current-revision frontend bundle missing'
    anonymous = Client(ctx.base)
    index = anonymous.request('GET', '/', raw=True)
    assert index.status == 200 and b'<div id="root">' in index.body
    for path, expected in [(PREFIX + '/profiles', 401), ('/api/auth/me', 401), ('/api/v1/api-keys', 401)]:
        reply = anonymous.request('GET', path)
        assert reply.status == expected and isinstance(reply.body, dict), 'SPA catch-all shadowed an API route'
    password = ctx.secrets['pw_ui']
    label = ctx.run + '-ui'
    ui_client, ui_dashboard, _ = onboard(ctx.base, label, password=password)
    profile_label = 'ui-visible-' + uuid.uuid4().hex[:12]
    created = ui_client.request('POST', PREFIX + '/profiles', {'label': profile_label, 'idempotency_key': ctx.run + ':ui-profile'})
    assert created.status == 201
    # A live paused task on the same composed app while the UI is exercised.
    profile = ctx.new_profile('ui-alongside')
    paused = ctx.task('pause', profile)
    pause = ctx.owner.wait(paused['task_id'], {'waiting_for_browser'}, timeout=120)
    # Real Chromium (the image's Playwright build) on the secure localhost origin.
    # The SPA needs a secure context (crypto.randomUUID); production serves HTTPS.
    from playwright.sync_api import sync_playwright
    from .assembled_run import UI_SCRIPT
    console = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_context().new_page()
            page.on('console', lambda message: console.append(message.text.encode()))
            page.on('pageerror', lambda error: console.append(str(error).encode()))
            page.goto(ctx.base + '/', wait_until='load', timeout=30000)
            outcome = page.evaluate(UI_SCRIPT, {'ui_email': label + '@acceptance.invalid', 'ui_password': password,
                                                'ui_label': profile_label, 'ui_key_name': label})
            assert outcome is True, 'UI boundary smoke failed: ' + str(outcome)[:600]
            token_stored = page.evaluate("() => !!localStorage.getItem('flowpilot_token')")
        finally:
            browser.close()
    assert token_stored
    ctx.browser_logs.append(('ui-chromium-console', b'\n'.join(console)))
    assert ctx.owner.get(paused['task_id']) == pause, 'UI activity disturbed a live task'
    assert ctx.owner.request('POST', paused['status_url'] + '/cancel').status == 202
    assert ctx.owner.wait(paused['task_id'], timeout=60)['state'] == 'cancelled'
    return {'frontend_index_sha256': hashlib.sha256((frontend / 'index.html').read_bytes()).hexdigest(),
            'spa_served_with_task_api': True, 'api_routes_not_shadowed': 3, 'real_browser_login': True,
            'ui_lists_task_profile_via_session': True, 'ui_api_keys_no_plaintext': True,
            'live_task_unchanged': True, 'browser_console_lines': len(console),
            'limitation': 'harness Chromium on http://localhost (secure context); SPA crashes on plain-HTTP non-localhost origins'}


# --------------------------------------------------------------------------
# operations: encrypted backup -> restore -> continue; disk-pressure preflight
# --------------------------------------------------------------------------

def _sibling(image, command, *, volumes=None, environment=None, tmpfs=None, timeout=300):
    """Short-lived run-labelled sibling container; returns (exit code, output)."""
    import docker
    client = docker.from_env()
    container = client.containers.run(image, command, detach=True, labels={'task-acceptance-run': os.environ['TASK_ACCEPTANCE_RUN_ID']},
                                       network_mode='none', volumes=volumes or {}, environment=environment or {},
                                       tmpfs=tmpfs or {}, working_dir='/')
    try:
        result = container.wait(timeout=timeout)
        return result['StatusCode'], container.logs(stdout=True, stderr=True)
    finally:
        container.remove(force=True)


def operations(ctx):
    import docker
    run = ctx.run
    tools = os.environ['TASK_ACCEPTANCE_TOOLS_IMAGE']
    revision = os.environ['TASK_ACCEPTANCE_REVISION']
    host_root, host_state, host_ops = (os.environ['TASK_ACCEPTANCE_HOST_ROOT'], os.environ['TASK_ACCEPTANCE_HOST_STATE'],
                                       os.environ['TASK_ACCEPTANCE_HOST_OPS'])
    # ops_backup opens SQLite with mode=ro itself; a writable mount only lets
    # SQLite create its shared-memory index for any WAL database left by a crash.
    volumes = {host_root: {'bind': '/lane', 'mode': 'ro'}, host_state: {'bind': '/state', 'mode': 'rw'},
               host_ops: {'bind': '/ops', 'mode': 'rw'}}
    status, output = _sibling(tools, ['sh', '-c', 'umask 077 && mkdir -p /ops/keys && age-keygen -o /ops/keys/identity.txt'], volumes=volumes)
    assert status == 0
    recipient = next(line.split(':', 1)[1].strip() for line in output.decode().splitlines() if line.startswith('Public key:'))
    assert recipient.startswith('age1')
    status, _ = _sibling(tools, ['python', '/lane/scripts/ops_backup.py', 'backup', '/state/service', '/ops/backups/state.age',
                                 '--revision', revision, '--quiesced', '--recipient', recipient], volumes=volumes)
    assert status == 0, 'ops_backup.py backup failed'
    backup = Path('/ops/backups/state.age')
    assert backup.stat().st_size > 0 and backup.read_bytes()[:21] == b'age-encryption.org/v1'
    # Refuse to clobber a known-good archive.
    status, _ = _sibling(tools, ['python', '/lane/scripts/ops_backup.py', 'backup', '/state/service', '/ops/backups/state.age',
                                 '--revision', revision, '--quiesced', '--recipient', recipient], volumes=volumes)
    assert status != 0
    status, _ = _sibling(tools, ['python', '/lane/scripts/ops_backup.py', 'restore', '/ops/backups/state.age', '/ops/restored/service',
                                 '--revision', revision + '-wrong', '--identity', '/ops/keys/identity.txt'], volumes=volumes)
    assert status != 0 and not Path('/ops/restored/service').exists(), 'restore accepted mismatched revision'
    status, _ = _sibling(tools, ['python', '/lane/scripts/ops_backup.py', 'restore', '/ops/backups/state.age', '/ops/restored/service',
                                 '--revision', revision, '--identity', '/ops/keys/identity.txt'], volumes=volumes)
    assert status == 0, 'ops_backup.py restore failed'
    manifest = json.loads(Path('/ops/restored/service/ops-manifest.json').read_text())
    assert manifest['revision'] == revision and manifest['files']
    port = 18083
    base = 'http://127.0.0.1:%d' % port
    seed = ctx.seed
    before = ctx.counters()
    with owned_child(port, {'PROFILES_DIR': '/ops/restored/service'}) as child, dump_on_failure(child):
        restored = Client(base, ctx.dashboard.token)
        assert restored.get(seed['task_id']) == seed['view'], 'restored task history differs'
        assert code(Client(base, ctx.owner.token).request('GET', PREFIX + '/profiles')) == 'unauthenticated'
        listing = restored.request('GET', PREFIX + '/profiles').body['items']
        assert next(p for p in listing if p['profile_id'] == seed['profile_id'])['state'] == 'available'
        request = {**ctx.program('restored', run + ':restored-continue'), 'session_id': seed['session_id']}
        receipt = restored.start(request)
        assert receipt['profile_id'] == seed['profile_id']
        # Bounded poll that records the slowest status read instead of hiding it.
        slow = Client(base, ctx.dashboard.token, timeout=90)
        deadline, slowest, done = time.monotonic() + 240, 0.0, None
        while time.monotonic() < deadline:
            started = time.monotonic()
            current = slow.get(receipt['task_id'])
            slowest = max(slowest, time.monotonic() - started)
            if current['state'] in {'completed', 'failed', 'cancelled', 'interrupted'}:
                done = current
                break
            time.sleep(.1)
        assert done is not None, 'restored task did not settle'
        ctx.seed['slowest_restored_poll_seconds'] = round(slowest, 2)
        assert done['state'] == 'completed' and done['result']['metrics']['reused'] == 1
        after = ctx.counters()
        assert after.get('cart', 0) == before.get('cart', 0) + 1 and after.get('prerequisite', 0) == before.get('prerequisite', 0)
        assert code(Client(base, ctx.foreign.token).request('GET', receipt['status_url'])) == 'not_found'
    # Disk-pressure preflight with the repository's ops_config runtime checks.
    image = docker.from_env().containers.get(run + '-backend').image.id
    preflight = {}
    for name, size in (('pressure', '64m'), ('control', '2g')):
        status, output = _sibling(image, ['/app/.venv/bin/python', '/lane/scripts/ops_config.py', '--local', '--runtime'],
                                  volumes={host_root: {'bind': '/lane', 'mode': 'ro'}},
                                  environment={'PROFILES_DIR': '/pressure', 'JWT_SECRET': 'x' * 40, 'PYTHONPATH': '/lane'},
                                  tmpfs={'/pressure': 'size=' + size})
        text = output.decode(errors='replace')
        preflight[name] = {'exit_nonzero': status != 0, 'disk_low_blocker': 'BLOCKER: DISK_LOW' in text}
    assert preflight['pressure'] == {'exit_nonzero': True, 'disk_low_blocker': True}
    assert not preflight['control']['disk_low_blocker']
    return {'tools_image_id': docker.from_env().images.get(tools).id, 'backup_encrypted_age': True,
            'restore_wrong_revision_rejected': True, 'no_clobber': True, 'restored_files': len(manifest['files']),
            'restored_history_identical': True, 'revoked_key_still_revoked': True,
            'continued_on_restored_profile': {'state': 'completed', 'script_reused': 1, 'prerequisite_repeated': 0},
            'disk_preflight': preflight, 'slowest_restored_status_poll_seconds': ctx.seed.get('slowest_restored_poll_seconds')}


# --------------------------------------------------------------------------
# artifact_secret_scan: everything this run produced
# --------------------------------------------------------------------------

def final_scan(ctx):
    import docker
    secrets = list(ctx.secrets.values())
    canaries = secrets + ctx.tokens
    state = scan_artifacts('/state', canaries, max_bytes=4 * 1024 ** 3)
    ops = scan_artifacts('/ops', canaries, max_bytes=4 * 1024 ** 3) if Path('/ops').is_dir() and any(Path('/ops').iterdir()) else None
    views = ctx.views  # Captured over HTTP before the main server stopped.
    own = docker.from_env().containers.get(ctx.run + '-backend').logs(stdout=True, stderr=True)
    blobs = scan_blobs([('backend-own-log', own), ('caddy', ctx.caddy_logs), *ctx.browser_logs, *views], canaries)
    traces = [p for p in Path('/state').rglob('*') if p.is_file() and 'trace' in p.name.lower()]
    from backend.trace import ENABLED
    gaps = state['gaps'] + (ops['gaps'] if ops else []) + blobs['gaps']
    findings = state['findings'] + (ops['findings'] if ops else []) + blobs['findings']
    assert len(ctx.browser_logs) >= 2 and state['archive_members_scanned'] > 0 and views
    assert not findings, 'secret canary persisted'
    assert not gaps, 'incomplete artifact coverage'
    assert not traces and not ENABLED
    variants = len(canary_variants(secrets[0]))
    return {'canaries': len(secrets), 'capability_tokens': len(ctx.tokens), 'variants_per_canary': variants,
            'state_files': state['files_scanned'], 'state_bytes': state['bytes_scanned'],
            'archive_members_expanded': state['archive_members_scanned'] + (ops['archive_members_scanned'] if ops else 0),
            'ops_files': ops['files_scanned'] if ops else 0, 'browser_log_blobs': len(ctx.browser_logs),
            'task_result_views': len(views), 'backend_log_bytes': len(own), 'proxy_log_bytes': len(ctx.caddy_logs),
            'raw_trace_files': 0, 'raw_tracing_enabled': False, 'findings': 0, 'gaps': 0}
