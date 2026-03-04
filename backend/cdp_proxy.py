"""CDP WebSocket proxy — streams browser screencast frames to the frontend.

Pipeline:
  Frontend canvas  <-->  /ws/browser/{sandbox_id}  <-->  Chromium CDP (port 9223)

Uses cdp-use (https://github.com/browser-use/cdp-use) for the CDP client.

Key design:
  - Connects to the BROWSER-LEVEL WebSocket (from /json/version).
  - Creates an independent flat session via Target.attachToTarget(flatten=True).
    This coexists with browser-use/Playwright's own session without conflict.
  - Frames arrive at native viewport resolution (no maxWidth/maxHeight downscaling)
    so mouse coordinates from the canvas are 1:1 with the browser viewport.
"""

import asyncio
import http.client
import json
import logging

from cdp_use.client import CDPClient
from fastapi import WebSocket, WebSocketDisconnect

from . import auth as _auth
from .sandbox import SandboxManager

logger = logging.getLogger(__name__)

JPEG_QUALITY = 80


async def handle(
    websocket: WebSocket,
    sandbox_id: str,
    sandbox_mgr: SandboxManager,
    token: str = "",
) -> None:
    # -- authenticate --
    user_id = _auth.decode_token(token)
    if not user_id:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    # -- ownership check --
    try:
        owner = sandbox_mgr.get_sandbox_owner(sandbox_id)
        if owner and owner != user_id:
            await websocket.close(code=4003, reason="Forbidden")
            return
    except ValueError:
        await websocket.close(code=4004, reason="Sandbox not found")
        return

    info = sandbox_mgr._sandboxes.get(sandbox_id)
    if not info:
        await websocket.close(code=4004, reason="Sandbox not found")
        return

    cdp_host = f"flowpilot-{sandbox_id}"
    cdp_port = info.cdp_port  # 9223

    try:
        browser_ws_url, target_id = _resolve_browser_and_target(cdp_host, cdp_port)
    except Exception as exc:
        logger.error("Cannot resolve CDP URL for sandbox %s: %s", sandbox_id, exc)
        await websocket.close(code=4503, reason="CDP not available")
        return

    await websocket.accept()
    logger.info("CDP proxy open: sandbox=%s user=%s target=%s", sandbox_id, user_id, target_id)

    try:
        async with CDPClient(
            browser_ws_url,
            additional_headers={"Host": "localhost"},
        ) as cdp:
            # Mutable state: which target/session the screencast + input are attached to.
            # browser-use may open new tabs; we track the active one and re-attach.
            active = {"target_id": target_id, "session_id": ""}

            async def _attach_to_target(tid: str) -> str:
                """Attach to a page target, enable screencast, return session_id."""
                result = await cdp.send_raw(
                    "Target.attachToTarget",
                    {"targetId": tid, "flatten": True},
                )
                sid = result["sessionId"]
                await cdp.send_raw("Page.enable", session_id=sid)
                await cdp.send_raw("Page.bringToFront", session_id=sid)
                await cdp.send_raw(
                    "Page.startScreencast",
                    {"format": "jpeg", "quality": JPEG_QUALITY, "everyNthFrame": 1},
                    session_id=sid,
                )
                active["target_id"] = tid
                active["session_id"] = sid
                logger.info("Screencast attached to target %s (session %s)", tid, sid)

                # Send current URL
                try:
                    nav = await cdp.send_raw("Page.getNavigationHistory", session_id=sid)
                    entries = nav.get("entries", [])
                    idx = nav.get("currentIndex", -1)
                    if 0 <= idx < len(entries):
                        url = entries[idx].get("url", "")
                        if url and not url.startswith("about:"):
                            await websocket.send_json({"type": "nav", "url": url})
                except Exception:
                    pass

                return sid

            # Initial attach
            await _attach_to_target(target_id)

            # -- Target tracking: follow browser-use when it opens new tabs --
            async def on_target_created(params: dict, _session_id: str | None) -> None:
                info = params.get("targetInfo", {})
                if info.get("type") != "page":
                    return
                new_tid = info.get("targetId", "")
                if not new_tid or new_tid == active["target_id"]:
                    return
                logger.info("New page target detected: %s — re-attaching screencast", new_tid)
                try:
                    await _attach_to_target(new_tid)
                except Exception as exc:
                    logger.warning("Failed to attach to new target %s: %s", new_tid, exc)

            # Discover targets so we get targetCreated events
            await cdp.send_raw("Target.setDiscoverTargets", {"discover": True})
            cdp._event_registry.register("Target.targetCreated", on_target_created)

            # -- Screencast frame handler --
            async def on_screencast_frame(params: dict, frame_session_id: str | None) -> None:
                if frame_session_id != active["session_id"]:
                    return
                screencast_sid = params["sessionId"]
                try:
                    await websocket.send_json({
                        "type": "frame",
                        "data": params["data"],
                        "sessionId": screencast_sid,
                        "metadata": params.get("metadata", {}),
                    })
                except Exception:
                    return
                asyncio.create_task(
                    cdp.send_raw(
                        "Page.screencastFrameAck",
                        {"sessionId": screencast_sid},
                        session_id=active["session_id"],
                    )
                )

            # -- Navigation handler --
            async def on_frame_navigated(params: dict, frame_session_id: str | None) -> None:
                if frame_session_id != active["session_id"]:
                    return
                url = params.get("frame", {}).get("url", "")
                if url and not url.startswith("about:"):
                    try:
                        await websocket.send_json({"type": "nav", "url": url})
                    except Exception:
                        pass

            cdp._event_registry.register("Page.screencastFrame", on_screencast_frame)
            cdp._event_registry.register("Page.frameNavigated", on_frame_navigated)

            # -- Input relay loop --
            while True:
                try:
                    data = await websocket.receive_json()
                except (WebSocketDisconnect, RuntimeError):
                    break
                except Exception:
                    continue

                msg_type = data.get("type")
                sid = active["session_id"]

                if msg_type == "mouse":
                    logger.debug(
                        "CDP input: %s at (%s, %s)",
                        data.get("eventType"), data.get("x"), data.get("y"),
                    )
                    try:
                        await cdp.send_raw(
                            "Input.dispatchMouseEvent",
                            {
                                "type": data["eventType"],
                                "x": data["x"],
                                "y": data["y"],
                                "button": data.get("button", "none"),
                                "buttons": data.get("buttons", 0),
                                "clickCount": data.get("clickCount", 0),
                                "modifiers": data.get("modifiers", 0),
                                "deltaX": data.get("deltaX", 0.0),
                                "deltaY": data.get("deltaY", 0.0),
                                "pointerType": "mouse",
                            },
                            session_id=sid,
                        )
                    except Exception:
                        pass

                elif msg_type == "key":
                    try:
                        await cdp.send_raw(
                            "Input.dispatchKeyEvent",
                            {
                                "type": data["eventType"],
                                "key": data.get("key", ""),
                                "code": data.get("code", ""),
                                "text": data.get("text", ""),
                                "unmodifiedText": data.get("unmodifiedText", ""),
                                "modifiers": data.get("modifiers", 0),
                                "windowsVirtualKeyCode": data.get("keyCode", 0),
                                "nativeVirtualKeyCode": data.get("keyCode", 0),
                                "location": data.get("location", 0),
                                "isKeypad": data.get("isKeypad", False),
                                "isSystemKey": False,
                            },
                            session_id=sid,
                        )
                    except Exception:
                        pass

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error("CDP proxy error (sandbox=%s): %s", sandbox_id, exc)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("CDP proxy closed: sandbox=%s", sandbox_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_browser_and_target(cdp_host: str, cdp_port: int) -> tuple[str, str]:
    """Return (browser_ws_url, first_page_target_id)."""
    # Browser-level WS URL
    conn = http.client.HTTPConnection(cdp_host, cdp_port, timeout=5)
    conn.request("GET", "/json/version", headers={"Host": "localhost"})
    resp = conn.getresponse()
    info = json.loads(resp.read())
    raw_browser_ws = info["webSocketDebuggerUrl"]
    path = "/" + "/".join(raw_browser_ws.split("/")[3:])
    browser_ws_url = f"ws://{cdp_host}:{cdp_port}{path}"

    # First top-level page target
    conn2 = http.client.HTTPConnection(cdp_host, cdp_port, timeout=5)
    conn2.request("GET", "/json", headers={"Host": "localhost"})
    resp2 = conn2.getresponse()
    targets = json.loads(resp2.read())
    target_id = next(
        t["id"]
        for t in targets
        if t.get("type") == "page" and not t.get("parentId")
    )

    return browser_ws_url, target_id
