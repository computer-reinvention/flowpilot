"""FastAPI backend for browser automation platform."""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .sandbox import SandboxManager
from .profile import ProfileManager
from .runner import FlowRunner, RunStatus
from .config import PROFILES_DIR, BASE_URL
from . import auth
from . import db as platform_db
from . import cdp_proxy
from . import webhooks as webhook_svc
from .models import Flow, Run, Session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", "/app/frontend"))

# ---------------------------------------------------------------------------
# Singleton services
# ---------------------------------------------------------------------------

_profiles_host_dir = os.environ.get("PROFILES_HOST_DIR", str(PROFILES_DIR))
sandbox_mgr = SandboxManager(profiles_host_dir=_profiles_host_dir)
profile_mgr = ProfileManager(profiles_dir=PROFILES_DIR)
flow_runner = FlowRunner()

# ---------------------------------------------------------------------------
# WebSocket connection registry
# ---------------------------------------------------------------------------

_ws_connections: dict[str, list[WebSocket]] = {}  # run_id -> [websocket, ...]
_run_tasks: dict[str, asyncio.Task] = {}  # run_id -> background task
_run_agents: dict[str, tuple] = {}  # run_id -> (agent, browser, flow, paused_ref)

ACTIVE_RUN_STATUSES: frozenset[str] = frozenset({"pending", "connecting", "running", "paused_for_hitl"})


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    await platform_db.init_db()
    await platform_db.seed_default_flows()

    # Rebuild _sandboxes from DB, clean up dead containers
    db_sandboxes = await platform_db.list_active_sandboxes()
    dead_ids = sandbox_mgr.reconcile_from_db(db_sandboxes)
    for sid in dead_ids:
        await platform_db.delete_sandbox(sid)
    if db_sandboxes:
        logger.info("Reconciled %d sandboxes from DB (%d dead/gone)", len(db_sandboxes), len(dead_ids))

    # Orphan cleanup: Docker containers with flowpilot label not in _sandboxes at all
    removed = sandbox_mgr.cleanup_orphans()
    if removed:
        logger.info("Cleaned up %d orphaned Docker containers", removed)

    logger.info("Platform backend starting. Profiles dir: %s", PROFILES_DIR)
    yield
    sandbox_mgr.stop_all()
    logger.info("Platform backend shutting down.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Browser Automation Platform",
    description="Generic browser automation via flow definitions",
    version="1.0.0",
    lifespan=lifespan,
)

_allowed_origins = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:8080,http://localhost:3000").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class SignupRequest(BaseModel):
    email: str
    password: str
    name: str


class LoginRequest(BaseModel):
    email: str
    password: str


class SandboxStartRequest(BaseModel):
    profile_name: str | None = None


class SandboxStopRequest(BaseModel):
    sandbox_id: str


class ProfileSaveRequest(BaseModel):
    sandbox_id: str
    profile_name: str = "default"


class FlowCreateRequest(BaseModel):
    name: str
    domain: str
    description: str
    entry_url: str | None = None
    inputs: list[dict] = []
    icon: str | None = None


class FlowUpdateRequest(BaseModel):
    name: str | None = None
    domain: str | None = None
    description: str | None = None
    entry_url: str | None = None
    inputs: list[dict] | None = None
    icon: str | None = None


class RunStartRequest(BaseModel):
    flow_id: str
    inputs: dict = {}
    profile_id: str | None = None  # defaults to user_id


class ApiKeyCreateRequest(BaseModel):
    name: str


class WebhookCreateRequest(BaseModel):
    url: str
    events: list[str] = []  # empty = subscribe to all


class SessionCaptureRequest(BaseModel):
    domain: str
    cookies: list[dict] = []
    localStorage: dict = {}
    sessionStorage: dict = {}
    userAgent: str | None = None
    profile_name: str = "default"


# ---------------------------------------------------------------------------
# Health (no auth)
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# Auth endpoints (no auth required)
# ---------------------------------------------------------------------------

@app.post("/api/auth/signup")
async def signup(req: SignupRequest) -> dict:
    if not req.email or not req.password or not req.name:
        raise HTTPException(status_code=400, detail="email, password, and name are required")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    try:
        user = await auth.create_user(req.email, req.password, req.name)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    token = auth.create_token(user["id"])
    return {"token": token, "user": user}


