"""Shared test fixtures — temp DB, async client, auth helpers."""

import pytest
import httpx
from unittest.mock import patch


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
