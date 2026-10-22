"""Agent session core: one user's browser, memory and saved scripts around the Pi driver.

Clients (the hosted task runtime, the local REPL, scripts) own all user I/O and
reach it through the on_question / human_help / on_handoff ports.
"""

import asyncio
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlparse

from browser_use import ActionResult, Browser

from .pi_browser import drive_browser, fill_template
from .replay import RunControl
from .reflex_inputs import ReflexInputError, bind_inputs, catalog_entry, normalize_fields
from .runner import PARTIAL_EFFECTS, _execute_reflex

# Agents do not reliably mark codes as secret; treat these labels as secret too.
SECRET_FIELD = re.compile(r'otp|one[- ]?time|passw|passcode|\bpin\b|cvv|cvc|2fa|mfa|token|secret|'
                          r'(sms|auth|login|verification|security|access)[ _-]?code|^code$', re.I)


def written_values(source, inputs, secrets=()):
    """Input values that appear literally in source. Secrets count at any length."""
    from .pi_browser import hardcoded_values
    from .tasks.memory import input_atoms
    atoms = list(input_atoms(inputs))
    return hardcoded_values(source, dict(enumerate(atoms))) + [
        '[secret]' for value in secrets if value and value in source]


def host_of(url):
    host = (urlparse(url).hostname or '').lower()
    return host


def is_secret_field(field):
    kind = field.get('kind') or field.get('type')
    return kind == 'secret' or bool(SECRET_FIELD.search(field['name']) or SECRET_FIELD.search(field.get('label') or ''))


