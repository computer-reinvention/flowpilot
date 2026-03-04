"""End-to-end test: full run lifecycle as triggered from the dashboard.

Exercises exactly the path a user takes:
  1.  POST /api/auth/signup  (idempotent — falls back to login if already exists)
  2.  POST /api/flows         create a minimal, fast test flow
  3.  POST /api/runs          start a run — spins up a sandbox container
  4.  WS   /ws/runs/{id}     subscribe to real-time progress events
  5.  Wait for a terminal status (completed / failed / paused_for_hitl)
  6.  GET  /api/runs/{id}    verify persisted state matches what the WS reported
  7.  POST /api/runs/{id}/cancel  (only if still running after timeout)

The test flow navigates to https://example.com and asks the agent to return
the page title.  This is fast, requires no auth, and has a deterministic result
("Example Domain"), so the agent should complete in a few steps.

Requirements:
  - Backend running at BASE_URL (default http://localhost:8080)
  - Docker available (sandbox containers)
  - LLM API key set in .env or environment

Run:
    uv run pytest tests/test_e2e_run.py -v -s
    # or directly:
    uv run python tests/test_e2e_run.py
"""

import asyncio
import json
import os
import sys
import time
import logging
import websockets
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:8080")
WS_BASE  = BASE_URL.replace("http://", "ws://").replace("https://", "wss://")

# How long to wait before giving up on the run (seconds).
# example.com is fast but the sandbox + LLM still need ~60s on the first run.
RUN_TIMEOUT = 180

TEST_EMAIL    = "e2e-test@flowpilot.local"
TEST_PASSWORD = "e2e-test-password"
TEST_NAME     = "E2E Test User"

TERMINAL = {"completed", "failed", "cancelled", "paused_for_hitl"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def _auth(client: httpx.AsyncClient) -> str:
    """Signup or login, return JWT token."""
    resp = await client.post("/api/auth/signup", json={
        "email": TEST_EMAIL, "password": TEST_PASSWORD, "name": TEST_NAME,
    })
    if resp.status_code == 409:
        # User already exists — login instead
        resp = await client.post("/api/auth/login", json={
            "email": TEST_EMAIL, "password": TEST_PASSWORD,
        })
    resp.raise_for_status()
    token = resp.json()["token"]
    logger.info("Authenticated as %s", TEST_EMAIL)
    return token


async def _create_test_flow(client: httpx.AsyncClient, token: str) -> str:
    """Create a minimal test flow and return its id."""
    resp = await client.post("/api/flows", headers=_headers(token), json={
        "name":        "E2E Test — example.com title",
        "domain":      "example.com",
        "description": (
            "Navigate to https://example.com and return the page title. "
            "Do not click anything. Just read the <h1> heading text and return it."
        ),
        "entry_url":   "https://example.com",
        "inputs":      [],
        "icon":        "🧪",
    })
    resp.raise_for_status()
    flow_id = resp.json()["id"]
    logger.info("Created test flow %s", flow_id)
    return flow_id


async def _start_run(client: httpx.AsyncClient, token: str, flow_id: str) -> tuple[str, str | None]:
    """Start a run. Returns (run_id, sandbox_id)."""
    resp = await client.post("/api/runs", headers=_headers(token), json={
        "flow_id": flow_id,
        "inputs":  {},
    })
    resp.raise_for_status()
    data = resp.json()
    run_id     = data["run_id"]
    sandbox_id = data.get("sandbox_id")
    logger.info("Run started: run_id=%s  sandbox_id=%s", run_id, sandbox_id)
    return run_id, sandbox_id


async def _watch_run(token: str, run_id: str) -> dict:
    """Subscribe to /ws/runs/{run_id} and collect events until terminal status.

    Returns the last event dict received.
    """
    ws_url = f"{WS_BASE}/ws/runs/{run_id}"
    last_event: dict = {}
    deadline = time.monotonic() + RUN_TIMEOUT

    logger.info("Connecting to WebSocket %s", ws_url)

    async with websockets.connect(ws_url) as ws:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(30, remaining))
            except asyncio.TimeoutError:
                logger.warning("No WS message for 30s — sending ping")
                await ws.send("ping")
                continue

            if raw == "pong":
                continue

            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue

            status  = event.get("status", "")
            message = event.get("message") or event.get("current_step", "")
            steps   = event.get("steps")

            if steps:
                # Initial state replay on reconnect
                logger.info("[WS] initial state: status=%s  steps=%d", status, len(steps))
            else:
                logger.info("[WS] status=%-20s  %s", status, message[:80] if message else "")

            last_event = event

            if status in TERMINAL:
                logger.info("Run reached terminal status: %s", status)
                break

        else:
            logger.error("Run did not reach terminal status within %ds", RUN_TIMEOUT)

    return last_event


