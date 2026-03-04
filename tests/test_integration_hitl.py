"""End-to-end test for the HITL (Human-In-The-Loop) auth flow.

Exercises the full API lifecycle:
  signup → create flow → start run → agent pauses for auth →
  user resumes → cookies captured → agent continues

Uses real HTTP endpoints with mocked browser/agent internals.
Run: uv run pytest tests/test_e2e_hitl.py -v
"""

import json
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from backend.models import Flow, Run, Session
from backend.runner import RunStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def e2e_client(tmp_path):
    """Full-stack async client with isolated DB, mocked sandbox/profile managers."""
    db_path = tmp_path / "e2e.db"

    with (
        patch("backend.config.DB_PATH", db_path),
        patch("backend.config.PROFILES_DIR", tmp_path),
        patch("backend.auth.DB_PATH", db_path),
        patch("backend.db.DB_PATH", db_path),
        patch("backend.main.PROFILES_DIR", tmp_path),
        patch("backend.main.sandbox_mgr") as mock_sandbox,
        patch("backend.main.profile_mgr") as mock_profile,
    ):
        # Configure sandbox mock to return plausible values
        mock_sandbox.start.return_value = {
            "sandbox_id": "sb-e2e-1",
            "cdp_url": "ws://localhost:19222",
            "browser_cdp_url": "http://localhost:16080/vnc.html",
        }
        mock_sandbox.cleanup_orphans.return_value = 0
        mock_sandbox.stop_all = MagicMock()
        mock_sandbox.stop = MagicMock()
        mock_sandbox.get_browser_cdp_url = MagicMock(return_value="http://localhost:16080/vnc.html")

        mock_profile.has_profile.return_value = False

        from backend import auth
        from backend import db as platform_db
        from backend.main import app

        await auth.init_db()
        await platform_db.init_db()

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.fixture
async def e2e_user(e2e_client):
    """Create a test user and return (user_dict, auth_headers)."""
    resp = await e2e_client.post("/api/auth/signup", json={
        "email": "hitl@example.com",
        "password": "testpass123",
        "name": "HITL Tester",
    })
    assert resp.status_code == 200
    data = resp.json()
    headers = {"Authorization": f"Bearer {data['token']}"}
    return data["user"], headers


