import asyncio
import struct

import pytest
from playwright.async_api import async_playwright

from backend.task_contracts import BrowserInput
from backend.tasks.interaction_transport import BrowserViewport


@pytest.mark.asyncio
async def test_real_chromium_drag_type_scroll_release_and_frames():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 640, "height": 480}, device_scale_factor=2)
            await page.set_content('''<input id="text" style="position:absolute;left:10px;top:10px">
            <div style="height:2400px"></div><script>
            window.events=[]; for(const kind of ['pointerdown','pointermove','pointerup','keydown','keyup'])
            document.addEventListener(kind,e=>events.push([e.type,e.buttons,e.key]));
            </script>''')
            handle = BrowserViewport(page)
            for data in [
                dict(kind="down", x=20, y=20, button="left"),
                dict(kind="move", x=100, y=20),
                dict(kind="up", x=100, y=20, button="left"),
                dict(kind="type", text="synthetic-otp-canary"),
                dict(kind="keydown", key="Shift"),
                dict(kind="release"),
                dict(kind="scroll", dx=0, dy=500),
            ]:
                await handle.send_input(BrowserInput(**data))
            assert await page.locator("#text").input_value() == "synthetic-otp-canary"
            await page.wait_for_function("scrollY > 0")
            events = await page.evaluate("events")
            assert any(e[0] == "pointermove" and e[1] == 1 for e in events)
            assert any(e[0] == "keyup" and e[2] == "Shift" for e in events)
            screenshot = await handle.screenshot()
            assert screenshot.startswith(b"\x89PNG")
            assert struct.unpack(">II", screenshot[16:24]) == (640, 480)
            frames = handle.frames()
            assert (await asyncio.wait_for(anext(frames), 5)).startswith(b"\xff\xd8")
            await frames.aclose()
            with pytest.raises(ValueError):
                await handle.send_input(BrowserInput(kind="move", x=640, y=10))
            await handle.release()
            with pytest.raises(RuntimeError, match="released"):
                await handle.send_input(BrowserInput(kind="type", text="forbidden"))
        finally:
            await browser.close()
