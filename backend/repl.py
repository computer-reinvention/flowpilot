"""Local conversational client: uv run python -m backend.repl --user alice.

A thin terminal client of backend.session: it supplies stdin for questions and
prints results; all agent behaviour lives in the session core.
"""

import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path

from .session import BrowserSession


async def ask_stdin(question):
    """on_question port: answer agent questions from the terminal."""
    print(f"\nagent> {question['message']}", flush=True)
    if question['kind'] == 'confirmation':
        while True:
            answer = (await asyncio.to_thread(input, 'you> Approve? [yes/no]: ')).strip().lower()
            if answer in ('yes', 'y', 'no', 'n'):
                return {'confirmed': answer in ('yes', 'y')}
            print('Please answer yes or no.')
    values = {}
    for field in question['fields']:
        label = field.get('label') or field['name']
        options = field.get('options') or []
        if options:
            label += ' (' + ', '.join(map(str, options)) + ')'
        value = await asyncio.to_thread(input, f'you> {label} (/browser to handle it yourself): ')
        if value.strip() == '/browser':
            return {'use_browser': True}
        values[field['name']] = value
    return {'values': values}


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
    async with BrowserSession(user, state, args.headless, browser_mode=args.browser_mode,
                              on_question=ask_stdin, verbose=True) as session:
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
