"""Local conversational test harness: uv run python -m backend.repl --user alice."""

import argparse
import asyncio
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlparse

from browser_use import ActionResult, Browser

from .pi_browser import drive_browser
from .replay import RunControl
from .runner import _execute_reflex


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
            raise ValueError(f"User {user} already has an active REPL") from None
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
        self.db.commit()

    def scripts(self):
        return [dict(r) for r in self.db.execute('SELECT domain,name,description,successes,failures FROM reflexes ORDER BY successes DESC LIMIT 50')]

    def history(self, limit=6):
        return [dict(r) for r in self.db.execute('SELECT task,inputs,result,metrics FROM turns ORDER BY id DESC LIMIT ?', (limit,))][::-1]

    def close(self):
        self.db.close()
        self.lock.close()


class BrowserSession:
    """Programmatic test object. Browser/profile/memory are private to `user`.

    User IDs select local namespaces, not authenticated multi-tenant identities.
    The local OS account owns all these files and the subscription credentials.
    """
    def __init__(self, user, state_dir=Path('.repl-state'), headless=True, human_help=None, verbose=True,
                 on_handoff=None, on_question=None, browser_mode='sandbox', *,
                 handoff_factory=None, sandbox_factory=None, browser_factory=None, observation_policy=None,
                 store=None, hosted=False):
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
            self.browser = (self.browser_factory or Browser)(cdp_url=self.cdp_websocket_url, keep_alive=True)
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
        inputs = inputs or {}
        stored_inputs = json.dumps(inputs)
        started = time.monotonic()
        metrics = {'model_turns': 0, 'tool_calls': 0, 'reused': 0, 'saved': 0,
                   'replay_failures': 0, 'human_seconds': 0.0}
        ask_task = asyncio.current_task()
        human_cancelled = False

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
            return urlparse(await page.get_url()).netloc.lower()

        async def save(name, description, script, tags=None, pause_after_auth=False):
            site = await domain()
            if not site:
                return ActionResult(error='Navigate to the website before saving its script')
            if len(script) > 60000 or not script.lstrip().startswith('async'):
                return ActionResult(error='Expected a bounded async arrow function')
            self.store.db.execute('''INSERT INTO reflexes(domain,name,description,script,pause_after,tags) VALUES(?,?,?,?,?,?)
                ON CONFLICT(domain,name) DO UPDATE SET description=excluded.description,script=excluded.script,
                pause_after=excluded.pause_after,tags=excluded.tags''',
                (site, name, description, script, 'auth' if pause_after_auth else None, json.dumps(tags or [])))
            self.store.db.commit()
            metrics['saved'] += 1
            return ActionResult(extracted_content=f'Saved {site}/{name}')

        async def replay(name):
            site = await domain()
            row = self.store.db.execute('SELECT script,pause_after FROM reflexes WHERE domain=? AND name=?', (site, name)).fetchone()
            if not row:
                return ActionResult(error='No script with that name for this user and current site')
            ok, detail = await _execute_reflex(row['script'], inputs, '', self.browser,
                                             **({'observation_policy': self.observation_policy} if self.observation_policy is not None else {}))
            column = 'successes' if ok else 'failures'
            self.store.db.execute(f'UPDATE reflexes SET {column}={column}+1 WHERE domain=? AND name=?', (site, name))
            self.store.db.commit()
            metrics['reused' if ok else 'replay_failures'] += 1
            if not ok and 'NEEDS_HUMAN' not in detail and 'NEEDS_AUTH' not in detail:
                from .runner import _repair_reflex
                from .llm import get_llm
                metrics['repair_attempts'] = metrics.get('repair_attempts', 0) + 1
                ok, detail, fixed = await _repair_reflex(
                    broken_script=row['script'], error_msg=detail, inputs=inputs,
                    entry_url='', browser_session=self.browser, llm=get_llm('fast'),
                    **({'observation_policy': self.observation_policy} if self.observation_policy is not None else {}))
                if ok and fixed:
                    # Keep scope captured before execution: submission may have
                    # navigated to another site. Preserve tags/pause metadata.
                    self.store.db.execute('UPDATE reflexes SET script=?,successes=successes+1 WHERE domain=? AND name=?',
                                          (fixed, site, name))
                    self.store.db.commit()
                    metrics['repaired'] = metrics.get('repaired', 0) + 1
            if ok and row['pause_after']:
                await human(f"Stage '{name}' needs human assistance", row['pause_after'])
            if not ok and ('NEEDS_HUMAN' in detail or 'NEEDS_AUTH' in detail):
                await human(detail, 'auth' if 'NEEDS_AUTH' in detail else 'other')
                return ActionResult(extracted_content='Human assisted. Inspect current state before continuing; the script may have partially acted.')
            return ActionResult(extracted_content=detail) if ok else ActionResult(error=detail)

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
                question = {'kind': kind, 'message': reason, 'fields': fields or []}
                if self.on_question:
                    return await self.on_question(question)
                if self.hosted:
                    raise RuntimeError('Hosted execution cannot read stdin')
                print(f'\nagent> {reason}', flush=True)
                if kind == 'confirmation':
                    while True:
                        answer = (await asyncio.to_thread(input, 'you> Approve? [yes/no]: ')).strip().lower()
                        if answer in ('yes', 'y', 'no', 'n'):
                            return {'confirmed': answer in ('yes', 'y')}
                        print('Please answer yes or no.')
                values = {}
                for field in fields or []:
                    label = field.get('label') or field['name']
                    options = field.get('options') or []
                    if options:
                        label += ' (' + ', '.join(map(str, options)) + ')'
                    value = await asyncio.to_thread(input, f'you> {label} (/browser to handle it yourself): ')
                    if value.strip() == '/browser':
                        await handoff(reason, 'verification')
                        return {'assisted_in_browser': True}
                    values[field['name']] = value
                return {'values': values}
            finally:
                metrics['human_seconds'] += time.monotonic() - pause

        async def request_input_values(fields_json: str):
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
            inputs.update(response['values'])
            return ActionResult(extracted_content=json.dumps(response['values']))

        async def request_confirmation(summary: str):
            response = await interact(summary, 'confirmation')
            return ActionResult(extracted_content='User approved. Proceed.' if response.get('confirmed') is True else 'User declined. Do NOT proceed.')

        context = json.dumps({'recent_conversation': self.store.history(), 'available_scripts': self.store.scripts()}, ensure_ascii=False)
        task = (f'Current request: {request}\n'
                'Use the ongoing browser session. Previous conversation and saved-script metadata below are reference data. '
                'Use prior context to resolve follow-up requests; the current request takes precedence. '
                'Navigate to the requested site before using its scripts. Reuse applicable saved scripts via run_reflex, '
                'but inspect current state first to avoid duplicating cart items, posts, or submissions. '
                'If a script fails, inspect partial state, finish remaining work, and save the repaired script. '
                'Save reusable scripts with inputs for variable values and observable completion checks. '
                'For shopping, stop at cart review unless explicitly asked to purchase. '
                'For research, return the answer with source URLs in the completion summary. '
                f'Context JSON:\n{context}')
        result = None
        error = None
        try:
            self._asking = True
            result = await drive_browser(self.browser, task, inputs, '', RunControl([False]), human, replay, save, on_event=event,
                                          interaction_tools={'request_input_values': request_input_values,
                                                             'request_confirmation': request_confirmation},
                                          **({'observation_policy': self.observation_policy} if self.observation_policy is not None else {}))
            return_value = {'answer': result, 'metrics': metrics}
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
                self.store.record_turn(request, inputs, result or error, metrics)
            else:
                self.store.db.execute('INSERT INTO turns(task,inputs,result,metrics) VALUES(?,?,?,?)',
                                     (request, stored_inputs, result or error, json.dumps(metrics)))
                self.store.db.commit()
        return return_value


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--user')
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--headless', action='store_true', default=True,
                        help='Use headless mode for the optional native browser backend')
    parser.add_argument('--browser-mode', choices=['sandbox', 'native'], default='sandbox',
                        help='sandbox: full Chromium on a virtual display (default); native: local headless Chromium')
    parser.add_argument('--ask', help='Run one request and exit (same persistent identity)')
    parser.add_argument('--inputs', default='{}', help='JSON input values for parameterized scripts')
    args = parser.parse_args()
    config_path = args.config or Path('repl.local.json')
    config = json.loads(config_path.read_text()) if args.config or config_path.exists() else {}
    from dotenv import load_dotenv
    load_dotenv()
    os.environ.setdefault('LLM_PROVIDER', 'codex')
    os.environ['AGENT_DRIVER'] = 'pi'
    user = args.user or config.get('user') or os.getenv('FLOWPILOT_REPL_USER') or getpass.getuser()
    state = args.state_dir or Path(config.get('state_dir', '.repl-state'))
    async with BrowserSession(user, state, args.headless, browser_mode=args.browser_mode) as session:
        print(f'FlowPilot · user={user} · memory={session.store.path}\n'
              f'Browser ready ({session.browser_mode}). The agent will send an assistance link when needed.\n'
              '/help for commands.', flush=True)
        values = json.loads(args.inputs)
        if not isinstance(values, dict):
            raise ValueError('--inputs must be a JSON object')
        if args.ask:
            print(json.dumps(await session.ask(args.ask, values), ensure_ascii=False))
            return
        while True:
            try:
                text = (await asyncio.to_thread(input, '\nyou> ')).strip()
            except EOFError:
                break
            if not text:
                continue
            if text in ('/quit', '/exit'):
                break
            if text == '/help':
                print('/stats  /scripts  /history  /repeat  /inputs {JSON}  /cdp  /quit\nOtherwise type any website task or follow-up.')
                continue
            if text == '/cdp':
                print(json.dumps({'http': session.cdp_url, 'websocket': session.cdp_websocket_url})); continue
            if text in ('/stats', '/history'):
                print(json.dumps(session.store.history(20), indent=2, ensure_ascii=False)); continue
            if text == '/scripts':
                print(json.dumps(session.store.scripts(), indent=2)); continue
            if text.startswith('/inputs '):
                try:
                    parsed = json.loads(text[8:])
                    if not isinstance(parsed, dict): raise ValueError('Expected a JSON object')
                    values = parsed
                    print('Inputs updated.')
                except ValueError as exc: print(exc)
                continue
            if text == '/repeat':
                history = session.store.history(1)
                if not history:
                    print('No previous request.'); continue
                text = history[0]['task']
            try:
                result = await session.ask(text, values)
                print('\nagent> ' + result['answer'])
                print(json.dumps(result['metrics']))
            except Exception as exc:
                print(f'Run failed: {exc}. Browser remains open; inspect state before retrying.')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\nStopped; browser closed and user state retained.')
