"""FastAPI backend for browser automation platform."""

import os
import json
import uuid
import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .sandbox import SandboxManager
from .profile import ProfileManager
from .runner import FlowRunner, RunStatus
from .config import PROFILES_DIR
from . import auth
from . import db as platform_db
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

sandbox_mgr = SandboxManager(profiles_host_dir=str(PROFILES_DIR))
profile_mgr = ProfileManager(profiles_dir=PROFILES_DIR)
flow_runner = FlowRunner()

# ---------------------------------------------------------------------------
# WebSocket connection registry
# ---------------------------------------------------------------------------

_ws_connections: dict[str, list[WebSocket]] = {}  # run_id -> [websocket, ...]
_run_tasks: dict[str, asyncio.Task] = {}  # run_id -> background task
_run_agents: dict[str, tuple] = {}  # run_id -> (agent, browser, flow)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    await platform_db.init_db()
    await platform_db.seed_default_flows()
    removed = sandbox_mgr.cleanup_orphans()
    if removed:
        logger.info("Cleaned up %d orphaned sandbox containers", removed)
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


class SessionCaptureRequest(BaseModel):
    domain: str
    cookies: list[dict] = []
    localStorage: dict = {}
    sessionStorage: dict = {}
    userAgent: str | None = None


# ---------------------------------------------------------------------------
# Health (no auth)
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# Auth endpoints (no auth required)
# ---------------------------------------------------------------------------