@app.post("/api/auth/login")
async def login(req: LoginRequest) -> dict:
    user = await auth.authenticate(req.email, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = auth.create_token(user["id"])
    return {"token": token, "user": user}


@app.get("/api/auth/me")
async def auth_me(user: dict = Depends(auth.get_current_user)) -> dict:
    return {"user": user}


# ---------------------------------------------------------------------------
# Sandbox endpoints (auth required)
# ---------------------------------------------------------------------------

@app.post("/api/sandbox/start")
async def sandbox_start(req: SandboxStartRequest, user: dict = Depends(auth.get_current_user)) -> dict:
    user_id = user["id"]
    profile_name = req.profile_name or "default"
    if not SandboxManager.validate_profile_name(profile_name):
        raise HTTPException(status_code=400, detail="Invalid profile name.")
    profile_path = None
    if profile_mgr.has_profile(user_id, profile_name):
        profile_path = str(PROFILES_DIR / user_id / profile_name)
    try:
        result = sandbox_mgr.start(profile_path=profile_path, user_id=user_id)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    await platform_db.create_sandbox(result["sandbox_id"], result["container_id"], user_id, result["cdp_url"])
    if profile_mgr.has_profile(user_id, profile_name):
        try:
            profile_mgr.restore_profile(user_id, profile_name, result["sandbox_id"])
        except Exception as e:
            logger.warning("Profile restore failed: %s", e)
    return result


@app.post("/api/sandbox/stop")
async def sandbox_stop(req: SandboxStopRequest, user: dict = Depends(auth.get_current_user)) -> dict:
    user_id = user["id"]
    try:
        owner = sandbox_mgr.get_sandbox_owner(req.sandbox_id)
        if owner and owner != user_id:
            raise HTTPException(status_code=403, detail="Sandbox does not belong to you")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        sandbox_mgr.stop(req.sandbox_id)
        await platform_db.delete_sandbox(req.sandbox_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "stopped", "sandbox_id": req.sandbox_id}


# ---------------------------------------------------------------------------
# Flow endpoints
# ---------------------------------------------------------------------------

@app.post("/api/flows")
async def flow_create(req: FlowCreateRequest, user: dict = Depends(auth.get_current_user)) -> dict:
    flow = Flow(
        id=str(uuid.uuid4())[:12],
        name=req.name,
        domain=req.domain,
        description=req.description,
        entry_url=req.entry_url,
        inputs=json.dumps(req.inputs),
        icon=req.icon,
        created_by=user["id"],
    )
    await platform_db.create_flow(flow)
    return _flow_to_dict(flow)


@app.get("/api/flows")
async def flow_list() -> list[dict]:
    rows = await platform_db.list_flows()
    for r in rows:
        r["inputs"] = json.loads(r.get("inputs", "[]"))
    return rows


@app.get("/api/flows/{flow_id}")
async def flow_get(flow_id: str) -> dict:
    flow = await platform_db.get_flow(flow_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")
    return _flow_to_dict(flow)


@app.put("/api/flows/{flow_id}")
async def flow_update(flow_id: str, req: FlowUpdateRequest, user: dict = Depends(auth.get_current_user)) -> dict:
    updates = req.model_dump(exclude_none=True)
    if "inputs" in updates:
        updates["inputs"] = json.dumps(updates["inputs"])
    flow = await platform_db.update_flow(flow_id, updates)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")
    return _flow_to_dict(flow)


@app.delete("/api/flows/{flow_id}")
async def flow_delete(flow_id: str, user: dict = Depends(auth.get_current_user)) -> dict:
    deleted = await platform_db.delete_flow(flow_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Flow not found")
    return {"status": "deleted"}



def _flow_to_dict(flow: Flow) -> dict:
    return {
        "id": flow.id,
        "name": flow.name,
        "domain": flow.domain,
        "description": flow.description,
        "entry_url": flow.entry_url,
        "inputs": json.loads(flow.inputs),
        "icon": flow.icon,
        "created_by": flow.created_by,
        "created_at": flow.created_at,
    }


# ---------------------------------------------------------------------------
# Session endpoints (extension capture)
# ---------------------------------------------------------------------------

@app.post("/api/sessions/capture")
async def session_capture(req: SessionCaptureRequest, user: dict = Depends(auth.get_current_user)) -> dict:
    session = Session(
        id=str(uuid.uuid4())[:12],
        user_id=user["id"],
        domain=req.domain,
        cookies=json.dumps(req.cookies),
        local_storage=json.dumps(req.localStorage),
        session_storage=json.dumps(req.sessionStorage),
        user_agent=req.userAgent,
        profile_name=req.profile_name,
    )
    await platform_db.create_session(session)
    return {"id": session.id, "domain": session.domain, "profile_name": session.profile_name, "captured_at": session.captured_at}


@app.get("/api/sessions")
async def session_list(user: dict = Depends(auth.get_current_user)) -> list[dict]:
    return await platform_db.list_sessions(user["id"])


@app.delete("/api/sessions/{session_id}")
async def session_delete(session_id: str, user: dict = Depends(auth.get_current_user)) -> dict:
    deleted = await platform_db.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Run endpoints
# ---------------------------------------------------------------------------

_EVENT_MAP: dict[RunStatus, str] = {
    RunStatus.RUNNING: "run.started",
    RunStatus.PAUSED_FOR_HITL: "run.paused",
    RunStatus.COMPLETED: "run.completed",
    RunStatus.FAILED: "run.failed",
}


def _build_webhook_payload(
    event: str,
    run_id: str,
    status: str,
    message: str,
    sandbox_id: str | None,
    profile_id: str,
    category: str | None = None,
) -> dict:
    payload: dict = {
        "event": event,
        "run_id": run_id,
        "status": status,
        "message": message,
        "profile_id": profile_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if sandbox_id and event in ("run.started", "run.paused"):
        payload["browser_cdp_url"] = f"{BASE_URL}/ws/browser/{sandbox_id}"
    if event == "run.paused":
        payload["resume_url"] = f"/api/v1/runs/{run_id}/resume"
        normalized = category if category in webhook_svc.HITL_CATEGORIES else "other"
        payload["category"] = normalized
    return payload


async def _start_run_impl(
    flow_id: str,
    inputs: dict,
    user_id: str,
    profile_id: str,
) -> dict:
    """Shared logic for starting a run — used by both /api/runs and /api/v1/runs."""
    flow = await platform_db.get_flow(flow_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")

    run_id = str(uuid.uuid4())[:12]
    run = Run(
        id=run_id,
        flow_id=flow.id,
        user_id=user_id,
        status="pending",
        inputs=json.dumps(inputs),
        profile_id=profile_id,
    )
    await platform_db.create_run(run)

    # Start sandbox
    profile_path = None
    if profile_mgr.has_profile(user_id, "default"):
        profile_path = str(PROFILES_DIR / user_id / "default")
    try:
        sandbox_result = sandbox_mgr.start(profile_path=profile_path, user_id=user_id)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"Failed to start sandbox: {e}")

    sandbox_id = sandbox_result["sandbox_id"]
    cdp_url = sandbox_result.get("cdp_url", "ws://localhost:9222")
    await platform_db.create_sandbox(
        sandbox_result["sandbox_id"], sandbox_result["container_id"],
        user_id, sandbox_result.get("cdp_url", ""),
    )

    await platform_db.update_run(run_id, {"sandbox_id": sandbox_id, "status": "connecting", "profile_id": profile_id})

    if profile_mgr.has_profile(user_id, "default"):
        try:
            profile_mgr.restore_profile(user_id, "default", sandbox_id)
        except Exception as e:
            logger.warning("Profile restore failed: %s", e)

    # Build storage_state from saved session for the flow's domain (scoped to profile_id)
    storage_state = None
    domain_session = await platform_db.get_session_for_domain(user_id, flow.domain, profile_name=profile_id)
    if not domain_session:
        domain_session = await platform_db.get_session_for_domain(user_id, flow.domain)
    if domain_session:
        cookies = json.loads(domain_session.cookies)
        local_storage = json.loads(domain_session.local_storage)
        storage_state = {"cookies": cookies, "origins": []}
        if local_storage and flow.entry_url:
            storage_state["origins"].append({
                "origin": flow.entry_url.rstrip("/"),
                "localStorage": [{"name": k, "value": v} for k, v in local_storage.items()],
            })

    async def on_progress(status: RunStatus, message: str) -> None:
        run_state = await platform_db.get_run(run_id)
        steps = json.loads(run_state.steps) if run_state else []
        steps.append({"status": status.value, "message": message, "timestamp": datetime.now(timezone.utc).isoformat()})
        await platform_db.update_run(run_id, {
            "status": status.value,
            "current_step": message,
            "steps": json.dumps(steps),
        })
        await _broadcast_run_progress(run_id, {
            "run_id": run_id,
            "status": status.value,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        if wh_event := _EVENT_MAP.get(status):
            # Parse category from HITL message: "[category] reason"
            category = None
            if status == RunStatus.PAUSED_FOR_HITL and message.startswith("["):
                end = message.find("]")
                if end > 0:
                    category = message[1:end]
            asyncio.create_task(webhook_svc.fire_event(
                user_id=user_id,
                event=wh_event,
                run_id=run_id,
                payload=_build_webhook_payload(wh_event, run_id, status.value, message, sandbox_id, profile_id, category),
            ))

    task = asyncio.create_task(
        _execute_run(
            run_id=run_id,
            flow=flow,
            inputs=inputs,
            user_id=user_id,
            sandbox_id=sandbox_id,
            cdp_url=cdp_url,
            storage_state=storage_state,
            progress_callback=on_progress,
            profile_id=profile_id,
        )
    )
    _run_tasks[run_id] = task
    task.add_done_callback(lambda t: _run_tasks.pop(run_id, None))

    return {
        "run_id": run_id,
        "sandbox_id": sandbox_id,
        "status": "connecting",
        "profile_id": profile_id,
    }


@app.post("/api/runs")
async def run_start(req: RunStartRequest, user: dict = Depends(auth.get_current_user)) -> dict:
    user_id = user["id"]
    effective_profile_id = req.profile_id or user_id
    return await _start_run_impl(req.flow_id, req.inputs, user_id, effective_profile_id)


async def _execute_run(
    run_id: str,
    flow: Flow,
    inputs: dict,
    user_id: str,
    sandbox_id: str,
    cdp_url: str,
    storage_state: dict | None,
    progress_callback: Callable[[RunStatus, str], Awaitable[None]],
    profile_id: str = "",
) -> None:
    """Background task that runs the flow."""

    async def _agent_ready(agent, browser, flow_obj, paused_ref) -> None:
        _run_agents[run_id] = (agent, browser, flow_obj, paused_ref)

    try:
        result = await flow_runner.run_flow(
            flow=flow,
            inputs=inputs,
            user_id=user_id,
            sandbox_id=sandbox_id,
            cdp_url=cdp_url,
            run_id=run_id,
            progress_callback=progress_callback,
            storage_state=storage_state,
            agent_ready_callback=_agent_ready,
        )
        await platform_db.update_run(run_id, {
            "status": "completed",
            "result": json.dumps(result),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
    except asyncio.CancelledError:
        logger.info("Run %s cancelled", run_id)
        await platform_db.update_run(run_id, {
            "status": "cancelled",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        await _broadcast_run_progress(run_id, {
            "run_id": run_id,
            "status": "cancelled",
            "message": "Run cancelled by user",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        asyncio.create_task(webhook_svc.fire_event(
            user_id=user_id,
            event="run.cancelled",
            run_id=run_id,
            payload=_build_webhook_payload("run.cancelled", run_id, "cancelled", "Run cancelled by user", sandbox_id, profile_id),
        ))
    except Exception as e:
        logger.error("Run %s failed: %s", run_id, e)
        await platform_db.update_run(run_id, {
            "status": "failed",
            "error": str(e),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        await _broadcast_run_progress(run_id, {
            "run_id": run_id,
            "status": "failed",
            "message": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    finally:
        _run_agents.pop(run_id, None)
        try:
            sandbox_mgr.stop(sandbox_id)
            await platform_db.delete_sandbox(sandbox_id)
        except Exception as e:
            logger.warning("Failed to stop sandbox %s: %s", sandbox_id, e)


@app.get("/api/runs")
async def run_list(user: dict = Depends(auth.get_current_user)) -> list[dict]:
    runs = await platform_db.list_runs(user["id"])
    for r in runs:
        if r.get("sandbox_id") and r.get("status") in ACTIVE_RUN_STATUSES:
            r["browser_cdp_url"] = f"{BASE_URL}/ws/browser/{r['sandbox_id']}"
        else:
            r["browser_cdp_url"] = None
    return runs


@app.get("/api/runs/{run_id}")
async def run_get(run_id: str, user: dict = Depends(auth.get_current_user)) -> dict:
    run = await platform_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    browser_cdp_url = None
    if run.sandbox_id and run.status in ("connecting", "running", "paused_for_hitl"):
        browser_cdp_url = f"{BASE_URL}/ws/browser/{run.sandbox_id}"
    return {
        "id": run.id,
        "flow_id": run.flow_id,
        "user_id": run.user_id,
        "status": run.status,
        "inputs": json.loads(run.inputs),
        "sandbox_id": run.sandbox_id,
        "browser_cdp_url": browser_cdp_url,
        "steps": json.loads(run.steps),
        "current_step": run.current_step,
        "result": json.loads(run.result) if run.result else None,
        "error": run.error,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
        "profile_id": run.profile_id,
    }


@app.post("/api/runs/{run_id}/cancel")
async def run_cancel(run_id: str, user: dict = Depends(auth.get_current_user)) -> dict:
    run = await platform_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.user_id != user["id"]:
        raise HTTPException(status_code=403, detail="Run does not belong to you")
    if run.status in ("completed", "failed", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Run already {run.status}")

    # Cancel the asyncio background task
    task = _run_tasks.get(run_id)
    if task and not task.done():
        task.cancel()

    # Stop the sandbox container
    if run.sandbox_id:
        try:
            sandbox_mgr.stop(run.sandbox_id)
            await platform_db.delete_sandbox(run.sandbox_id)
        except Exception as e:
            logger.warning("Failed to stop sandbox %s during cancel: %s", run.sandbox_id, e)

    # Update DB and broadcast
    await platform_db.update_run(run_id, {
        "status": "cancelled",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    })
    await _broadcast_run_progress(run_id, {
        "run_id": run_id,
        "status": "cancelled",
        "message": "Run cancelled by user",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    asyncio.create_task(webhook_svc.fire_event(
        user_id=user["id"],
        event="run.cancelled",
        run_id=run_id,
        payload=_build_webhook_payload("run.cancelled", run_id, "cancelled", "Run cancelled by user", run.sandbox_id, run.profile_id),
    ))

    return {"run_id": run_id, "status": "cancelled"}


@app.post("/api/runs/{run_id}/resume")
async def run_resume(run_id: str, user: dict = Depends(auth.get_current_user)) -> dict:
    run = await platform_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.user_id != user["id"]:
        raise HTTPException(status_code=403, detail="Run does not belong to you")
    if run.status != "paused_for_hitl":
        raise HTTPException(status_code=409, detail=f"Run is not paused for human input (status: {run.status})")

    agent_info = _run_agents.get(run_id)
    if not agent_info:
        raise HTTPException(status_code=404, detail="Agent not found for this run — it may have already finished")

    agent, browser, flow, paused_ref = agent_info

    # Capture cookies from the browser and persist as a session scoped to the run's profile_id
    try:
        storage_state = await browser.get_storage_state()
        cookies = storage_state.get("cookies", [])
        if cookies:
            session = Session(
                id=str(uuid.uuid4())[:12],
                user_id=user["id"],
                domain=flow.domain,
                cookies=json.dumps(cookies),
                profile_name=run.profile_id or flow.domain,
            )
            await platform_db.create_session(session)
            logger.info(
                "Captured %d cookies for domain %s profile %s (run %s)",
                len(cookies), flow.domain, session.profile_name, run_id,
            )
    except Exception as e:
        logger.warning("Failed to capture storage state on resume (run %s): %s", run_id, e)

    paused_ref[0] = False
    agent.resume()

    # Update DB and broadcast
    await platform_db.update_run(run_id, {"status": "running", "current_step": "Resumed after human assistance"})
    await _broadcast_run_progress(run_id, {
        "run_id": run_id,
        "status": "running",
        "message": "Resumed after human assistance",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    return {"run_id": run_id, "status": "running"}


@app.post("/api/runs/{run_id}/pause")
async def run_pause(run_id: str, user: dict = Depends(auth.get_current_user)) -> dict:
    """Manual pause — user can trigger pause from the UI as a fallback."""
    run = await platform_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.user_id != user["id"]:
        raise HTTPException(status_code=403, detail="Run does not belong to you")
    if run.status != "running":
        raise HTTPException(status_code=409, detail=f"Run is not running (status: {run.status})")

    agent_info = _run_agents.get(run_id)
    if not agent_info:
        raise HTTPException(status_code=404, detail="Agent not found for this run")

    agent = agent_info[0]
    agent.pause()

    await platform_db.update_run(run_id, {"status": "paused_for_hitl", "current_step": "Paused by user"})
    await _broadcast_run_progress(run_id, {
        "run_id": run_id,
        "status": "paused_for_hitl",
        "message": "Paused by user",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    asyncio.create_task(webhook_svc.fire_event(
        user_id=user["id"],
        event="run.paused",
        run_id=run_id,
        payload=_build_webhook_payload("run.paused", run_id, "paused_for_hitl", "Paused by user", run.sandbox_id, run.profile_id, "other"),
    ))

    return {"run_id": run_id, "status": "paused_for_hitl"}


# ---------------------------------------------------------------------------
# WebSocket for run progress
# ---------------------------------------------------------------------------

@app.websocket("/ws/runs/{run_id}")
async def ws_run(websocket: WebSocket, run_id: str) -> None:
    await websocket.accept()

    if run_id not in _ws_connections:
        _ws_connections[run_id] = []
    _ws_connections[run_id].append(websocket)

    logger.info("WebSocket client connected for run %s", run_id)

    try:
        run = await platform_db.get_run(run_id)
        if run:
            await websocket.send_json({
                "run_id": run.id,
                "status": run.status,
                "current_step": run.current_step,
                "steps": json.loads(run.steps),
                "error": run.error,
            })

        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected for run %s", run_id)
    finally:
        if run_id in _ws_connections:
            _ws_connections[run_id] = [
                ws for ws in _ws_connections[run_id] if ws != websocket
            ]
            if not _ws_connections[run_id]:
                del _ws_connections[run_id]


@app.websocket("/ws/browser/{sandbox_id}")
async def ws_browser(
    websocket: WebSocket,
    sandbox_id: str,
    token: str = Query(default=""),
) -> None:
    """CDP screencast proxy — streams browser frames and forwards input events."""
    await cdp_proxy.handle(websocket, sandbox_id, sandbox_mgr, token=token)


async def _broadcast_run_progress(run_id: str, data: dict) -> None:
    clients = _ws_connections.get(run_id, [])
    dead = []
    for ws in clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if run_id in _ws_connections:
            _ws_connections[run_id] = [
                w for w in _ws_connections[run_id] if w != ws
            ]


# ---------------------------------------------------------------------------
# Profile endpoints (auth required) — tar-based profile save/restore
# ---------------------------------------------------------------------------

@app.post("/api/profile/save")
async def profile_save(req: ProfileSaveRequest, user: dict = Depends(auth.get_current_user)) -> dict:
    user_id = user["id"]
    if not SandboxManager.validate_profile_name(req.profile_name):
        raise HTTPException(status_code=400, detail="Invalid profile name.")
    try:
        owner = sandbox_mgr.get_sandbox_owner(req.sandbox_id)
        if owner and owner != user_id:
            raise HTTPException(status_code=403, detail="Sandbox does not belong to you")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        saved_path = profile_mgr.save_profile(req.sandbox_id, req.profile_name, user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"profile_name": req.profile_name, "path": str(saved_path), "status": "saved"}


@app.get("/api/profile/status")
async def profile_status(profile_name: str = "default", user: dict = Depends(auth.get_current_user)) -> dict:
    user_id = user["id"]
    info = profile_mgr.get_profile_info(user_id, profile_name)
    return {
        "connected": info["exists"],
        "profile_name": profile_name if info["exists"] else None,
        "last_saved": info.get("last_modified"),
        "source": info.get("source"),
    }


# ---------------------------------------------------------------------------
# API Keys (v1)
# ---------------------------------------------------------------------------

@app.post("/api/v1/api-keys")
async def api_key_create(req: ApiKeyCreateRequest, user: dict = Depends(auth.get_current_user)) -> dict:
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Key name is required")
    plaintext = auth.generate_api_key()
    key_hash = auth.hash_api_key(plaintext)
    key_prefix = plaintext[:12]  # "fp_live_XXXX" (12 chars)
    key_data = await platform_db.create_api_key(user["id"], req.name.strip(), key_hash, key_prefix)
    key_data["key"] = plaintext  # returned only once
    return key_data


@app.get("/api/v1/api-keys")
async def api_key_list(user: dict = Depends(auth.get_current_user)) -> list[dict]:
    return await platform_db.list_api_keys(user["id"])


@app.delete("/api/v1/api-keys/{key_id}")
async def api_key_delete(key_id: str, user: dict = Depends(auth.get_current_user)) -> dict:
    deleted = await platform_db.delete_api_key(key_id, user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="API key not found")
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Webhooks (v1)
# ---------------------------------------------------------------------------

@app.post("/api/v1/webhooks")
async def webhook_create(req: WebhookCreateRequest, user: dict = Depends(auth.get_current_user)) -> dict:
    if not req.url.strip():
        raise HTTPException(status_code=400, detail="URL is required")
    invalid = [e for e in req.events if e not in webhook_svc.VALID_EVENTS]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown events: {invalid}. Valid: {sorted(webhook_svc.VALID_EVENTS)}")
    secret = webhook_svc.generate_webhook_secret()
    wh = await platform_db.create_webhook(user["id"], req.url.strip(), secret, req.events)
    wh["secret"] = secret  # returned only once
    return wh


@app.get("/api/v1/webhooks")
async def webhook_list(user: dict = Depends(auth.get_current_user)) -> list[dict]:
    return await platform_db.list_webhooks(user["id"])


@app.delete("/api/v1/webhooks/{webhook_id}")
async def webhook_delete(webhook_id: str, user: dict = Depends(auth.get_current_user)) -> dict:
    deleted = await platform_db.delete_webhook(webhook_id, user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return {"status": "deleted"}


@app.post("/api/v1/webhooks/{webhook_id}/test")
async def webhook_test(webhook_id: str, user: dict = Depends(auth.get_current_user)) -> dict:
    wh = await platform_db.get_webhook(webhook_id, user["id"])
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook not found")
    ping_payload = {
        "event": "ping",
        "webhook_id": webhook_id,
        "message": "Test delivery from FlowPilot",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload_str = json.dumps(ping_payload, separators=(",", ":"))
    body = payload_str.encode()
    signature = webhook_svc.sign_payload(wh["secret"], body)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                wh["url"],
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-FlowPilot-Signature": signature,
                    "X-FlowPilot-Event": "ping",
                    "X-FlowPilot-Delivery": "test",
                },
            )
        return {"status": "delivered", "response_status": resp.status_code}
    except Exception as e:
        return {"status": "failed", "error": str(e)}


# ---------------------------------------------------------------------------
# Public Run API (v1) — usable with API keys
# ---------------------------------------------------------------------------

@app.post("/api/v1/runs")
async def v1_run_start(req: RunStartRequest, user: dict = Depends(auth.get_current_user)) -> dict:
    user_id = user["id"]
    effective_profile_id = req.profile_id or user_id
    return await _start_run_impl(req.flow_id, req.inputs, user_id, effective_profile_id)


@app.get("/api/v1/runs/{run_id}")
async def v1_run_get(run_id: str, user: dict = Depends(auth.get_current_user)) -> dict:
    run = await platform_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.user_id != user["id"]:
        raise HTTPException(status_code=403, detail="Run does not belong to you")
    browser_cdp_url = None
    if run.sandbox_id and run.status in ("connecting", "running", "paused_for_hitl"):
        browser_cdp_url = f"{BASE_URL}/ws/browser/{run.sandbox_id}"
    return {
        "id": run.id,
        "flow_id": run.flow_id,
        "user_id": run.user_id,
        "status": run.status,
        "inputs": json.loads(run.inputs),
        "sandbox_id": run.sandbox_id,
        "browser_cdp_url": browser_cdp_url,
        "steps": json.loads(run.steps),
        "current_step": run.current_step,
        "result": json.loads(run.result) if run.result else None,
        "error": run.error,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
        "profile_id": run.profile_id,
        "resume_url": f"/api/v1/runs/{run_id}/resume" if run.status == "paused_for_hitl" else None,
    }


@app.post("/api/v1/runs/{run_id}/resume")
async def v1_run_resume(run_id: str, user: dict = Depends(auth.get_current_user)) -> dict:
    # Delegate to existing handler logic — re-use the same implementation
    return await run_resume(run_id, user)


# ---------------------------------------------------------------------------
# Static files (frontend)
# ---------------------------------------------------------------------------

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
