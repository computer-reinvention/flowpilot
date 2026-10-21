"""Inside the owned backend container: actual producers, model boundary only.

No legacy main lifespan is run. Its actual account router is reused; task host
startup/shutdown is composed through frozen factories. Low-level mouse movement
is instrumented only for an explicitly armed input fault while the REAL runtime
writer lock is held. No handle, repository, transport or observation is replaced.
"""

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import multiprocessing
import os
from pathlib import Path
import threading
import time
import uuid

from .client import Client
from .evidence import Evidence
from .release_fence import DirectResumeTarget, InputOperationGate, ReleaseAudit, verify_direct_api_resume
from .security import scan_artifacts, verify_key_hashes
from .site import synthetic_site
from .system import onboard, ownership, profiles, same_account_key_scope
from .wire import PREFIX, assistance_state, no_store, view

GATES = {}
MODEL_EVENTS = []


async def deterministic_driver(task, system, definitions, dispatch, **kwargs):
    actual_dispatch = dispatch
    async def dispatch(name, args):
        MODEL_EVENTS.append({'tool': name, 'phase': 'entered'})
        try:
            result = await actual_dispatch(name, args)
        except BaseException as exc:
            MODEL_EVENTS.append({'tool': name, 'phase': 'raised', 'exception_type': type(exc).__name__,
                                 'error_code': str(getattr(getattr(exc, 'error', None), 'code', ''))})
            raise
        MODEL_EVENTS.append({'tool': name, 'phase': 'returned', 'success': result.get('success') if isinstance(result, dict) else None,
                             'has_error': bool(result.get('error')) if isinstance(result, dict) else False,
                             'readiness': result.get('readiness') if isinstance(result, dict) else None})
        return result
    # BrowserSession supplies contextual prose around the current user request.
    request = task.split('Current request: ', 1)[1].split('\n', 1)[0]
    assert request.startswith('acceptance:'), 'only labelled synthetic requests allowed'
    spec = json.loads(request.removeprefix('acceptance:'))
    scenario, url = spec['scenario'], spec['url']
    await dispatch('navigate', {'url': url})
    await dispatch('observe', {})

    async def execute(script, condition='async () => true'):
        await dispatch('observe', {})
        result = await dispatch('execute', {'script': script, 'precondition': 'async () => true', 'postcondition': condition})
        assert result.get('success'), 'checked browser script failed'
        return result.get('result')

    async def effect(action):
        return await execute('async () => { await fetch("/effects", {method:"POST", body:JSON.stringify({action:' + json.dumps(action) + '})}); return true; }')

    increment = '''async () => { const n=Number(localStorage.getItem('cart')||0)+1;
        localStorage.setItem('cart',String(n)); await fetch('/effects',{method:'POST',body:JSON.stringify({action:'cart'})}); return n; }'''
    verify = 'async () => true'
    if scenario == 'seed':
        await execute('''async () => { if(localStorage.getItem('owner')) throw Error('not fresh');
            localStorage.setItem('owner','private'); document.cookie='synthetic_session=private; Max-Age=3600; Path=/';
            await fetch('/effects',{method:'POST',body:JSON.stringify({action:'prerequisite'})}); return true; }''')
        await execute(increment, 'async () => localStorage.getItem("cart") === "1"')
        await dispatch('save_reflex', {'name': 'synthetic_increment', 'description': 'Increment synthetic cart once; inspect current cart before reuse', 'script': increment})
        verify = 'async () => localStorage.getItem("cart") === "1"'
    elif scenario == 'follow':
        result = await execute('async () => localStorage.getItem("owner") === "private" && document.cookie.includes("synthetic_session=private")')
        assert str(result).lower() == 'true', 'private profile was not restored'
        await dispatch('observe', {})
        reused = await dispatch('run_reflex', {'name': 'synthetic_increment'})
        assert not reused.get('error'), 'saved profile script missing or failed'
        verify = 'async () => localStorage.getItem("cart") === "2"'
    elif scenario == 'clean':
        verify = 'async () => localStorage.getItem("owner") === null && localStorage.getItem("cart") === null && !document.cookie.includes("synthetic_session=")'
    elif scenario == 'question':
        response = await dispatch('request_input_values', {'fields_json': '[{"name":"otp","label":"Synthetic OTP","type":"secret"}]'})
        values = json.loads(response['extracted_content'])
        assert values['otp'].startswith('synthetic-canary-'), 'input did not reach actual model driver'
    elif scenario == 'decline':
        await dispatch('request_confirmation', {'summary': 'Commit synthetic submission?'})
        # This executes only if the real host wrongly lets a declined task proceed.
        await effect('submit')
    elif scenario in {'pause', 'drain', 'crash'}:
        if scenario == 'crash':
            await effect('cart')
        for number in range(spec.get('pauses', 1)):
            await dispatch('human_help', {'reason': 'Owned synthetic browser pause ' + str(number)})
            await dispatch('observe', {})
        await effect('agent')
    else:
        raise AssertionError('unknown synthetic model program')
    await dispatch('complete', {'verify': verify, 'summary': 'Synthetic ' + scenario + ' verified'})


