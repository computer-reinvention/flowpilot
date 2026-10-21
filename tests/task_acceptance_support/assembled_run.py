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
import subprocess
import sys
import time
import uuid

from .client import Client
from .evidence import Evidence
from .release_fence import DirectResumeTarget, InputOperationGate, ReleaseAudit, verify_direct_api_resume
from .security import scan_artifacts, verify_key_hashes
from .site import synthetic_site
from .system import onboard, ownership, profiles, same_account_key_scope
from .wire import PREFIX, assistance_state, no_store, view
from .assistance import current_pause, expired, metadata, mint

GATES = {}
MODEL_EVENTS = []
GUARD_OBSERVATIONS = {}


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
    context = json.JSONDecoder().raw_decode(task.split('Context JSON:\n', 1)[1])[0]
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
        assert len(context['available_scripts']) == 1 and len(context['recent_conversation']) == 1
        result = await execute('async () => localStorage.getItem("owner") === "private" && document.cookie.includes("synthetic_session=private")')
        assert str(result).lower() == 'true', 'private profile was not restored'
        await dispatch('observe', {})
        reused = await dispatch('run_reflex', {'name': 'synthetic_increment'})
        assert not reused.get('error'), 'saved profile script missing or failed'
        verify = 'async () => localStorage.getItem("cart") === "2"'
    elif scenario == 'clean':
        assert context['available_scripts'] == [] and context['recent_conversation'] == []
        verify = 'async () => localStorage.getItem("owner") === null && localStorage.getItem("cart") === null && !document.cookie.includes("synthetic_session=")'
    elif scenario == 'question':
        response = await dispatch('request_input_values', {'fields_json': '[{"name":"otp","label":"Synthetic OTP","type":"secret"}]'})
        values = json.loads(response['extracted_content'])
        assert values['otp'].startswith('synthetic-canary-'), 'input did not reach actual model driver'
    elif scenario == 'guards':
        from backend.task_contracts import TaskFault
        await dispatch('navigate', {'url': url + '/frames'})
        frame = await dispatch('observe', {})
        assert frame['readiness'] == 'unknown'
        GUARD_OBSERVATIONS['cross_origin'] = frame['readiness']
        await dispatch('navigate', {'url': url + '/volatile'})
        volatile = await dispatch('observe', {})
        assert volatile['readiness'] in {'timeout', 'unstable'}
        GUARD_OBSERVATIONS['volatile'] = volatile['readiness']
        await dispatch('navigate', {'url': url})
        await execute('''async () => { setTimeout(() => { const n=document.createElement('button');
            n.id='same-url-late'; n.textContent='Late changed action'; document.body.appendChild(n); }, 5000); return true; }''')
        before = await dispatch('observe', {})
        assert before['readiness'] == 'ready'
        await asyncio.sleep(5.2)
        try:
            await dispatch('execute', {'script': 'async () => { await fetch("/effects", {method:"POST",body:JSON.stringify({action:"submit"})}); return true; }',
                                      'precondition': 'async () => true', 'postcondition': 'async () => true'})
        except TaskFault as exc:
            assert exc.error.code == 'stale_generation'
            GUARD_OBSERVATIONS['same_url_stale'] = str(exc.error.code)
        else:
            raise AssertionError('stale same-URL action was not fenced')
        await dispatch('observe', {})
        repeated = {'script': 'async () => 17', 'precondition': 'async () => true', 'postcondition': 'async () => true'}
        assert (await dispatch('execute', repeated))['success']
        await dispatch('observe', {})
        try:
            await dispatch('execute', repeated)
        except TaskFault as exc:
            assert exc.error.code == 'no_progress'
            GUARD_OBSERVATIONS['repeat'] = str(exc.error.code)
        else:
            raise AssertionError('consumed no-progress action was repeated')
        verify = 'async () => !!document.querySelector("#same-url-late")'
    elif scenario == 'decline':
        await dispatch('request_confirmation', {'summary': 'Commit synthetic submission?'})
        # This executes only if the real host wrongly lets a declined task proceed.
        await effect('submit')
    elif scenario in {'pause', 'drain', 'crash', 'viewer'}:
        if scenario == 'crash':
            await effect('cart')
        for number in range(spec.get('pauses', 1)):
            await dispatch('human_help', {'reason': 'Owned synthetic browser pause ' + str(number)})
            await dispatch('observe', {})
        if scenario == 'viewer':
            verify = 'async () => document.querySelector("#name").value === "synthetic-human"'
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
    logging.disable(logging.CRITICAL)
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


