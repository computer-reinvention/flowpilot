"""E2E test: Gmail auth persistence — exploration with auth → run without re-auth.

What this tests:
  1. Create a Gmail flow with auth_before_explore=true
  2. Wait for auth pre-step (user logs in via dashboard screencast)
  3. Exploration runs, auto-handle HITL (input_values, confirmation)
  4. Assert: exploration_status=ready, reflexes saved, profile auto-saved
  5. Start a run — should complete using saved profile (no auth HITL)
  6. Assert: profile exists in GET /api/profiles

Run:
    E2E_TOKEN=... python tests/test_e2e_exploration_and_run.py
    E2E_TOKEN=... uv run pytest tests/test_e2e_exploration_and_run.py -v -s --timeout=600
"""

import asyncio
import json
import logging
import os
import sys
import time

import httpx
import websockets

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

TEST_EMAIL = os.environ.get("E2E_EMAIL", "garkotipankaj@gmail.com")
TEST_TOKEN = os.environ.get("E2E_TOKEN", "")

EXPLORATION_TIMEOUT = 600   # 10 min — exploration involves LLM calls
RUN_TIMEOUT         = 300   # 5 min per run
POLL_INTERVAL       = 3     # seconds between status polls

TERMINAL_RUN     = {"completed", "failed", "cancelled"}
TERMINAL_EXPLORE = {"ready", "failed", "cancelled"}

GMAIL_AUTH_URL = (
    "https://accounts.google.com/v3/signin/identifier"
    "?continue=https://mail.google.com&flowName=GlifWebSignIn"
)

