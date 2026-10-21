"""Run with uv run python -m scripts.smoke_repl_state (no model calls)."""
import asyncio
from pathlib import Path
import tempfile
import time

from backend.repl import BrowserSession


async def visit(session):
    await session.context.route('https://repl-state.test/**', lambda route: route.fulfill(body='<title>Fixture</title>', content_type='text/html'))
    page = session.context.pages[0]
    await page.goto('https://repl-state.test/')
    return page


async def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        async with BrowserSession('alice', root, headless=True, verbose=False) as session:
            page = await visit(session)
            await page.evaluate("localStorage.setItem('identity','alice')")
            await session.context.add_cookies([{'name':'session-test','value':'alice',
                'url':'https://repl-state.test', 'expires':time.time()+3600}])
        async with BrowserSession('bob', root, headless=True, verbose=False) as session:
            page = await visit(session)
            assert await page.evaluate("localStorage.getItem('identity')") is None
            assert not await session.context.cookies('https://repl-state.test')
        async with BrowserSession('alice', root, headless=True, verbose=False) as session:
            page = await visit(session)
            assert await page.evaluate("localStorage.getItem('identity')") == 'alice'
            assert any(c['value']=='alice' for c in await session.context.cookies('https://repl-state.test'))
    print('PASS: persistent cookies/localStorage survive restart; other user sees neither')


if __name__ == '__main__':
    asyncio.run(main())