def crash_restart(site, account_token, profile_id, run):
    """Kill only an owned backend CHILD after a real browser website mutation.

    Keep state/network/JWT identity stable, then boot entirely fresh producers.
    No mocked repository, recovery method, browser, or orchestration is used.
    """
    context = multiprocessing.get_context('spawn')
    process = None
    base = 'http://127.0.0.1:18081'
    before = site.counters()
    try:
        process = context.Process(target=serve, args=(18081,))
        process.start()
        wait_ready(base, process)
        owner = Client(base, account_token)
        request = {**program(owner, site, 'crash', run + ':crash'), 'profile_id': profile_id}
        receipt = owner.start(request)
        waiting = owner.wait(receipt['task_id'], {'waiting_for_browser'}, timeout=120)
        assert site.counters().get('cart', 0) == before.get('cart', 0) + 1
        interaction = waiting['interaction']
        grant = owner.request('POST', receipt['status_url'] + '/assistance',
                              {'interaction_id': interaction['interaction_id'], 'generation': interaction['generation']})
        assert grant.status == 201
        process.kill()  # This specific owned child; never namespace PID1/global pkill.
        process.join(timeout=10)
        assert process.exitcode is not None and process.exitcode < 0
        process = context.Process(target=serve, args=(18081,))
        process.start()
        wait_ready(base, process)
        fresh = Client(base, account_token)
        recovered = fresh.get(receipt['task_id'])
        assert recovered['state'] == 'interrupted' and recovered['error']['code'] == 'interrupted'
        assert recovered.get('interaction') is None
        expired(fresh, grant.body)
        assert fresh.start(request) == receipt
        time.sleep(.5)
        view(fresh.get(receipt['task_id']), recovered)
        assert site.counters().get('cart', 0) == before.get('cart', 0) + 1
        assert site.counters().get('agent', 0) == before.get('agent', 0)
        listing = fresh.request('GET', PREFIX + '/profiles')
        assert listing.status == 200
        profile = next(item for item in listing.body['items'] if item['profile_id'] == profile_id)
        assert profile['state'] == 'recovery_required'
        deleted = fresh.request('DELETE', PREFIX + '/profiles/' + profile_id)
        assert deleted.status == 204
        return {'backend_signal': 'SIGKILL', 'recovered_state': 'interrupted', 'extra_cart_effects': 1,
                'extra_agent_effects': 0, 'automatic_replay': False, 'same_admission_receipt': True,
                'prior_capability_rejected': True, 'recovery_profile_deleted_explicitly': True}
    finally:
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=40)
            if process.is_alive():
                process.kill()
                process.join(timeout=10)


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
            import jwt
            identity = dashboard.request('GET', '/api/auth/me')
            assert identity.status == 200
            user_id = identity.body['user']['id']
            for secret, expires in [(os.environ['JWT_SECRET'], 1), ('synthetic-wrong-signing-key-000000000000000', 4102444800)]:
                invalid = jwt.encode({'user_id': user_id, 'exp': expires}, secret, algorithm='HS256')
                Client(base, invalid).expect_error('GET', PREFIX + '/profiles', None, 'unauthenticated')
            bad_login = Client(base).request('POST', '/api/auth/login', {'email': run + '-owner@acceptance.invalid', 'password': 'synthetic-wrong-password'})
            assert bad_login.status == 401
            no_store(bad_login.headers)
            owned_profiles = profiles(owner, run)
            foreign_profiles = profiles(foreign, run + '-foreign')
            evidence.check('owned_profile_authority', lambda: ownership(base, owner, foreign, owned_profiles))
            evidence.check('same_account_key_scope', lambda: same_account_key_scope(owner, dashboard, foreign, owned_profiles, run), 'same_account_key_scope')

            def task(scenario, profile, account=owner, **extra):
                return account.start({**program(account, site, scenario, run + ':' + uuid.uuid4().hex, **extra), 'profile_id': profile})

            seed_request = {**program(owner, site, 'seed', run + ':seed'), 'profile_id': owned_profiles[0]}
            stage = 'concurrent_admission'
            second_created = dashboard.request('POST', '/api/v1/api-keys', {'name': run + ':second-task-key'})
            assert second_created.status == 200
            no_store(second_created.headers)
            second_key = Client(base, second_created.body['key'])
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=4) as pool:
                receipts = list(pool.map(lambda number: (owner if number % 2 else second_key).start(seed_request), range(4)))
            evidence.record('concurrent_admission_observation', 'passed' if all(r == receipts[0] for r in receipts) else 'failed',
                            'actual admission receipts compared including revision',
                            distinct_task_ids=len({r['task_id'] for r in receipts}), revisions=[r['revision'] for r in receipts])
            assert all(receipt == receipts[0] for receipt in receipts)
            receipt = receipts[0]
            owner.expect_error('POST', PREFIX, {**seed_request, 'inputs': {'changed': True}}, 'conflict')
            foreign.expect_error('GET', receipt['status_url'], None, 'not_found')
            foreign.expect_error('POST', PREFIX, {**program(foreign, site, 'clean', run + ':foreign-session'), 'session_id': receipt['session_id']}, 'not_found')
            completed = owner.wait(receipt['task_id'], timeout=120)
            stage = 'seed_browser_completion'
            evidence.record('seed_terminal_observation', 'passed' if completed['state'] == 'completed' else 'failed',
                            'actual executor terminal status', state=completed['state'],
                            error_code=(completed.get('error') or {}).get('code'), effect_counters=site.counters())
            assert completed['state'] == 'completed', 'seed did not complete'
            assert site.counters() == {'prerequisite': 1, 'cart': 1}
            fresh = Client(base, second_key.token)
            stage = 'profile_followup'
            follow = fresh.start({**program(fresh, site, 'follow', run + ':follow'), 'session_id': receipt['session_id']})
            assert follow['session_id'] == receipt['session_id'] and follow['profile_id'] == receipt['profile_id']
            followed = fresh.wait(follow['task_id'], timeout=120)
            assert followed['state'] == 'completed' and followed['result']['metrics']['reused'] == 1
            assert site.counters() == {'prerequisite': 1, 'cart': 2}
            evidence.record('real_browser_followup_idempotency', 'passed', 'concurrent HTTP admission once; durable profile cookie/storage and saved script restored; fresh client follow-up',
                            admission_attempts=4, distinct_tasks=1, effect_counters=site.counters())
            second_revoked = dashboard.request('DELETE', '/api/v1/api-keys/' + second_created.body['id'])
            assert second_revoked.status == 200
            no_store(second_revoked.headers)
            second_key.expect_error('GET', PREFIX + '/profiles', None, 'unauthenticated')
            evidence.requirements['http_retry_followup']['outcome'] = 'passed'
            for account, profile in [(owner, owned_profiles[1]), (foreign, foreign_profiles[0])]:
                stage = 'browser_profile_separation'
                clean = task('clean', profile, account)
                assert account.wait(clean['task_id'], timeout=120)['state'] == 'completed'
            evidence.record('real_browser_profile_separation', 'passed', 'same-account second profile and foreign account have no private cookie/localStorage/cart state')
            evidence.requirements['profile_website_isolation']['outcome'] = 'passed'

            stage = 'published_standalone_example'
            def example(command, body=None, object_id=None):
                argv = [sys.executable, '/lane/examples/browser_tasks/http_client.py', command]
                if object_id:
                    argv.append(object_id)
                result = subprocess.run(argv, cwd='/state', input=None if body is None else json.dumps(body),
                                        env={'PATH': os.environ.get('PATH', ''), 'FLOWPILOT_BASE_URL': base, 'FLOWPILOT_API_KEY': foreign.token},
                                        text=True, capture_output=True, timeout=90)
                assert result.returncode == 0, 'published standalone client command failed'
                return json.loads(result.stdout)
            example_profile = example('profile-create', {'label': 'Owned standalone example', 'idempotency_key': run + ':example-profile'})
            example_task = example('start', {**program(foreign, site, 'clean', run + ':example-task'), 'profile_id': example_profile['profile_id']})
            example_result = example('poll', object_id=example_task['task_id'])
            assert example_result['state'] == 'completed'
            assert example('profile-delete', object_id=example_profile['profile_id']) is None
            evidence.record('real_published_standalone_client', 'passed', '004 unmodified stdlib CLI runs profile-create/start/poll/profile-delete in separate processes outside repository cwd; key only in environment, no SDK/backend imports')
            evidence.requirements['standalone_example']['outcome'] = 'passed'

            stage = 'native_pi_state_guards'
            guarded = task('guards', owned_profiles[1])
            assert owner.wait(guarded['task_id'], timeout=120)['state'] == 'completed'
            assert site.counters().get('submit', 0) == 0
            evidence.record('real_pi_state_guards', 'passed', 'native policy through real Pi dispatch: cross-origin unknown, volatile not-ready, delayed same-URL stale action and consumed-action repeat fenced', **GUARD_OBSERVATIONS)
            evidence.requirements['pi_readiness']['outcome'] = 'passed'

            stage = 'paused_capacity_before_spawn'
            import docker
            docker_client = docker.from_env()
            def browser_count():
                return len(docker_client.containers.list(all=True, filters={'network': os.environ['SANDBOX_NETWORK'], 'label': 'flowpilot-task=1'}))
            active = []
            for profile in owned_profiles:
                current = task('pause', profile)
                owner.wait(current['task_id'], {'waiting_for_browser'}, timeout=120)
                active.append((owner, current))
            assert browser_count() == 2
            owner.expect_error('POST', PREFIX, {**program(owner, site, 'pause', run + ':busy-session'), 'session_id': active[0][1]['session_id']}, 'conflict')
            owner.expect_error('POST', PREFIX, {**program(owner, site, 'pause', run + ':busy-profile'), 'profile_id': owned_profiles[0]}, 'conflict')
            owner.expect_error('POST', PREFIX, program(owner, site, 'pause', run + ':account-full'), 'capacity_exhausted')
            assert browser_count() == 2
            foreign_active = task('pause', foreign_profiles[0], foreign)
            foreign.wait(foreign_active['task_id'], {'waiting_for_browser'}, timeout=120)
            active.append((foreign, foreign_active))
            assert browser_count() == 3
            foreign.expect_error('POST', PREFIX, program(foreign, site, 'pause', run + ':global-full'), 'capacity_exhausted')
            assert browser_count() == 3
            for account, current in active:
                assert account.request('POST', current['status_url'] + '/cancel').status == 202
                settled = account.wait(current['task_id'], timeout=120)
                assert settled['state'] == 'cancelled'
                repeated = account.request('POST', current['status_url'] + '/cancel')
                assert repeated.status == 202 and repeated.body == settled
            assert browser_count() == 0
            evidence.record('real_paused_capacity_before_spawn', 'passed', 'real paused containers count toward account/global caps; busy session/profile and overload reject without spawning; cancellation releases all three',
                            paused_account_cap=2, paused_global_cap=3, rejected_spawns=0, remaining_browsers=0)
            evidence.requirements['capacity_paused']['outcome'] = 'passed'

            question = task('question', owned_profiles[1])
            stage = 'question'
            waiting = owner.wait(question['task_id'], {'waiting_for_input'}, timeout=120)
            interaction = waiting['interaction']
            canary = os.environ['TASK_ACCEPTANCE_OTP_CANARY']
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

            stage = 'private_viewer_and_pause_generations'
            browser_task = task('viewer', owned_profiles[1], pauses=2)
            first = current_pause(owner, browser_task['task_id'], timeout=120)['interaction']
            mint_request = {'interaction_id': first['interaction_id'], 'generation': first['generation']}
            foreign.expect_error('POST', browser_task['status_url'] + '/assistance', mint_request, 'not_found')
            grant = mint(owner, browser_task, first)
            metadata(owner, grant, first, site.browser_url)
            execution_epoch = holder['audit'].current_pause[2]
            capability_client = Client(base)
            page = capability_client.request('GET', grant['assistance_url'], raw=True)
            screen = capability_client.request('GET', grant['assistance_url'] + '/screen', raw=True)
            assert page.status == 200 and b'<html' in page.body.lower()
            assert screen.status == 200 and screen.body.startswith(b'\x89PNG\r\n\x1a\n')
            no_store(page.headers)
            no_store(screen.headers)
            origin = {'Origin': base}
            commands = [{'kind': 'down', 'x': 100, 'y': 15, 'button': 'left'},
                        {'kind': 'up', 'x': 100, 'y': 15, 'button': 'left'},
                        {'kind': 'type', 'text': 'synthetic-human'},
                        {'kind': 'keydown', 'key': 'Shift'}, {'kind': 'keyup', 'key': 'Shift'}]
            for command in commands:
                assert capability_client.request('POST', grant['assistance_url'] + '/input', command, origin).status == 200
            rotated = mint(owner, browser_task, first)
            expired(owner, grant)
            response = {'kind': 'browser', 'generation': first['generation'], 'browser_resolved': True,
                        'idempotency_key': run + ':viewer-first'}
            assert capability_client.request('POST', rotated['assistance_url'] + '/respond', response, origin).status == 202
            second = current_pause(owner, browser_task['task_id'], first['generation'], timeout=120)['interaction']
            grant2 = mint(owner, browser_task, second)
            metadata(owner, grant2, second, site.browser_url)
            assert holder['audit'].current_pause[2] == execution_epoch
            assert first['generation'] != second['generation']
            expired(owner, rotated)
            evidence.record('real_viewer_generation_metadata', 'passed', 'real HTML/PNG/pointer/keyboard/type and viewer resume; second pause uses same private execution epoch, distinct public pause generation',
                            pause_generations=[first['generation'], second['generation']], viewport_only=True)
            evidence.requirements['assistance_generation_metadata']['outcome'] = 'passed'
            from datetime import datetime, timezone
            remaining = (datetime.fromisoformat(grant2['expires_at'].replace('Z', '+00:00')) - datetime.now(timezone.utc)).total_seconds()
            assert remaining <= 31
            time.sleep(max(0, remaining) + .15)
            expired(owner, grant2)
            evidence.record('real_wallclock_expiry', 'passed', 'active-pause grant elapsed naturally and rejects state and input; no clock mocking')
            evidence.requirements['assistance_wallclock_expiry']['outcome'] = 'passed'
            renewed = mint(owner, browser_task, second)
            response = {**response, 'generation': second['generation'], 'idempotency_key': run + ':viewer-second'}
            assert capability_client.request('POST', renewed['assistance_url'] + '/respond', response, origin).status == 202
            assert owner.wait(browser_task['task_id'], timeout=120)['state'] == 'completed'
            evidence.record('real_private_viewer_resume', 'passed', 'actual model completion checks DOM text entered through human capability, not an API-key viewer shortcut')

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
            stage = 'revocation_with_admitted_task'
            still_admitted = task('pause', owned_profiles[1])
            paused = owner.wait(still_admitted['task_id'], {'waiting_for_browser'}, timeout=120)
            revoke_baseline = site.counters()
            revoked = dashboard.request('DELETE', '/api/v1/api-keys/' + key_id)
            assert revoked.status == 200
            no_store(revoked.headers)
            owner.expect_error('GET', PREFIX + '/profiles', None, 'unauthenticated')
            owner.expect_error('POST', still_admitted['status_url'] + '/assistance',
                               {'interaction_id': paused['interaction']['interaction_id'], 'generation': paused['interaction']['generation']}, 'unauthenticated')
            unchanged = dashboard.get(still_admitted['task_id'])
            assert unchanged['state'] == 'waiting_for_browser' and unchanged['interaction'] == paused['interaction']
            assert dashboard.request('POST', still_admitted['status_url'] + '/cancel').status == 202
            assert dashboard.wait(still_admitted['task_id'], timeout=120)['state'] == 'cancelled'
            assert site.counters() == revoke_baseline
            evidence.requirements['auth_key_no_store']['outcome'] = 'passed'
            evidence.requirements['auth_ownership']['outcome'] = 'passed'
            evidence.record('real_revocation_no_store', 'passed', 'revoked API key cannot perform subsequent control calls')
            evidence.check('real_key_hash_at_rest', lambda: verify_key_hashes('/state/platform.db', [foreign.token]), 'key_hash_at_rest')
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
        if not thread.is_alive() and 'foreign' in locals():
            evidence.check('real_crash_after_website_effect', lambda: crash_restart(site, foreign.token, foreign_profiles[1], run), 'crash_after_effect')
    Path('/state/assembled_evidence.json').write_text(json.dumps({'checks': evidence.checks, 'requirements': evidence.requirements}))


if __name__ == '__main__':
    run_inside()
