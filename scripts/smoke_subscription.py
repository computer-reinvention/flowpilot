"""Live Pi subscription browser demo; run through docker compose exec -T backend uv run python."""
import asyncio
import json
import socket
from urllib.parse import quote

from browser_use import Browser, ActionResult
from backend.pi_browser import drive_browser
from backend.replay import RunControl
from backend.runner import _async_cdp_get, _execute_reflex, _repair_reflex
from backend.llm import get_llm
from backend.sandbox import SandboxManager

HTML = """<html><head><title>FlowPilot subscription demo</title></head><body>
<h1>Demo booking</h1><label>Name <input id="name"></label>
<label>Destination <input id="destination"></label>
<button id="submit" onclick="document.querySelector('#receipt').textContent='Booked for '+document.querySelector('#name').value+' to '+document.querySelector('#destination').value;window.submissions=(window.submissions||0)+1">Book demo</button>
<output id="receipt"></output></body></html>"""


async def main():
    manager = SandboxManager()
    sandbox = manager.start(user_id='subscription-demo')
    browser = None
    saved = {}
    try:
        host = sandbox['cdp_url'].split('//')[1].split(':')[0]
        for _ in range(40):
            try:
                info = await _async_cdp_get(host, 9223, '/json/version')
                if info: break
            except Exception: pass
            await asyncio.sleep(.5)
        else: raise RuntimeError('CDP not ready')
        path = '/' + '/'.join(info['webSocketDebuggerUrl'].split('/')[3:])
        browser = Browser(cdp_url=f'ws://{socket.gethostbyname(host)}:9223{path}')
        await browser.start()
        page = await browser.must_get_current_page()
        url = 'data:text/html,' + quote(HTML)
        await page.goto(url)

        async def save(name, description, script):
            saved[name] = script
            return ActionResult(extracted_content='Saved')

        async def human(*args):
            raise RuntimeError('This demo has no human barrier')

        async def reflex(name):
            return ActionResult(error='No existing scripts; perform the task and save one')

        result = await asyncio.wait_for(drive_browser(
            browser,
            'On the current demo page, fill Name and Destination using the input values and click Book demo exactly once. '
            'Verify the receipt. Save a reusable parameterized reflex named book_demo before calling complete. '
            'The script should fill the current page using inputs.name and inputs.destination and check the receipt. '
            'Use codemode to compose observation and actions where appropriate. This is a synthetic test with no external booking.',
            {'name': 'Ada', 'destination': 'Kyoto'}, '', RunControl([False]), human, reflex, save), 180)
        first = await page.evaluate("() => ({receipt:document.querySelector('#receipt').textContent,count:window.submissions})")
        assert json.loads(first) == {'receipt':'Booked for Ada to Kyoto','count':1}, first
        assert 'book_demo' in saved
        await page.goto(url)
        ok, detail = await _execute_reflex(saved['book_demo'], {'name':'Grace','destination':'Lisbon'}, '', browser)
        assert ok, detail
        second = await page.evaluate("() => ({receipt:document.querySelector('#receipt').textContent,count:window.submissions})")
        assert json.loads(second) == {'receipt':'Booked for Grace to Lisbon','count':1}, second
        await page.goto(url)
        await page.evaluate("() => { document.querySelector('#name').id='full-name'; const b=document.querySelector('#submit'); b.setAttribute('onclick', b.getAttribute('onclick').replace('#name','#full-name')); }")
        ok, failure = await _execute_reflex(saved['book_demo'], {'name':'Lin','destination':'Oslo'}, '', browser)
        assert not ok, 'Changed selector should require repair'
        repaired, detail, fixed = await asyncio.wait_for(_repair_reflex(
            saved['book_demo'], failure, {'name':'Lin','destination':'Oslo'}, '', browser, get_llm('fast')), 180)
        assert repaired and fixed, detail
        third = await page.evaluate("() => ({receipt:document.querySelector('#receipt').textContent,count:window.submissions})")
        assert json.loads(third) == {'receipt':'Booked for Lin to Oslo','count':1}, third
        print(json.dumps({'passed': True, 'agent_result': result, 'ai_run':json.loads(first),
                          'zero_model_replay':json.loads(second), 'repaired_run':json.loads(third)}))
    finally:
        if browser: await browser.stop()
        manager.stop(sandbox['sandbox_id'])


asyncio.run(main())