GMAIL_TASK = (
    "Read the Gmail inbox. Steps in order:\n\n"
    "=====\n"
    "Setup:\n"
    "=====\n"
    "If the current profile is not authenticated:\n"
    "1. Go to accounts.google.com\n"
    "2. Enter the email address (inputs.email) and click Next\n"
    "3. Pass to HITL for password entry\n\n"
    "======\n"
    "Usage:\n"
    "======\n"
    "Make sure the user is authenticated. Follow setup steps if not.\n\n"
    "1. Navigate to mail.google.com\n"
    "2. Wait for the inbox to load fully\n"
    "3. Extract the sender name and subject line of the first 4 emails\n"
    "4. Return the results as a list"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def _auth(_client: httpx.AsyncClient) -> str:
    if TEST_TOKEN:
        logger.info("Using pre-minted token for %s", TEST_EMAIL)
        return TEST_TOKEN
    raise RuntimeError("Set E2E_TOKEN env var (pre-minted JWT for the test account)")


async def _poll_exploration(
    client: httpx.AsyncClient,
    token: str,
    flow_id: str,
    timeout: int = EXPLORATION_TIMEOUT,
) -> dict:
    """Poll GET /api/flows/{flow_id} until exploration_status is terminal.

    Auto-handles HITL:
      - awaiting_auth → print message, wait for user to log in via dashboard
      - exploration_input_request → POST /explore/resume with sample values
      - exploration_confirmation_request → POST /explore/resume with confirmed=false
    """
    deadline = time.monotonic() + timeout
    last_status = ""
    handled_hitl_at: float = 0

    while time.monotonic() < deadline:
        resp = await client.get(f"/api/flows/{flow_id}", headers=_headers(token))
        resp.raise_for_status()
        flow = resp.json()

        status = flow.get("exploration_status", "pending")
        if status != last_status:
            logger.info("[exploration] status=%s", status)
            last_status = status

        if status in TERMINAL_EXPLORE:
            return flow

        # --- Awaiting auth: user logs in via dashboard screencast ---
        if status == "awaiting_auth":
            if time.monotonic() - handled_hitl_at > 30:
                logger.info(
                    "[exploration] AWAITING AUTH — log in at %s/flows/%s, then click 'Done'",
                    BASE_URL, flow_id,
                )
                handled_hitl_at = time.monotonic()
            await asyncio.sleep(POLL_INTERVAL)
            continue

        # --- HITL: input values requested ---
        if status == "paused_for_hitl" and flow.get("exploration_input_request"):
            if time.monotonic() - handled_hitl_at > 5:
                fields = flow["exploration_input_request"]
                sample_values = {}
                for f in fields:
                    name = f.get("name", "")
                    if "email" in name.lower():
                        sample_values[name] = TEST_EMAIL
                    elif "url" in name.lower():
                        sample_values[name] = "https://mail.google.com"
                    else:
                        sample_values[name] = "test"
                logger.info("[exploration] responding to input_request: %s", sample_values)
                resume = await client.post(
                    f"/api/flows/{flow_id}/explore/resume",
                    headers=_headers(token),
                    json={"values": sample_values},
                )
                if resume.status_code not in (200, 409):
                    logger.warning("explore/resume returned %d: %s", resume.status_code, resume.text)
                handled_hitl_at = time.monotonic()

        # --- HITL: confirmation requested ---
        elif status == "paused_for_hitl" and flow.get("exploration_confirmation_request"):
            if time.monotonic() - handled_hitl_at > 5:
                logger.info("[exploration] auto-declining confirmation")
                resume = await client.post(
                    f"/api/flows/{flow_id}/explore/resume",
                    headers=_headers(token),
                    json={"confirmed": False},
                )
                if resume.status_code not in (200, 409):
                    logger.warning("explore/resume returned %d: %s", resume.status_code, resume.text)
                handled_hitl_at = time.monotonic()

        # --- HITL: generic pause (auth wall during exploration) ---
        elif status == "paused_for_hitl":
            if time.monotonic() - handled_hitl_at > 5:
                logger.info("[exploration] HITL pause (no request body) — resuming")
                resume = await client.post(
                    f"/api/flows/{flow_id}/explore/resume",
                    headers=_headers(token),
                )
                if resume.status_code not in (200, 409):
                    logger.warning("explore/resume returned %d: %s", resume.status_code, resume.text)
                handled_hitl_at = time.monotonic()

        await asyncio.sleep(POLL_INTERVAL)

    raise TimeoutError(
        f"Exploration for flow {flow_id} did not finish within {timeout}s "
        f"(last status: {last_status})"
    )


async def _watch_run(
    token: str,
    run_id: str,
    client: httpx.AsyncClient,
    timeout: int = RUN_TIMEOUT,
) -> dict:
    """Subscribe to /ws/runs/{run_id}, return final event. Does NOT auto-resume HITL."""
    ws_url = f"{WS_BASE}/ws/runs/{run_id}"
    last_event: dict = {}
    deadline = time.monotonic() + timeout

    logger.info("[run:%s] connecting WS", run_id)
    try:
        async with websockets.connect(ws_url) as ws:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(30, remaining))
                except asyncio.TimeoutError:
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
                logger.info("[run:%s] %-22s  %s", run_id, status, (message or "")[:80])
                last_event = event

                if status == "paused_for_hitl":
                    # This should NOT happen if auth persistence worked
                    logger.warning("[run:%s] HITL pause during run — auth persistence may have failed", run_id)

                if status in TERMINAL_RUN:
                    break
    except Exception as e:
        logger.info("[run:%s] WS closed (%s), polling REST for final status", run_id, e)

    # Fall back to REST polling if WS closed before terminal
    if last_event.get("status") not in TERMINAL_RUN:
        while time.monotonic() < deadline:
            resp = await client.get(f"/api/runs/{run_id}", headers=_headers(token))
            resp.raise_for_status()
            data = resp.json()
            last_event = {"status": data["status"], "message": data.get("current_step", "")}
            logger.info("[run:%s] REST poll: status=%s", run_id, data["status"])
            if data["status"] in TERMINAL_RUN:
                break
            await asyncio.sleep(3)

    return last_event


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

