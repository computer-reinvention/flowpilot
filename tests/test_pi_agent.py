import asyncio
import json

import pytest

from backend import pi_agent


@pytest.fixture
def host(tmp_path, monkeypatch):
    path = tmp_path / "host.mjs"
    monkeypatch.setattr(pi_agent, "HOST", path)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    return path


async def test_bridge_dispatches_only_allowed_tools(host):
    host.write_text('''
import readline from 'node:readline';
const send = x => console.log(JSON.stringify(x));
readline.createInterface({input:process.stdin}).on('line', line => {
 const m = JSON.parse(line);
 if(m.type === 'start') send({type:'tool',id:'1',name:'shell',args:{}});
 if(m.type === 'tool_result') send({type:'result',text:m.error});
});
''')
    async def dispatch(*args):
        pytest.fail("Unknown tool must never reach dispatch")
    result = await pi_agent.run_pi("task", "system", [], dispatch)
    assert result == "Unknown tool"


async def test_cancellation_cancels_pending_tool_and_reaps_child(host):
    host.write_text('''
import readline from 'node:readline';
readline.createInterface({input:process.stdin}).on('line', line => {
 if(JSON.parse(line).type === 'start') console.log(JSON.stringify({type:'tool',id:'1',name:'human_help',args:{}}));
});
''')
    started = asyncio.Event()
    cancelled = asyncio.Event()
    async def dispatch(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    task = asyncio.create_task(pi_agent.run_pi("task", "system", [{"name": "human_help"}], dispatch))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


async def test_real_pi_host_rejects_unknown_model_without_provider_call(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("OPENAI_MODEL", "nonexistent-model-for-test")
    with pytest.raises(RuntimeError, match="catalog does not contain"):
        await pi_agent.run_pi("task", "system", [], None)
