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
import json
import logging

import httpx
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

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
        browser_ws_url, target_id = await _resolve_browser_and_target(cdp_host, cdp_port)
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

            # -- Tab tracking --
            # Maintain a list of open page targets so the frontend can render a tab bar.
            open_tabs: dict[str, dict] = {}  # targetId -> {id, title, url}

            async def _send_tab_list() -> None:
                """Push current tab list + active tab to the frontend."""
                try:
                    await websocket.send_json({
                        "type": "tabs",
                        "tabs": list(open_tabs.values()),
                        "activeTargetId": active["target_id"],
                    })
                except Exception:
                    pass

            async def on_target_created(params: dict, _session_id: str | None) -> None:
                info = params.get("targetInfo", {})
                if info.get("type") != "page":
                    return
                new_tid = info.get("targetId", "")
                if not new_tid:
                    return
                open_tabs[new_tid] = {
                    "id": new_tid,
                    "title": info.get("title", ""),
                    "url": info.get("url", ""),
                }
                # Auto-switch screencast to the new tab
                if new_tid != active["target_id"]:
                    logger.info("New page target detected: %s — re-attaching screencast", new_tid)
                    try:
                        await _attach_to_target(new_tid)
                    except Exception as exc:
                        logger.warning("Failed to attach to new target %s: %s", new_tid, exc)
                await _send_tab_list()

            async def on_target_info_changed(params: dict, _session_id: str | None) -> None:
                info = params.get("targetInfo", {})
                if info.get("type") != "page":
                    return
                tid = info.get("targetId", "")
                if tid in open_tabs:
                    open_tabs[tid] = {
                        "id": tid,
                        "title": info.get("title", ""),
                        "url": info.get("url", ""),
                    }
                    await _send_tab_list()

            async def on_target_destroyed(params: dict, _session_id: str | None) -> None:
                tid = params.get("targetId", "")
                if tid not in open_tabs:
                    return
                del open_tabs[tid]
                # If the destroyed tab was active, switch back to the last remaining tab
                if tid == active["target_id"] and open_tabs:
                    fallback_tid = list(open_tabs.keys())[-1]
                    logger.info("Active tab closed, switching to %s", fallback_tid)
                    try:
                        await _attach_to_target(fallback_tid)
                    except Exception as exc:
                        logger.warning("Failed to switch to fallback target: %s", exc)
                await _send_tab_list()

            # Seed initial tab list
            open_tabs[target_id] = {"id": target_id, "title": "", "url": ""}

            # Discover targets so we get target events
            await cdp.send_raw("Target.setDiscoverTargets", {"discover": True})
            cdp._event_registry.register("Target.targetCreated", on_target_created)
            cdp._event_registry.register("Target.targetInfoChanged", on_target_info_changed)
            cdp._event_registry.register("Target.targetDestroyed", on_target_destroyed)

            await _send_tab_list()

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
                async def _ack_frame(sid: str, sc_sid: str) -> None:
                    try:
                        await cdp.send_raw(
                            "Page.screencastFrameAck",
                            {"sessionId": sc_sid},
                            session_id=sid,
                        )
                    except (ConnectionClosedError, ConnectionClosedOK, ConnectionError):
                        pass

                asyncio.create_task(_ack_frame(active["session_id"], screencast_sid))

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

            # -- Loading state handlers --
            async def on_loading_started(params: dict, frame_session_id: str | None) -> None:
                if frame_session_id != active["session_id"]:
                    return
                try:
                    await websocket.send_json({"type": "loading", "loading": True})
                except Exception:
                    pass

            async def on_loading_stopped(params: dict, frame_session_id: str | None) -> None:
                if frame_session_id != active["session_id"]:
                    return
                try:
                    await websocket.send_json({"type": "loading", "loading": False})
                except Exception:
                    pass

            cdp._event_registry.register("Page.screencastFrame", on_screencast_frame)
            cdp._event_registry.register("Page.frameNavigated", on_frame_navigated)
            cdp._event_registry.register("Page.frameStartedLoading", on_loading_started)
            cdp._event_registry.register("Page.frameStoppedLoading", on_loading_stopped)

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
                    except (ConnectionClosedError, ConnectionClosedOK, ConnectionError):
                        break
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
                    except (ConnectionClosedError, ConnectionClosedOK, ConnectionError):
                        break
                    except Exception:
                        pass

                elif msg_type == "switchTab":
                    target_id = data.get("targetId", "")
                    if target_id and target_id in open_tabs and target_id != active["target_id"]:
                        try:
                            await _attach_to_target(target_id)
                            await _send_tab_list()
                        except Exception as exc:
                            logger.warning("Tab switch to %s failed: %s", target_id, exc)

                elif msg_type == "navigate":
                    action = data.get("action")
                    try:
                        if action == "back":
                            nav = await cdp.send_raw("Page.getNavigationHistory", session_id=sid)
                            idx = nav.get("currentIndex", 0)
                            entries = nav.get("entries", [])
                            if idx > 0:
                                await cdp.send_raw(
                                    "Page.navigateToHistoryEntry",
                                    {"entryId": entries[idx - 1]["id"]},
                                    session_id=sid,
                                )
                        elif action == "forward":
                            nav = await cdp.send_raw("Page.getNavigationHistory", session_id=sid)
                            idx = nav.get("currentIndex", 0)
                            entries = nav.get("entries", [])
                            if idx < len(entries) - 1:
                                await cdp.send_raw(
                                    "Page.navigateToHistoryEntry",
                                    {"entryId": entries[idx + 1]["id"]},
                                    session_id=sid,
                                )
                        elif action == "reload":
                            await cdp.send_raw("Page.reload", session_id=sid)
                        elif action == "goto":
                            url = data.get("url", "")
                            if url:
                                if not url.startswith(("http://", "https://")):
                                    url = "https://" + url
                                await cdp.send_raw(
                                    "Page.navigate", {"url": url}, session_id=sid,
                                )
                    except (ConnectionClosedError, ConnectionClosedOK, ConnectionError):
                        break
                    except Exception as exc:
                        logger.warning("Navigate %s failed: %s", action, exc)

    except WebSocketDisconnect:
        pass
    except (ConnectionClosedError, ConnectionClosedOK, ConnectionError):
        logger.debug("CDP connection closed for sandbox %s (sandbox stopped)", sandbox_id)
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

async def _resolve_browser_and_target(cdp_host: str, cdp_port: int) -> tuple[str, str]:
    """Return (browser_ws_url, first_page_target_id). Fully async — no event loop blocking."""
    base = f"http://{cdp_host}:{cdp_port}"
    headers = {"Host": "localhost"}

    async with httpx.AsyncClient(timeout=5) as client:
        # Browser-level WS URL
        resp = await client.get(f"{base}/json/version", headers=headers)
        resp.raise_for_status()
        info = resp.json()
        raw_browser_ws = info["webSocketDebuggerUrl"]
        path = "/" + "/".join(raw_browser_ws.split("/")[3:])
        browser_ws_url = f"ws://{cdp_host}:{cdp_port}{path}"

        # First top-level page target
        resp2 = await client.get(f"{base}/json", headers=headers)
        resp2.raise_for_status()
        targets = resp2.json()

    target_id = next(
        t["id"]
        for t in targets
        if t.get("type") == "page" and not t.get("parentId")
    )

    return browser_ws_url, target_id
