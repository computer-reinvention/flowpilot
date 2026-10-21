"""Tests for HITL (Human-In-The-Loop) pause/resume endpoints."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_run(run_id="run-1", user_id="user-1", status="paused_for_hitl", sandbox_id="sb-1"):
    from backend.models import Run
    return Run(
        id=run_id, flow_id="flow-1", user_id=user_id,
        status=status, sandbox_id=sandbox_id,
    )


def _make_flow(flow_id="flow-1"):
    from backend.models import Flow
    return Flow(
        id=flow_id, name="Test Flow", domain="example.com",
        task="A test flow", inputs="[]", created_by="user-1",
    )


@pytest.fixture
async def test_user_id(client, auth_headers):
    """Get the actual user ID from the auth token."""
    resp = await client.get("/api/auth/me", headers=auth_headers)
    return resp.json()["user"]["id"]


@pytest.mark.asyncio
async def test_resume_paused_run(client, auth_headers, test_user_id):
    """Resume a paused_for_hitl run — should capture cookies, create session, resume agent."""
    from backend.main import _run_agents
    import backend.db as db_mod

    run = _make_run(status="paused_for_hitl", user_id=test_user_id)
    flow = _make_flow()

    original_get_run = db_mod.get_run
    original_update_run = db_mod.update_run
    original_create_session = db_mod.create_session

    db_mod.get_run = AsyncMock(return_value=run)
    db_mod.update_run = AsyncMock()
    db_mod.create_session = AsyncMock()

    mock_agent = MagicMock()
    mock_agent.resume = MagicMock()

    mock_browser = MagicMock()
    mock_browser.export_storage_state = AsyncMock(return_value={
        "cookies": [
            {"name": "session_id", "value": "abc123", "domain": "example.com"},
        ],
        "origins": [],
    })

    paused_ref = [True]
    _run_agents["run-1"] = (mock_agent, mock_browser, flow, paused_ref)

    try:
        resp = await client.post("/api/runs/run-1/resume", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["run_id"] == "run-1"

        mock_agent.resume.assert_called_once()
        mock_browser.export_storage_state.assert_called_once()

        db_mod.create_session.assert_called_once()
        session_arg = db_mod.create_session.call_args[0][0]
        assert session_arg.domain == "example.com"
        assert session_arg.profile_name == run.profile_id
        cookies = json.loads(session_arg.cookies)
        assert len(cookies) == 1
        assert cookies[0]["name"] == "session_id"

        db_mod.update_run.assert_called_once()
        assert db_mod.update_run.call_args[0][1]["status"] == "running"
    finally:
        _run_agents.pop("run-1", None)
        db_mod.get_run = original_get_run
        db_mod.update_run = original_update_run
        db_mod.create_session = original_create_session


@pytest.mark.asyncio
async def test_resume_not_paused(client, auth_headers, test_user_id):
    """Resume a run that is not paused_for_hitl — should return 409."""
    import backend.db as db_mod

    run = _make_run(status="running", user_id=test_user_id)
    original = db_mod.get_run
    db_mod.get_run = AsyncMock(return_value=run)

    try:
        resp = await client.post("/api/runs/run-1/resume", headers=auth_headers)
        assert resp.status_code == 409
    finally:
        db_mod.get_run = original


@pytest.mark.asyncio
async def test_resume_wrong_user(client, auth_headers):
    """Resume a run belonging to another user — should return 403."""
    import backend.db as db_mod

    run = _make_run(user_id="other-user")
    original = db_mod.get_run
    db_mod.get_run = AsyncMock(return_value=run)

    try:
        resp = await client.post("/api/runs/run-1/resume", headers=auth_headers)
        assert resp.status_code == 403
    finally:
        db_mod.get_run = original


@pytest.mark.asyncio
async def test_resume_not_found(client, auth_headers):
    """Resume a non-existent run — should return 404."""
    import backend.db as db_mod

    original = db_mod.get_run
    db_mod.get_run = AsyncMock(return_value=None)

    try:
        resp = await client.post("/api/runs/nope/resume", headers=auth_headers)
        assert resp.status_code == 404
    finally:
        db_mod.get_run = original


@pytest.mark.asyncio
async def test_pause_fallback(client, auth_headers, test_user_id):
    """Manual pause endpoint — should pause a running agent."""
    from backend.main import _run_agents
    import backend.db as db_mod

    run = _make_run(status="running", user_id=test_user_id)
    flow = _make_flow()

    original_get_run = db_mod.get_run
    original_update_run = db_mod.update_run

    db_mod.get_run = AsyncMock(return_value=run)
    db_mod.update_run = AsyncMock()

    mock_agent = MagicMock()
    mock_agent.pause = MagicMock()
    mock_browser = MagicMock()

    paused_ref = [False]
    _run_agents["run-1"] = (mock_agent, mock_browser, flow, paused_ref)

    try:
        resp = await client.post("/api/runs/run-1/pause", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "paused_for_hitl"

        mock_agent.pause.assert_called_once()

        db_mod.update_run.assert_called_once()
        assert db_mod.update_run.call_args[0][1]["status"] == "paused_for_hitl"
    finally:
        _run_agents.pop("run-1", None)
        db_mod.get_run = original_get_run
        db_mod.update_run = original_update_run


@pytest.mark.asyncio
async def test_pause_not_running(client, auth_headers, test_user_id):
    """Pause a run that is not running — should return 409."""
    import backend.db as db_mod

    run = _make_run(status="completed", user_id=test_user_id)
    original = db_mod.get_run
    db_mod.get_run = AsyncMock(return_value=run)

    try:
        resp = await client.post("/api/runs/run-1/pause", headers=auth_headers)
        assert resp.status_code == 409
    finally:
        db_mod.get_run = original