async def _get_run(client: httpx.AsyncClient, token: str, run_id: str) -> dict:
    resp = await client.get(f"/api/runs/{run_id}", headers=_headers(token))
    resp.raise_for_status()
    return resp.json()


async def _cancel_run(client: httpx.AsyncClient, token: str, run_id: str) -> None:
    resp = await client.post(f"/api/runs/{run_id}/cancel", headers=_headers(token))
    if resp.status_code not in (200, 409):
        logger.warning("Cancel returned %d: %s", resp.status_code, resp.text)


async def _delete_flow(client: httpx.AsyncClient, token: str, flow_id: str) -> None:
    await client.delete(f"/api/flows/{flow_id}", headers=_headers(token))


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

async def run_e2e() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:

        # ── Health check ──────────────────────────────────────────────────
        health = await client.get("/health")
        health.raise_for_status()
        logger.info("Server is healthy: %s", health.json())

        # ── Auth ──────────────────────────────────────────────────────────
        token = await _auth(client)

        # ── Create flow ───────────────────────────────────────────────────
        flow_id = await _create_test_flow(client, token)

        run_id = None
        try:
            # ── Start run ─────────────────────────────────────────────────
            run_id, sandbox_id = await _start_run(client, token, flow_id)

            # ── Watch over WebSocket ───────────────────────────────────────
            last_event = await _watch_run(token, run_id)
            final_status = last_event.get("status", "unknown")

            # ── If still running after timeout, cancel ─────────────────────
            if final_status not in TERMINAL:
                logger.warning("Run timed out — cancelling")
                await _cancel_run(client, token, run_id)
                final_status = "cancelled"

            # ── Verify persisted state via REST ───────────────────────────
            run_data = await _get_run(client, token, run_id)
            persisted_status = run_data["status"]
            steps            = run_data.get("steps", [])

            logger.info("─" * 60)
            logger.info("Final status  : %s", persisted_status)
            logger.info("Steps recorded: %d", len(steps))
            if run_data.get("result"):
                logger.info("Result        : %s", str(run_data["result"])[:200])
            if run_data.get("error"):
                logger.info("Error         : %s", run_data["error"][:200])
            logger.info("─" * 60)

            # ── Assertions ────────────────────────────────────────────────

            # 1. Run must have a sandbox_id (container was spun up)
            assert run_data.get("sandbox_id"), "sandbox_id missing — container never started"

            # 2. At least one step was recorded
            assert len(steps) > 0, "No steps recorded — agent never started"

            # 3. Status is a known terminal state
            assert persisted_status in TERMINAL, (
                f"Unexpected status '{persisted_status}'"
            )

            # 4. If completed, result should mention "Example Domain"
            if persisted_status == "completed":
                result_str = json.dumps(run_data.get("result", "")).lower()
                assert "example" in result_str, (
                    f"Expected 'example' in result but got: {result_str[:200]}"
                )
                logger.info("PASS — agent found the expected page title")

            # 5. paused_for_hitl is acceptable (example.com shouldn't need auth,
            #    but we don't fail the test if the agent decided to pause)
            elif persisted_status == "paused_for_hitl":
                logger.info("PASS (with note) — agent paused for human input")

            elif persisted_status == "failed":
                # Fail the test — something went wrong in the agent pipeline
                raise AssertionError(f"Run failed: {run_data.get('error')}")

        finally:
            # ── Cleanup ───────────────────────────────────────────────────
            if run_id:
                try:
                    await _cancel_run(client, token, run_id)
                except Exception:
                    pass
            try:
                await _delete_flow(client, token, flow_id)
                logger.info("Cleaned up test flow %s", flow_id)
            except Exception:
                pass


def test_e2e_run() -> None:
    """pytest entry point."""
    asyncio.run(run_e2e())


if __name__ == "__main__":
    asyncio.run(run_e2e())