@pytest.fixture
async def e2e_flow(e2e_client, e2e_user):
    """Create a test flow that requires auth."""
    _, headers = e2e_user
    resp = await e2e_client.post("/api/flows", headers=headers, json={
        "name": "Protected Site Flow",
        "domain": "protected.example.com",
        "description": "Navigate to the protected page and extract the user dashboard data.",
        "entry_url": "https://protected.example.com/dashboard",
        "inputs": [
            {"name": "username", "label": "Username", "type": "text"},
        ],
    })
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# E2E HITL Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_hitl_full_lifecycle(e2e_client, e2e_user, e2e_flow):
    """
    Full lifecycle test:
    1. Start a run via the API
    2. Simulate the agent reaching paused_for_hitl state
    3. Resume the agent via the API
    4. Verify cookies are captured and session is persisted
    5. Verify the run transitions back to running
    """
    from backend.main import _run_agents, _run_tasks
    import backend.db as db_mod

    user, headers = e2e_user
    flow = e2e_flow
    user_id = user["id"]

    # ── Step 1: Start a run ──────────────────────────────────────────
    # We need to mock flow_runner.run_flow since we don't have a real CDP
    # endpoint. Instead, we'll intercept the _execute_run to set up the
    # paused state manually.

    run_started = asyncio.Event()
    run_id_holder = {}

    original_execute_run = None
    # We'll patch at the module level
    import backend.main as main_mod

    original_run_flow = main_mod.flow_runner.run_flow

    async def fake_run_flow(
        flow, inputs, user_id, sandbox_id, cdp_url, run_id,
        progress_callback=None, storage_state=None, agent_ready_callback=None,
    ):
        """Simulates an agent that pauses for auth mid-run."""
        run_id_holder["id"] = run_id

        # Simulate: connecting → running → paused_for_hitl
        if progress_callback:
            await progress_callback(RunStatus.CONNECTING, "Connecting to sandbox...")
            await progress_callback(RunStatus.RUNNING, "Agent started")

        # Set up mock agent and browser
        mock_agent = MagicMock()
        mock_browser = MagicMock()
        mock_browser.get_storage_state = AsyncMock(return_value={
            "cookies": [
                {"name": "auth_token", "value": "tok_abc123", "domain": "protected.example.com"},
                {"name": "session_id", "value": "sess_xyz", "domain": "protected.example.com"},
            ],
            "origins": [],
        })

        # Register agent via callback (same as real run_flow does)
        if agent_ready_callback:
            paused_ref = [False]
            await agent_ready_callback(mock_agent, mock_browser, flow, paused_ref)

        # Agent hits a login wall → pauses
        if progress_callback:
            await progress_callback(RunStatus.PAUSED_FOR_HITL, "Login needed: This site requires authentication")

        run_started.set()

        # Wait for resume (simulates agent.pause() blocking)
        resume_event = asyncio.Event()
        mock_agent.resume = lambda: resume_event.set()

        # Wait up to 10 seconds for resume
        await asyncio.wait_for(resume_event.wait(), timeout=10)

        # After resume, agent continues and completes
        if progress_callback:
            await progress_callback(RunStatus.RUNNING, "Resumed — continuing with authenticated session")
            await progress_callback(RunStatus.COMPLETED, "Flow completed successfully")

        return {
            "raw_result": "Dashboard data extracted successfully",
            "flow_name": flow.name,
            "inputs": inputs,
        }

    main_mod.flow_runner.run_flow = fake_run_flow

    try:
        # Start the run via API
        resp = await e2e_client.post("/api/runs", headers=headers, json={
            "flow_id": flow["id"],
            "inputs": {"username": "testuser"},
        })
        assert resp.status_code == 200, f"Failed to start run: {resp.text}"
        run_data = resp.json()
        run_id = run_data["run_id"]
        assert run_data["status"] == "connecting"

        # Wait for the fake agent to reach paused state
        await asyncio.wait_for(run_started.wait(), timeout=5)
        # Small delay to let DB update propagate
        await asyncio.sleep(0.2)

        # ── Step 2: Verify run is paused + has browser_cdp_url ──────────────
        resp = await e2e_client.get(f"/api/runs/{run_id}", headers=headers)
        assert resp.status_code == 200
        run_state = resp.json()
        assert run_state["status"] == "paused_for_hitl", f"Expected paused_for_hitl, got {run_state['status']}"
        # browser_cdp_url should be present for active runs (mocked sandbox returns it)
        assert "browser_cdp_url" in run_state

        # Check steps include the pause message
        steps = run_state["steps"]
        pause_steps = [s for s in steps if s["status"] == "paused_for_hitl"]
        assert len(pause_steps) > 0, "No paused_for_hitl step found in run steps"
        assert "Login needed" in pause_steps[0]["message"]

        # ── Step 3: Verify no sessions exist yet ─────────────────────
        resp = await e2e_client.get("/api/sessions", headers=headers)
        assert resp.status_code == 200
        sessions_before = resp.json()
        sessions_for_domain = [s for s in sessions_before if s.get("domain") == "protected.example.com"]
        assert len(sessions_for_domain) == 0, "Session should not exist before resume"

        # ── Step 4: Resume the run ───────────────────────────────────
        resp = await e2e_client.post(f"/api/runs/{run_id}/resume", headers=headers)
        assert resp.status_code == 200, f"Resume failed: {resp.text}"
        resume_data = resp.json()
        assert resume_data["status"] == "running"

        # ── Step 5: Verify cookies were captured as a session ────────
        resp = await e2e_client.get("/api/sessions", headers=headers)
        assert resp.status_code == 200
        sessions_after = resp.json()
        sessions_for_domain = [s for s in sessions_after if s.get("domain") == "protected.example.com"]
        assert len(sessions_for_domain) == 1, f"Expected 1 session, got {len(sessions_for_domain)}"
        assert sessions_for_domain[0]["domain"] == "protected.example.com"
        # profile_name should be set to the flow domain on HITL resume
        assert sessions_for_domain[0]["profile_name"] == "protected.example.com"

        # ── Step 6: Wait for run to complete ─────────────────────────
        # The fake_run_flow will continue after resume
        task = _run_tasks.get(run_id)
        if task:
            await asyncio.wait_for(task, timeout=10)

        # Small delay for DB writes
        await asyncio.sleep(0.2)

        resp = await e2e_client.get(f"/api/runs/{run_id}", headers=headers)
        assert resp.status_code == 200
        final_state = resp.json()
        assert final_state["status"] == "completed", f"Expected completed, got {final_state['status']}"
        assert final_state["result"] is not None
        assert "Dashboard data extracted" in str(final_state["result"])

        # ── Step 7: Verify full step history ─────────────────────────
        steps = final_state["steps"]
        statuses = [s["status"] for s in steps]
        assert "connecting" in statuses, "Missing connecting step"
        assert "running" in statuses, "Missing running step"
        assert "paused_for_hitl" in statuses, "Missing paused_for_hitl step"
        assert "completed" in statuses, "Missing completed step"

    finally:
        main_mod.flow_runner.run_flow = original_run_flow
        _run_agents.pop(run_id_holder.get("id", ""), None)


