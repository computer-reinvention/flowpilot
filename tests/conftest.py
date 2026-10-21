"""Shared test fixtures — temp DB, async client, auth helpers."""

import os
import pytest
import httpx
from unittest.mock import patch


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="Run live browser/provider tests against FLOWPILOT_URL")


def pytest_collection_modifyitems(config, items):
    """Keep offline checks deterministic; live tests remain an explicit gate."""
    live_enabled = config.getoption("--live") and os.environ.get("FLOWPILOT_SKIP_E2E") != "1"
    for item in items:
        if item.path.name.startswith("test_e2e_") or item.path.name == "test_flow_run.py":
            item.add_marker(pytest.mark.live)
            if not live_enabled:
                item.add_marker(pytest.mark.skip(reason="requires --live, running app and provider credentials"))


@pytest.fixture
async def client(tmp_path):
    """Async HTTP client against the FastAPI app with isolated temp DB."""
    db_path = tmp_path / "test.db"

    with (
        patch("backend.config.DB_PATH", db_path),
        patch("backend.config.PROFILES_DIR", tmp_path),
        patch("backend.auth.DB_PATH", db_path),
        patch("backend.db.DB_PATH", db_path),
        patch("backend.main.PROFILES_DIR", tmp_path),
        patch("backend.main.sandbox_mgr"),
        patch("backend.main.profile_mgr"),
        patch("backend.main._run_exploration"),
    ):
        from backend import auth
        from backend import db as platform_db
        from backend.main import app

        await auth.init_db()
        await platform_db.init_db()

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.fixture
async def auth_token(client: httpx.AsyncClient) -> str:
    """Create a test user and return the JWT token."""
    resp = await client.post("/api/auth/signup", json={
        "email": "test@example.com",
        "password": "testpass123",
        "name": "Test User",
    })
    assert resp.status_code == 200
    return resp.json()["token"]


@pytest.fixture
def auth_headers(auth_token: str) -> dict:
    """Authorization headers with Bearer token."""
    return {"Authorization": f"Bearer {auth_token}"}
