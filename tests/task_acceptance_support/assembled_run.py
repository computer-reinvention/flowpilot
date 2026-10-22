"""Inside the owned backend container: actual producers, model boundary only.

No legacy main lifespan is run. Its actual account router is reused; task host
startup/shutdown is composed through frozen factories. Low-level mouse movement
is instrumented only for an explicitly armed input fault while the REAL runtime
writer lock is held. No handle, repository, transport or observation is replaced.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
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
# In-process driver barriers (main server thread only): name -> (reached, release).
HOLDS = {}
# sha256 of every transient answer the CURRENT model driver received; never values.
QUESTION_DELIVERIES = []
MAIN_PORT = 8080  # The repository Caddyfile proxies to 127.0.0.1:8080.


def hold(name):
    HOLDS[name] = (threading.Event(), threading.Event())
    return HOLDS[name]


async def barrier(name):
    reached, release = HOLDS[name]
    reached.set()
    assert await asyncio.to_thread(release.wait, 90), 'harness barrier was not released'


@dataclass
class CapabilityFailpoint:
    """Owned save_capability gate for one task; reached only via the real mint path."""
    task_id: str
    entered: threading.Event = field(default_factory=threading.Event)
    permit: threading.Event = field(default_factory=threading.Event)
    max_hold: float = 30


class ControlAudit:
    """Transparent Control proxy, test-only and harness-owned.

    Every call is delegated to the REAL Control. It records the private execution
    lease the executor presents on its authorizing progress callback (so stale
    callbacks can later be replayed with genuine leases) and the order of mint
    calls. One explicitly armed failpoint may hold save_capability AFTER the
    assistance_lease lookup and BEFORE the commit transaction; it never alters
    arguments or results. Not reachable over HTTP.
    """
    def __init__(self, inner, run_label):
        assert run_label.startswith('task-acceptance-')
        self.inner = inner
        self.run_label = run_label
        self.leases = {}
        self.calls = []
        self.failpoint = None

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def arm(self, task_id):
        assert self.failpoint is None
        self.failpoint = CapabilityFailpoint(task_id)
        return self.failpoint

    def disarm(self):
        if self.failpoint:
            self.failpoint.permit.set()
        self.failpoint = None

    def order(self, task_id):
        return [method for task, method in self.calls if task == task_id]

    async def progress(self, lease, expected_revision, summary):
        self.leases.setdefault(lease.task_id, lease)
        return await self.inner.progress(lease, expected_revision, summary)

    async def assistance_lease(self, principal, task_id, request):
        self.calls.append((task_id, 'assistance_lease'))
        return await self.inner.assistance_lease(principal, task_id, request)

    async def revoke_capabilities(self, lease):
        self.calls.append((lease.task_id, 'revoke_capabilities'))
        return await self.inner.revoke_capabilities(lease)

    async def save_capability(self, binding):
        self.calls.append((binding.lease.task_id, 'save_capability'))
        point = self.failpoint
        if point is not None and point.task_id == binding.lease.task_id and not point.entered.is_set():
            point.entered.set()
            assert await asyncio.to_thread(point.permit.wait, point.max_hold), 'owned failpoint was not released'
        return await self.inner.save_capability(binding)


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
    elif scenario == 'restored':
        # Continue on a profile restored from an encrypted ops backup.
        assert len(context['available_scripts']) == 1
        result = await execute('async () => localStorage.getItem("owner") === "private" && document.cookie.includes("synthetic_session=private") && localStorage.getItem("cart") === "2"')
        assert str(result).lower() == 'true', 'restored profile state missing'
        await dispatch('observe', {})
        reused = await dispatch('run_reflex', {'name': 'synthetic_increment'})
        assert not reused.get('error'), 'restored saved script missing or failed'
        verify = 'async () => localStorage.getItem("cart") === "3"'
    elif scenario == 'question':
        response = await dispatch('request_input_values', {'fields_json': '[{"name":"otp","label":"Synthetic OTP","type":"secret"}]'})
        values = json.loads(response['extracted_content'])
        assert values['otp'].startswith('synthetic-canary-'), 'input did not reach actual model driver'
        QUESTION_DELIVERIES.append(hashlib.sha256(values['otp'].encode()).hexdigest())
        if spec.get('hold'):
            # Signal receipt WITHOUT the value, then keep the secret only in
            # executor memory until the owned backend child is SIGKILLed.
            await effect('otp_held')
            await asyncio.sleep(600)
        if spec.get('use_otp'):
            # Enter the transient code into the website as a model would: a
            # script literal typed into a password field and submitted once.
            await execute('async () => { const f=document.querySelector("#name"); f.type="password"; f.value=' + json.dumps(values['otp'])
                          + '; const r=await fetch("/otp",{method:"POST",body:JSON.stringify({value:f.value})}); f.value=""; return r.ok; }')
    elif scenario == 'approve':
        approved = await dispatch('request_confirmation', {'summary': 'Commit synthetic submission?'})
        assert approved.get('extracted_content') == 'User approved. Proceed.', 'approval did not reach model'
        await effect('submit')
    elif scenario == 'longjs':
        # Long-running page JavaScript: signal start, block the renderer, then
        # attempt a late effect that must never happen after cancellation.
        busy = int(spec.get('busy_ms', 10000))
        await execute('async () => { await fetch("/effects",{method:"POST",body:JSON.stringify({action:"longjs"})}); const end=Date.now()+' + str(busy)
                      + '; while (Date.now() < end) {} await fetch("/effects",{method:"POST",body:JSON.stringify({action:"late"})}); return true; }')
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
    if spec.get('barrier'):
        await barrier(spec['barrier'])
    await dispatch('complete', {'verify': verify, 'summary': 'Synthetic ' + scenario + ' verified'})


UI_SCRIPT = r'''async (inputs) => {
  let step = 'start'; const errors = [];
  const wait = async (find, ms) => { const end = Date.now() + (ms || 8000);
    while (Date.now() < end) { const found = find(); if (found) return found; await new Promise(r => setTimeout(r, 100)); }
    throw new Error('UI element did not appear'); };
  try { return await flow(); } catch (e) {
    const labels = [...document.querySelectorAll('button, a, h1, h2')].map(e => e.tagName + ':' + e.textContent.trim().slice(0, 24)).slice(0, 25);
    return 'failed:' + step + ' ' + innerWidth + 'x' + innerHeight + ' ' + JSON.stringify(labels) + ' root=' +
      (document.querySelector('#root') || {innerHTML: ''}).innerHTML.length + ' text=' + JSON.stringify(document.body.innerText.slice(0, 160)) + ' url=' + location.pathname + ' errors=' + JSON.stringify(errors); }
  async function flow() {
  step = 'login-form';
  addEventListener('error', e => errors.push(String(e.message).slice(0, 300)));
  addEventListener('unhandledrejection', e => errors.push('rejection:' + String(e.reason).slice(0, 300)));
  const set = (element, value) => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(element, value);
    element.dispatchEvent(new Event('input', {bubbles: true})); };
  const email = await wait(() => document.querySelector('#login-email'));
  set(email, inputs.ui_email);
  const password = document.querySelector('#login-password');
  set(password, inputs.ui_password);
  step = 'submit';
  password.form.requestSubmit();
  step = 'session-token';
  await wait(() => localStorage.getItem('flowpilot_token'));
  const find = text => [...document.querySelectorAll('button, a')].find(e => e.textContent.trim() === text);
  const button = async text => {
    try { return await wait(() => find(text), 2000); } catch (e) {}
    // Narrow viewports render the sidebar off-canvas; open it once, like a user.
    const trigger = document.querySelector('[data-sidebar="trigger"]');
    if (trigger) trigger.click();
    return await wait(() => find(text), 6000);
  };
  step = 'nav-tasks';
  (await button('Browser tasks')).click();
  step = 'task-profiles';
  await wait(() => document.body.innerText.includes('Owned browser-task profiles') && document.body.innerText.includes(inputs.ui_label));
  step = 'nav-keys';
  (await button('API Keys')).click();
  step = 'key-list';
  await wait(() => document.body.innerText.includes(inputs.ui_key_name));
  step = 'no-plaintext-key';
  if (/fp_live_[a-f0-9]{32}/.test(document.body.innerText)) throw new Error('plaintext key rendered');
  step = 'back-to-tasks';
  (await button('Browser tasks')).click();
  await wait(() => document.body.innerText.includes(inputs.ui_label));
  return true;
  }
}'''


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

    # Explicit test inputs; only the separately owned expiry server shortens waits.
    human_wait = int(os.environ.get('TASK_ACCEPTANCE_HUMAN_WAIT', '90'))
    ttl = int(os.environ.get('TASK_ACCEPTANCE_ASSISTANCE_TTL', '30'))
    settings = TaskSettings(enabled=True, state_dir=Path(os.environ['PROFILES_DIR']) / 'browser-tasks', sandbox_pids=1024,
                            cancellation_seconds=2, shutdown_seconds=30, task_timeout_seconds=180,
                            human_wait_seconds=human_wait, assistance_ttl_seconds=ttl, api_requests_per_minute=10000,
                            readiness_timeout_seconds=15)
    executor = ReleaseAudit(Executor(settings, runtime_factory=runtime_factory), os.environ['TASK_ACCEPTANCE_RUN_ID'])
    control = ControlAudit(create_control(settings), os.environ['TASK_ACCEPTANCE_RUN_ID'])
    audit_holder['audit'] = executor
    audit_holder['control'] = control
    audit_holder['settings'] = settings
    bindings = TaskBindings(control, executor, create_assistance(settings), create_observation(settings))
    @asynccontextmanager
    async def lifespan(app):
        audit_holder['loop'] = asyncio.get_running_loop()
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
    compose_legacy(app, main)
    return app


def compose_legacy(app, main):
    """Reuse the actual legacy flow/run route objects and this revision's SPA.

    The legacy lifespan (global Docker orphan sweep) is never run. Its task API
    mount (disabled settings) is excluded so the enabled host above stays the
    only task authority; the SPA catch-all is mounted last, as in main.
    """
    from fastapi.routing import APIRoute
    from starlette.staticfiles import StaticFiles
    from backend.task_contracts import API_PREFIX
    present = {(route.path, frozenset(route.methods)) for route in app.router.routes if isinstance(route, APIRoute)}
    for route in main.app.router.routes:
        if (isinstance(route, APIRoute) and not route.path.startswith(API_PREFIX) and route.path != '/health/browser-tasks'
                and (route.path, frozenset(route.methods)) not in present):
            app.router.routes.append(route)
    frontend = os.environ.get('TASK_ACCEPTANCE_FRONTEND_DIR')
    if frontend and Path(frontend, 'index.html').is_file():
        app.mount('/', StaticFiles(directory=frontend, html=True), name='frontend')


def serve(port, stop=None, holder=None, env=None, host='127.0.0.1'):
    os.environ.update(env or {})  # Spawned children only; read before backend import.
    if threading.current_thread() is threading.main_thread():
        import faulthandler
        import signal
        faulthandler.register(signal.SIGUSR1, all_threads=True)  # Owned-child hang triage: stack locations only.
    logging.disable(logging.CRITICAL)
    import uvicorn
    holder = {} if holder is None else holder
    # Mirror ops_start.sh: proxy headers trusted only from loopback (the Caddy peer).
    server = uvicorn.Server(uvicorn.Config(build_app(holder), host=host, port=port, proxy_headers=True,
                                         forwarded_allow_ips='127.0.0.1', log_level='critical', access_log=False))
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


def selected(group):
    """Development-only subset selection; unselected gates simply stay blocked."""
    chosen = os.environ.get('TASK_ACCEPTANCE_SCENARIOS', '')
    return not chosen or group in chosen.split(',')


def run_inside():
    from . import assembled_gates as gates
    logging.disable(logging.CRITICAL)
    run = os.environ['TASK_ACCEPTANCE_RUN_ID']
    evidence = Evidence('assembled_docker_deterministic_model')
    secret_values = json.loads(os.environ['TASK_ACCEPTANCE_SECRETS_JSON'])
    holder = {}
    base = 'http://127.0.0.1:%d' % MAIN_PORT
    # Bind the disposable bridge too: the task browser loads the composed SPA.
    thread = threading.Thread(target=serve, args=(MAIN_PORT, None, holder, None, '0.0.0.0'), daemon=True)
    thread.start()
    canaries = [secret_values['otp_main']]
    stage = 'startup'
    ctx = None
    with synthetic_site('/state/site', run + '-backend', '0.0.0.0') as site:
        try:
            wait_ready(base)
            stage = 'account_onboarding'
            owner, dashboard, key_id = onboard(base, run + '-owner', password=secret_values['pw_owner'])
            foreign, foreign_dashboard, _ = onboard(base, run + '-foreign', password=secret_values['pw_foreign'])
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
            ctx = gates.Ctx(evidence=evidence, base=base, run=run, site=site, holder=holder, owner=owner, dashboard=dashboard,
                            foreign=foreign, foreign_dashboard=foreign_dashboard, owned_profiles=owned_profiles,
                            foreign_profiles=foreign_profiles, secrets=secret_values)
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
            assert followed['state'] == 'completed' and followed['result']['metrics']['reused'] == 1, \
                'follow-up ' + followed['state'] + ' ' + str((followed.get('error') or {}).get('code'))
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
                cleaned = account.wait(clean['task_id'], timeout=120)
                assert cleaned['state'] == 'completed', 'clean ' + cleaned['state'] + ' ' + str((cleaned.get('error') or {}).get('code'))
            evidence.record('real_browser_profile_separation', 'passed', 'same-account second profile and foreign account have no private cookie/localStorage/cart state')
            evidence.requirements['profile_website_isolation']['outcome'] = 'passed'

            if selected('core'):
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

            ctx.seed = {'task_id': receipt['task_id'], 'session_id': receipt['session_id'], 'profile_id': receipt['profile_id'],
                        'view': owner.get(receipt['task_id'])}
            if selected('interactions'):
                # Question first: the approve matrix reuses its bound response key.
                stage = 'question_matrix'
                gates.check(ctx, 'real_question_transient_delivery', lambda: gates.question_matrix(ctx))
                stage = 'confirmation_matrix'
                gates.check(ctx, 'real_confirmation_decline', lambda: gates.confirmation_decline(ctx))
                gates.check(ctx, 'real_confirmation_approve_matrix', lambda: gates.confirmation_approve(ctx))

            if selected('core'):
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
            if selected('atomic'):
                stage = 'atomic_commit'
                gates.check(ctx, 'real_assistance_atomic_commit', lambda: gates.atomic_commit(ctx))
            if selected('cancel'):
                stage = 'cancel_fencing'
                gates.check(ctx, 'real_cancel_fencing', lambda: gates.cancel_fencing(ctx))
            if selected('tls'):
                stage = 'tls_proxy'
                gates.check(ctx, 'real_tls_proxy_assistance', lambda: gates.tls_assistance(ctx))
            if selected('compat'):
                stage = 'compatibility'
                gates.check(ctx, 'real_legacy_flow_api', lambda: gates.legacy_flow_api(ctx))
                gates.check(ctx, 'real_ui_boundary_browser', lambda: gates.ui_boundary(ctx))
            stage = 'result_snapshot'
            ctx.snapshot_views()
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
            evidence.check('real_key_hash_at_rest', lambda: verify_key_hashes(Path(os.environ['PROFILES_DIR']) / 'platform.db', [foreign.token]), 'key_hash_at_rest')
            # Mid-run snapshot; the full post-restart/backup scan follows below.
            scan = scan_artifacts('/state', canaries, max_bytes=4 * 1024 ** 3)
            evidence.record('real_secret_snapshot_scan', 'failed' if scan['findings'] else 'blocked' if scan['gaps'] else 'passed',
                            'actual SQLite/history/state scan including in-memory expansion of profile archives', **scan)
        except Exception as exc:
            import traceback
            traceback.print_exc()  # Private container stderr only; never retained by the host.
            frames = [{'path': frame.filename, 'line': frame.lineno} for frame in traceback.extract_tb(exc.__traceback__)]
            evidence.record('assembled_scenario', 'failed', 'actual assembled scenario stopped; rerun exact launcher for failure boundary',
                            stage=stage, exception_type=type(exc).__name__, traceback_frames=frames[-6:], model_events=MODEL_EVENTS[-24:])
        finally:
            if holder.get('server'):
                holder['server'].should_exit = True
            thread.join(timeout=45)
            if thread.is_alive():
                evidence.record('owned_server_shutdown', 'failed', 'server did not stop within bounded cleanup')
        if not thread.is_alive() and ctx is not None:
            # Every remaining phase owns a fresh composition; the main server is stopped.
            if selected('core'):
                evidence.check('real_crash_after_website_effect', lambda: crash_restart(site, foreign.token, foreign_profiles[1], run), 'crash_after_effect')
            if selected('restart'):
                gates.check(ctx, 'real_question_secret_restart', lambda: gates.question_restart(ctx))
            if selected('expiry'):
                gates.check(ctx, 'real_interaction_expiry', lambda: gates.expiry_server(ctx))
            if selected('ops'):
                gates.check(ctx, 'real_ops_backup_restore_continue', lambda: gates.operations(ctx))
            if selected('scan'):
                gates.check(ctx, 'real_secret_canary_full_scan', lambda: gates.final_scan(ctx))
    for requirement, names in REQUIREMENT_PROOFS.items():
        gates.settle_requirement(evidence, requirement, names)
    Path('/state/assembled_evidence.json').write_text(json.dumps({'checks': evidence.checks, 'requirements': evidence.requirements,
                                                                 'scenario_selection': os.environ.get('TASK_ACCEPTANCE_SCENARIOS') or 'all'}))


# A requirement passes only when EVERY listed actual check passed in this run.
# compatibility is settled host-side, because the ./chat REPL path runs there.
REQUIREMENT_PROOFS = {
    'questions_secrets': ['real_question_transient_delivery', 'real_question_secret_restart', 'real_interaction_expiry',
                          'real_secret_canary_full_scan'],
    'confirmation_decline': ['real_confirmation_decline', 'real_confirmation_approve_matrix', 'real_interaction_expiry'],
    'assistance_tls': ['real_tls_proxy_assistance'],
    'assistance_atomic_commit': ['real_assistance_atomic_commit'],
    'cancel_fencing': ['real_cancel_fencing'],
    'artifact_secret_scan': ['real_secret_snapshot_scan', 'real_secret_canary_full_scan'],
    'operations': ['real_ops_backup_restore_continue'],
}


if __name__ == '__main__':
    # Import the package module so spawned children and gates share one copy.
    from tests.task_acceptance_support import assembled_run as module
    module.run_inside()
