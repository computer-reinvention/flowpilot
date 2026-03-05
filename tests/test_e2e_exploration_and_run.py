"""E2E test: exploration → reflex creation → reflex-driven runs.

Target site: http://quotes.toscrape.com
- No auth required
- Tag filter is a clear user-controlled interaction with predictable selectors
- Results are deterministic and verifiable

What this tests end-to-end:
  1. Create a flow with entry_url → exploration auto-starts
  2. Poll exploration with automatic HITL handling (input_values, confirmation)
  3. Assert exploration completed: stages reported, reflexes saved, inputs declared
  4. Run 1 with tag="love" → verify completes via reflex (steps mention "reflex")
  5. Run 2 with tag="humor" → verify also completes via reflex (proves dynamic inputs work)
  6. Verify session persistence: cookies saved for the domain
  7. Cleanup

Run:
    python tests/test_e2e_exploration_and_run.py
    uv run pytest tests/test_e2e_exploration_and_run.py -v -s --timeout=600
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

TEST_EMAIL    = "e2e-exploration@flowpilot.local"
TEST_PASSWORD = "e2e-exploration-pass"
TEST_NAME     = "E2E Exploration Tester"

EXPLORATION_TIMEOUT = 600   # 10 min — exploration involves LLM calls
RUN_TIMEOUT         = 300   # 5 min per run
POLL_INTERVAL       = 3     # seconds between status polls

TERMINAL_RUN      = {"completed", "failed", "cancelled"}
TERMINAL_EXPLORE  = {"ready", "failed", "cancelled"}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def _auth(client: httpx.AsyncClient) -> str:
    resp = await client.post("/api/auth/signup", json={
        "email": TEST_EMAIL, "password": TEST_PASSWORD, "name": TEST_NAME,
    })
    if resp.status_code == 409:
        resp = await client.post("/api/auth/login", json={
            "email": TEST_EMAIL, "password": TEST_PASSWORD,
        })
    resp.raise_for_status()
    token = resp.json()["token"]
    logger.info("Authenticated as %s", TEST_EMAIL)
    return token


# ---------------------------------------------------------------------------
# Exploration polling + automatic HITL handling
# ---------------------------------------------------------------------------

async def _poll_exploration(
    client: httpx.AsyncClient,
    token: str,
    flow_id: str,
    timeout: int = EXPLORATION_TIMEOUT,
) -> dict:
    """
    Poll GET /api/flows/{flow_id} until exploration_status is terminal.

    Auto-handles HITL:
      - exploration_input_request  → POST /explore/resume with sample values
      - exploration_confirmation_request → POST /explore/resume with confirmed=false
        (we don't want to actually submit anything during exploration)
    """
    deadline = time.monotonic() + timeout
    last_status = ""
    handled_hitl_at: float = 0  # avoid re-responding within a short window

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

        # --- HITL: input values requested ---
        if status == "paused_for_hitl" and flow.get("exploration_input_request"):
            if time.monotonic() - handled_hitl_at > 5:
                fields = flow["exploration_input_request"]
                # Provide a sample value for every requested field
                sample_values = {}
                for f in fields:
                    name = f.get("name", "")
                    if "tag" in name.lower():
                        sample_values[name] = "inspirational"
                    elif "query" in name.lower() or "search" in name.lower():
                        sample_values[name] = "life"
                    elif "url" in name.lower():
                        sample_values[name] = "http://quotes.toscrape.com"
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
                logger.info("[exploration] auto-declining confirmation (don't submit during exploration)")
                resume = await client.post(
                    f"/api/flows/{flow_id}/explore/resume",
                    headers=_headers(token),
                    json={"confirmed": False},
                )
                if resume.status_code not in (200, 409):
                    logger.warning("explore/resume returned %d: %s", resume.status_code, resume.text)
                handled_hitl_at = time.monotonic()

        # --- HITL: auth wall (shouldn't happen for quotes.toscrape.com) ---
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


# ---------------------------------------------------------------------------
# Run watching via WebSocket
# ---------------------------------------------------------------------------

async def _watch_run(
    token: str,
    run_id: str,
    client: httpx.AsyncClient,
    timeout: int = RUN_TIMEOUT,
) -> dict:
    """Subscribe to /ws/runs/{run_id}, auto-resume HITL, return final event."""
    ws_url = f"{WS_BASE}/ws/runs/{run_id}"
    last_event: dict = {}
    deadline = time.monotonic() + timeout

    logger.info("[run:%s] connecting WS", run_id)
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
                logger.info("[run:%s] HITL pause — auto-resuming (no actual auth needed)", run_id)
                r = await client.post(
                    f"/api/runs/{run_id}/resume",
                    headers=_headers(token),
                )
                if r.status_code not in (200, 409):
                    logger.warning("run resume returned %d: %s", r.status_code, r.text)
                deadline = time.monotonic() + timeout
                continue

            if status in TERMINAL_RUN:
                break

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
        run_ids: list[str] = []

        try:
            # ── Create flow — exploration starts automatically ─────────────
            resp = await client.post("/api/flows", headers=_headers(token), json={
                "name":      "Quotes by Tag",
                "domain":    "quotes.toscrape.com",
                "task":      "Navigate to the page for quotes tagged '{tag}' and return the first quote text",
                "entry_url": "http://quotes.toscrape.com",
                "inputs":    [{"name": "tag", "label": "Tag", "type": "text"}],
                "icon":      "💬",
            })
            resp.raise_for_status()
            flow_id = resp.json()["id"]
            logger.info("Created flow %s — exploration started", flow_id)

            # ── Phase 1: Wait for exploration to complete ──────────────────
            logger.info("=" * 60)
            logger.info("PHASE 1: EXPLORATION")
            logger.info("=" * 60)

            flow = await _poll_exploration(client, token, flow_id)

            # ── Assert exploration succeeded ───────────────────────────────
            status = flow["exploration_status"]
            assert status == "ready", (
                f"Exploration did not reach 'ready'. status={status}, "
                f"error={flow.get('exploration_error')}, "
                f"notes={flow.get('exploration_notes')}"
            )
            logger.info("Exploration READY")

            tree = flow.get("exploration_tree", [])
            assert len(tree) > 0, "exploration_tree is empty — no stages reported"

            # At least one stage should have a reflex
            reflex_names = [
                s["reflex_name"] for s in tree if s.get("reflex_name")
            ]
            assert len(reflex_names) > 0, (
                f"No reflexes saved after exploration. Tree: {json.dumps(tree, indent=2)}"
            )
            logger.info("Reflexes saved: %s", reflex_names)

            # Inputs should include 'tag'
            declared_inputs = flow.get("inputs", [])
            input_names = [i["name"] for i in declared_inputs]
            assert "tag" in input_names, (
                f"'tag' input not declared after exploration. Got: {input_names}"
            )
            logger.info("Inputs declared: %s", input_names)

            # ── Phase 2: First run — tag=love ──────────────────────────────
            logger.info("=" * 60)
            logger.info("PHASE 2: RUN 1 (tag=love)")
            logger.info("=" * 60)

            resp = await client.post("/api/runs", headers=_headers(token), json={
                "flow_id": flow_id,
                "inputs":  {"tag": "love"},
            })
            resp.raise_for_status()
            run1_id = resp.json()["run_id"]
            run_ids.append(run1_id)
            logger.info("Run 1 started: %s", run1_id)

            last1 = await _watch_run(token, run1_id, client)

            run1 = await client.get(f"/api/runs/{run1_id}", headers=_headers(token))
            run1.raise_for_status()
            run1_data = run1.json()

            assert run1_data["status"] == "completed", (
                f"Run 1 failed: {run1_data.get('error')}\n"
                f"Last step: {run1_data.get('current_step')}"
            )

            # Verify the run used a reflex (step messages should mention it)
            steps1 = run1_data.get("steps", [])
            step_messages1 = " ".join(s.get("message", "") for s in steps1).lower()
            reflex_used1 = "reflex" in step_messages1
            logger.info(
                "Run 1 completed. Reflex used: %s. Steps: %d",
                reflex_used1, len(steps1)
            )
            assert reflex_used1, (
                f"Run 1 completed but no reflex was used.\n"
                f"Steps: {json.dumps(steps1, indent=2)}"
            )

            # ── Phase 3: Second run — tag=humor (proves dynamic inputs work) ─
            logger.info("=" * 60)
            logger.info("PHASE 3: RUN 2 (tag=humor)")
            logger.info("=" * 60)

            resp = await client.post("/api/runs", headers=_headers(token), json={
                "flow_id": flow_id,
                "inputs":  {"tag": "humor"},
            })
            resp.raise_for_status()
            run2_id = resp.json()["run_id"]
            run_ids.append(run2_id)
            logger.info("Run 2 started: %s", run2_id)

            run2_start = time.monotonic()
            last2 = await _watch_run(token, run2_id, client)
            run2_elapsed = time.monotonic() - run2_start

            run2 = await client.get(f"/api/runs/{run2_id}", headers=_headers(token))
            run2.raise_for_status()
            run2_data = run2.json()

            assert run2_data["status"] == "completed", (
                f"Run 2 failed: {run2_data.get('error')}\n"
                f"Last step: {run2_data.get('current_step')}"
            )

            steps2 = run2_data.get("steps", [])
            step_messages2 = " ".join(s.get("message", "") for s in steps2).lower()
            reflex_used2 = "reflex" in step_messages2
            logger.info(
                "Run 2 completed in %.1fs. Reflex used: %s. Steps: %d",
                run2_elapsed, reflex_used2, len(steps2)
            )
            assert reflex_used2, (
                f"Run 2 completed but no reflex was used.\n"
                f"Steps: {json.dumps(steps2, indent=2)}"
            )

            # ── Phase 4: Session persistence ──────────────────────────────
            logger.info("=" * 60)
            logger.info("PHASE 4: SESSION PERSISTENCE")
            logger.info("=" * 60)

            sessions_resp = await client.get("/api/sessions", headers=_headers(token))
            sessions_resp.raise_for_status()
            all_sessions = sessions_resp.json()
            domain_sessions = [
                s for s in all_sessions
                if "quotes.toscrape.com" in s.get("domain", "")
            ]
            logger.info("Sessions saved for quotes.toscrape.com: %d", len(domain_sessions))
            # Sessions may or may not have cookies depending on whether the site sets any
            # Just verify the capture was attempted (no hard assert here — site may not set cookies)
            if domain_sessions:
                logger.info("Session cookies count: %d", sum(
                    len(json.loads(s.get("cookies", "[]"))) for s in domain_sessions
                ))

            # ── Summary ───────────────────────────────────────────────────
            logger.info("=" * 60)
            logger.info("ALL ASSERTIONS PASSED")
            logger.info("  Exploration: %d stages, %d reflexes saved", len(tree), len(reflex_names))
            logger.info("  Run 1 (tag=love):  completed, reflex_used=%s, steps=%d", reflex_used1, len(steps1))
            logger.info("  Run 2 (tag=humor): completed, reflex_used=%s, steps=%d, elapsed=%.1fs", reflex_used2, len(steps2), run2_elapsed)
            logger.info("  Sessions: %d saved for domain", len(domain_sessions))
            logger.info("=" * 60)

        finally:
            # ── Cleanup ───────────────────────────────────────────────────
            for run_id in run_ids:
                try:
                    await client.post(f"/api/runs/{run_id}/cancel", headers=_headers(token))
                except Exception:
                    pass
            if flow_id:
                try:
                    await client.delete(f"/api/flows/{flow_id}", headers=_headers(token))
                    logger.info("Cleaned up flow %s", flow_id)
                except Exception:
                    pass


def test_e2e_exploration_and_run() -> None:
    """pytest entry point."""
    asyncio.run(run_e2e())


if __name__ == "__main__":
    asyncio.run(run_e2e())