class UserStore:
    def __init__(self, root: Path, user: str):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", user):
            raise ValueError("User must be 1–64 letters, digits, underscores or hyphens")
        self.path = root.resolve() / user
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (self.path / 'session.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise ValueError(f"User {user} already has an active session") from None
        self.db = sqlite3.connect(self.path / 'memory.sqlite')
        os.chmod(self.path / 'memory.sqlite', 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS reflexes (
                domain TEXT, name TEXT, description TEXT, script TEXT,
                successes INTEGER DEFAULT 0, failures INTEGER DEFAULT 0,
                PRIMARY KEY(domain,name));
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY, task TEXT, inputs TEXT, result TEXT,
                metrics TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        ''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(reflexes)')}
        if 'pause_after' not in columns:
            self.db.execute('ALTER TABLE reflexes ADD COLUMN pause_after TEXT')
        if 'tags' not in columns:
            self.db.execute("ALTER TABLE reflexes ADD COLUMN tags TEXT DEFAULT '[]'")
        if 'inputs' not in columns:
            # JSON declared-input schema; NULL for legacy undeclared reflexes.
            self.db.execute('ALTER TABLE reflexes ADD COLUMN inputs TEXT')
        # Health: a new script starts as a candidate, one successful replay makes it
        # active, consecutive failures make it broken (not offered for lookup). A new
        # script for an active reflex waits in `candidate` instead of replacing it.
        for column, ddl in (('steps', 'TEXT'), ('status', "TEXT DEFAULT 'active'"),
                            ('consecutive_failures', 'INTEGER DEFAULT 0'), ('candidate', 'TEXT'),
                            ('updated_at', 'TEXT')):
            if column not in columns:
                self.db.execute(f'ALTER TABLE reflexes ADD COLUMN {column} {ddl}')
        self.db.commit()

    BROKEN_AFTER = 2  # consecutive failed replays

    def _catalog(self, where, params, limit):
        rows = self.db.execute(
            'SELECT domain,name,description,successes,failures,status,inputs,script,steps,candidate FROM reflexes '
            f"WHERE (status != 'broken' OR candidate IS NOT NULL) {where} "
            "ORDER BY status = 'active' DESC, successes - failures DESC, updated_at DESC LIMIT ?", (*params, limit))
        result = []
        for row in rows:
            item = dict(row)
            fields, script, steps, candidate = item.pop('inputs'), item.pop('script'), item.pop('steps'), item.pop('candidate')
            if item['status'] == 'broken':
                item['status'] = 'candidate'  # only the waiting candidate is usable
                fields = json.loads(candidate).get('inputs')
                fields = json.dumps(fields) if fields is not None else None
            item.update(catalog_entry(json.loads(fields) if fields else None, script))
            if steps:
                steps = json.loads(steps)
                item['steps'] = len(steps)
                start = next((step['url'] for step in steps if step['type'] == 'navigate'), None)
                if start:
                    item['start_url'] = start
            result.append(item)
        return result

    def scripts(self):
        """Catalog for the model: usable reflexes, active and proven first."""
        return self._catalog('', (), 50)

    def for_site(self, site):
        """Usable reflexes for one host, offered when the agent arrives there."""
        return self._catalog('AND domain = ?', (site,), 20)

    def reflex(self, site, name):
        row = self.db.execute('SELECT rowid AS id, * FROM reflexes WHERE domain=? AND name=?', (site, name)).fetchone()
        if row is None:
            # A recorded sequence opens its own start page, so it can run from anywhere.
            rows = self.db.execute('SELECT rowid AS id, * FROM reflexes WHERE name=? AND steps IS NOT NULL', (name,)).fetchall()
            row = rows[0] if len(rows) == 1 else None
        return row

    @staticmethod
    def usable(row):
        """(version, which) to replay: the main script unless broken, else a waiting candidate."""
        if row['status'] != 'broken':
            return {k: row[k] for k in ('script', 'steps', 'inputs', 'pause_after', 'description')}, 'main'
        if row['candidate']:
            candidate = json.loads(row['candidate'])
            return {**candidate, 'inputs': json.dumps(candidate['inputs']) if candidate.get('inputs') is not None else None,
                    'steps': json.dumps(candidate['steps']) if candidate.get('steps') else None}, 'candidate'
        return None, None

    def save_reflex(self, site, name, description, script, steps, fields, pause_after, tags):
        version = {'description': description, 'script': script, 'steps': steps, 'inputs': fields,
                   'pause_after': pause_after, 'tags': tags or []}
        steps_json = json.dumps(steps) if steps else None
        row = self.db.execute('SELECT status, script, steps FROM reflexes WHERE domain=? AND name=?', (site, name)).fetchone()
        if row and (row['script'], row['steps']) == (script, steps_json):
            self.db.execute("UPDATE reflexes SET description=?, inputs=?, updated_at=datetime('now') WHERE domain=? AND name=?",
                            (description, None if fields is None else json.dumps(fields), site, name))
            self.db.commit()
            return f'Unchanged {site}/{name}; its record is kept'
        if row and row['status'] == 'active':
            self.db.execute("UPDATE reflexes SET candidate=?, updated_at=datetime('now') WHERE domain=? AND name=?",
                            (json.dumps(version), site, name))
            self.db.commit()
            return (f'Saved a candidate for {site}/{name}; the active script stays in use until it fails, '
                    'then the candidate is tried and replaces it after one successful replay')
        self.db.execute('''INSERT INTO reflexes(domain,name,description,script,pause_after,tags,inputs,steps,status,
                successes,failures,consecutive_failures,candidate,updated_at) VALUES(?,?,?,?,?,?,?,?,'candidate',0,0,0,NULL,datetime('now'))
            ON CONFLICT(domain,name) DO UPDATE SET description=excluded.description,script=excluded.script,
                pause_after=excluded.pause_after,tags=excluded.tags,inputs=excluded.inputs,steps=excluded.steps,
                status='candidate',successes=0,failures=0,consecutive_failures=0,candidate=NULL,updated_at=excluded.updated_at''',
            (site, name, description, script, pause_after, json.dumps(tags or []),
             None if fields is None else json.dumps(fields), steps_json))
        self.db.commit()
        return f'Saved {site}/{name} as a candidate; it becomes active after one successful replay'

    def record_replay(self, row_id, which, ok):
        """Apply a replay outcome to the reflex's health."""
        row = self.db.execute('SELECT * FROM reflexes WHERE rowid=?', (row_id,)).fetchone()
        if row is None:
            return
        if which == 'candidate':
            if ok:
                candidate = json.loads(row['candidate'])
                self.db.execute('''UPDATE reflexes SET description=?,script=?,steps=?,inputs=?,pause_after=?,tags=?,
                    status='active',successes=1,failures=0,consecutive_failures=0,candidate=NULL,updated_at=datetime('now')
                    WHERE rowid=?''', (candidate['description'], candidate['script'],
                                       json.dumps(candidate['steps']) if candidate.get('steps') else None,
                                       None if candidate.get('inputs') is None else json.dumps(candidate['inputs']),
                                       candidate.get('pause_after'), json.dumps(candidate.get('tags') or []), row_id))
            else:
                self.db.execute("UPDATE reflexes SET candidate=NULL, updated_at=datetime('now') WHERE rowid=?", (row_id,))
        elif ok:
            self.db.execute('''UPDATE reflexes SET successes=successes+1, consecutive_failures=0, status='active',
                updated_at=datetime('now') WHERE rowid=?''', (row_id,))
        else:
            self.db.execute('''UPDATE reflexes SET failures=failures+1, consecutive_failures=consecutive_failures+1,
                status=CASE WHEN consecutive_failures+1 >= ? THEN 'broken' ELSE status END, updated_at=datetime('now')
                WHERE rowid=?''', (self.BROKEN_AFTER, row_id))
        self.db.commit()

    def history(self, limit=6):
        return [dict(r) for r in self.db.execute('SELECT task,inputs,result,metrics FROM turns ORDER BY id DESC LIMIT ?', (limit,))][::-1]

    def close(self):
        self.db.close()
        self.lock.close()


class BrowserSession:
    """One user's agent session. Browser/profile/memory are private to `user`.

    User IDs select storage namespaces; authentication belongs to the client
    (hosted runtime passes a ProfileStore keyed by account and profile).
    """
    def __init__(self, user, state_dir=Path('.repl-state'), headless=True, human_help=None, verbose=False,
                 on_handoff=None, on_question=None, browser_mode='sandbox', *,
                 handoff_factory=None, sandbox_factory=None, browser_factory=None, observation_policy=None,
                 store=None, hosted=False, on_progress=None):
        self.user = user
        self.store = store if store is not None else UserStore(Path(state_dir), user)
        self.hosted = hosted
        if hosted and (handoff_factory is None or on_question is None or observation_policy is None):
            self.store.close()
            raise ValueError('Hosted sessions require interaction and observation ports')
        self.headless = headless
        self.human_callback = human_help
        self.verbose = verbose
        self.browser = None
        self.playwright = None
        self.context = None
        self.cdp_url = None
        self.cdp_websocket_url = None
        self.handoff = None
        self.on_handoff = on_handoff
        self.on_question = on_question
        self.on_progress = on_progress
        self.handoff_factory = handoff_factory
        self.sandbox_factory = sandbox_factory
        self.browser_factory = browser_factory
        self.observation_policy = observation_policy
        self._asking = False
        if browser_mode not in ('sandbox', 'native'):
            self.store.close()
            raise ValueError('browser_mode must be sandbox or native')
        self.browser_mode = browser_mode
        self.sandbox = None
        self.remote_browser = None

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        try:
            # Own the persistent context ourselves: browser-use copies Chrome
            # profiles to temporary directories when launching them directly.
            self.playwright = await async_playwright().start()
            if self.browser_mode == 'sandbox':
                from .repl_sandbox import ChatSandbox
                self.sandbox = (self.sandbox_factory or ChatSandbox)(self.store.path, self.user)
                self.cdp_url, self.cdp_websocket_url = await self.sandbox.start()
                self.remote_browser = await self.playwright.chromium.connect_over_cdp(self.cdp_websocket_url)
                self.context = self.remote_browser.contexts[0]
            else:
                profile = self.store.path / 'browser'
                self.context = await self.playwright.chromium.launch_persistent_context(
                    str(profile), headless=self.headless,
                    args=['--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1'])
                port, websocket_path = (profile / 'DevToolsActivePort').read_text().splitlines()[:2]
                self.cdp_url = f'http://127.0.0.1:{port}'
                self.cdp_websocket_url = f'ws://127.0.0.1:{port}{websocket_path}'
            # Match the sandbox window (1280x800); browser-use otherwise forces 1920x1080,
            # which costs renderer memory and raster time on every page.
            self.browser = (self.browser_factory or Browser)(cdp_url=self.cdp_websocket_url, keep_alive=True,
                                                           viewport={'width': 1280, 'height': 800})
            await self.browser.start()
            from .handoff import HandoffServer
            self.handoff = (self.handoff_factory or HandoffServer)(self.context, self.cdp_websocket_url, self.user, self.on_handoff)
            await self.handoff.start()
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *args):
        try:
            if self.handoff:
                await self.handoff.close()
            if self.browser:
                await self.browser.stop()
        finally:
            try:
                if self.context and not self.sandbox:
                    await self.context.close()
                if self.remote_browser and self.remote_browser.is_connected():
                    from playwright.async_api import Error as PlaywrightError
                    client = await self.remote_browser.new_browser_cdp_session()
                    try:
                        # Browser.close flushes profile storage; SIGTERM alone
                        # can lose recent local-storage writes. REPL sandboxes
                        # disable Supervisor restart for this intentional exit.
                        await client.send('Browser.close')
                    except PlaywrightError:
                        if self.remote_browser.is_connected():
                            raise
                if self.playwright:
                    await self.playwright.stop()
            finally:
                try:
                    if self.sandbox:
                        await self.sandbox.close()
                finally:
                    self.store.close()

    async def ask(self, request: str, inputs=None):
        if not self.browser:
            raise RuntimeError('Use async with BrowserSession(...) before ask()')
        if self._asking:
            raise RuntimeError('This session already has an active request')
        inputs = {} if inputs is None else inputs
        task_inputs = inputs  # callbacks below take their own `inputs` arguments
        stored_inputs = json.dumps(inputs)
        started = time.monotonic()
        metrics = {'model_turns': 0, 'tool_calls': 0, 'reused': 0, 'saved': 0,
                   'replay_failures': 0, 'human_seconds': 0.0}
        self.last_metrics = metrics  # finalized even when the request is cancelled
        ask_task = asyncio.current_task()
        human_cancelled = False
        private_inputs = False
        secrets = []

        async def handoff(*args, **kwargs):
            from .handoff import HandoffCancelled
            nonlocal human_cancelled
            try:
                return await self.handoff.request(*args, **kwargs)
            except HandoffCancelled:
                human_cancelled = True
                # Tool dispatch runs in a child task. Cancel the owning ask, not
                # merely the tool, so Pi cannot recover and continue acting.
                ask_task.cancel()
                raise

        def event(e):
            if e['type'] == 'metric':
                metrics['model_turns'] += 1
            else:
                metrics['tool_calls'] += 1
                if self.verbose:
                    print(f"  · {e['name']}", flush=True)

        async def domain():
            page = await self.browser.must_get_current_page()
            parsed = urlparse(await page.get_url())
            if self.hosted:
                host = (parsed.hostname or '').lower()
                if ':' in host:
                    host = '[' + host + ']'
                return host + (f':{parsed.port}' if parsed.port else '')
            return parsed.netloc.lower()

        async def save(name, description, script='', tags=None, pause_after_auth=False, inputs=None, steps=None):
            site = await domain()
            if steps:
                # A recorded sequence belongs to the site it starts on.
                start = next((step['url'] for step in steps if step['type'] == 'navigate'), None)
                site = host_of(start) if start else site
            if not site:
                return ActionResult(error='Navigate to the website before saving its script')
            if steps:
                if not isinstance(steps, list) or len(steps) > 40 or any(
                        step.get('type') not in ('navigate', 'script') for step in steps):
                    return ActionResult(error='Expected at most 40 navigate/script steps')
                if any(step['type'] == 'script' and not step['script'].lstrip().startswith('async') for step in steps):
                    return ActionResult(error='Every script step must be an async arrow function')
            elif len(script) > 60000 or not script.lstrip().startswith('async'):
                return ActionResult(error='Expected a bounded async arrow function')
            if len(json.dumps(steps or [])) > 200000:
                return ActionResult(error='Recorded steps exceed persistence bounds')
            try:
                fields = normalize_fields(inputs)
            except (TypeError, ValueError) as exc:
                return ActionResult(error=f'Invalid declared inputs: {exc}')
            # A saved script is a sequence of steps with placeholders: every task or
            # interaction value must be read from inputs, never written into it.
            literal = written_values(json.dumps([name, description, script, steps, tags, fields]), task_inputs, secrets)
            if literal:
                return ActionResult(error='Parameterize input values before saving scripts: read '
                                          f'{", ".join(literal)} from inputs.<key>')
            if self.hosted:
                if len(name) > 128 or len(description) > 2000 or len(tags or []) > 32 or len(json.dumps(tags)) > 4000:
                    return ActionResult(error='Script metadata exceeds persistence bounds')
            message = self.store.save_reflex(site, name, description, script, steps, fields,
                                             'auth' if pause_after_auth else None, tags)
            if self.hosted:
                self.store.db.execute('DELETE FROM reflexes WHERE rowid NOT IN (SELECT rowid FROM reflexes ORDER BY rowid DESC LIMIT 100)')
                self.store.db.commit()
            metrics['saved'] += 1
            return ActionResult(extracted_content=message)

        def bound_inputs(name, version, call_inputs):
            # Declared reflexes receive exactly their declared inputs: call values,
            # then task/interaction values, then documented defaults.
            return bind_inputs(name, json.loads(version['inputs']) if version['inputs'] else None, call_inputs, task_inputs)

        async def resolve(name):
            row = self.store.reflex(await domain(), name)
            if not row:
                return None, None, None
            version, which = self.store.usable(row)
            return row, version, which

        async def bind(name, call_inputs=None):
            row, version, _ = await resolve(name)
            if not version:
                # Unknown or broken script: replay reports it; nothing to validate here.
                return call_inputs or {}
            try:
                return bound_inputs(name, version, call_inputs)
            except ReflexInputError:
                metrics['input_errors'] = metrics.get('input_errors', 0) + 1
                raise

        async def run_steps(steps, stage_inputs):
            """Replay a recorded sequence one step at a time under the observation policy:
            each step is checked against a fresh snapshot, runs with its own pre- and
            postcondition, and the page must reach steady state before the next."""
            policy = self.observation_policy
            options = {'observation_policy': policy} if policy is not None else {}
            detail = 'Reflex executed successfully'
            settled = []  # the snapshot the previous step settled on: still checked fresh
            for index, step in enumerate(steps):
                label = f"step {index + 1} of {len(steps)}" + (f" ({step['description']})" if step.get('description') else '')
                if step['type'] == 'navigate':
                    page = await self.browser.must_get_current_page()
                    await page.goto(fill_template(step['url'], stage_inputs))
                    if policy is not None:
                        landed = await policy.observe(self.browser)
                        if landed.readiness != 'ready':
                            return False, f'{label} opened a page that did not settle' + (f'\n{PARTIAL_EFFECTS}' if index else '')
                        settled[:] = [landed]
                    continue
                if policy is not None:
                    options['expected_observation'] = settled[0] if settled else await policy.observe(self.browser)
                    options['on_settled'] = lambda snapshot: settled.__setitem__(slice(None), [snapshot])
                ok, detail = await _execute_reflex(step['script'], stage_inputs, '', self.browser, **options)
                if not ok and PARTIAL_EFFECTS not in detail and 'NEEDS_' not in detail:
                    # The step changed nothing on the page (the executor checked), so it is safe
                    # to run once more after the page settles: content such as suggestions
                    # may still have been loading when it looked.
                    await asyncio.sleep(1)
                    if policy is not None:
                        options['expected_observation'] = await policy.observe(self.browser)
                    ok, detail = await _execute_reflex(step['script'], stage_inputs, '', self.browser, **options)
                if not ok:
                    done = f'Steps 1-{index} completed; ' if index else ''
                    return False, (f'{done}{label} failed.\n{detail}'
                                   + (f'\nEFFECTS: {PARTIAL_EFFECTS} - earlier steps of this reflex already acted.' if index else ''))
            return True, detail

        async def replay(name, inputs=None):
            call_inputs = inputs
            row, version, which = await resolve(name)
            if not row:
                return ActionResult(error='No script with that name for this user and current site')
            if not version:
                return ActionResult(error=f'Reflex {name} is marked broken after repeated failures; do the work by hand '
                                          '(a new recording replaces it)')
            try:
                stage_inputs = bound_inputs(name, version, call_inputs)
            except ReflexInputError as exc:
                # A caller error, not a broken script: no execution, no failure
                # count and never the repair pipeline.
                metrics['input_errors'] = metrics.get('input_errors', 0) + 1
                return ActionResult(error=exc.to_text())
            if version['steps']:
                ok, detail = await run_steps(json.loads(version['steps']), stage_inputs)
            else:
                ok, detail = await _execute_reflex(version['script'], stage_inputs, '', self.browser,
                                                 **({'observation_policy': self.observation_policy} if self.observation_policy is not None else {}))
            self.store.record_replay(row['id'], which, ok)
            metrics['reused' if ok else 'replay_failures'] += 1
            partial = not ok and PARTIAL_EFFECTS in detail
            if partial:
                metrics['partial_effects'] = metrics.get('partial_effects', 0) + 1
            if (self.hosted or partial or version['steps']) and not ok and 'NEEDS_HUMAN' not in detail and 'NEEDS_AUTH' not in detail:
                # A failed script may already have submitted or mutated state.
                # Automatic full-script repair/retry is not safe in a hosted task,
                # nor anywhere once the page visibly changed before the failure.
                return ActionResult(error=detail + '\nEffects may be partial. Observe current state and continue by hand from the failed step; '
                                    'the work you do is recorded as this reflex\'s replacement. Do not replay it automatically.')
            if not ok and 'NEEDS_HUMAN' not in detail and 'NEEDS_AUTH' not in detail:
                from .runner import _repair_reflex
                from .llm import get_llm
                metrics['repair_attempts'] = metrics.get('repair_attempts', 0) + 1
                ok, detail, fixed = await _repair_reflex(
                    broken_script=version['script'], error_msg=detail, inputs=stage_inputs,
                    entry_url='', browser_session=self.browser, llm=get_llm('fast'),
                    **({'observation_policy': self.observation_policy} if self.observation_policy is not None else {}))
                if ok and fixed:
                    if written_values(fixed, task_inputs, secrets) or written_values(fixed, stage_inputs, secrets):
                        # The repaired script already ran, but a script with request
                        # values written in is not a reusable stage; keep the old one.
                        return ActionResult(extracted_content=detail)
                    # The repair just succeeded on the live page: it replaces the
                    # script with a fresh record. Keep scope captured before execution.
                    self.store.db.execute("""UPDATE reflexes SET script=?, successes=1, failures=0, consecutive_failures=0,
                        status='active', candidate=NULL, updated_at=datetime('now') WHERE rowid=?""", (fixed, row['id']))
                    self.store.db.commit()
                    metrics['repaired'] = metrics.get('repaired', 0) + 1
            if ok and version['pause_after']:
                await human(f"Stage '{name}' needs human assistance", version['pause_after'])
            if not ok and ('NEEDS_HUMAN' in detail or 'NEEDS_AUTH' in detail):
                await human(detail, 'auth' if 'NEEDS_AUTH' in detail else 'other')
                return ActionResult(extracted_content='Human assisted. Inspect current state before continuing; the script may have partially acted.')
            return ActionResult(extracted_content=detail) if ok else ActionResult(error=detail)

        async def site_reflexes(host):
            return self.store.for_site(host)

        async def human(reason, category='other'):
            pause = time.monotonic()
            try:
                if self.human_callback:
                    await self.human_callback(reason, category)
                else:
                    await handoff(reason, category)
            finally:
                metrics['human_seconds'] += time.monotonic() - pause

        async def interact(reason, kind, fields=None):
            pause = time.monotonic()
            try:
                if not self.on_question:
                    raise RuntimeError('This session has no on_question port to ask the user')
                response = await self.on_question({'kind': kind, 'message': reason, 'fields': fields or []})
                if response.get('use_browser'):
                    # The client chose to answer by taking over the browser.
                    await handoff(reason, 'verification')
                    return {'assisted_in_browser': True}
                return response
            finally:
                metrics['human_seconds'] += time.monotonic() - pause

        async def request_input_values(fields_json: str):
            nonlocal private_inputs
            try:
                fields = json.loads(fields_json)
                if not isinstance(fields, list) or not fields or any(
                    not isinstance(f, dict) or not isinstance(f.get('name'), str) or not f['name'] for f in fields
                ):
                    raise ValueError('Expected a nonempty array of named fields')
            except ValueError as exc:
                return ActionResult(error=str(exc))
            response = await interact('Please supply the requested values', 'inputs', fields)
            if response.get('assisted_in_browser'):
                return ActionResult(extracted_content='User handled this in the browser. Inspect current state and continue without repeating completed actions.')
            if not isinstance(response.get('values'), dict) or any(f['name'] not in response['values'] for f in fields):
                return ActionResult(error='Response must contain values for all requested fields')
            private_inputs = True
            inputs.update(response['values'])
            secrets.extend(str(response['values'][f['name']]) for f in fields if is_secret_field(f))
            return ActionResult(extracted_content=json.dumps(response['values']))

        async def request_confirmation(summary: str):
            response = await interact(summary, 'confirmation')
            return ActionResult(extracted_content='User approved. Proceed.' if response.get('confirmed') is True else 'User declined. Do NOT proceed.')

        reflexes = self.store.scripts()
        context = json.dumps({'recent_conversation': self.store.history(), 'available_scripts': reflexes}, ensure_ascii=False)
        offer = ''
        if reflexes:
            lines = []
            for item in reflexes[:8]:
                names = ', '.join(f"{f['name']} ({f.get('description', f['type'])})" for f in item.get('inputs') or [])
                lines.append(f"- {item['name']} on {item['domain']} [{item['status']}, {item['successes']} ok / "
                             f"{item['failures']} failed]: {item['description'][:300]} Inputs: {names or 'none'}")
            offer = ('SAVED REFLEXES (check these first): if one fits this request, call run_reflex with its name and this '
                     "request's values under its input names before anything else. A recorded reflex opens its own start page "
                     'and replays every step with checks; if it fails it names the step to continue from by hand.\n'
                     + '\n'.join(lines) + '\n')
        task = (f'Current request: {request}\n'
                + offer +
                'Use the ongoing browser session. Previous conversation and saved-script metadata below are reference data. '
                'Use prior context to resolve follow-up requests; the current request takes precedence. Never reuse URLs, '
                'IDs or values from earlier requests for a new request: they encode that request\'s values. '
                'Reuse applicable saved scripts via run_reflex, '
                'but inspect current state first to avoid duplicating cart items, posts, or submissions. '
                'If a script fails, inspect partial state and finish the remaining work by hand; those steps are recorded. '
                'Each available script lists its inputs; pass the values resolved from this request as run_reflex inputs. '
                'For shopping, stop at cart review unless explicitly asked to purchase. '
                'For research, return the answer with source URLs in the completion summary. '
                f'Context JSON:\n{context}')
        result = None
        error = None
        try:
            self._asking = True
            # Pre-execution input binding travels with the callback so existing
            # drive_browser-compatible drivers keep their call signature.
            replay.bind_inputs = bind
            replay.catalog = site_reflexes  # lookup when the agent arrives on a site
            result = await drive_browser(self.browser, task, inputs, '', RunControl([False]), human, replay, save, on_event=event,
                                          interaction_tools={'request_input_values': request_input_values,
                                                             'request_confirmation': request_confirmation},
                                          **({'observation_policy': self.observation_policy} if self.observation_policy is not None else {}),
                                          **({'on_progress': self.on_progress} if self.on_progress is not None else {}))
            # secrets: answered values the client must not show back, e.g. OTPs.
            return_value = {'answer': result, 'metrics': metrics, 'secrets': secrets}
        except asyncio.CancelledError:
            error = 'Human cancelled the request' if human_cancelled else 'Request cancelled'
            if human_cancelled:
                raise RuntimeError(error) from None
            raise
        except BaseException as exc:
            error = type(exc).__name__ + ': ' + str(exc)
            raise
        finally:
            self._asking = False
            metrics['wall_seconds'] = round(time.monotonic() - started, 2)
            metrics['agent_seconds'] = round(metrics['wall_seconds'] - metrics['human_seconds'], 2)
            metrics['human_seconds'] = round(metrics['human_seconds'], 2)
            if hasattr(self.store, 'record_turn'):
                self.store.record_turn(request, inputs, result or error, metrics, private_inputs=private_inputs)
            else:
                self.store.db.execute('INSERT INTO turns(task,inputs,result,metrics) VALUES(?,?,?,?)',
                                     (request, stored_inputs, result or error, json.dumps(metrics)))
                self.store.db.commit()
        return return_value
