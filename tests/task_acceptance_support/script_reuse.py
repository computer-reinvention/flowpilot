"""`script_reuse_repair` gate: real Chromium, synthetic loopback site, deterministic model boundary.

The Pi model is replaced by a scripted driver that calls the real dispatch; the
repair "model" is a deterministic program generator that reads only the observed
page elements. Everything else (REPL memory, input binding, observation guards,
CDP execution, repair pipeline) is the production code path.

Proves: (a) one saved parameterized reflex reused with different input sets,
(b) actual selector repair after the site's markup changes, persisted and reused,
(c) a partial-effect failure is not double-applied and identical calls are fenced.
"""

import asyncio
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

PAGE = '''<!doctype html><title>Synthetic rail</title><body><main>
<form onsubmit="return false">
<label>From <input id="origin" name="origin"></label><label>To <input id="destination" name="destination"></label>
<label>Date <input id="date" name="date" type="date"></label><label>Passengers <input id="passengers" name="passengers" type="number" min="1"></label>
<button id="{search}" type="button">Search trains</button></form>
<output id="results" data-result></output>
<section><span>Cart</span> <output id="cart-count" data-result>0</output>
<button id="add-fare" type="button">Add fare</button><button id="{extras}" type="button">Confirm extras</button>
<output id="extras" data-result></output></section></main>
<script>
const $ = s => document.querySelector(s);
const effect = async (kind, detail) => (await (await fetch('/effect?kind=' + kind + '&detail=' + encodeURIComponent(detail || ''), {{method: 'POST'}})).json()).count;
$('#{search}').onclick = async () => {{
  await effect('search', [$('#origin').value, $('#destination').value, $('#date').value, $('#passengers').value].join('|'));
  $('#results').textContent = 'Results: ' + $('#origin').value + ' -> ' + $('#destination').value + ' on ' + $('#date').value + ' for ' + $('#passengers').value;
}};
$('#add-fare').onclick = async () => {{ await effect('add'); $('#cart-count').textContent = String(+$('#cart-count').textContent + 1); }};
$('#{extras}').onclick = async () => {{ await effect('extras'); $('#extras').textContent = 'Extras confirmed'; }};
</script></body>'''

MARKUP = {1: {'search': 'search-btn', 'extras': 'confirm-extras'},
          2: {'search': 'find-trains', 'extras': 'extras-confirm'}}

SEARCH_SCRIPT = r'''async (inputs) => {
  const q = s => document.querySelector(s);
  const els = {origin: q('#origin'), destination: q('#destination'), date: q('#date'), passengers: q('#passengers'), go: q('#search-btn')};
  // Resolve every control BEFORE changing the page (reusable workflow contract).
  for (const [key, el] of Object.entries(els)) if (!el) throw new Error('Missing control ' + key);
  const set = (el, v) => { el.value = String(v); el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); };
  for (const key of ['origin', 'destination', 'date', 'passengers']) set(els[key], inputs[key]);
  els.go.click();
  const want = 'Results: ' + inputs.origin + ' -> ' + inputs.destination + ' on ' + inputs.date + ' for ' + inputs.passengers;
  for (let i = 0; i < 150; i++) {
    if (q('#results').textContent === want) return {success: true, data: q('#results').textContent};
    await new Promise(r => setTimeout(r, 20));
  }
  throw new Error('Results did not match the requested search');
}'''

SEARCH_INPUTS = [
    {'name': 'origin', 'type': 'string', 'description': 'Departure city'},
    {'name': 'destination', 'type': 'string', 'description': 'Arrival city'},
    {'name': 'date', 'type': 'string', 'description': 'Absolute date YYYY-MM-DD'},
    {'name': 'passengers', 'type': 'integer', 'default': '1'},
]