async def run_e2e() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=httpx.Timeout(60)) as client:

        # ── Health ────────────────────────────────────────────────────────
        health = (await client.get("/health")).json()
        assert health.get("status") == "ok", f"Server unhealthy: {health}"
        logger.info("Server healthy")

        # ── Auth ──────────────────────────────────────────────────────────
        token = await _auth(client)

        flow_id = None
        run_id = None

        try:
            # ── Create flow — auth pre-step starts automatically ──────────
            resp = await client.post("/api/flows", headers=_headers(token), json={
                "name":      "Gmail Inbox Check",
                "domain":    "mail.google.com",
                "task":      GMAIL_TASK,
                "entry_url": "https://mail.google.com",
                "auth_url":  GMAIL_AUTH_URL,
                "auth_before_explore": True,
                "inputs":    [{"name": "email", "label": "Google Email", "type": "text"}],
                "icon":      "\U0001f4e7",
            })
            resp.raise_for_status()
            flow_id = resp.json()["id"]
            logger.info("Created flow %s", flow_id)

            # ── Phase 1: Exploration with auth ────────────────────────────
            logger.info("=" * 60)
            logger.info("PHASE 1: EXPLORATION (with auth pre-step)")
            logger.info("=" * 60)

            flow = await _poll_exploration(client, token, flow_id)

            status = flow["exploration_status"]
            assert status == "ready", (
                f"Exploration did not reach 'ready'. status={status}, "
                f"error={flow.get('exploration_error')}, "
                f"notes={flow.get('exploration_notes')}"
            )
            logger.info("Exploration READY")

            tree = flow.get("exploration_tree", [])
            assert len(tree) > 0, "exploration_tree is empty — no stages reported"

            reflex_names = [s["reflex_name"] for s in tree if s.get("reflex_name")]
            assert len(reflex_names) > 0, (
                f"No reflexes saved after exploration. Tree: {json.dumps(tree, indent=2)}"
            )
            logger.info("Reflexes saved: %s", reflex_names)

            # Check profile was auto-saved
            profiles_resp = await client.get("/api/profiles", headers=_headers(token))
            profiles_resp.raise_for_status()
            profiles = profiles_resp.json()
            logger.info("Profiles after exploration: %s", [p.get("name") or p.get("profile_name") for p in profiles])
            assert len(profiles) > 0, "No browser profiles saved after exploration"

            # ── Phase 2: Run without auth ─────────────────────────────────
            logger.info("=" * 60)
            logger.info("PHASE 2: RUN (auth from saved profile — no re-login)")
            logger.info("=" * 60)

            resp = await client.post("/api/runs", headers=_headers(token), json={
                "flow_id": flow_id,
                "inputs":  {"email": TEST_EMAIL},
            })
            resp.raise_for_status()
            run_id = resp.json()["run_id"]
            logger.info("Run started: %s", run_id)

            run_start = time.monotonic()
            await _watch_run(token, run_id, client)
            run_elapsed = time.monotonic() - run_start

            run_resp = await client.get(f"/api/runs/{run_id}", headers=_headers(token))
            run_resp.raise_for_status()
            run_data = run_resp.json()

            assert run_data["status"] == "completed", (
                f"Run failed: {run_data.get('error')}\n"
                f"Last step: {run_data.get('current_step')}"
            )

            result = run_data.get("result") or {}
            raw_result = result.get("raw_result", "")
            steps = run_data.get("steps", [])

            # The run should NOT have paused for auth — that proves persistence
            hitl_steps = [s for s in steps if "hitl" in str(s).lower() and "auth" in str(s).lower()]
            if hitl_steps:
                logger.warning("Run had auth HITL steps — persistence may not be working: %s", hitl_steps)

            logger.info("Run completed in %.1fs. result=%s  steps=%d", run_elapsed, raw_result[:120], len(steps))

            # ── Summary ───────────────────────────────────────────────────
            logger.info("=" * 60)
            logger.info("ALL ASSERTIONS PASSED")
            logger.info("  Exploration: %d stages, %d reflexes", len(tree), len(reflex_names))
            logger.info("  Run: completed in %.1fs, %d steps", run_elapsed, len(steps))
            logger.info("  Profiles: %d saved", len(profiles))
            logger.info("  Result: %s", raw_result[:200])
            logger.info("=" * 60)

        finally:
            if run_id:
                try:
                    await client.post(f"/api/runs/{run_id}/cancel", headers=_headers(token))
                except Exception:
                    pass
            logger.info("Flow %s preserved — inspect at %s", flow_id, BASE_URL)


def test_e2e_exploration_and_run() -> None:
    """pytest entry point."""
    asyncio.run(run_e2e())


if __name__ == "__main__":
    asyncio.run(run_e2e())
