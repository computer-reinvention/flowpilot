"""Credential-free, loopback-only Chromium smoke and shared synthetic fixture.

Run from the repository: python -m scripts.smoke_observation
Cleanup is scoped to the Browser instance, labelled temporary profile and server
created here. Never kill unrelated Chromium processes or delete shared profiles.
"""

import asyncio
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from backend.observation import create_observation
from backend.task_contracts import TaskFault
from backend.task_settings import TaskSettings

PAGE = b'''<!doctype html><title>lane007 synthetic observation</title>
<body><main><button id="action" onclick="window.right++;document.querySelector('#result').textContent='Receipt '+window.right">Submit</button>
<input id="field" value="initial"><input id="secret" type="password" value="fixture-secret">
<output id="result" data-result>Total $5</output><time id="clock">0</time></main>
<script>window.right=0;window.wrong=0;window.noops=0;
setInterval(()=>document.querySelector('#clock').textContent=Date.now(),20);
</script></body>'''


@asynccontextmanager
async def synthetic_browser():
    from browser_use import Browser
    from playwright.async_api import async_playwright
    os.environ.setdefault('ANONYMIZED_TELEMETRY', 'false')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/noise':
                time.sleep(1)
                body = b'ok'
            else:
                body = PAGE
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name='flowpilot-lane007-fixture', daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/'
    temp_root = Path('/private/var/folders/tp/nvzb0t5s0jqb9lyy9r0wn0940000gn/T/opencode')
    try:
        async with async_playwright() as playwright:
            with tempfile.TemporaryDirectory(prefix='flowpilot-lane007-', dir=temp_root if temp_root.exists() else None) as profile:
                browser = Browser(headless=True, executable_path=playwright.chromium.executable_path,
                                  user_data_dir=profile, enable_default_extensions=False,
                                  keep_alive=False, args=['--disable-background-networking'])
                try:
                    await browser.start()
                    page = await browser.must_get_current_page()
                    await page.goto(url)
                    yield browser, page, url
                finally:
                    await browser.kill()
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)


async def smoke():
    settings = TaskSettings(readiness_window_seconds=0.15, readiness_timeout_seconds=2)
    policy = create_observation(settings)
    async with synthetic_browser() as (browser, page, _):
        await page.evaluate('''() => {
          action.disabled = true; document.body.setAttribute('aria-busy','true');
          setInterval(()=>fetch('/noise').catch(()=>{}),40);
          setTimeout(()=>{action.disabled=false; result.textContent='Hydrated $9';
            document.body.removeAttribute('aria-busy');},220);
        }''')
        started = time.monotonic()
        observation = await policy.observe(browser)
        assert observation.readiness == 'ready'
        assert time.monotonic() - started >= 0.22
        await page.evaluate('''() => {
          const replacement=action.cloneNode(true);
          replacement.onclick=()=>window.wrong++;
          action.replaceWith(replacement);
        }''')
        try:
            await policy.require_fresh(browser, observation)
        except TaskFault:
            pass
        else:
            raise AssertionError('Stale same-URL replacement accepted')
        assert await page.evaluate('() => window.wrong') == '0'
        print(json.dumps({'readiness': observation.readiness, 'same_url_stale_rejected': True,
                          'wrong_target_effects': 0, 'browser': 'local Chromium / browser-use Actor'}))


if __name__ == '__main__':
    asyncio.run(smoke())