ADD_SCRIPT = r'''async (inputs) => {
  const q = s => document.querySelector(s);
  const add = q('#add-fare');
  if (!add) throw new Error('Missing add control');
  const before = +q('#cart-count').textContent;
  add.click();
  for (let i = 0; i < 150 && +q('#cart-count').textContent === before; i++) await new Promise(r => setTimeout(r, 20));
  // Second step looked up only after the first effect: a markup change here leaves a partial effect.
  const confirm = q('#confirm-extras');
  if (!confirm) throw new Error('Missing extras control');
  confirm.click();
  for (let i = 0; i < 150; i++) {
    if (q('#extras').textContent === 'Extras confirmed') return {success: true, fare_class: inputs.fare_class};
    await new Promise(r => setTimeout(r, 20));
  }
  throw new Error('Extras not confirmed');
}'''


@asynccontextmanager
async def rail_site():
    state = {'version': 1, 'effects': {'search': 0, 'add': 0, 'extras': 0}, 'searches': []}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body, kind):
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            ids = MARKUP[state['version']]
            self._send(PAGE.format(**ids).encode(), 'text/html; charset=utf-8')

        def do_POST(self):
            query = parse_qs(urlparse(self.path).query)
            kind = query.get('kind', [''])[0]
            with lock:
                if kind not in state['effects']:
                    self.send_response(404); self.end_headers(); return
                state['effects'][kind] += 1
                if kind == 'search':
                    state['searches'].append(query.get('detail', [''])[0])
                count = state['effects'][kind]
            self._send(json.dumps({'count': count}).encode(), 'application/json')

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name='flowpilot-r2-rail', daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/', state
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)


@asynccontextmanager
async def rail_browser():
    from browser_use import Browser
    from playwright.async_api import async_playwright
    os.environ.setdefault('ANONYMIZED_TELEMETRY', 'false')
    async with rail_site() as (url, state):
        async with async_playwright() as playwright:
            with tempfile.TemporaryDirectory(prefix='flowpilot-r2-') as profile:
                browser = Browser(headless=True, executable_path=playwright.chromium.executable_path,
                                  user_data_dir=profile, enable_default_extensions=False,
                                  keep_alive=False, args=['--disable-background-networking'])
                try:
                    await browser.start()
                    page = await browser.must_get_current_page()
                    await page.goto(url)
                    yield browser, url, state
                finally:
                    await browser.kill()


def selector_repair_generator(calls):
    """Deterministic stand-in for the repair model: maps each missing v1 id to the
    v2 control it can see in the observed elements. No provider is contacted."""
    async def generate(llm, request):
        calls.append(request['operation'])
        script = request['broken_script']
        for old, new in zip(MARKUP[1].values(), MARKUP[2].values()):
            if '#' + old in script:
                assert new in request['elements'], 'repair must be grounded in the observed page'
                script = script.replace('#' + old, '#' + new)
        return script
    return generate


