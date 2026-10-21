"""Replay contracts: full sequence, flow isolation, failures and human boundaries."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.models import Flow
from backend.replay import replay_plan
from backend import runner


def flow(tree=None):
    return Flow(id="flow-a", name="Two stages", domain="example.test", task="Complete both stages",
                inputs="[]", created_by="user-a", entry_url="https://example.test",
                exploration_tree=json.dumps(tree if tree is not None else [
                    {"reflex_name": "first"}, {"reflex_name": "second"}]))


def reflex(name, **kwargs):
    return {"name": name, "script": name, "description": name, "tags": [], **kwargs}


@pytest.fixture
def harness(monkeypatch):
    db = SimpleNamespace(list_reflexes_for_domain=AsyncMock(return_value=[reflex("first"), reflex("second")]),
                         get_reflex=AsyncMock(return_value=None), upsert_reflex=AsyncMock())
    browser = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(runner, "_get_db", lambda: db)
    monkeypatch.setattr(runner, "Browser", lambda **kwargs: browser)
    monkeypatch.setattr(runner, "_async_cdp_get", AsyncMock(return_value={"webSocketDebuggerUrl": "ws://localhost/devtools/browser/test"}))
    execute = AsyncMock(return_value=(True, "done"))
    monkeypatch.setattr(runner, "_execute_reflex", execute)
    model = MagicMock(side_effect=AssertionError("Warm replay must not construct a model"))
    monkeypatch.setattr(runner, "_get_llm", model)
    monkeypatch.setattr(runner, "_get_fast_llm", model)
    progress = AsyncMock()
    return SimpleNamespace(db=db, browser=browser, execute=execute, progress=progress, model=model)


async def run(h, callback=None):
    return await runner.FlowRunner().run_flow(flow(), {}, "user-a", "sb", "ws://localhost:9223", "run",
                                              progress_callback=h.progress, agent_ready_callback=callback)


async def test_all_stages_complete_without_model(harness):
    result = await run(harness)
    assert [c.kwargs["script"] for c in harness.execute.await_args_list] == ["first", "second"]
    assert result["execution_mode"] == "replay"
    assert len(result["raw_result"]) == 2
    completed = [c for c in harness.progress.await_args_list if c.args[0] == runner.RunStatus.COMPLETED]
    assert len(completed) == 1
    harness.model.assert_not_called()
    harness.browser.stop.assert_awaited_once()


async def test_repair_continues_sequence(harness, monkeypatch):
    harness.execute.side_effect = [(False, "selector missing"), (True, "second done")]
    monkeypatch.setattr(runner, "_get_fast_llm", lambda: object())
    monkeypatch.setattr(runner, "_repair_reflex", AsyncMock(return_value=(True, "first repaired", "fixed")))
    result = await run(harness)
    assert len(result["raw_result"]) == 2
    assert harness.db.upsert_reflex.await_args.kwargs["flow_id"] == "flow-a"


async def test_failed_prerequisite_does_not_run_dependents(harness, monkeypatch):
    harness.execute.return_value = (False, "missing element")
    monkeypatch.setattr(runner, "_get_fast_llm", lambda: object())
    monkeypatch.setattr(runner, "_repair_reflex", AsyncMock(return_value=(False, "unrepairable", None)))
    # Stop at handoff; no successful completion or dependent script is allowed.
    with pytest.raises(AssertionError, match="Warm replay"):
        await run(harness)
    assert harness.execute.await_count == 1
    assert all(c.args[0] != runner.RunStatus.COMPLETED for c in harness.progress.await_args_list)


async def test_pause_after_stage_resumes_at_next_stage(harness):
    harness.db.list_reflexes_for_domain.return_value[0]["pause_after"] = "auth"
    paused = asyncio.Event()
    controls = []

    async def ready(control, *_):
        controls.append(control)

    async def progress(status, message):
        if status == runner.RunStatus.PAUSED_FOR_HITL:
            paused.set()

    harness.progress.side_effect = progress
    task = asyncio.create_task(run(harness, ready))
    try:
        await asyncio.wait_for(paused.wait(), 2)
        assert harness.execute.await_count == 1
        assert not task.done()
        controls[0].resume()
        result = await asyncio.wait_for(task, 2)
        assert result["execution_mode"] == "replay"
        assert [c.kwargs["script"] for c in harness.execute.await_args_list] == ["first", "second"]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancel_while_paused_stops_browser(harness):
    async def ready(control, *_):
        control.pause()
        asyncio.get_running_loop().call_soon(task.cancel)
    task = asyncio.create_task(run(harness, ready))
    with pytest.raises(asyncio.CancelledError):
        await task
    harness.execute.assert_not_awaited()
    harness.browser.stop.assert_awaited_once()


async def test_explicit_tree_excludes_unrelated_scripts():
    db = SimpleNamespace(list_reflexes_for_domain=AsyncMock(return_value=[reflex("first"), reflex("unrelated")]),
                         get_reflex=AsyncMock(return_value=reflex("second")))
    plan, complete = await replay_plan(db, flow())
    assert complete
    assert [r["name"] for r in plan] == ["first", "second"]
    db.list_reflexes_for_domain.assert_awaited_once_with("example.test", flow_id="flow-a")
    db.get_reflex.assert_awaited_once_with("example.test", "second")


async def test_missing_stage_is_not_a_complete_plan():
    db = SimpleNamespace(list_reflexes_for_domain=AsyncMock(return_value=[reflex("first")]),
                         get_reflex=AsyncMock(return_value=None))
    _, complete = await replay_plan(db, flow())
    assert not complete


async def test_no_tree_does_not_treat_stage_script_as_whole_flow():
    db = SimpleNamespace(list_reflexes_for_domain=AsyncMock(return_value=[reflex("first")]))
    _, complete = await replay_plan(db, flow([]))
    assert not complete


async def test_reflex_scopes_and_idempotent_migration(tmp_path, monkeypatch):
    import aiosqlite
    from backend import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "db.sqlite")
    async with aiosqlite.connect(str(db.DB_PATH)) as conn:
        await conn.execute("""CREATE TABLE reflexes (
            id TEXT PRIMARY KEY, domain TEXT NOT NULL, name TEXT NOT NULL,
            description TEXT NOT NULL, script TEXT NOT NULL, pause_after TEXT,
            tags TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, UNIQUE(domain, name))""")
        await conn.execute("INSERT INTO reflexes VALUES ('old-id','example.test','same','legacy','legacy script',NULL,'[]','2026','2026')")
        await conn.commit()
    await db.init_db()
    legacy = await db.get_reflex("example.test", "same")
    assert legacy["id"] == "old-id" and legacy["script"] == "legacy script"
    one = await db.upsert_reflex("example.test", "same", "one", "script 1", flow_id="a")
    await db.upsert_reflex("example.test", "same", "two", "script 2", flow_id="b")
    await db.init_db()
    assert (await db.get_reflex("example.test", "same"))["id"] == legacy["id"]
    assert (await db.get_reflex("example.test", "same", flow_id="a"))["id"] == one["id"]
    assert (await db.search_reflexes("example.test", "same", flow_id="b"))[0]["script"] == "script 2"
