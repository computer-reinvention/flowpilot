"""FastAPI backend for browser automation platform."""

import asyncio
import json
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import httpx
import websockets
from fastapi import FastAPI, APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .sandbox import SandboxManager
from .profile import ProfileManager
from .runner import FlowRunner, RunStatus
from .explorer import explore_flow
from .session_utils import capture_session
from .config import PROFILES_DIR, BASE_URL
from . import auth
from . import db as platform_db
from . import cdp_proxy
from . import webhooks as webhook_svc
from .models import Flow, Run, Session
from .task_bootstrap import install_task_api

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("browser_use").setLevel(logging.DEBUG)
logging.getLogger("Agent").setLevel(logging.DEBUG)
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
_flow_ws_connections: dict[str, list[WebSocket]] = {}  # flow_id -> [websocket, ...]
_run_tasks: dict[str, asyncio.Task] = {}  # run_id -> background task
_run_agents: dict[str, tuple] = {}  # run_id -> (agent, browser, flow, paused_ref)
_exploration_tasks: dict[str, asyncio.Task] = {}  # flow_id -> background exploration task

ACTIVE_RUN_STATUSES: frozenset[str] = frozenset({"pending", "connecting", "running", "paused_for_hitl"})


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    await platform_db.init_db()

    # Reset any stale exploration states from before restart
    stale_flows = await platform_db.list_flows()
    for sf in stale_flows:
        if sf.get("exploration_status") in ("exploring", "paused_for_hitl", "awaiting_auth"):
            await platform_db.update_flow(sf["id"], {
                "exploration_status": "failed",
                "exploration_error": "Exploration interrupted by server restart",
            })

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
    await app.state.browser_tasks.startup()
    try:
        yield
    finally:
        await app.state.browser_tasks.shutdown()
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
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=72, repr=False)
    name: str = Field(min_length=1, max_length=200)

    @field_validator('password')
    @classmethod
    def password_bytes(cls, value):
        if len(value.encode()) > 72:
            raise ValueError('Password exceeds byte limit')
        return value

    @field_validator('email', 'name')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Field must not be blank')
        return value


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=72, repr=False)


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
    task: str
    entry_url: str | None = None
    icon: str | None = None
    auth_url: str | None = None
    auth_before_explore: bool = False
    inputs: list[dict] | None = None  # initial inputs at creation


class FlowUpdateRequest(BaseModel):
    name: str | None = None
    domain: str | None = None
    task: str | None = None
    entry_url: str | None = None
    inputs: list[dict] | None = None
    icon: str | None = None
    auth_url: str | None = None
    auth_before_explore: bool | None = None


class RunStartRequest(BaseModel):
    flow_id: str
    inputs: dict = {}
    profile_id: str | None = None  # defaults to user's id


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class WebhookCreateRequest(BaseModel):
    url: str
    events: list[str] = []  # empty = subscribe to all


class TriggerHookCreateRequest(BaseModel):
    name: str


class TriggerHookExecuteRequest(BaseModel):
    inputs: dict = {}
    profile_id: str | None = None  # defaults to hook owner's user id


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

account_router = APIRouter(route_class=auth.SensitiveRoute)


@account_router.post("/api/auth/signup")
async def signup(req: SignupRequest) -> dict:
    if not req.email or not req.password or not req.name:
        raise HTTPException(status_code=400, detail="email, password, and name are required")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    try:
        user = await auth.create_user(req.email, req.password, req.name)
    except ValueError:
        raise HTTPException(status_code=409, detail='Unable to create account') from None
    token = auth.create_token(user["id"])
    return {"token": token, "user": user}