async def prove_script_reuse_repair(state_dir, *, hosted=False):
    """Run the gate; return sanitized counters. Raises AssertionError on any violation."""
    from backend import llm as llm_module, pi_browser, repl, runner
    from backend.observation import create_observation
    from backend.task_contracts import ErrorCode, TaskFault
    from backend.task_settings import TaskSettings

    policy = create_observation(TaskSettings(readiness_window_seconds=0.08, readiness_timeout_seconds=3))
    repairs = []
    originals = (pi_browser.run_pi, runner.generate_program, llm_module.get_llm)
    runner.generate_program = selector_repair_generator(repairs)
    llm_module.get_llm = lambda role='agent': None
    evidence = {}
    try:
        async with rail_browser() as (browser, url, site):
            options = dict(hosted=True, handoff_factory=object, on_question=AsyncMock()) if hosted else {}
            session = repl.BrowserSession('r2-gate', state_dir, verbose=False, observation_policy=policy, **options)
            session.browser = browser
            try:
                A = {'origin': 'Berlin', 'destination': 'London', 'date': '2026-11-02', 'passengers': 2}
                B = {'origin': 'Paris', 'destination': 'Amsterdam', 'date': '2026-11-05'}
                C = {'origin': 'Munich', 'destination': 'Vienna', 'date': '2026-12-01', 'passengers': 3}
                D = {'origin': 'Hamburg', 'destination': 'Copenhagen', 'date': '2026-12-09'}

                # Ask 1: save once, reuse with two different input sets.
                async def first(task, system, tools, dispatch, **kwargs):
                    await dispatch('observe', {})
                    saved = await dispatch('save_reflex', {'name': 'search_trains', 'description': 'Search trains from the search form',
                                                           'script': SEARCH_SCRIPT, 'inputs': SEARCH_INPUTS})
                    assert not saved.get('error'), saved
                    for values in (A, B):
                        await dispatch('observe', {})
                        result = await dispatch('run_reflex', {'name': 'search_trains', 'inputs': values})
                        assert not result.get('error'), 'parameterized reuse failed'
                    # Missing inputs: typed error, nothing runs, nothing repaired.
                    rejected = await dispatch('run_reflex', {'name': 'search_trains', 'inputs': {'origin': 'Berlin'}})
                    assert rejected['error_code'] == 'invalid_input' and rejected['missing'] == ['destination', 'date']
                    # Identical stage + identical inputs is the same action: fenced.
                    await dispatch('observe', {})
                    try:
                        await dispatch('run_reflex', {'name': 'search_trains', 'inputs': B})
                        raise AssertionError('identical reflex call was not fenced')
                    except TaskFault as fault:
                        assert fault.error.code == ErrorCode.NO_PROGRESS
                    await dispatch('complete', {'verify': "async()=>document.querySelector('#results').textContent.includes('Amsterdam')",
                                                'summary': 'Two searches'})
                pi_browser.run_pi = first
                one = await session.ask('Find trains Berlin to London on 2 Nov for 2, then Paris to Amsterdam on 5 Nov')
                assert site['searches'] == ['Berlin|London|2026-11-02|2', 'Paris|Amsterdam|2026-11-05|1'], 'searches'
                assert one['metrics']['reused'] == 2 and one['metrics']['input_errors'] == 1
                assert repairs == [] and one['metrics'].get('repair_attempts') is None

                # Markup changes (site redeploy); same URL serves renamed controls.
                site['version'] = 2
                catalog = {}

                async def second(task, system, tools, dispatch, **kwargs):
                    catalog['inputs'] = json.loads(task.split('Context JSON:\n', 1)[1].rsplit('\nInput values:', 1)[0])['available_scripts'][0]['inputs']
                    await dispatch('navigate', {'url': url})
                    await dispatch('observe', {})
                    result = await dispatch('run_reflex', {'name': 'search_trains', 'inputs': C})
                    catalog['c_error'] = bool(result.get('error'))
                    if hosted:
                        # Hosted tasks never auto-repair/replay; the model repairs explicitly.
                        assert 'Do not replay this stage automatically' in result['error']
                        repaired = SEARCH_SCRIPT.replace('#search-btn', '#find-trains')
                        await dispatch('observe', {})
                        # One-off execute carries this request's values; the saved script stays parameterized.
                        one_off = f'async (inputs, entryUrl) => ({repaired})({json.dumps(C)}, entryUrl)'
                        done = await dispatch('execute', {'script': one_off, 'precondition': 'async()=>!!document.querySelector("#find-trains")',
                                                          'postcondition': "async()=>document.querySelector('#results').textContent.includes('Vienna')"})
                        assert done['success']
                        await dispatch('save_reflex', {'name': 'search_trains', 'description': 'Search trains (repaired)',
                                                       'script': repaired, 'inputs': SEARCH_INPUTS})
                    await dispatch('observe', {})
                    again = await dispatch('run_reflex', {'name': 'search_trains', 'inputs': D})
                    assert not again.get('error'), 'repaired script did not reuse with new inputs'
                    await dispatch('complete', {'verify': "async()=>document.querySelector('#results').textContent.includes('Copenhagen')",
                                                'summary': 'Repaired and reused'})
                pi_browser.run_pi = second
                two = await session.ask('Trains Munich to Vienna on 1 Dec for 3, then Hamburg to Copenhagen on 9 Dec')
                assert catalog['inputs'][0]['name'] == 'origin', 'catalog must expose declared inputs'
                assert site['searches'][2:] == ['Munich|Vienna|2026-12-01|3', 'Hamburg|Copenhagen|2026-12-09|1'], 'repair searches'
                row = session.store.db.execute("SELECT script FROM reflexes WHERE name='search_trains'").fetchone()
                assert '#find-trains' in row['script'] and '#search-btn' not in row['script'], 'repair not persisted'
                if hosted:
                    assert repairs == [] and catalog['c_error']
                else:
                    assert repairs == ['repair the remaining work of this failed stage'] and not catalog['c_error']
                    assert two['metrics']['repaired'] == 1

                # Partial effect: first step lands, second step's selector changed.
                async def third(task, system, tools, dispatch, **kwargs):
                    await dispatch('observe', {})
                    await dispatch('save_reflex', {'name': 'add_fare', 'description': 'Add fare and confirm extras',
                                                   'script': ADD_SCRIPT, 'inputs': [{'name': 'fare_class', 'type': 'string', 'enum': ['saver', 'flex']}]})
                    await dispatch('observe', {})
                    failed = await dispatch('run_reflex', {'name': 'add_fare', 'inputs': {'fare_class': 'saver'}})
                    assert failed.get('error'), 'partial-effect failure was automatically repaired/replayed'
                    assert runner.PARTIAL_EFFECTS in failed['error'] and 'Do not replay' in failed['error']
                    await dispatch('observe', {})
                    try:
                        await dispatch('run_reflex', {'name': 'add_fare', 'inputs': {'fare_class': 'saver'}})
                        raise AssertionError('partial stage was replayed')
                    except TaskFault as fault:
                        assert fault.error.code == ErrorCode.NO_PROGRESS
                    # Remaining work only, checked; then save the corrected whole stage for future tasks.
                    done = await dispatch('execute', {'script': "async()=>{document.querySelector('#extras-confirm').click(); return true;}",
                                                      'precondition': "async()=>document.querySelector('#cart-count').textContent==='1' && document.querySelector('#extras').textContent===''",
                                                      'postcondition': "async()=>{for(let i=0;i<150;i++){if(document.querySelector('#extras').textContent==='Extras confirmed')return true;await new Promise(r=>setTimeout(r,20));}return false;}"})
                    assert done['success']
                    saved = await dispatch('save_reflex', {'name': 'add_fare', 'description': 'Add fare and confirm extras (repaired)',
                                                           'script': ADD_SCRIPT.replace('#confirm-extras', '#extras-confirm'),
                                                           'inputs': [{'name': 'fare_class', 'type': 'string', 'enum': ['saver', 'flex']}]})
                    assert not saved.get('error')
                    await dispatch('complete', {'verify': "async()=>document.querySelector('#cart-count').textContent==='1'",
                                                'summary': 'One fare with extras'})
                pi_browser.run_pi = third
                repairs_before = len(repairs)
                three = await session.ask('Add a saver fare with extras')
                assert site['effects']['add'] == 1, 'partial effect was double-applied'
                assert site['effects']['extras'] == 1
                assert len(repairs) == repairs_before, 'partial effect entered automatic repair'
                assert three['metrics']['partial_effects'] == 1
                evidence = {'searches': site['effects']['search'], 'adds': site['effects']['add'],
                            'extras': site['effects']['extras'], 'automatic_repairs': len(repairs),
                            'input_errors': one['metrics']['input_errors'], 'hosted': hosted}
            finally:
                session.store.close()
    finally:
        pi_browser.run_pi, runner.generate_program, llm_module.get_llm = originals
    return evidence


def check_script_reuse_repair(state_dir):
    """Synchronous Evidence.check action for harness wiring."""
    local = asyncio.run(prove_script_reuse_repair(os.path.join(state_dir, 'local')))
    hosted = asyncio.run(prove_script_reuse_repair(os.path.join(state_dir, 'hosted'), hosted=True))
    return {'local_searches': local['searches'], 'local_automatic_repairs': local['automatic_repairs'],
            'hosted_automatic_repairs': hosted['automatic_repairs'], 'partial_adds': [local['adds'], hosted['adds']]}
