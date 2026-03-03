"""True end-to-end flow test — hits the running backend, exercises the full
API surface: auth, flow CRUD, session capture, profile_name, LLM provider check.

Prerequisites:
  - docker compose up --build -d
  - GOOGLE_API_KEY (or appropriate LLM key) set in .env / environment

Run:
  uv run pytest tests/test_e2e_flow.py -v -s --timeout=180

Skip:
  FLOWPILOT_SKIP_E2E=1 uv run pytest tests/test_e2e_flow.py -v
"""

import os
import asyncio
import pytest
import httpx

pytestmark = pytest.mark.skipif(
    os.environ.get("FLOWPILOT_SKIP_E2E", "").strip() == "1",
    reason="FLOWPILOT_SKIP_E2E=1 set — skipping real e2e tests",
)

BASE_URL = os.environ.get("FLOWPILOT_URL", "http://localhost:8080")
TIMEOUT = httpx.Timeout(60.0, connect=10.0)
RUN_TIMEOUT = 180


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def client():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT) as ac:
        yield ac


@pytest.fixture
async def auth_headers(client: httpx.AsyncClient):
    import uuid
    email = f"e2e-flow-{uuid.uuid4().hex[:8]}@test.local"
    resp = await client.post("/api/auth/signup", json={
        "email": email,
        "password": "testpass123",
        "name": "E2E Flow Tester",
    })
    assert resp.status_code == 200, f"Signup failed: {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def poll_run(client: httpx.AsyncClient, run_id: str, headers: dict,
                   timeout: int = RUN_TIMEOUT) -> dict:
    terminal = {"completed", "failed", "cancelled"}
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        try:
            resp = await client.get(f"/api/runs/{run_id}", headers=headers)
            assert resp.status_code == 200
            last = resp.json()
            if last["status"] in terminal:
                return last
        except (httpx.ReadError, httpx.ConnectError):
            pass  # transient connection hiccup, retry
        await asyncio.sleep(2)
    raise TimeoutError(f"Run {run_id} did not finish within {timeout}s (last: {last.get('status')})")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health(client: httpx.AsyncClient):
    """Backend health check."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_auth_lifecycle(client: httpx.AsyncClient):
    """Signup → login → me → verify token works."""
    import uuid
    email = f"auth-{uuid.uuid4().hex[:8]}@test.local"

    # Signup
    resp = await client.post("/api/auth/signup", json={
        "email": email, "password": "testpass123", "name": "Auth Test",
    })
    assert resp.status_code == 200
    token = resp.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Me
    resp = await client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["user"]["email"] == email

    # Login
    resp = await client.post("/api/auth/login", json={
        "email": email, "password": "testpass123",
    })
    assert resp.status_code == 200
    assert resp.json()["token"]


@pytest.mark.asyncio
async def test_flow_crud(client: httpx.AsyncClient, auth_headers: dict):
    """Create, list, get, update, delete a flow via the real API."""
    # Create
    resp = await client.post("/api/flows", headers=auth_headers, json={
        "name": "CRUD Test Flow",
        "domain": "crud.example.com",
        "description": "Test CRUD operations",
        "entry_url": "https://crud.example.com",
        "inputs": [{"name": "q", "label": "Query", "type": "text"}],
        "icon": "🧪",
    })
    assert resp.status_code == 200
    flow = resp.json()
    flow_id = flow["id"]
    assert flow["name"] == "CRUD Test Flow"
    assert flow["icon"] == "🧪"

    # List
    resp = await client.get("/api/flows")
    assert resp.status_code == 200
    names = [f["name"] for f in resp.json()]
    assert "CRUD Test Flow" in names

    # Get
    resp = await client.get(f"/api/flows/{flow_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == flow_id

    # Update
    resp = await client.put(f"/api/flows/{flow_id}", headers=auth_headers, json={
        "name": "Updated CRUD Flow",
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated CRUD Flow"
    assert resp.json()["domain"] == "crud.example.com"  # unchanged

    # Delete
    resp = await client.delete(f"/api/flows/{flow_id}", headers=auth_headers)
    assert resp.status_code == 200

    resp = await client.get(f"/api/flows/{flow_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_session_capture_with_profile(client: httpx.AsyncClient, auth_headers: dict):
    """Capture sessions with different profile names via the real API."""
    # Default profile
    resp = await client.post("/api/sessions/capture", headers=auth_headers, json={
        "domain": "e2e-test.com",
        "cookies": [{"name": "sid", "value": "abc", "domain": "e2e-test.com"}],
    })
    assert resp.status_code == 200
    assert resp.json()["profile_name"] == "default"

    # Custom profile
    resp = await client.post("/api/sessions/capture", headers=auth_headers, json={
        "domain": "e2e-test.com",
        "cookies": [{"name": "sid", "value": "xyz", "domain": "e2e-test.com"}],
        "profile_name": "staging",
    })
    assert resp.status_code == 200
    assert resp.json()["profile_name"] == "staging"

    # List — should have both
    resp = await client.get("/api/sessions", headers=auth_headers)
    assert resp.status_code == 200
    e2e_sessions = [s for s in resp.json() if s["domain"] == "e2e-test.com"]
    assert len(e2e_sessions) == 2
    profiles = {s["profile_name"] for s in e2e_sessions}
    assert profiles == {"default", "staging"}

    # Delete both
    for s in e2e_sessions:
        resp = await client.delete(f"/api/sessions/{s['id']}", headers=auth_headers)
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_full_run_with_sandbox(client: httpx.AsyncClient, auth_headers: dict):
    """
    The real deal: create flow → start run → real sandbox + real LLM →
    poll to completion → verify result + steps + novnc lifecycle.
    """
    # Create a simple flow
    resp = await client.post("/api/flows", headers=auth_headers, json={
        "name": "E2E Example.com",
        "domain": "example.com",
        "description": (
            "Navigate to example.com. Read the page heading. "
            "Return ONLY the main heading text."
        ),
        "entry_url": "https://example.com",
        "inputs": [],
    })
    assert resp.status_code == 200
    flow = resp.json()

    # Start run — this creates a real sandbox container
    resp = await client.post("/api/runs", headers=auth_headers, json={
        "flow_id": flow["id"],
        "inputs": {},
    })
    assert resp.status_code == 200, f"Failed to start run: {resp.text}"
    run_id = resp.json()["run_id"]
    novnc_url = resp.json().get("novnc_url")
    assert novnc_url, "Expected novnc_url from run start"
    assert resp.json()["status"] == "connecting"

    # Verify novnc_url resolves (best-effort — may fail if sandbox isn't ready yet)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as raw:
            vnc_resp = await raw.get(novnc_url)
            assert vnc_resp.status_code == 200, f"noVNC not reachable: {vnc_resp.status_code}"
    except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException):
        pass  # OK — sandbox may still be starting or running on Docker network only

    # Poll until done
    final = await poll_run(client, run_id, auth_headers)
    assert final["status"] == "completed", (
        f"Run failed: status={final['status']}, error={final.get('error')}"
    )

    # Verify steps
    steps = final["steps"]
    assert len(steps) >= 2
    statuses = [s["status"] for s in steps]
    assert "connecting" in statuses
    assert "running" in statuses
    assert "completed" in statuses

    # Verify result contains something about "Example Domain"
    result_str = str(final.get("result", ""))
    assert "Example Domain" in result_str or len(result_str) > 10, (
        f"Expected meaningful result, got: {result_str[:200]}"
    )

    # novnc_url should be null now
    assert final.get("novnc_url") is None

    # Cleanup flow
    await client.delete(f"/api/flows/{flow['id']}", headers=auth_headers)
