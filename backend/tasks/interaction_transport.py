"""Viewport-only Playwright transport, over an executor-owned page."""

import asyncio
import base64
from contextlib import suppress

from ..task_contracts import BrowserInput


async def release_controls(page, keys, buttons):
    """Try every held control even when one dispatch fails."""
    failed = False
    for key in tuple(keys):
        try:
            await page.keyboard.up(key)
            keys.discard(key)
        except Exception:
            failed = True
    for button in tuple(buttons):
        try:
            await page.mouse.up(button=button)
            buttons.discard(button)
        except Exception:
            failed = True
    if failed:
        raise RuntimeError("Unable to release browser controls") from None


class BrowserViewport:
    """A single human writer. Release drains input and permanently fences it."""

    def __init__(self, page):
        self.page = page
        self._lock = asyncio.Lock()
        self._closed = False
        self._keys = set()
        self._buttons = set()

    def _require_open(self):
        if self._closed:
            raise RuntimeError("Browser writer has been released")

    async def screenshot(self):
        async with self._lock:
            self._require_open()
            return await self.page.screenshot(type="png", full_page=False, scale="css")

    async def send_input(self, command: BrowserInput):
        async with self._lock:
            self._require_open()
            kind = command.kind
            if kind in {"move", "down", "up"}:
                viewport = await self.page.evaluate("({width: innerWidth, height: innerHeight})")
                if command.x >= viewport["width"] or command.y >= viewport["height"]:
                    raise ValueError("Point is outside the viewport")
                await self.page.mouse.move(command.x, command.y)
                if kind != "move":
                    if kind == "down":
                        self._buttons.add(command.button)
                    await getattr(self.page.mouse, kind)(button=command.button)
                    if kind == "up":
                        self._buttons.discard(command.button)
            elif kind == "scroll":
                await self.page.mouse.wheel(command.dx, command.dy)
            elif kind == "type":
                await self.page.keyboard.insert_text(command.text)
            elif kind in {"keydown", "keyup"}:
                if kind == "keydown":
                    self._keys.add(command.key)
                await getattr(self.page.keyboard, "down" if kind == "keydown" else "up")(command.key)
                if kind == "keyup":
                    self._keys.discard(command.key)
            elif kind == "release":
                await release_controls(self.page, self._keys, self._buttons)

    async def release(self):
        async with self._lock:
            self._closed = True
            await release_controls(self.page, self._keys, self._buttons)

    async def frames(self):
        """Bounded latest-frame queue; detach only this page's CDP session."""
        self._require_open()
        client = await self.page.context.new_cdp_session(self.page)
        queue = asyncio.Queue(maxsize=1)

        async def frame(event):
            await client.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(base64.b64decode(event["data"]))

        client.on("Page.screencastFrame", frame)
        try:
            viewport = await self.page.evaluate("({width: innerWidth, height: innerHeight})")
            await client.send("Page.startScreencast", {"format": "jpeg", "quality": 75, "everyNthFrame": 1,
                                                     "maxWidth": viewport["width"], "maxHeight": viewport["height"]})
            while not self._closed:
                try:
                    yield await asyncio.wait_for(queue.get(), .5)
                except TimeoutError:
                    continue
        finally:
            client.remove_listener("Page.screencastFrame", frame)
            with suppress(Exception):
                await client.send("Page.stopScreencast")
            with suppress(Exception):
                await client.detach()
