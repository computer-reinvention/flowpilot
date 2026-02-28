"""FastAPI backend for uber-bookr — browser automation for Uber booking."""

import os
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
from .booking import UberBookingAgent, BookingStatus, get_booking
from .config import PROFILES_DIR
from . import auth

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_PROFILE = os.environ.get("DEFAULT_PROFILE", "default")
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", "/app/frontend"))

# ---------------------------------------------------------------------------
# Singleton services
# ---------------------------------------------------------------------------

sandbox_mgr = SandboxManager(profiles_host_dir=str(PROFILES_DIR))
profile_mgr = ProfileManager(profiles_dir=PROFILES_DIR)
booking_agent = UberBookingAgent()

# ---------------------------------------------------------------------------
# WebSocket connection registry
# ---------------------------------------------------------------------------

_ws_connections: dict[str, list[WebSocket]] = {}  # booking_id -> [websocket, ...]
_booking_tasks: dict[str, asyncio.Task] = {}  # booking_id -> background task


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    await auth.init_db()
    # H3: Clean up orphaned sandbox containers from previous runs
    removed = sandbox_mgr.cleanup_orphans()
    if removed:
        logger.info("Cleaned up %d orphaned sandbox containers", removed)
    logger.info("uber-bookr backend starting. Profiles dir: %s", PROFILES_DIR)
    yield
    # H3: Stop all tracked sandboxes on shutdown
    sandbox_mgr.stop_all()
    logger.info("uber-bookr backend shutting down.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="uber-bookr",
    description="Self-hosted browser automation for Uber booking",
    version="0.2.0",
    lifespan=lifespan,
)

# CORS — restrict origins (set ALLOWED_ORIGINS env var for production)
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


