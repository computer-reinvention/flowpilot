"""Exercise CDP remote control from Chromium and WebKit viewers (no LLM calls)."""
import asyncio
import tempfile

from playwright.async_api import async_playwright

from backend.repl import BrowserSession


FIXTURE = '''<style>body{height:2500px}#drag{width:300px;height:100px;background:lightblue}</style>
<input id="text"><div id="drag">Drag across this area</div>
<iframe srcdoc="<input id='inside' type='checkbox'>"></iframe>
<script>window.events=[];for(const type of ['mousemove','mousedown','mouseup','keydown','keyup','wheel'])
document.addEventListener(type,e=>events.push({type,trusted:e.isTrusted,buttons:e.buttons,key:e.key,x:e.clientX,y:e.clientY}));</script>'''


async def main():
    async with async_playwright() as p:
        for engine in (p.chromium, p.webkit):
            delivered = asyncio.Queue()
            with tempfile.TemporaryDirectory() as state:
                async with BrowserSession('control-test', state, on_handoff=delivered.put) as session:
                    target = session.context.pages[0]
                    await target.set_content(FIXTURE)
                    pause = asyncio.create_task(session.handoff.request('Test remote control'))
                    event = await delivered.get()
                    viewer_browser = await engine.launch(headless=True)
                    try:
                        viewer = await viewer_browser.new_page(viewport={'width': 1000, 'height': 1000})
                        errors = []
                        viewer.on('pageerror', lambda error: errors.append(str(error)))
                        await viewer.goto(event['assistance_url'])
                        await viewer.wait_for_function("document.querySelector('#screen').src.startsWith('data:image/jpeg') && document.querySelector('#screen').naturalWidth > 0")
                        async def coords(x, y):
                            return await viewer.locator('#screen').evaluate(
                                '(img,p)=>{const r=img.getBoundingClientRect();return {x:r.x+p.x*r.width/viewport.width,y:r.y+p.y*r.height/viewport.height}}', {'x':x,'y':y})
                        async def center(selector):
                            box = await target.locator(selector).bounding_box()
                            return await coords(box['x']+box['width']/2, box['y']+box['height']/2)
                        pos = await center('#text')
                        await viewer.mouse.click(**pos)
                        await viewer.keyboard.type('hello')
                        await target.wait_for_function("document.querySelector('#text').value === 'hello'")
                        # Modifier and key-up/down must reach the target, not the viewer.
                        await viewer.keyboard.press('Shift+ArrowLeft')
                        await viewer.keyboard.press('Backspace')
                        await target.wait_for_function("document.querySelector('#text').value === 'hell'")
                        start = await coords(30, 70)
                        end = await coords(230, 100)
                        await viewer.mouse.move(**start)
                        await viewer.mouse.down()
                        await viewer.mouse.move(**end, steps=12)
                        await viewer.mouse.up()
                        await target.wait_for_function("events.some(e=>e.type==='mousemove'&&e.buttons===1&&e.x>150)")
                        # Click into an iframe through the same coordinate path.
                        box = await target.frame_locator('iframe').locator('#inside').bounding_box()
                        pos = await coords(box['x']+box['width']/2, box['y']+box['height']/2)
                        await viewer.mouse.click(**pos)
                        await target.frame_locator('iframe').locator('#inside').wait_for()
                        await viewer.evaluate('queue')
                        assert await target.frame_locator('iframe').locator('#inside').is_checked()
                        await viewer.mouse.wheel(0, 400)
                        await target.wait_for_function('scrollY > 0')
                        records = await target.evaluate('events')
                        assert all(e['trusted'] for e in records)
                        assert any(e['type']=='keydown' and e['key']=='Shift' for e in records)
                        assert any(e['type']=='keyup' and e['key']=='Shift' for e in records)
                        assert not pause.done()
                        await viewer.locator('#resume').click()
                        await pause
                        assert (await viewer.request.get(event['assistance_url'])).status == 410
                        assert not errors, errors
                        print({'engine':engine.name,'passed':True,'stream':'CDP JPEG',
                               'checks':['direct typing','modifier keys','drag movement','iframe click','scroll','resume','expired link']})
                    finally:
                        if not pause.done():
                            pause.cancel()
                            await asyncio.gather(pause, return_exceptions=True)
                        await viewer_browser.close()


if __name__ == '__main__':
    asyncio.run(main())