@app.post("/api/auth/signup")
async def signup(req: SignupRequest):
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
async def login(req: LoginRequest):
    user = await auth.authenticate(req.email, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = auth.create_token(user["id"])
    return {"token": token, "user": user}


@app.get("/api/auth/me")
async def auth_me(user: dict = Depends(auth.get_current_user)):
    return {"user": user}


# ---------------------------------------------------------------------------
# Sandbox endpoints (auth required)
# ---------------------------------------------------------------------------

@app.post("/api/sandbox/start")
async def sandbox_start(req: SandboxStartRequest, user: dict = Depends(auth.get_current_user)):
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
        if "already has" in str(e) and "active sandboxes" in str(e):
            raise HTTPException(status_code=429, detail=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    if profile_mgr.has_profile(user_id, profile_name):
        try:
            profile_mgr.restore_profile(user_id, profile_name, result["sandbox_id"])
        except Exception as e:
            logger.warning("Profile restore failed: %s", e)
    return result


@app.post("/api/sandbox/stop")
async def sandbox_stop(req: SandboxStopRequest, user: dict = Depends(auth.get_current_user)):
    user_id = user["id"]
    try:
        owner = sandbox_mgr.get_sandbox_owner(req.sandbox_id)
        if owner and owner != user_id:
            raise HTTPException(status_code=403, detail="Sandbox does not belong to you")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        sandbox_mgr.stop(req.sandbox_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "stopped", "sandbox_id": req.sandbox_id}


# ---------------------------------------------------------------------------
# Flow endpoints
# ---------------------------------------------------------------------------

@app.post("/api/flows")
async def flow_create(req: FlowCreateRequest, user: dict = Depends(auth.get_current_user)):
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
async def flow_list():
    rows = await platform_db.list_flows()
    for r in rows:
        r["inputs"] = json.loads(r.get("inputs", "[]"))
    return rows


@app.get("/api/flows/{flow_id}")
async def flow_get(flow_id: str):
    flow = await platform_db.get_flow(flow_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")
    return _flow_to_dict(flow)


@app.put("/api/flows/{flow_id}")
async def flow_update(flow_id: str, req: FlowUpdateRequest, user: dict = Depends(auth.get_current_user)):
    updates = req.model_dump(exclude_none=True)
    if "inputs" in updates:
        updates["inputs"] = json.dumps(updates["inputs"])
    flow = await platform_db.update_flow(flow_id, updates)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")
    return _flow_to_dict(flow)


@app.delete("/api/flows/{flow_id}")
async def flow_delete(flow_id: str, user: dict = Depends(auth.get_current_user)):
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
async def session_capture(req: SessionCaptureRequest, user: dict = Depends(auth.get_current_user)):
    session = Session(
        id=str(uuid.uuid4())[:12],
        user_id=user["id"],
        domain=req.domain,
        cookies=json.dumps(req.cookies),
        local_storage=json.dumps(req.localStorage),
        session_storage=json.dumps(req.sessionStorage),
        user_agent=req.userAgent,
    )
    await platform_db.create_session(session)
    return {"id": session.id, "domain": session.domain, "captured_at": session.captured_at}


@app.get("/api/sessions")
async def session_list(user: dict = Depends(auth.get_current_user)):
    return await platform_db.list_sessions(user["id"])


@app.delete("/api/sessions/{session_id}")
async def session_delete(session_id: str, user: dict = Depends(auth.get_current_user)):
    deleted = await platform_db.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Run endpoints
# ---------------------------------------------------------------------------

@app.post("/api/runs")
async def run_start(req: RunStartRequest, user: dict = Depends(auth.get_current_user)):
    user_id = user["id"]

    flow = await platform_db.get_flow(req.flow_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")

    run_id = str(uuid.uuid4())[:12]
    run = Run(
        id=run_id,
        flow_id=flow.id,
        user_id=user_id,
        status="pending",
        inputs=json.dumps(req.inputs),
    )
    await platform_db.create_run(run)

    # Start sandbox
    profile_path = None
    if profile_mgr.has_profile(user_id, "default"):
        profile_path = str(PROFILES_DIR / user_id / "default")
    try:
        sandbox_result = sandbox_mgr.start(profile_path=profile_path, user_id=user_id)
    except RuntimeError as e:
        if "already has" in str(e) and "active sandboxes" in str(e):
            raise HTTPException(status_code=429, detail=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to start sandbox: {e}")

    sandbox_id = sandbox_result["sandbox_id"]
    cdp_url = sandbox_result.get("cdp_url", "ws://localhost:9222")

    await platform_db.update_run(run_id, {"sandbox_id": sandbox_id, "status": "connecting"})

    if profile_mgr.has_profile(user_id, "default"):
        try:
            profile_mgr.restore_profile(user_id, "default", sandbox_id)
        except Exception as e:
            logger.warning("Profile restore failed: %s", e)

    # Build storage_state from saved session for the flow's domain
    storage_state = None
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

    async def on_progress(status: RunStatus, message: str):
        steps = json.loads((await platform_db.get_run(run_id)).steps) if await platform_db.get_run(run_id) else []
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

    task = asyncio.create_task(
        _execute_run(
            run_id=run_id,
            flow=flow,
            inputs=req.inputs,
            user_id=user_id,
            sandbox_id=sandbox_id,
            cdp_url=cdp_url,
            storage_state=storage_state,
            progress_callback=on_progress,
        )
    )
    _run_tasks[run_id] = task
    task.add_done_callback(lambda t: _run_tasks.pop(run_id, None))

    return {
        "run_id": run_id,
        "sandbox_id": sandbox_id,
        "novnc_url": sandbox_result.get("novnc_url"),
        "status": "connecting",
    }


async def _execute_run(
    run_id: str,
    flow: Flow,
    inputs: dict,
    user_id: str,
    sandbox_id: str,
    cdp_url: str,
    storage_state: dict | None,
    progress_callback,
):
    """Background task that runs the flow."""

    async def _agent_ready(agent, browser, flow_obj):
        _run_agents[run_id] = (agent, browser, flow_obj)

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
        except Exception as e:
            logger.warning("Failed to stop sandbox %s: %s", sandbox_id, e)


@app.get("/api/runs")
async def run_list(user: dict = Depends(auth.get_current_user)):
    return await platform_db.list_runs(user["id"])


@app.get("/api/runs/{run_id}")
async def run_get(run_id: str, user: dict = Depends(auth.get_current_user)):
    run = await platform_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return {
        "id": run.id,
        "flow_id": run.flow_id,
        "user_id": run.user_id,
        "status": run.status,
        "inputs": json.loads(run.inputs),
        "sandbox_id": run.sandbox_id,
        "steps": json.loads(run.steps),
        "current_step": run.current_step,
        "result": json.loads(run.result) if run.result else None,
        "error": run.error,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
    }


@app.post("/api/runs/{run_id}/cancel")
async def run_cancel(run_id: str, user: dict = Depends(auth.get_current_user)):
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
        except (ValueError, Exception) as e:
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

    return {"run_id": run_id, "status": "cancelled"}


@app.post("/api/runs/{run_id}/resume")
async def run_resume(run_id: str, user: dict = Depends(auth.get_current_user)):
    run = await platform_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.user_id != user["id"]:
        raise HTTPException(status_code=403, detail="Run does not belong to you")
    if run.status != "paused_for_auth":
        raise HTTPException(status_code=409, detail=f"Run is not paused for auth (status: {run.status})")

    agent_info = _run_agents.get(run_id)
    if not agent_info:
        raise HTTPException(status_code=404, detail="Agent not found for this run — it may have already finished")

    agent, browser, flow = agent_info

    # Capture cookies from the browser and persist as a session
    try:
        storage_state = await browser.get_storage_state()
        cookies = storage_state.get("cookies", [])
        if cookies:
            session = Session(
                id=str(uuid.uuid4())[:12],
                user_id=user["id"],
                domain=flow.domain,
                cookies=json.dumps(cookies),
            )
            await platform_db.create_session(session)
            logger.info("Captured %d cookies for domain %s (run %s)", len(cookies), flow.domain, run_id)
    except Exception as e:
        logger.warning("Failed to capture storage state on resume (run %s): %s", run_id, e)

    # Resume the agent
    agent.resume()

    # Update DB and broadcast
    await platform_db.update_run(run_id, {"status": "running", "current_step": "Resumed after human login"})
    await _broadcast_run_progress(run_id, {
        "run_id": run_id,
        "status": "running",
        "message": "Resumed after human login",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    return {"run_id": run_id, "status": "running"}


@app.post("/api/runs/{run_id}/pause")
async def run_pause(run_id: str, user: dict = Depends(auth.get_current_user)):
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

    await platform_db.update_run(run_id, {"status": "paused_for_auth", "current_step": "Paused by user"})
    await _broadcast_run_progress(run_id, {
        "run_id": run_id,
        "status": "paused_for_auth",
        "message": "Paused by user",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    return {"run_id": run_id, "status": "paused_for_auth"}


# ---------------------------------------------------------------------------
# WebSocket for run progress
# ---------------------------------------------------------------------------

@app.websocket("/ws/runs/{run_id}")
async def ws_run(websocket: WebSocket, run_id: str):
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


async def _broadcast_run_progress(run_id: str, data: dict):
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
async def profile_save(req: ProfileSaveRequest, user: dict = Depends(auth.get_current_user)):
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
async def profile_status(profile_name: str = "default", user: dict = Depends(auth.get_current_user)):
    user_id = user["id"]
    info = profile_mgr.get_profile_info(user_id, profile_name)
    return {
        "connected": info["exists"],
        "profile_name": profile_name if info["exists"] else None,
        "last_saved": info.get("last_modified"),
        "source": info.get("source"),
    }


# ---------------------------------------------------------------------------
# Static files (frontend)
# ---------------------------------------------------------------------------

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