class BookingStartRequest(BaseModel):
    pickup: str
    dropoff: str
    ride_type: str = "UberGo"
    profile_name: str = "default"


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
    """Create a new user account."""
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
    """Log in with email and password."""
    user = await auth.authenticate(req.email, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = auth.create_token(user["id"])
    return {"token": token, "user": user}


@app.get("/api/auth/me")
async def auth_me(user: dict = Depends(auth.get_current_user)):
    """Get current user info."""
    return {"user": user}


# ---------------------------------------------------------------------------
# Sandbox endpoints (auth required)
# ---------------------------------------------------------------------------

@app.post("/api/sandbox/start")
async def sandbox_start(req: SandboxStartRequest, user: dict = Depends(auth.get_current_user)):
    """Start a new browser sandbox container."""
    user_id = user["id"]
    profile_name = req.profile_name or "default"

    # H5: Validate profile name
    if not SandboxManager.validate_profile_name(profile_name):
        raise HTTPException(status_code=400, detail="Invalid profile name. Use only alphanumeric, hyphens, underscores.")

    profile_path = None
    if profile_mgr.has_profile(user_id, profile_name):
        profile_path = str(PROFILES_DIR / user_id / profile_name)
        logger.info("Will restore profile '%s' for user %s into new sandbox", profile_name, user_id)

    try:
        result = sandbox_mgr.start(profile_path=profile_path, user_id=user_id)
    except RuntimeError as e:
        # H2: Per-user limit exceeded returns 429
        if "already has" in str(e) and "active sandboxes" in str(e):
            raise HTTPException(status_code=429, detail=str(e))
        raise HTTPException(status_code=500, detail=str(e))

    if profile_mgr.has_profile(user_id, profile_name):
        try:
            profile_mgr.restore_profile(user_id, profile_name, result["sandbox_id"])
        except Exception as e:
            logger.warning("Tar-based profile restore failed: %s", e)

    return result


@app.post("/api/sandbox/stop")
async def sandbox_stop(req: SandboxStopRequest, user: dict = Depends(auth.get_current_user)):
    """Stop and remove a sandbox container."""
    user_id = user["id"]

    # H4: Verify sandbox belongs to the requesting user
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
# Profile endpoints (auth required)
# ---------------------------------------------------------------------------

@app.post("/api/profile/save")
async def profile_save(req: ProfileSaveRequest, user: dict = Depends(auth.get_current_user)):
    """Save the browser profile from a running sandbox to host storage."""
    user_id = user["id"]

    # H5: Validate profile name
    if not SandboxManager.validate_profile_name(req.profile_name):
        raise HTTPException(status_code=400, detail="Invalid profile name. Use only alphanumeric, hyphens, underscores.")

    # H4: Verify sandbox belongs to the requesting user
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

    return {
        "profile_name": req.profile_name,
        "path": str(saved_path),
        "status": "saved",
    }


@app.get("/api/profile/status")
async def profile_status(profile_name: str = "default", user: dict = Depends(auth.get_current_user)):
    """Check if a saved profile exists and return its metadata."""
    user_id = user["id"]
    info = profile_mgr.get_profile_info(user_id, profile_name)

    return {
        "connected": info["exists"],
        "profile_name": profile_name if info["exists"] else None,
        "last_saved": info.get("last_modified"),
        "source": info.get("source"),
    }


@app.post("/api/profile/import")
async def profile_import(request: Request, user: dict = Depends(auth.get_current_user)):
    """Import session data from the Chrome extension."""
    user_id = user["id"]
    try:
        session_data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    profile_name = session_data.pop("profile_name", "default") if "profile_name" in session_data else "default"

    # H5: Validate profile name
    if not SandboxManager.validate_profile_name(profile_name):
        raise HTTPException(status_code=400, detail="Invalid profile name. Use only alphanumeric, hyphens, underscores.")

    cookies = session_data.get("cookies", [])
    if not cookies:
        raise HTTPException(status_code=400, detail="No cookies in payload")

    try:
        profile_mgr.import_session(user_id, profile_name, session_data)
    except Exception as e:
        logger.error("Failed to import session: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")

    logger.info("Imported session for user %s profile '%s': %d cookies", user_id, profile_name, len(cookies))

    return {
        "status": "imported",
        "profile_name": profile_name,
        "cookie_count": len(cookies),
    }


# ---------------------------------------------------------------------------
# Booking endpoints (auth required)
# ---------------------------------------------------------------------------

@app.post("/api/booking/start")
async def booking_start(req: BookingStartRequest, user: dict = Depends(auth.get_current_user)):
    """Start an Uber booking."""
    user_id = user["id"]
    booking_id = str(uuid.uuid4())[:12]
    profile_name = req.profile_name or "default"

    # H5: Validate profile name
    if not SandboxManager.validate_profile_name(profile_name):
        raise HTTPException(status_code=400, detail="Invalid profile name. Use only alphanumeric, hyphens, underscores.")

    profile_path = None
    if profile_mgr.has_profile(user_id, profile_name):
        profile_path = str(PROFILES_DIR / user_id / profile_name)

    try:
        sandbox_result = sandbox_mgr.start(profile_path=profile_path, user_id=user_id)
    except RuntimeError as e:
        if "already has" in str(e) and "active sandboxes" in str(e):
            raise HTTPException(status_code=429, detail=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to start sandbox: {e}")

    sandbox_id = sandbox_result["sandbox_id"]
    cdp_url = sandbox_result.get("cdp_url", "ws://localhost:9222")

    if profile_mgr.has_profile(user_id, profile_name):
        try:
            profile_mgr.restore_profile(user_id, profile_name, sandbox_id)
        except Exception as e:
            logger.warning("Profile restore failed: %s", e)

    async def on_progress(status: BookingStatus, message: str):
        await _broadcast_booking_progress(booking_id, {
            "booking_id": booking_id,
            "status": status.value,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    task = asyncio.create_task(
        _run_booking(
            booking_id=booking_id,
            pickup=req.pickup,
            dropoff=req.dropoff,
            ride_type=req.ride_type,
            sandbox_id=sandbox_id,
            cdp_url=cdp_url,
            progress_callback=on_progress,
            user_id=user_id,
            profile_name=profile_name,
        )
    )
    _booking_tasks[booking_id] = task
    task.add_done_callback(lambda t: _booking_tasks.pop(booking_id, None))

    return {
        "booking_id": booking_id,
        "sandbox_id": sandbox_id,
        "novnc_url": sandbox_result.get("novnc_url"),
        "status": "starting",
    }


async def _run_booking(
    booking_id: str,
    pickup: str,
    dropoff: str,
    ride_type: str,
    sandbox_id: str,
    cdp_url: str,
    progress_callback,
    user_id: str,
    profile_name: str = "default",
):
    """Background task that runs the booking agent."""
    try:
        await booking_agent.book_ride(
            pickup=pickup,
            dropoff=dropoff,
            ride_type=ride_type,
            sandbox_id=sandbox_id,
            cdp_url=cdp_url,
            booking_id=booking_id,
            progress_callback=progress_callback,
            user_id=user_id,
            profile_name=profile_name,
        )
    except Exception as e:
        logger.error("Booking %s failed: %s", booking_id, e)
        await _broadcast_booking_progress(booking_id, {
            "booking_id": booking_id,
            "status": "failed",
            "message": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    finally:
        try:
            sandbox_mgr.stop(sandbox_id)
        except Exception as e:
            logger.warning("Failed to stop sandbox %s: %s", sandbox_id, e)


@app.get("/api/booking/{booking_id}/status")
async def booking_status(booking_id: str, user: dict = Depends(auth.get_current_user)):
    """Get the current status of a booking."""
    state = get_booking(booking_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Booking {booking_id} not found")

    return {
        "booking_id": state.booking_id,
        "status": state.status.value,
        "steps_completed": state.steps_completed,
        "current_step": state.current_step,
        "error": state.error,
        "result": state.result,
    }


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws/booking/{booking_id}")
async def ws_booking(websocket: WebSocket, booking_id: str):
    """Real-time booking progress updates."""
    await websocket.accept()

    if booking_id not in _ws_connections:
        _ws_connections[booking_id] = []
    _ws_connections[booking_id].append(websocket)

    logger.info("WebSocket client connected for booking %s", booking_id)

    try:
        state = get_booking(booking_id)
        if state:
            await websocket.send_json({
                "booking_id": state.booking_id,
                "status": state.status.value,
                "steps_completed": state.steps_completed,
                "current_step": state.current_step,
                "error": state.error,
            })

        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected for booking %s", booking_id)
    finally:
        if booking_id in _ws_connections:
            _ws_connections[booking_id] = [
                ws for ws in _ws_connections[booking_id] if ws != websocket
            ]
            if not _ws_connections[booking_id]:
                del _ws_connections[booking_id]


async def _broadcast_booking_progress(booking_id: str, data: dict):
    """Send a progress update to all WebSocket clients watching a booking."""
    clients = _ws_connections.get(booking_id, [])
    dead = []
    for ws in clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)

    for ws in dead:
        if booking_id in _ws_connections:
            _ws_connections[booking_id] = [
                w for w in _ws_connections[booking_id] if w != ws
            ]


# ---------------------------------------------------------------------------
# Static files (frontend)
# ---------------------------------------------------------------------------

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