@pytest.mark.asyncio
async def test_e2e_hitl_resume_wrong_user(e2e_client, e2e_user, e2e_flow):
    """
    Verify that a different user cannot resume another user's paused run.
    """
    from backend.main import _run_agents
    import backend.db as db_mod

    user, headers = e2e_user
    flow = e2e_flow

    # Create a second user
    resp = await e2e_client.post("/api/auth/signup", json={
        "email": "attacker@example.com",
        "password": "badpass123",
        "name": "Attacker",
    })
    assert resp.status_code == 200
    attacker_headers = {"Authorization": f"Bearer {resp.json()['token']}"}

    # Create a paused run for the first user directly in DB
    run = Run(
        id="run-authz-test",
        flow_id=flow["id"],
        user_id=user["id"],
        status="paused_for_hitl",
        sandbox_id="sb-1",
    )
    await db_mod.create_run(run)

    mock_flow = Flow(
        id=flow["id"], name=flow["name"], domain=flow["domain"],
        description=flow["description"], inputs=json.dumps(flow["inputs"]),
        created_by=user["id"],
    )
    mock_agent = MagicMock()
    mock_agent.resume = MagicMock()
    mock_browser = MagicMock()
    mock_browser.get_storage_state = AsyncMock(return_value={
        "cookies": [{"name": "authz_cookie", "value": "secret", "domain": "protected.example.com"}],
    })
    _run_agents["run-authz-test"] = (mock_agent, mock_browser, mock_flow)

    try:
        # Attacker tries to resume — should be rejected
        resp = await e2e_client.post("/api/runs/run-authz-test/resume", headers=attacker_headers)
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}"

        # Verify run is still paused after attacker's failed attempt
        resp = await e2e_client.get("/api/runs/run-authz-test", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "paused_for_hitl", "Run should still be paused after 403"
        mock_agent.resume.assert_not_called()

        # Owner resumes — should succeed
        resp = await e2e_client.post("/api/runs/run-authz-test/resume", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"

        # Verify agent.resume() was called exactly once (only by owner)
        mock_agent.resume.assert_called_once()

        # Verify cookies were captured
        mock_browser.get_storage_state.assert_called_once()

        # Verify session was persisted for owner with profile_name
        resp = await e2e_client.get("/api/sessions", headers=headers)
        sessions = [s for s in resp.json() if s["domain"] == "protected.example.com"]
        assert len(sessions) == 1, f"Expected 1 session for owner, got {len(sessions)}"
        assert sessions[0]["profile_name"] == "protected.example.com"

        # Verify attacker has no sessions
        resp = await e2e_client.get("/api/sessions", headers=attacker_headers)
        attacker_sessions = [s for s in resp.json() if s["domain"] == "protected.example.com"]
        assert len(attacker_sessions) == 0, "Attacker should have no sessions"

        # Verify DB status is running
        resp = await e2e_client.get("/api/runs/run-authz-test", headers=headers)
        assert resp.json()["status"] == "running"
    finally:
        _run_agents.pop("run-authz-test", None)


@pytest.mark.asyncio
async def test_e2e_hitl_manual_pause_then_resume(e2e_client, e2e_user, e2e_flow):
    """
    Test the manual pause fallback: user pauses a running agent, then resumes.
    """
    from backend.main import _run_agents
    import backend.db as db_mod

    user, headers = e2e_user
    flow = e2e_flow

    # Create a running run directly in DB
    run = Run(
        id="run-manual-pause",
        flow_id=flow["id"],
        user_id=user["id"],
        status="running",
        sandbox_id="sb-2",
    )
    await db_mod.create_run(run)

    mock_flow = Flow(
        id=flow["id"], name=flow["name"], domain=flow["domain"],
        description=flow["description"], inputs=json.dumps(flow["inputs"]),
        created_by=user["id"],
    )
    mock_agent = MagicMock()
    mock_browser = MagicMock()
    mock_browser.get_storage_state = AsyncMock(return_value={
        "cookies": [{"name": "manual_cookie", "value": "val", "domain": "protected.example.com"}],
    })
    _run_agents["run-manual-pause"] = (mock_agent, mock_browser, mock_flow)

    try:
        # Step 1: Manually pause
        resp = await e2e_client.post("/api/runs/run-manual-pause/pause", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "paused_for_hitl"
        mock_agent.pause.assert_called_once()

        # Step 2: Verify state in DB
        resp = await e2e_client.get("/api/runs/run-manual-pause", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "paused_for_hitl"

        # Step 3: Resume
        resp = await e2e_client.post("/api/runs/run-manual-pause/resume", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"
        mock_agent.resume.assert_called_once()

        # Step 4: Verify session was created with profile_name
        resp = await e2e_client.get("/api/sessions", headers=headers)
        sessions = [s for s in resp.json() if s["domain"] == "protected.example.com"]
        assert len(sessions) >= 1
        assert sessions[0]["profile_name"] == "protected.example.com"

        # Step 5: Verify state back to running
        resp = await e2e_client.get("/api/runs/run-manual-pause", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"
    finally:
        _run_agents.pop("run-manual-pause", None)
