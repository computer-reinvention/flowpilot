"""True end-to-end HITL test — hits the running backend, starts a real sandbox,
uses a real LLM, and verifies the full lifecycle through HTTP.

Prerequisites:
  - docker compose up --build -d
  - GOOGLE_API_KEY (or appropriate LLM key) set in .env / environment
  - flowpilot-sandbox image built

Run:
  uv run pytest tests/test_e2e_hitl.py -v -s --timeout=180

Skip:
  FLOWPILOT_SKIP_E2E=1 uv run pytest tests/test_e2e_hitl.py -v
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
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
RUN_TIMEOUT = 180  # max seconds to wait for a run to finish


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def client():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT) as ac:
        yield ac


@pytest.fixture
async def auth_headers(client: httpx.AsyncClient):
    """Sign up a fresh user (or login if exists) and return auth headers."""
    import uuid
    email = f"e2e-{uuid.uuid4().hex[:8]}@test.local"
    resp = await client.post("/api/auth/signup", json={
        "email": email,
        "password": "testpass123",
        "name": "E2E Tester",
    })
    assert resp.status_code == 200, f"Signup failed: {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture
async def test_flow(client: httpx.AsyncClient, auth_headers: dict):
    """Create a simple test flow that visits a public site — no auth needed."""
    resp = await client.post("/api/flows", headers=auth_headers, json={
        "name": "HN Title Check",
        "domain": "news.ycombinator.com",
        "description": (
            "Go to the Hacker News homepage. Read the title of the #1 story. "
            "Return ONLY the title text, nothing else."
        ),
        "entry_url": "https://news.ycombinator.com",
        "inputs": [],
    })
    assert resp.status_code == 200, f"Flow creation failed: {resp.text}"
    return resp.json()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def poll_run(client: httpx.AsyncClient, run_id: str, headers: dict,
                   timeout: int = RUN_TIMEOUT) -> dict:
    """Poll GET /api/runs/{id} until the run reaches a terminal state."""
    terminal = {"completed", "failed", "cancelled"}
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/api/runs/{run_id}", headers=headers)
        assert resp.status_code == 200
        last = resp.json()
        if last["status"] in terminal:
            return last
        await asyncio.sleep(2)
    raise TimeoutError(f"Run {run_id} did not finish within {timeout}s (last status: {last.get('status')})")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_full_run(client: httpx.AsyncClient, auth_headers: dict, test_flow: dict):
    """
    Real end-to-end: signup → create flow → start run (real sandbox + real LLM) →
    poll until completed → verify steps, result, novnc_url, session list.
    """
    flow = test_flow

    # ── 1. Start a run ────────────────────────────────────────────────
    resp = await client.post("/api/runs", headers=auth_headers, json={
        "flow_id": flow["id"],
        "inputs": {},
    })
    assert resp.status_code == 200, f"Failed to start run: {resp.text}"
    run_data = resp.json()
    run_id = run_data["run_id"]
    assert run_data["status"] == "connecting"
    assert run_data.get("novnc_url"), "Expected novnc_url on run start response"

    # ── 2. Verify run appears in list with novnc_url ──────────────────
    resp = await client.get("/api/runs", headers=auth_headers)
    assert resp.status_code == 200
    runs = resp.json()
    active = [r for r in runs if r["id"] == run_id]
    assert len(active) == 1, f"Run {run_id} not in list"
    assert active[0].get("novnc_url"), "Expected novnc_url in run list for active run"

    # ── 3. Poll until completion ──────────────────────────────────────
    final = await poll_run(client, run_id, auth_headers)

    assert final["status"] == "completed", (
        f"Run ended with status={final['status']}, error={final.get('error')}"
    )
    assert final["result"] is not None, "Expected a result on completed run"

    # ── 4. Verify steps were recorded ─────────────────────────────────
    steps = final["steps"]
    assert len(steps) >= 2, f"Expected at least 2 steps, got {len(steps)}"
    statuses = [s["status"] for s in steps]
    assert "connecting" in statuses, "Missing connecting step"
    assert "running" in statuses, "Missing running step"
    assert "completed" in statuses, "Missing completed step"

    # ── 5. novnc_url should be gone for completed runs ────────────────
    assert final.get("novnc_url") is None, "novnc_url should be null for completed run"

    # ── 6. Verify run list shows completed status ─────────────────────
    resp = await client.get("/api/runs", headers=auth_headers)
    updated = [r for r in resp.json() if r["id"] == run_id]
    assert updated[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_e2e_cancel_run(client: httpx.AsyncClient, auth_headers: dict, test_flow: dict):
    """Start a real run, then cancel it mid-execution."""
    flow = test_flow

    # Start run
    resp = await client.post("/api/runs", headers=auth_headers, json={
        "flow_id": flow["id"],
        "inputs": {},
    })
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    # Wait briefly for sandbox to spin up, then check it's still active
    await asyncio.sleep(5)

    resp = await client.get(f"/api/runs/{run_id}", headers=auth_headers)
    assert resp.status_code == 200
    current = resp.json()["status"]

    if current in ("completed", "failed", "cancelled"):
        # Run finished before we could cancel — that's OK for a timing test,
        # just verify the state is terminal
        pytest.skip(f"Run finished too fast (status={current}) — cancel timing test not applicable")

    # Cancel
    resp = await client.post(f"/api/runs/{run_id}/cancel", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # Verify final state
    resp = await client.get(f"/api/runs/{run_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_e2e_reconnect_active_run(client: httpx.AsyncClient, auth_headers: dict, test_flow: dict):
    """Start a run, then simulate reconnection by fetching the run mid-flight."""
    flow = test_flow

    # Start run
    resp = await client.post("/api/runs", headers=auth_headers, json={
        "flow_id": flow["id"],
        "inputs": {},
    })
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    original_novnc = resp.json().get("novnc_url")

    # Wait for it to be running
    await asyncio.sleep(5)

    # Reconnect: GET the run — should have novnc_url + steps
    resp = await client.get(f"/api/runs/{run_id}", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("connecting", "running", "paused_for_auth", "completed", "failed")

    if data["status"] in ("connecting", "running", "paused_for_auth"):
        assert data.get("novnc_url"), "Active run should have novnc_url on reconnect"

    # Let it finish
    final = await poll_run(client, run_id, auth_headers)
    assert final["status"] in ("completed", "failed")
