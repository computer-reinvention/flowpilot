"""Run inside the backend container: real Docker/CDP replay and human boundary.

docker compose exec -T backend uv run python < scripts/smoke_replay.py
Uses a temporary database and removes its sandbox; needs no model credentials.
"""
import asyncio
import json
import tempfile
from pathlib import Path

from backend import db
from backend.models import Flow
from backend.runner import FlowRunner, RunStatus
from backend.sandbox import SandboxManager


async def main():
    manager = SandboxManager('/app/profiles')
    sandbox = manager.start(user_id='replay-smoke')
    old_path = db.DB_PATH
    controls = []
    events = []
    try:
        with tempfile.TemporaryDirectory() as directory:
            db.DB_PATH = Path(directory) / 'smoke.sqlite'
            await db.init_db()
            flow = Flow(id='smoke-flow', name='Real replay smoke', domain='smoke.test',
                        task='Complete two stages with a human boundary', inputs='[]',
                        created_by='smoke', exploration_tree=json.dumps([
                            {'reflex_name': 'first'}, {'reflex_name': 'second'}]))
            await db.upsert_reflex('smoke.test', 'first', 'Create first stage',
                "async () => { document.body.innerHTML='<main id=first>First</main>'; window.count=(window.count||0)+1; return {success:true,count:window.count}; }",
                pause_after='consent', flow_id=flow.id)
            await db.upsert_reflex('smoke.test', 'second', 'Check human completion and finish',
                "async () => { if(window.count!==1 || !document.querySelector('#human')) throw new Error('Wrong resume state'); document.body.innerHTML='<main id=done>Complete</main>'; return {success:true,count:window.count}; }",
                flow_id=flow.id)

            async def ready(control, browser, *_):
                controls.append((control, browser))

            async def progress(status, message):
                events.append(status.value)
                if status == RunStatus.PAUSED_FOR_HITL:
                    control, browser = controls[0]
                    page = await browser.must_get_current_page()
                    assert await page.evaluate('() => window.count') == '1'
                    await page.evaluate("() => document.body.insertAdjacentHTML('beforeend','<div id=human>Approved</div>')")
                    control.resume()
                if status == RunStatus.COMPLETED:
                    page = await controls[0][1].must_get_current_page()
                    assert await page.evaluate("() => !!document.querySelector('#done')") == 'True'

            result = await asyncio.wait_for(FlowRunner().run_flow(
                flow, {}, 'smoke', sandbox['sandbox_id'], sandbox['cdp_url'], 'smoke-run',
                progress_callback=progress, agent_ready_callback=ready), 90)
            assert result['execution_mode'] == 'replay'
            assert len(result['raw_result']) == 2
            assert 'paused_for_hitl' in events
            print(json.dumps({'passed': True, 'result': result, 'events': events}))
    finally:
        db.DB_PATH = old_path
        manager.stop(sandbox['sandbox_id'])


asyncio.run(main())
