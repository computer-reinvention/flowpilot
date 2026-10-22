"""Real headless browser + clickable human handoff + subscription agent.

Run: uv run python -m scripts.smoke_handoff
The second browser simulates a person following the delivered link.
"""
import asyncio
import tempfile

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from backend.session import BrowserSession


HTML = '''<title>OTP fixture</title><h1>Verification required</h1>
<input id="otp" aria-label="OTP"><button id="verify" onclick="if(document.querySelector('#otp').value==='246810'){document.querySelector('#result').textContent='Verified';window.effects=(window.effects||0)+1}">Verify</button>
<p id="result">Waiting for OTP</p>'''


async def main():
    load_dotenv()
    events = asyncio.Queue()
    with tempfile.TemporaryDirectory() as state:
        async with BrowserSession('handoff-smoke', state, on_handoff=events.put) as session:
            await session.context.route('https://handoff.test/**',
                                        lambda route: route.fulfill(body=HTML, content_type='text/html'))
            async with async_playwright() as p:
                human_browser = await p.chromium.launch(headless=True)
                try:
                    task = asyncio.create_task(session.ask(
                        'Visit https://handoff.test. This is an OTP verification page. '
                        'Request human help for the OTP. After assistance, inspect the result '
                        'and report whether verification succeeded. Do not submit again.'))
                    event = await asyncio.wait_for(events.get(), 90)
                    assert not task.done()
                    assert event['browser_cdp_url'].startswith('ws://127.0.0.1:')
                    viewer = await human_browser.new_page()
                    errors = []
                    viewer.on('pageerror', lambda error: errors.append(str(error)))
                    await viewer.goto(event['assistance_url'])
                    await viewer.locator('#screen').wait_for()
                    await viewer.wait_for_function("document.querySelector('#screen').naturalWidth > 0")
                    target = session.context.pages[-1]
                    async def click_target(selector):
                        box = await target.locator(selector).bounding_box()
                        image = await viewer.locator('#screen').bounding_box()
                        dimensions = await viewer.locator('#screen').evaluate('(img)=>({w:img.naturalWidth,h:img.naturalHeight})')
                        await viewer.locator('#screen').click(position={
                            'x':(box['x']+box['width']/2)*image['width']/dimensions['w'],
                            'y':(box['y']+box['height']/2)*image['height']/dimensions['h']})
                    await click_target('#otp')
                    await viewer.locator('#text').fill('246810')
                    await viewer.locator('#type').click()
                    await target.wait_for_function("document.querySelector('#otp').value === '246810'")
                    await click_target('#verify')
                    await target.wait_for_function("document.querySelector('#result').textContent === 'Verified'")
                    assert not task.done(), 'Agent must remain paused until explicit resume'
                    await viewer.locator('#resume').click()
                    result = await asyncio.wait_for(task, 90)
                    assert await target.evaluate('window.effects') == 1
                    assert not errors, errors
                    assert (await viewer.request.get(event['assistance_url'])).status == 410
                    print({'passed': True, 'answer': result['answer'], 'effects': 1,
                           'link_expired': True, 'metrics': result['metrics']})
                finally:
                    if 'task' in locals() and not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    await human_browser.close()


if __name__ == '__main__':
    asyncio.run(main())
