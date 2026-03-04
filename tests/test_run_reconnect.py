"""Tests for run reconnection: browser_cdp_url in GET /api/runs and GET /api/runs/{run_id}."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def mock_db_and_services(monkeypatch):
    """Patch DB init, sandbox manager, and profile manager to avoid real I/O."""
    import backend.main as main_mod
    import backend.db as db_mod

    main_mod.sandbox_mgr = MagicMock()
    main_mod.sandbox_mgr.stop = MagicMock()
    main_mod.sandbox_mgr.cleanup_orphans = MagicMock(return_value=0)
    main_mod.sandbox_mgr.stop_all = MagicMock()

    main_mod.profile_mgr = MagicMock()

    monkeypatch.setattr(db_mod, "init_db", AsyncMock())
    monkeypatch.setattr(db_mod, "seed_default_flows", AsyncMock())


@pytest.fixture
def fake_user():
    return {"id": "user-1", "email": "test@test.com", "name": "Test"}


@pytest.fixture
def auth_override(fake_user):
    """Override auth dependency to return fake user."""
    from backend import auth
    from backend.main import app

    async def _fake_user(request=None):
        return fake_user

    app.dependency_overrides[auth.get_current_user] = _fake_user
    yield
    app.dependency_overrides.clear()


def _make_run(run_id="run-1", user_id="user-1", status="running", sandbox_id="sb-1"):
    from backend.models import Run
    return Run(
        id=run_id, flow_id="flow-1", user_id=user_id,
        status=status, sandbox_id=sandbox_id,
    )


# ── GET /api/runs/{run_id} ────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_get_active_includes_browser_cdp_url(auth_override):
    """GET /api/runs/{id} for an active run should include browser_cdp_url."""
    from backend.main import app
    import backend.db as db_mod

    run = _make_run(status="running")
    db_mod.get_run = AsyncMock(return_value=run)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/runs/run-1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["browser_cdp_url"] is not None
    assert "sb-1" in data["browser_cdp_url"]


@pytest.mark.asyncio
async def test_run_get_paused_includes_browser_cdp_url(auth_override):
    """GET /api/runs/{id} for a paused_for_hitl run should include browser_cdp_url."""
    from backend.main import app
    import backend.db as db_mod

    run = _make_run(status="paused_for_hitl")
    db_mod.get_run = AsyncMock(return_value=run)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/runs/run-1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["browser_cdp_url"] is not None
    assert "sb-1" in data["browser_cdp_url"]


@pytest.mark.asyncio
async def test_run_get_completed_no_browser_cdp_url(auth_override):
    """GET /api/runs/{id} for a completed run should have browser_cdp_url=null."""
    from backend.main import app
    import backend.db as db_mod

    run = _make_run(status="completed")
    db_mod.get_run = AsyncMock(return_value=run)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/runs/run-1")

    assert resp.status_code == 200
    assert resp.json()["browser_cdp_url"] is None


@pytest.mark.asyncio
async def test_run_get_connecting_includes_browser_cdp_url(auth_override):
    """GET /api/runs/{id} for a connecting run should include browser_cdp_url."""
    from backend.main import app
    import backend.db as db_mod

    run = _make_run(status="connecting")
    db_mod.get_run = AsyncMock(return_value=run)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/runs/run-1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["browser_cdp_url"] is not None
    assert "sb-1" in data["browser_cdp_url"]


# ── GET /api/runs (list) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_list_active_includes_browser_cdp_url(auth_override, fake_user):
    """GET /api/runs list should include browser_cdp_url for active runs."""
    from backend.main import app
    import backend.db as db_mod

    db_mod.list_runs = AsyncMock(return_value=[
        {
            "id": "run-1", "flow_id": "f1", "user_id": "user-1",
            "status": "running", "sandbox_id": "sb-1",
            "inputs": "{}", "steps": "[]", "current_step": None,
            "result": None, "error": None, "profile_id": "",
            "created_at": "2025-01-01T00:00:00Z", "completed_at": None,
        },
        {
            "id": "run-2", "flow_id": "f1", "user_id": "user-1",
            "status": "completed", "sandbox_id": "sb-2",
            "inputs": "{}", "steps": "[]", "current_step": None,
            "result": None, "error": None, "profile_id": "",
            "created_at": "2025-01-01T00:00:00Z", "completed_at": "2025-01-01T00:05:00Z",
        },
    ])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/runs")

    assert resp.status_code == 200
    runs = resp.json()
    assert len(runs) == 2

    active = next(r for r in runs if r["id"] == "run-1")
    assert active["browser_cdp_url"] is not None
    assert "sb-1" in active["browser_cdp_url"]

    completed = next(r for r in runs if r["id"] == "run-2")
    assert completed["browser_cdp_url"] is None


@pytest.mark.asyncio
async def test_run_list_paused_for_hitl_includes_browser_cdp_url(auth_override, fake_user):
    """GET /api/runs should include browser_cdp_url for paused_for_hitl runs."""
    from backend.main import app
    import backend.db as db_mod

    db_mod.list_runs = AsyncMock(return_value=[
        {
            "id": "run-3", "flow_id": "f1", "user_id": "user-1",
            "status": "paused_for_hitl", "sandbox_id": "sb-3",
            "inputs": "{}", "steps": "[]", "current_step": "Needs input",
            "result": None, "error": None, "profile_id": "",
            "created_at": "2025-01-01T00:00:00Z", "completed_at": None,
        },
    ])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/runs")

    assert resp.status_code == 200
    runs = resp.json()
    assert runs[0]["browser_cdp_url"] is not None
    assert "sb-3" in runs[0]["browser_cdp_url"]


@pytest.mark.asyncio
async def test_run_get_not_found(auth_override):
    """GET /api/runs/{id} for a non-existent run should return 404."""
    from backend.main import app
    import backend.db as db_mod

    db_mod.get_run = AsyncMock(return_value=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/runs/nonexistent")

    assert resp.status_code == 404