@account_router.post("/api/auth/login")
async def login(req: LoginRequest) -> dict:
    user = await auth.authenticate(req.email, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = auth.create_token(user["id"])
    return {"token": token, "user": user}


@account_router.get("/api/auth/me")
async def auth_me(user: dict = Depends(auth.get_current_user)) -> dict:
    return {"user": user}


# ---------------------------------------------------------------------------
# Sandbox endpoints (auth required)
# ---------------------------------------------------------------------------

@app.post("/api/sandbox/start")
async def sandbox_start(req: SandboxStartRequest, user: dict = Depends(auth.get_current_user)) -> dict:
    user_id = user["id"]
    profile_name = req.profile_name or user_id
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
    # Save browser profile before stopping
    try:
        profile_mgr.save_profile(req.sandbox_id, user_id, user_id)
        logger.info("Auto-saved browser profile on sandbox stop (sandbox=%s)", req.sandbox_id)
    except Exception as e:
        logger.warning("Profile auto-save failed on sandbox stop: %s", e)
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
    initial_inputs = json.dumps(req.inputs) if req.inputs else "[]"
    flow = Flow(
        id=str(uuid.uuid4())[:12],
        name=req.name,
        domain=req.domain,
        task=req.task,
        entry_url=req.entry_url,
        inputs=initial_inputs,
        icon=req.icon,
        created_by=user["id"],
        auth_url=req.auth_url,
        auth_before_explore=req.auth_before_explore,
    )
    await platform_db.create_flow(flow)

    if req.auth_before_explore and req.auth_url and flow.entry_url:
        # Auth pre-step: start sandbox for manual login before exploration
        await _start_auth_prestep(flow, user["id"], profile_id=user["id"])
    elif flow.entry_url:
        # Normal auto-explore
        await platform_db.update_flow(flow.id, {"exploration_status": "exploring"})
        flow.exploration_status = "exploring"
        task = asyncio.create_task(_run_exploration(flow, user["id"], profile_id=user["id"]))
        _exploration_tasks[flow.id] = task
        task.add_done_callback(lambda t: _exploration_tasks.pop(flow.id, None))
    return _flow_to_dict(flow)


@app.get("/api/flows")
async def flow_list() -> list[dict]:
    rows = await platform_db.list_flows()
    for r in rows:
        r["inputs"] = json.loads(r.get("inputs", "[]"))
        r["exploration_tree"] = json.loads(r.get("exploration_tree", "[]"))
        eir = r.get("exploration_input_request")
        r["exploration_input_request"] = json.loads(eir) if eir else None
        r["exploration_confirmation_request"] = r.get("exploration_confirmation_request")
        r["exploration_sandbox_id"] = r.get("exploration_sandbox_id")
        r["auth_url"] = r.get("auth_url")
        r["auth_before_explore"] = bool(r.get("auth_before_explore", 0))
        r["auth_sandbox_id"] = r.get("auth_sandbox_id")
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
    if "auth_before_explore" in updates:
        updates["auth_before_explore"] = int(updates["auth_before_explore"])
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


_run_auth_events: dict[str, asyncio.Event] = {}  # run_id -> event set when auth done
_exploration_agents: dict[str, tuple] = {}  # flow_id -> (agent, browser, paused_ref, hitl_response_ref)


async def _start_auth_prestep(flow: Flow, user_id: str, profile_id: str = "") -> None:
    """Start a sandbox for manual auth before exploration begins."""
    # Skip if cookies already exist for this profile+domain
    domain_session = await platform_db.get_session_for_domain(user_id, flow.domain, profile_name=profile_id)
    if domain_session:
        logger.info("Auth pre-step skipped for flow %s — session already exists", flow.id)
        await platform_db.update_flow(flow.id, {"exploration_status": "exploring"})
        flow.exploration_status = "exploring"
        task = asyncio.create_task(_run_exploration(flow, user_id, profile_id=profile_id))
        _exploration_tasks[flow.id] = task
        task.add_done_callback(lambda t: _exploration_tasks.pop(flow.id, None))
        return

    # Start sandbox and navigate to auth_url via CDP
    profile_path = None
    if profile_mgr.has_profile(user_id, profile_id):
        profile_path = str(PROFILES_DIR / user_id / profile_id)
    try:
        sandbox_result = sandbox_mgr.start(profile_path=profile_path, user_id=user_id)
    except RuntimeError as e:
        await platform_db.update_flow(flow.id, {"exploration_status": "failed", "exploration_error": f"Auth sandbox failed: {e}"})
        return

    sandbox_id = sandbox_result["sandbox_id"]
    cdp_url = sandbox_result.get("cdp_url", "ws://localhost:9222")
    await platform_db.create_sandbox(sandbox_id, sandbox_result["container_id"], user_id, cdp_url)

    # Navigate to auth URL via CDP
    _cdp_hp = cdp_url.split("//")[1].split("/")[0]
    _cdp_h, _cdp_p = _cdp_hp.rsplit(":", 1)
    cdp_info = None
    for _attempt in range(20):
        try:
            async with httpx.AsyncClient(timeout=3) as _hc:
                _resp = await _hc.get(f"http://{_cdp_h}:{_cdp_p}/json/version", headers={"Host": "localhost"})
                if _resp.status_code == 200:
                    cdp_info = _resp.json()
                    break
        except (httpx.HTTPError, Exception):
            pass
        if _attempt < 19:
            await asyncio.sleep(2)
    if cdp_info is None:
        await platform_db.update_flow(flow.id, {"exploration_status": "failed", "exploration_error": "CDP not reachable for auth"})
        try:
            sandbox_mgr.stop(sandbox_id)
            await platform_db.delete_sandbox(sandbox_id)
        except Exception:
            pass
        return

    # Navigate to auth URL via raw CDP websocket (Page.navigate)
    try:
        async with httpx.AsyncClient(timeout=5) as _hc2:
            _targets_resp = await _hc2.get(f"http://{_cdp_h}:{_cdp_p}/json/list", headers={"Host": "localhost"})
            targets = _targets_resp.json()
            page_target = next((t for t in targets if t["type"] == "page"), None)
        if page_target:
            page_ws = page_target["webSocketDebuggerUrl"]
            page_ws = page_ws.replace("localhost", _cdp_h).replace("127.0.0.1", _cdp_h)
            async with websockets.connect(page_ws) as ws:
                await ws.send(json.dumps({"id": 1, "method": "Page.enable", "params": {}}))
                await ws.recv()
                await ws.send(json.dumps({"id": 2, "method": "Page.navigate", "params": {"url": flow.auth_url}}))
                await ws.recv()
    except Exception as e:
        logger.warning("CDP navigate to auth_url failed: %s", e)

    await platform_db.update_flow(flow.id, {
        "exploration_status": "awaiting_auth",
        "auth_sandbox_id": sandbox_id,
        "auth_profile_id": profile_id,
    })
    flow.exploration_status = "awaiting_auth"
    await _broadcast_flow_update(flow.id)


@app.post("/api/flows/{flow_id}/auth/done")
async def flow_auth_done(flow_id: str, user: dict = Depends(auth.get_current_user)) -> dict:
    """User completed manual auth — capture cookies, stop sandbox, start exploration."""
    flow = await platform_db.get_flow(flow_id)
    if not flow or not flow.auth_sandbox_id:
        raise HTTPException(status_code=404, detail="No auth session for this flow")

    sandbox_id = flow.auth_sandbox_id
    auth_user_id = user["id"]
    auth_profile_id = flow.auth_profile_id or user["id"]

    # Get CDP URL from the sandbox record
    sandbox_row = await platform_db.get_sandbox(sandbox_id)
    cdp_url = sandbox_row["cdp_url"] if sandbox_row else f"ws://flowpilot-{sandbox_id}:9223"

    # Clear auth sandbox from flow
    await platform_db.update_flow(flow_id, {"auth_sandbox_id": None, "auth_profile_id": None})

    # Capture cookies via CDP
    _cdp_hp = cdp_url.split("//")[1].split("/")[0]
    _cdp_h, _cdp_p = _cdp_hp.rsplit(":", 1)
    try:
        async with httpx.AsyncClient(timeout=3) as _hc:
            _resp = await _hc.get(f"http://{_cdp_h}:{_cdp_p}/json/version", headers={"Host": "localhost"})
            _resp.raise_for_status()
            cdp_info = _resp.json()
        ws_path = "/" + "/".join(cdp_info["webSocketDebuggerUrl"].split("/")[3:])
        ws_url = f"ws://{_cdp_h}:{_cdp_p}{ws_path}"

        from cdp_use.client import CDPClient
        cdp_client = CDPClient(ws_url)
        await cdp_client.connect()
        result = await cdp_client.send("Network.getAllCookies", {})
        cookies = result.get("cookies", [])
        await cdp_client.close()

        if cookies:
            flow = await platform_db.get_flow(flow_id)
            session = Session(
                id=str(uuid.uuid4())[:12],
                user_id=auth_user_id,
                domain=flow.domain if flow else "",
                cookies=json.dumps(cookies),
                profile_name=auth_profile_id,
            )
            await platform_db.create_session(session)
            logger.info("Auth pre-step captured %d cookies for flow %s", len(cookies), flow_id)
    except Exception as e:
        logger.warning("Failed to capture auth cookies for flow %s: %s", flow_id, e)

    # Save browser profile before stopping the auth sandbox
    try:
        profile_mgr.save_profile(sandbox_id, auth_profile_id, auth_user_id)
        logger.info("Auto-saved browser profile after auth (flow=%s, profile=%s)", flow_id, auth_profile_id)
    except Exception as e:
        logger.warning("Profile auto-save failed after auth: %s", e)

    # Stop the auth sandbox
    try:
        sandbox_mgr.stop(sandbox_id)
        await platform_db.delete_sandbox(sandbox_id)
    except Exception as e:
        logger.warning("Failed to stop auth sandbox: %s", e)

    # Start exploration
    flow = await platform_db.get_flow(flow_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")

    await platform_db.update_flow(flow.id, {"exploration_status": "exploring"})
    flow.exploration_status = "exploring"
    task = asyncio.create_task(_run_exploration(flow, auth_user_id, profile_id=auth_profile_id))
    _exploration_tasks[flow.id] = task
    task.add_done_callback(lambda t: _exploration_tasks.pop(flow.id, None))
    await _broadcast_flow_update(flow.id)
    return {"status": "exploring", "flow_id": flow_id}


async def _run_exploration(flow: Flow, user_id: str, profile_id: str = "") -> None:
    """Background task: spin up sandbox, run explorer with HITL support, tear down."""
    sandbox_result = None
    try:
        profile_path = None
        if profile_mgr.has_profile(user_id, profile_id):
            profile_path = str(PROFILES_DIR / user_id / profile_id)
        sandbox_result = sandbox_mgr.start(profile_path=profile_path, user_id=user_id)
        sandbox_id = sandbox_result["sandbox_id"]
        cdp_url = sandbox_result.get("cdp_url", "ws://localhost:9222")
        await platform_db.create_sandbox(
            sandbox_id, sandbox_result["container_id"], user_id, cdp_url,
        )
        await platform_db.update_flow(flow.id, {"exploration_sandbox_id": sandbox_id})

        # Load saved session cookies if available
        storage_state = None
        domain_session = await platform_db.get_session_for_domain(user_id, flow.domain, profile_name=profile_id)
        if domain_session:
            cookies = json.loads(domain_session.cookies)
            local_storage = json.loads(domain_session.local_storage)
            storage_state = {"cookies": cookies, "origins": []}
            if local_storage and flow.entry_url:
                storage_state["origins"].append({
                    "origin": flow.entry_url.rstrip("/"),
                    "localStorage": [{"name": k, "value": v} for k, v in local_storage.items()],
                })
            logger.info("Exploration using %d saved cookies for %s (profile=%s)", len(cookies), flow.domain, profile_id)

        def on_agent_ready(agent, browser, paused_ref, sb_id, hitl_response_ref=None):
            _exploration_agents[flow.id] = (agent, browser, paused_ref, hitl_response_ref)

        async def on_session_capture(browser):
            await capture_session(browser, user_id, flow.domain, profile_id, context="exploration-end")

        await explore_flow(
            flow, sandbox_id, cdp_url,
            storage_state=storage_state,
            agent_ready_callback=on_agent_ready,
            on_status_change=_broadcast_flow_update,
            on_session_capture=on_session_capture,
        )
    except Exception as e:
        logger.error("Exploration failed for flow %s: %s", flow.id, e, exc_info=True)
        await platform_db.update_flow(flow.id, {
            "exploration_status": "failed",
            "exploration_error": str(e),
        })
    finally:
        _exploration_agents.pop(flow.id, None)
        await platform_db.update_flow(flow.id, {"exploration_sandbox_id": None})
        if sandbox_result:
            sb_id = sandbox_result["sandbox_id"]
            try:
                profile_mgr.save_profile(sb_id, profile_id or user_id, user_id)
                logger.info("Auto-saved browser profile for exploration (flow=%s, profile=%s)", flow.id, profile_id or user_id)
            except Exception as e:
                logger.warning("Profile auto-save failed for exploration: %s", e)
            try:
                sandbox_mgr.stop(sb_id)
                await platform_db.delete_sandbox(sb_id)
            except Exception as e:
                logger.warning("Failed to stop exploration sandbox: %s", e)


class ExploreRequest(BaseModel):
    profile_id: str | None = None  # defaults to user's id


@app.post("/api/flows/{flow_id}/explore")
async def flow_explore(flow_id: str, req: ExploreRequest | None = None, user: dict = Depends(auth.get_current_user)) -> dict:
    flow = await platform_db.get_flow(flow_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")
    if not flow.entry_url:
        raise HTTPException(status_code=400, detail="Flow has no entry_url — nothing to explore")
    # Save profile from existing sandbox, then cancel and kill it
    _exploration_agents.pop(flow_id, None)
    if flow.exploration_sandbox_id:
        try:
            profile_mgr.save_profile(flow.exploration_sandbox_id, user["id"], user["id"])
            logger.info("Auto-saved browser profile on explore restart (flow=%s)", flow_id)
        except Exception as e:
            logger.warning("Profile auto-save failed on explore restart: %s", e)
        try:
            sandbox_mgr.stop(flow.exploration_sandbox_id)
            await platform_db.delete_sandbox(flow.exploration_sandbox_id)
        except Exception:
            pass
    old_task = _exploration_tasks.pop(flow_id, None)
    if old_task and not old_task.done():
        old_task.cancel()
    # Reset
    await platform_db.update_flow(flow_id, {
        "exploration_status": "pending",
        "exploration_error": None,
        "exploration_notes": None,
        "exploration_tree": "[]",
    })
    flow = await platform_db.get_flow(flow_id)
    profile_id = (req.profile_id if req and req.profile_id else None) or user["id"]
    task = asyncio.create_task(_run_exploration(flow, user["id"], profile_id=profile_id))
    _exploration_tasks[flow_id] = task
    task.add_done_callback(lambda t: _exploration_tasks.pop(flow_id, None))
    return {"status": "exploring", "flow_id": flow_id}


@app.post("/api/flows/{flow_id}/explore/cancel")
async def flow_explore_cancel(flow_id: str, user: dict = Depends(auth.get_current_user)) -> dict:
    """Cancel a running exploration without restarting it."""
    flow = await platform_db.get_flow(flow_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")

    # Save profile + tear down agent + sandbox
    _exploration_agents.pop(flow_id, None)
    if flow.exploration_sandbox_id:
        try:
            profile_mgr.save_profile(flow.exploration_sandbox_id, user["id"], user["id"])
            logger.info("Auto-saved browser profile on explore cancel (flow=%s)", flow_id)
        except Exception as e:
            logger.warning("Profile auto-save failed on explore cancel: %s", e)
        try:
            sandbox_mgr.stop(flow.exploration_sandbox_id)
            await platform_db.delete_sandbox(flow.exploration_sandbox_id)
        except Exception:
            pass

    # Cancel asyncio task
    old_task = _exploration_tasks.pop(flow_id, None)
    if old_task and not old_task.done():
        old_task.cancel()

    await platform_db.update_flow(flow_id, {
        "exploration_status": "cancelled",
        "exploration_error": None,
    })
    await _broadcast_flow_update(flow_id)
    return {"status": "cancelled", "flow_id": flow_id}


class ExploreResumeBody(BaseModel):
    values: dict[str, str] | None = None
    confirmed: bool | None = None


@app.post("/api/flows/{flow_id}/explore/resume")
async def flow_explore_resume(flow_id: str, body: ExploreResumeBody | None = None, user: dict = Depends(auth.get_current_user)) -> dict:
    """Resume exploration after HITL — user provided values, confirmation, or completed auth."""
    agent_info = _exploration_agents.get(flow_id)
    if not agent_info:
        raise HTTPException(status_code=404, detail="No active exploration for this flow")
    agent, browser, paused_ref, hitl_response_ref = agent_info

    # Store user response on the shared ref so the paused tool can read it
    if hitl_response_ref is not None and body:
        hitl_response_ref[0] = body.model_dump(exclude_none=True)

    # Capture cookies from the browser for future runs
    resume_flow = await platform_db.get_flow(flow_id)
    if resume_flow:
        explore_profile = resume_flow.auth_profile_id or user["id"]
        await capture_session(browser, user["id"], resume_flow.domain, explore_profile, context="exploration-resume")

    paused_ref[0] = False
    agent.resume()
    await platform_db.update_flow(flow_id, {
        "exploration_status": "exploring",
        "exploration_input_request": None,
        "exploration_confirmation_request": None,
    })
    await _broadcast_flow_update(flow_id)
    return {"status": "exploring", "flow_id": flow_id}


def _flow_to_dict(flow: Flow) -> dict:
    return {
        "id": flow.id,
        "name": flow.name,
        "domain": flow.domain,
        "task": flow.task,
        "entry_url": flow.entry_url,
        "inputs": json.loads(flow.inputs),
        "icon": flow.icon,
        "created_by": flow.created_by,
        "created_at": flow.created_at,
        "exploration_status": flow.exploration_status,
        "exploration_error": flow.exploration_error,
        "exploration_notes": flow.exploration_notes,
        "exploration_tree": json.loads(flow.exploration_tree),
        "exploration_sandbox_id": flow.exploration_sandbox_id,
        "exploration_input_request": json.loads(flow.exploration_input_request) if flow.exploration_input_request else None,
        "exploration_confirmation_request": flow.exploration_confirmation_request,
        "auth_url": flow.auth_url,
        "auth_before_explore": bool(flow.auth_before_explore),
        "auth_sandbox_id": flow.auth_sandbox_id,
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
# Profile endpoints
# ---------------------------------------------------------------------------

@app.get("/api/profiles")
async def profiles_list(user: dict = Depends(auth.get_current_user)) -> list[dict]:
    return profile_mgr.list_profiles(user["id"])


@app.delete("/api/profiles/{profile_name}")
async def profile_delete(profile_name: str, user: dict = Depends(auth.get_current_user)) -> dict:
    deleted = profile_mgr.delete_profile(user["id"], profile_name)
    if not deleted:
        raise HTTPException(status_code=404, detail="Profile not found")
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
        normalized = category if category in webhook_svc.HITL_CATEGORIES else "other"
        from .handoff import pause_payload
        payload.update(pause_payload(message, normalized,
                                    payload.get("browser_cdp_url"),
                                    f"{BASE_URL}/api/v1/runs/{run_id}/resume"))
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
    if profile_mgr.has_profile(user_id, profile_id):
        profile_path = str(PROFILES_DIR / user_id / profile_id)
    try:
        sandbox_result = sandbox_mgr.start(profile_path=profile_path, user_id=user_id)
    except RuntimeError as e:
        await platform_db.update_run(run_id, {"status": "failed", "error": f"Sandbox start failed: {e}"})
        raise HTTPException(status_code=500, detail=f"Failed to start sandbox: {e}")

    sandbox_id = sandbox_result["sandbox_id"]
    cdp_url = sandbox_result.get("cdp_url", "ws://localhost:9222")
    await platform_db.create_sandbox(
        sandbox_result["sandbox_id"], sandbox_result["container_id"],
        user_id, sandbox_result.get("cdp_url", ""),
    )

    await platform_db.update_run(run_id, {"sandbox_id": sandbox_id, "status": "connecting", "profile_id": profile_id})

    if profile_mgr.has_profile(user_id, profile_id):
        try:
            profile_mgr.restore_profile(user_id, profile_id, sandbox_id)
        except Exception as e:
            logger.warning("Profile restore failed: %s", e)

    # Build storage_state from saved session for the flow's domain (scoped to profile_id)
    storage_state = None
    domain_session = await platform_db.get_session_for_domain(user_id, flow.domain, profile_name=profile_id)
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
    return await _start_run_impl(req.flow_id, req.inputs, user_id, req.profile_id or user_id)


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
        # Auth pre-step for runs: if flow has auth_url and no saved session, pause for login
        if flow.auth_url and storage_state is None:
            await progress_callback(RunStatus.PAUSED_FOR_HITL, f"[auth] Please log in at {flow.auth_url}")
            # Navigate the run's sandbox browser to the auth URL via CDP
            import http.client as _http_client
            _cdp_hp = cdp_url.split("//")[1].split("/")[0]
            _cdp_h, _cdp_p = _cdp_hp.rsplit(":", 1)
            try:
                conn = _http_client.HTTPConnection(_cdp_h, int(_cdp_p), timeout=3)
                conn.request("GET", "/json/version", headers={"Host": "localhost"})
                resp = conn.getresponse()
                cdp_info = json.loads(resp.read())
                ws_path = "/" + "/".join(cdp_info["webSocketDebuggerUrl"].split("/")[3:])
                ws_cdp = f"ws://{_cdp_h}:{_cdp_p}{ws_path}"
                from cdp_use.client import CDPClient
                cdp_client = CDPClient(ws_cdp)
                await cdp_client.connect()
                await cdp_client.send("Page.enable", {})
                await cdp_client.send("Page.navigate", {"url": flow.auth_url})
                await cdp_client.close()
            except Exception as e:
                logger.warning("CDP navigate to auth_url failed for run %s: %s", run_id, e)

            # Wait for resume (user logs in and clicks resume in the UI)
            auth_event = asyncio.Event()
            _run_auth_events[run_id] = auth_event
            await auth_event.wait()
            _run_auth_events.pop(run_id, None)

            # Recapture cookies after auth using a temporary browser connection.
            # Note: _run_agents[run_id] is not populated yet (run_flow hasn't been called),
            # so we create a short-lived Browser just to export the post-login storage state.
            _tmp_browser = None
            try:
                from browser_use import Browser as _Browser
                _tmp_browser = _Browser(cdp_url=cdp_url, headers={"Host": "localhost"})
                await _tmp_browser.start()
                await capture_session(_tmp_browser, user_id, flow.domain, profile_id, context=f"run-auth:{run_id}")
            except Exception as _e:
                logger.warning("Auth pre-step capture failed for run %s: %s", run_id, _e)
            finally:
                if _tmp_browser is not None:
                    try:
                        await _tmp_browser.stop()
                    except Exception:
                        pass
            # Rebuild storage_state from the newly saved session
            domain_session = await platform_db.get_session_for_domain(user_id, flow.domain, profile_name=profile_id)
            if domain_session:
                cookies = json.loads(domain_session.cookies)
                local_storage = json.loads(domain_session.local_storage)
                storage_state = {"cookies": cookies, "origins": []}
                if local_storage and flow.entry_url:
                    storage_state["origins"].append({
                        "origin": flow.entry_url.rstrip("/"),
                        "localStorage": [{"name": k, "value": v} for k, v in local_storage.items()],
                    })

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
        # Capture cookies + save browser profile before tearing down the sandbox
        agent_info = _run_agents.pop(run_id, None)
        if agent_info:
            _, browser, _, _ = agent_info
            await capture_session(browser, user_id, flow.domain, profile_id, context=f"run-end:{run_id}")
        try:
            profile_mgr.save_profile(sandbox_id, profile_id or user_id, user_id)
            logger.info("Auto-saved browser profile for run %s (profile=%s)", run_id, profile_id or user_id)
        except Exception as e:
            logger.warning("Profile auto-save failed for run %s: %s", run_id, e)
        try:
            sandbox_mgr.stop(sandbox_id)
            await platform_db.delete_sandbox(sandbox_id)
        except Exception as e:
            logger.warning("Failed to stop sandbox %s: %s", sandbox_id, e)


@app.get("/api/runs")
async def run_list(user: dict = Depends(auth.get_current_user), flow_id: str | None = Query(default=None)) -> list[dict]:
    runs = await platform_db.list_runs(user["id"], flow_id=flow_id)
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

    # Save profile + stop the sandbox container
    if run.sandbox_id:
        try:
            profile_mgr.save_profile(run.sandbox_id, run.profile_id or user["id"], user["id"])
            logger.info("Auto-saved browser profile on run cancel (run=%s)", run_id)
        except Exception as e:
            logger.warning("Profile auto-save failed on run cancel: %s", e)
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

    # Check if this is an auth pre-step resume (before agent starts)
    auth_event = _run_auth_events.get(run_id)
    if auth_event:
        auth_event.set()
        await platform_db.update_run(run_id, {"status": "running", "current_step": "Auth completed, starting flow"})
        await _broadcast_run_progress(run_id, {
            "run_id": run_id,
            "status": "running",
            "message": "Auth completed, starting flow",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return {"run_id": run_id, "status": "running"}

    agent_info = _run_agents.get(run_id)
    if not agent_info:
        raise HTTPException(status_code=404, detail="Agent not found for this run — it may have already finished")

    agent, browser, flow, paused_ref = agent_info

    # Capture cookies from the browser and persist as a session scoped to the run's profile_id
    await capture_session(browser, user["id"], flow.domain, run.profile_id, context=f"run-resume:{run_id}")

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


@app.websocket("/ws/flows/{flow_id}")
async def ws_flow(websocket: WebSocket, flow_id: str) -> None:
    """Stream flow updates (exploration status, HITL requests) to the frontend."""
    await websocket.accept()
    if flow_id not in _flow_ws_connections:
        _flow_ws_connections[flow_id] = []
    _flow_ws_connections[flow_id].append(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep alive; ignore client messages
    except WebSocketDisconnect:
        pass
    finally:
        if flow_id in _flow_ws_connections:
            _flow_ws_connections[flow_id] = [
                ws for ws in _flow_ws_connections[flow_id] if ws != websocket
            ]
            if not _flow_ws_connections[flow_id]:
                del _flow_ws_connections[flow_id]


async def _broadcast_flow_update(flow_id: str) -> None:
    """Push current flow state to all subscribed WebSocket clients."""
    clients = _flow_ws_connections.get(flow_id, [])
    if not clients:
        return
    flow = await platform_db.get_flow(flow_id)
    if not flow:
        return
    data = _flow_to_dict(flow)
    dead = []
    for ws in clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if flow_id in _flow_ws_connections:
            _flow_ws_connections[flow_id] = [
                w for w in _flow_ws_connections[flow_id] if w != ws
            ]


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

@account_router.post("/api/v1/api-keys")
async def api_key_create(req: ApiKeyCreateRequest, user: dict = Depends(auth.get_current_user)) -> dict:
    from .task_settings import TaskSettings
    auth.rate_limit('keys:' + user['id'], TaskSettings.from_env().login_requests_per_minute)
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Key name is required")
    plaintext = auth.generate_api_key()
    key_hash = await asyncio.to_thread(auth.hash_api_key, plaintext)
    key_prefix = plaintext[:12]  # "fp_live_XXXX" (12 chars)
    key_data = await platform_db.create_api_key(user["id"], req.name.strip(), key_hash, key_prefix)
    key_data["key"] = plaintext  # returned only once
    return key_data


@account_router.get("/api/v1/api-keys")
async def api_key_list(user: dict = Depends(auth.get_current_user)) -> list[dict]:
    return await platform_db.list_api_keys(user["id"])


@account_router.delete("/api/v1/api-keys/{key_id}")
async def api_key_delete(key_id: str, user: dict = Depends(auth.get_current_user)) -> dict:
    deleted = await platform_db.delete_api_key(key_id, user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="API key not found")
    return {"status": "deleted"}


app.include_router(account_router)


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
# Trigger Hooks (v1)
# ---------------------------------------------------------------------------

@app.post("/api/v1/flows/{flow_id}/hooks")
async def trigger_hook_create(flow_id: str, req: TriggerHookCreateRequest, user: dict = Depends(auth.get_current_user)) -> dict:
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Hook name is required")
    flow = await platform_db.get_flow(flow_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")
    token = secrets.token_urlsafe(32)
    hook = await platform_db.create_trigger_hook(flow_id, user["id"], req.name.strip(), token)
    hook["url"] = f"{BASE_URL}/api/v1/hooks/{token}"
    return hook


@app.get("/api/v1/flows/{flow_id}/hooks")
async def trigger_hook_list(flow_id: str, user: dict = Depends(auth.get_current_user)) -> list[dict]:
    hooks = await platform_db.list_trigger_hooks(flow_id, user["id"])
    for h in hooks:
        h["url"] = f"{BASE_URL}/api/v1/hooks/{h['token']}"
    return hooks


@app.delete("/api/v1/flows/{flow_id}/hooks/{hook_id}")
async def trigger_hook_delete(flow_id: str, hook_id: str, user: dict = Depends(auth.get_current_user)) -> dict:
    deleted = await platform_db.delete_trigger_hook(hook_id, user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Hook not found")
    return {"status": "deleted"}


@app.post("/api/v1/hooks/{token}")
async def trigger_hook_execute(token: str, req: TriggerHookExecuteRequest) -> dict:
    hook = await platform_db.get_trigger_hook_by_token(token)
    if not hook:
        raise HTTPException(status_code=404, detail="Invalid hook token")
    result = await _start_run_impl(hook["flow_id"], req.inputs, hook["user_id"], req.profile_id or hook["user_id"])
    await platform_db.touch_trigger_hook(hook["id"])
    return result


# ---------------------------------------------------------------------------
# Public Run API (v1) — usable with API keys
# ---------------------------------------------------------------------------

@app.post("/api/v1/runs")
async def v1_run_start(req: RunStartRequest, user: dict = Depends(auth.get_current_user)) -> dict:
    user_id = user["id"]
    return await _start_run_impl(req.flow_id, req.inputs, user_id, req.profile_id or user_id)


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

install_task_api(app)

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
