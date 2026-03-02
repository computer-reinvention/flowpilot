"""Tests for POST /api/runs/{run_id}/cancel endpoint."""

import json
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def mock_db_and_services(monkeypatch):
    """Patch DB init, sandbox manager, and profile manager to avoid real I/O."""
    import backend.main as main_mod
    import backend.db as db_mod

    # Stub sandbox_mgr
    main_mod.sandbox_mgr = MagicMock()
    main_mod.sandbox_mgr.stop = MagicMock()

    # Stub profile_mgr
    main_mod.profile_mgr = MagicMock()

    # No-op DB init and seed
    monkeypatch.setattr(db_mod, "init_db", AsyncMock())
    monkeypatch.setattr(db_mod, "seed_default_flows", AsyncMock())

    # Stub sandbox cleanup
    main_mod.sandbox_mgr.cleanup_orphans = MagicMock(return_value=0)
    main_mod.sandbox_mgr.stop_all = MagicMock()


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


@pytest.mark.asyncio
async def test_cancel_running_run(auth_override, fake_user):
    """Cancel a running run — should return 200 with status cancelled."""
    from backend.main import app, _run_tasks
    import backend.db as db_mod

    run = _make_run()
    db_mod.get_run = AsyncMock(return_value=run)
    db_mod.update_run = AsyncMock()

    # Put a dummy task in _run_tasks
    dummy = asyncio.get_event_loop().create_future()
    _run_tasks["run-1"] = asyncio.ensure_future(_never_finish(dummy))

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/runs/run-1/cancel")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cancelled"
        assert data["run_id"] == "run-1"

        # Verify DB was updated
        db_mod.update_run.assert_called_once()
        call_args = db_mod.update_run.call_args
        assert call_args[0][0] == "run-1"
        assert call_args[0][1]["status"] == "cancelled"
    finally:
        _run_tasks.pop("run-1", None)
        dummy.cancel()


async def _never_finish(future):
    """Coroutine that waits forever (simulates a long-running run)."""
    await future


@pytest.mark.asyncio
async def test_cancel_not_found(auth_override):
    """Cancel a non-existent run — should return 404."""
    from backend.main import app
    import backend.db as db_mod

    db_mod.get_run = AsyncMock(return_value=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/runs/nope/cancel")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cancel_wrong_user(auth_override):
    """Cancel a run that belongs to another user — should return 403."""
    from backend.main import app
    import backend.db as db_mod

    run = _make_run(user_id="other-user")
    db_mod.get_run = AsyncMock(return_value=run)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/runs/run-1/cancel")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_cancel_already_completed(auth_override):
    """Cancel a completed run — should return 409."""
    from backend.main import app
    import backend.db as db_mod

    run = _make_run(status="completed")
    db_mod.get_run = AsyncMock(return_value=run)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/runs/run-1/cancel")

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_cancel_already_cancelled(auth_override):
    """Cancel an already-cancelled run — should return 409."""
    from backend.main import app
    import backend.db as db_mod

    run = _make_run(status="cancelled")
    db_mod.get_run = AsyncMock(return_value=run)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/runs/run-1/cancel")

    assert resp.status_code == 409
