"""FastAPI backend for uber-bookr — browser automation for Uber booking."""

import os
import uuid
import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sandbox import SandboxManager
from profile import ProfileManager
from booking import UberBookingAgent, BookingStatus, get_booking

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROFILES_DIR = Path(os.environ.get("PROFILES_DIR", "/app/profiles"))
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


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("uber-bookr backend starting. Profiles dir: %s", PROFILES_DIR)
    yield
    logger.info("uber-bookr backend shutting down.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="uber-bookr",
    description="Self-hosted browser automation for Uber booking",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow all for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

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
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# Sandbox endpoints
# ---------------------------------------------------------------------------

@app.post("/api/sandbox/start")
async def sandbox_start(req: SandboxStartRequest):
    """Start a new browser sandbox container.

    If a saved profile exists for the given profile_name, it will be restored
    into the sandbox so the user's Uber login session persists.
    """
    profile_path = None
    if req.profile_name and profile_mgr.has_profile(req.profile_name):
        profile_path = str(PROFILES_DIR / req.profile_name)
        logger.info("Will restore profile '%s' into new sandbox", req.profile_name)

    try:
        result = sandbox_mgr.start(profile_path=profile_path)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Also do tar-based profile restore for full fidelity
    if req.profile_name and profile_mgr.has_profile(req.profile_name):
        try:
            profile_mgr.restore_profile(req.profile_name, result["sandbox_id"])
        except Exception as e:
            logger.warning("Tar-based profile restore failed (file-copy may have succeeded): %s", e)

    return result


@app.post("/api/sandbox/stop")
async def sandbox_stop(req: SandboxStopRequest):
    """Stop and remove a sandbox container."""
    try:
        sandbox_mgr.stop(req.sandbox_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "stopped", "sandbox_id": req.sandbox_id}


# ---------------------------------------------------------------------------
# Profile endpoints
# ---------------------------------------------------------------------------

@app.post("/api/profile/save")
async def profile_save(req: ProfileSaveRequest):
    """Save the browser profile from a running sandbox to host storage."""
    try:
        saved_path = profile_mgr.save_profile(req.sandbox_id, req.profile_name)
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
async def profile_status(profile_name: str = "default"):
    """Check if a saved profile exists and return its metadata."""
    info = profile_mgr.get_profile_info(profile_name)

    return {
        "connected": info["exists"],
        "profile_name": profile_name if info["exists"] else None,
        "last_saved": info.get("last_modified"),
        "source": info.get("source"),
    }


@app.post("/api/profile/import")
async def profile_import(request: Request):
    """Import session data from the Chrome extension.

    Receives cookies, localStorage, sessionStorage from the extension
    and writes them into a profile directory. Cookies are saved in
    Playwright-compatible format for injection at booking time.
    """
    try:
        session_data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    profile_name = session_data.pop("profile_name", "default") if "profile_name" in session_data else "default"

    cookies = session_data.get("cookies", [])
    if not cookies:
        raise HTTPException(status_code=400, detail="No cookies in payload")

    try:
        profile_mgr.import_session(profile_name, session_data)
    except Exception as e:
        logger.error("Failed to import session: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")

    logger.info("Imported session for profile '%s': %d cookies", profile_name, len(cookies))

    return {
        "status": "imported",
        "profile_name": profile_name,
        "cookie_count": len(cookies),
    }


# ---------------------------------------------------------------------------
# Booking endpoints
# ---------------------------------------------------------------------------

@app.post("/api/booking/start")
async def booking_start(req: BookingStartRequest):
    """Start an Uber booking.

    Spins up a sandbox with the saved profile, then runs the browser-use agent
    to automate the booking. Progress is streamed via WebSocket at
    /ws/booking/{booking_id}.
    """
    booking_id = str(uuid.uuid4())[:12]

    # Start a sandbox with the user's saved profile
    profile_path = None
    if profile_mgr.has_profile(req.profile_name):
        profile_path = str(PROFILES_DIR / req.profile_name)

    try:
        sandbox_result = sandbox_mgr.start(profile_path=profile_path)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"Failed to start sandbox: {e}")

    sandbox_id = sandbox_result["sandbox_id"]
    cdp_url = sandbox_result.get("cdp_url", "ws://localhost:9222")

    # Restore full profile via tar if available
    if profile_mgr.has_profile(req.profile_name):
        try:
            profile_mgr.restore_profile(req.profile_name, sandbox_id)
        except Exception as e:
            logger.warning("Profile restore failed: %s", e)

    # Progress callback that broadcasts to WebSocket clients
    async def on_progress(status: BookingStatus, message: str):
        await _broadcast_booking_progress(booking_id, {
            "booking_id": booking_id,
            "status": status.value,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # Run the booking in the background so the HTTP response returns immediately
    asyncio.create_task(
        _run_booking(
            booking_id=booking_id,
            pickup=req.pickup,
            dropoff=req.dropoff,
            ride_type=req.ride_type,
            sandbox_id=sandbox_id,
            cdp_url=cdp_url,
            progress_callback=on_progress,
            profile_name=req.profile_name,
        )
    )

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


@app.get("/api/booking/{booking_id}/status")
async def booking_status(booking_id: str):
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
    """Real-time booking progress updates.

    Clients connect here before or after calling POST /api/booking/start.
    The server pushes JSON messages as the booking progresses.
    Send "ping" to receive "pong" as a heartbeat.
    """
    await websocket.accept()

    if booking_id not in _ws_connections:
        _ws_connections[booking_id] = []
    _ws_connections[booking_id].append(websocket)

    logger.info("WebSocket client connected for booking %s", booking_id)

    try:
        # Send current state if booking already exists
        state = get_booking(booking_id)
        if state:
            await websocket.send_json({
                "booking_id": state.booking_id,
                "status": state.status.value,
                "steps_completed": state.steps_completed,
                "current_step": state.current_step,
                "error": state.error,
            })

        # Keep connection alive — progress_callback pushes updates
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