def build_app(audit_holder):
    from fastapi import FastAPI
    from backend import auth, db, main, pi_browser
    from backend.repl import BrowserSession
    from backend.task_bootstrap import install_task_api
    from backend.task_ports import TaskBindings
    from backend.task_settings import TaskSettings
    from backend.tasks.control import create_control
    from backend.tasks.executor import Executor
    from backend.tasks.runtime import ConversationRuntime
    from backend.tasks.assistance import create_assistance, create_router as assistance_router
    from backend.tasks.routes import create_router
    from backend.observation import create_observation

    pi_browser.run_pi = deterministic_driver  # The sole conversational/model substitute.

    class InstrumentedSession(BrowserSession):
        async def __aenter__(self):
            await super().__aenter__()
            def instrument(page):
                original = page.mouse.move
                async def move(x, y, **kwargs):
                    gate = GATES.get('current')
                    if gate and not gate.entered.is_set() and (x, y) == (3, 3):
                        return await gate.invoke(lambda: original(x, y, **kwargs))
                    return await original(x, y, **kwargs)
                page.mouse.move = move
            for page in self.context.pages:
                instrument(page)
            self.context.on('page', instrument)
            return self

    def runtime_factory(settings, execution, bindings):
        return ConversationRuntime(settings, execution, bindings, session_factory=InstrumentedSession)

    settings = TaskSettings(enabled=True, state_dir=Path('/state/browser-tasks'), sandbox_pids=1024,
                            cancellation_seconds=2, shutdown_seconds=30, task_timeout_seconds=180,
                            human_wait_seconds=90, assistance_ttl_seconds=30, api_requests_per_minute=10000,
                            readiness_timeout_seconds=15)
    executor = ReleaseAudit(Executor(settings, runtime_factory=runtime_factory), os.environ['TASK_ACCEPTANCE_RUN_ID'])
    audit_holder['audit'] = executor
    bindings = TaskBindings(create_control(settings), executor, create_assistance(settings), create_observation(settings))
    @asynccontextmanager
    async def lifespan(app):
        await auth.init_db()
        await db.init_db()
        await app.state.browser_tasks.startup()
        try:
            yield
        finally:
            await app.state.browser_tasks.shutdown()
    app = FastAPI(lifespan=lifespan)
    app.include_router(main.account_router)  # Real signup/login/key routes + SensitiveRoute.
    install_task_api(app, settings, bindings=bindings, routers=(create_router(bindings), assistance_router(bindings)))
    return app


def serve(port, stop=None, holder=None):
    import uvicorn
    holder = {} if holder is None else holder
    server = uvicorn.Server(uvicorn.Config(build_app(holder), host='127.0.0.1', port=port,
                                         log_level='critical', access_log=False))
    holder['server'] = server
    server.run()


def wait_ready(base, process=None):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if process is not None:
            assert process.is_alive(), 'owned backend child exited before readiness'
        try:
            response = Client(base, timeout=1).request('GET', '/health/browser-tasks')
            if response.status == 200 and response.body.get('ready'):
                return
        except Exception:
            pass
        time.sleep(.05)
    raise TimeoutError('actual task composition never became ready')


def program(owner, site, scenario, key, **extra):
    request = {'task': 'acceptance:' + json.dumps({'scenario': scenario, 'url': site.browser_url, **extra}),
               'idempotency_key': key}
    return request


def run_inside():
    logging.disable(logging.CRITICAL)
    run = os.environ['TASK_ACCEPTANCE_RUN_ID']
    evidence = Evidence('assembled_docker_deterministic_model')
    holder = {}
    base = 'http://127.0.0.1:18080'
    thread = threading.Thread(target=serve, args=(18080, None, holder), daemon=True)
    thread.start()
    canaries = []
    stage = 'startup'
    with synthetic_site('/state/site', run + '-backend', '0.0.0.0') as site:
        try:
            wait_ready(base)
            stage = 'account_onboarding'
            owner, dashboard, key_id = onboard(base, run + '-owner')
            foreign, foreign_dashboard, _ = onboard(base, run + '-foreign')
            evidence.record('onboarding_real_http', 'passed', 'real account signup/login/key routes and no-store; no SDK client')
            evidence.requirements['onboarding_http']['outcome'] = 'passed'
            owned_profiles = profiles(owner, run)
            foreign_profiles = profiles(foreign, run + '-foreign')
            evidence.check('owned_profile_authority', lambda: ownership(base, owner, foreign, owned_profiles))
            evidence.check('same_account_key_scope', lambda: same_account_key_scope(owner, dashboard, foreign, owned_profiles, run), 'same_account_key_scope')

            def task(scenario, profile, account=owner, **extra):
                return account.start({**program(account, site, scenario, run + ':' + uuid.uuid4().hex, **extra), 'profile_id': profile})

            seed_request = {**program(owner, site, 'seed', run + ':seed'), 'profile_id': owned_profiles[0]}
            stage = 'concurrent_admission'
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=4) as pool:
                receipts = list(pool.map(lambda _: owner.start(seed_request), range(4)))
            evidence.record('concurrent_admission_observation', 'passed' if all(r == receipts[0] for r in receipts) else 'failed',
                            'actual admission receipts compared including revision',
                            distinct_task_ids=len({r['task_id'] for r in receipts}), revisions=[r['revision'] for r in receipts])
            assert all(receipt == receipts[0] for receipt in receipts)
            receipt = receipts[0]
            owner.expect_error('POST', PREFIX, {**seed_request, 'inputs': {'changed': True}}, 'conflict')
            foreign.expect_error('GET', receipt['status_url'], None, 'not_found')
            completed = owner.wait(receipt['task_id'], timeout=120)
            stage = 'seed_browser_completion'
            evidence.record('seed_terminal_observation', 'passed' if completed['state'] == 'completed' else 'failed',
                            'actual executor terminal status', state=completed['state'],
                            error_code=(completed.get('error') or {}).get('code'), effect_counters=site.counters())
            assert completed['state'] == 'completed', 'seed did not complete'
            assert site.counters() == {'prerequisite': 1, 'cart': 1}
            fresh = Client(base, owner.token)
            stage = 'profile_followup'
            follow = fresh.start({**program(fresh, site, 'follow', run + ':follow'), 'session_id': receipt['session_id']})
            assert follow['session_id'] == receipt['session_id'] and follow['profile_id'] == receipt['profile_id']
            followed = fresh.wait(follow['task_id'], timeout=120)
            assert followed['state'] == 'completed' and followed['result']['metrics']['reused'] == 1
            assert site.counters() == {'prerequisite': 1, 'cart': 2}
            evidence.record('real_browser_followup_idempotency', 'passed', 'concurrent HTTP admission once; durable profile cookie/storage and saved script restored; fresh client follow-up',
                            admission_attempts=4, distinct_tasks=1, effect_counters=site.counters())
            evidence.requirements['http_retry_followup']['outcome'] = 'passed'
            for account, profile in [(owner, owned_profiles[1]), (foreign, foreign_profiles[0])]:
                stage = 'browser_profile_separation'
                clean = task('clean', profile, account)
                assert account.wait(clean['task_id'], timeout=120)['state'] == 'completed'
            evidence.record('real_browser_profile_separation', 'passed', 'same-account second profile and foreign account have no private cookie/localStorage/cart state')
            # Script/history negative coverage isn't inferred from cookie isolation.

            question = task('question', owned_profiles[1])
            stage = 'question'
            waiting = owner.wait(question['task_id'], {'waiting_for_input'}, timeout=120)
            interaction = waiting['interaction']
            canary = 'synthetic-canary-' + uuid.uuid4().hex
            canaries.append(canary)
            response = {'kind': 'input', 'generation': interaction['generation'], 'values': {'otp': canary}, 'idempotency_key': run + ':answer'}
            path = question['status_url'] + '/interactions/' + interaction['interaction_id'] + '/respond'
            foreign.expect_error('POST', path, response, 'not_found')
            assert owner.request('POST', path, response).status == 202
            assert owner.request('POST', path, response).status == 202
            owner.expect_error('POST', path, {**response, 'values': {'otp': canary + 'different'}}, 'conflict')
            assert owner.wait(question['task_id'], timeout=120)['state'] == 'completed'
            evidence.record('real_question_transient_delivery', 'passed', 'current actual model driver received transient synthetic OTP; duplicate receipt and mismatched retry checked')
            decline = task('decline', owned_profiles[1])
            stage = 'decline'
            waiting = owner.wait(decline['task_id'], {'waiting_for_confirmation'}, timeout=120)
            interaction = waiting['interaction']
            response = {'kind': 'confirmation', 'generation': interaction['generation'], 'confirmed': False, 'idempotency_key': run + ':decline'}
            path = decline['status_url'] + '/interactions/' + interaction['interaction_id'] + '/respond'
            assert owner.request('POST', path, response).status == 202
            outcome = owner.wait(decline['task_id'], timeout=120)
            assert outcome['state'] == 'failed' and outcome['error']['code'] == 'conflict'
            assert site.counters().get('submit', 0) == 0
            evidence.record('real_confirmation_decline', 'passed', 'host fenced model continuation on decline; durable submit counter remains zero')

            for mode in ('success', 'failure', 'timeout'):
                stage = 'direct_resume_' + mode
                current = task('drain', owned_profiles[1])
                waiting = owner.wait(current['task_id'], {'waiting_for_browser'}, timeout=120)
                interaction = waiting['interaction']
                grant = owner.request('POST', current['status_url'] + '/assistance', {'interaction_id': interaction['interaction_id'], 'generation': interaction['generation']})
                assert grant.status == 201
                state = Client(base).request('GET', grant.body['assistance_url'] + '/state')
                assert state.status == 200
                assistance_state(state.body, interaction, site.browser_url)
                gate = InputOperationGate(run, 'inside_executor_input_registry')
                GATES['current'] = gate
                target = DirectResumeTarget(owner, current, interaction, grant.body, gate, holder['audit'], site.counters, timeout=15)
                requirement = {'success': 'direct_api_resume_drain', 'failure': 'resume_release_failure', 'timeout': 'resume_release_timeout'}[mode]
                try:
                    observations = asyncio.run(verify_direct_api_resume(target, mode))
                except Exception as exc:
                    evidence.record(requirement, 'failed', 'real assembled direct-API fence assertion failed', exception_type=type(exc).__name__)
                    evidence.requirements[requirement]['outcome'] = 'failed'
                else:
                    evidence.record(requirement, 'passed', 'real stream/input/Control/Executor with actual writer lock; only owned low-level mouse gate and release failure injection', **observations)
                    evidence.requirements[requirement]['outcome'] = 'passed'
                finally:
                    gate.permit.set()
                    holder['audit'].mode = 'success'
                    GATES.pop('current', None)
                if mode == 'success':
                    assert owner.wait(current['task_id'], timeout=120)['state'] == 'completed'
                else:
                    cancel = owner.request('POST', current['status_url'] + '/cancel')
                    assert cancel.status == 202
                    cancelled = owner.wait(current['task_id'], timeout=120)
                    assert cancelled['state'] == 'cancelled'
                    view(owner.get(current['task_id']), cancelled)
                    evidence.record('real_cancel_' + mode, 'passed', 'held failure scenario browser stopped/saved and task cancelled after cleanup')
            if all(evidence.requirements[r]['outcome'] == 'passed' for r in ['direct_api_resume_drain', 'resume_release_failure', 'resume_release_timeout']):
                evidence.requirements['resume_wrong_generation']['outcome'] = 'passed'
            revoked = dashboard.request('DELETE', '/api/v1/api-keys/' + key_id)
            assert revoked.status == 200
            no_store(revoked.headers)
            owner.expect_error('GET', PREFIX + '/profiles', None, 'unauthenticated')
            evidence.requirements['auth_key_no_store']['outcome'] = 'passed'
            evidence.record('real_revocation_no_store', 'passed', 'revoked API key cannot perform subsequent control calls')
            # Database/history raw scan excludes browser archives explicitly.
            scan = scan_artifacts('/state', canaries)
            evidence.record('real_secret_snapshot_scan', 'failed' if scan['findings'] else 'blocked' if scan['gaps'] else 'passed',
                            'actual SQLite/history/state scan; compressed profile archives remain an explicit coverage gap', **scan)
        except Exception as exc:
            import traceback
            frames = [{'path': frame.filename, 'line': frame.lineno} for frame in traceback.extract_tb(exc.__traceback__)]
            evidence.record('assembled_scenario', 'failed', 'actual assembled scenario stopped; rerun exact launcher for failure boundary',
                            stage=stage, exception_type=type(exc).__name__, traceback_frames=frames[-6:], model_events=MODEL_EVENTS[-24:])
        finally:
            if holder.get('server'):
                holder['server'].should_exit = True
            thread.join(timeout=45)
            if thread.is_alive():
                evidence.record('owned_server_shutdown', 'failed', 'server did not stop within bounded cleanup')
    Path('/state/assembled_evidence.json').write_text(json.dumps({'checks': evidence.checks, 'requirements': evidence.requirements}))


if __name__ == '__main__':
    run_inside()
