import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.task_bootstrap import TaskHost
from backend.task_contracts import (
    AssistanceRequest, BrowserMetadata, ErrorCode, Interaction, Principal, TaskFault, TaskState,
    TaskView, task_error,
)
from backend.task_ports import AssistanceBinding, Lease
from backend.task_settings import TaskSettings
from backend.tasks.assistance import create_assistance, create_router
from tests.fixtures.browser_tasks.doubles import bindings_double, browser_double


TOKEN = "A" * 43
BASE = "/help/" + TOKEN
ORIGIN = {"Origin": "https://viewer.test"}


def scenario():
    timestamp = datetime.now(timezone.utc)
    settings = TaskSettings(enabled=True)
    assistance = create_assistance(settings)
    bindings = replace(bindings_double(), assistance=assistance)
    lease = Lease(principal=Principal(account_id="account_a"), task_id="task_a", session_id="session_a", profile_id="profile_a", generation=7)
    interaction = Interaction(interaction_id="pause_a", generation=3, kind="browser", message="Synthetic intervention", expires_at=timestamp + timedelta(minutes=5))
    binding = AssistanceBinding(lease=lease, interaction_id="pause_a", generation=3, digest=hashlib.sha256(TOKEN.encode()).hexdigest(), expires_at=timestamp + timedelta(minutes=3))
    view = TaskView(task_id="task_a", session_id="session_a", profile_id="profile_a", state="waiting_for_browser", revision=2, interaction=interaction, created_at=timestamp, updated_at=timestamp)
    running = view.model_copy(update={"state": TaskState.RUNNING, "interaction": None, "revision": 3})
    state = {"binding": binding, "view": view, "lease": lease, "revoked": False, "saved": []}

    async def capability(digest, now):
        if digest != state["binding"].digest or state["revoked"] or state["binding"].expires_at <= now:
            raise TaskFault(task_error(ErrorCode.ASSISTANCE_EXPIRED, "Assistance is unavailable"))
        return state["binding"]

    async def get(principal, task_id, poll):
        if principal != lease.principal or task_id != lease.task_id:
            raise TaskFault(task_error(ErrorCode.NOT_FOUND, "Task not found"))
        return state["view"]

    async def assistance_lease(principal, task_id, request):
        current = await get(principal, task_id, None)
        interaction = current.interaction
        if (current.state != TaskState.WAITING_FOR_BROWSER or not interaction or
                request.interaction_id != interaction.interaction_id or request.generation != interaction.generation or
                interaction.expires_at <= datetime.now(timezone.utc)):
            raise TaskFault(task_error(ErrorCode.STALE_GENERATION, "Interaction is no longer active"))
        return state["lease"]

    async def save(binding):
        current = await assistance_lease(binding.lease.principal, binding.lease.task_id,
                                         AssistanceRequest(interaction_id=binding.interaction_id, generation=binding.generation))
        if current != binding.lease or binding.expires_at <= datetime.now(timezone.utc):
            raise TaskFault(task_error(ErrorCode.STALE_GENERATION, "Interaction is no longer active"))
        state["binding"] = binding
        state["saved"].append(binding)
        state["revoked"] = False

    async def revoke(lease_arg):
        assert lease_arg == lease
        state["revoked"] = True

    async def respond(principal, task_id, interaction_id, response):
        assert principal == lease.principal and task_id == lease.task_id
        assert interaction_id == binding.interaction_id
        state["view"] = running
        return running

    handle = browser_double()
    for name in ("send_input", "release"):
        getattr(handle, name).side_effect = None
        getattr(handle, name).return_value = None
    handle.screenshot.side_effect = None
    handle.screenshot.return_value = b"\x89PNG synthetic"
    handle.metadata.side_effect = None
    handle.metadata.return_value = BrowserMetadata(url="https://example.test/form", viewport_width=1280, viewport_height=720)
    bindings.control.capability.side_effect = capability
    bindings.control.get.side_effect = get
    bindings.control.assistance_lease.side_effect = assistance_lease
    bindings.control.save_capability.side_effect = save
    bindings.control.revoke_capabilities.side_effect = revoke
    bindings.control.respond.side_effect = respond
    bindings.control.cancel.side_effect = None
    bindings.control.cancel.return_value = running.model_copy(update={"state": TaskState.CANCELLING})
    bindings.executor.browser.side_effect = None
    bindings.executor.browser.return_value = handle
    host = TaskHost(settings, bindings, (create_router(bindings),))
    return host, bindings, handle, state


@pytest.mark.asyncio
async def test_viewer_headers_and_no_model_or_status_capability():
    host, bindings, handle, state = scenario()
    await host.startup()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.app), base_url="https://viewer.test") as client:
            response = await client.get(BASE)
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
            assert "script-src 'nonce-" in response.headers["Content-Security-Policy"]
            assert TOKEN not in response.text
            assert "CDP" not in response.text
            assert (await client.get(BASE + "/screen")).content == b"\x89PNG synthetic"
            assert TOKEN not in repr(state["binding"])
            assert TOKEN not in state["view"].model_dump_json()
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"Origin": "null"}, {"Origin": "https://foreign.test"}, {"Origin": "http://viewer.test"}, {"Origin": "https://viewer.test:444"}, {"Origin": "https://viewer.test/"}, {"Origin": "https://viewer.test", "Host": "bad@viewer.test"}, {"Origin": "https://evil.test", "X-Forwarded-Host": "evil.test", "X-Forwarded-Proto": "https"}])
async def test_input_requires_real_same_origin_not_forwarded_claim(headers):
    host, bindings, handle, state = scenario()
    await host.startup()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.app), base_url="https://viewer.test") as client:
            response = await client.post(BASE + "/input", headers=headers, json={"kind": "type", "text": "private-canary"})
            assert response.status_code == 404
            assert "private-canary" not in response.text
            handle.send_input.assert_not_awaited()
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [{"kind": "Runtime.evaluate", "expression": "secret"}, {"kind": "type", "text": "s" * 8001}, {"kind": "move", "x": -1, "y": 1}, {"kind": "type", "text": "canary", "cdp_url": "ws://attacker"}])
async def test_strict_bounded_input_never_echoes_values(command):
    host, bindings, handle, state = scenario()
    await host.startup()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.app), base_url="https://viewer.test") as client:
            response = await client.post(BASE + "/input", headers=ORIGIN, json=command)
            assert response.status_code == 422
            assert "canary" not in response.text and "attacker" not in response.text
            assert (await client.post(BASE + "/input", headers=ORIGIN, content=b"x" * 32769)).status_code == 422
            handle.send_input.assert_not_awaited()
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_every_frozen_command_and_drain_before_shared_response():
    host, bindings, handle, state = scenario()
    await host.startup()
    commands = json.loads((Path(__file__).parent / "fixtures/browser_tasks/viewer_commands.json").read_text())["commands"]
    entered, proceed = asyncio.Event(), asyncio.Event()
    order = []

    async def input(command):
        order.append(command.kind)
        if command.kind == "down":
            entered.set()
            await proceed.wait()

    async def release():
        order.append("fence")

    old_respond = bindings.control.respond.side_effect

    async def respond(*args):
        assert order[-1] == "fence"
        order.append("respond")
        return await old_respond(*args)

    handle.send_input.side_effect = input
    handle.release.side_effect = release
    bindings.control.respond.side_effect = respond
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.app), base_url="https://viewer.test") as client:
            for command in commands:
                if command["kind"] != "down":
                    assert (await client.post(BASE + "/input", headers=ORIGIN, json=command)).json() == {"accepted": True}
            click = asyncio.create_task(client.post(BASE + "/input", headers=ORIGIN, json={"kind": "down", "x": 1, "y": 1, "button": "left"}))
            await entered.wait()
            response = {"generation": 3, "idempotency_key": "synthetic-response", "kind": "browser", "browser_resolved": True}
            resume = asyncio.create_task(client.post(BASE + "/respond", headers=ORIGIN, json=response))
            await asyncio.sleep(.02)
            assert not resume.done()
            proceed.set()
            assert (await click).status_code == 200
            assert (await resume).status_code == 202
            assert order[-2:] == ["fence", "respond"]
            assert (await client.post(BASE + "/input", headers=ORIGIN, json={"kind": "release"})).status_code == 410
            assert (await client.post(BASE + "/respond", headers=ORIGIN, json=response)).status_code == 410
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["forged", "expired", "revoked", "generation", "execution_generation", "foreign_profile", "running"])
async def test_invalid_capabilities_fail_all_http_endpoints(condition):
    host, bindings, handle, state = scenario()
    await host.startup()
    base = BASE
    if condition == "forged":
        base = "/help/" + "B" * 43
    elif condition == "expired":
        state["binding"] = state["binding"].model_copy(update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    elif condition == "revoked":
        state["revoked"] = True
    elif condition == "generation":
        state["view"] = state["view"].model_copy(update={"interaction": state["view"].interaction.model_copy(update={"generation": 4})})
    elif condition == "foreign_profile":
        state["view"] = state["view"].model_copy(update={"profile_id": "foreign"})
    elif condition == "execution_generation":
        state["lease"] = state["lease"].model_copy(update={"generation": 8})
    else:
        state["view"] = state["view"].model_copy(update={"state": TaskState.RUNNING, "interaction": None})
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.app), base_url="https://viewer.test") as client:
            for suffix in ("", "/state", "/screen"):
                assert (await client.get(base + suffix)).status_code == 410
            for suffix, body in [("/input", {"kind": "release"}), ("/respond", {"generation": 3, "idempotency_key": "test-response-001", "kind": "browser", "browser_resolved": True}), ("/cancel", {})]:
                assert (await client.post(base + suffix, headers=ORIGIN, json=body)).status_code == 410
            handle.send_input.assert_not_awaited()
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_public_mint_and_state_use_real_execution_lease_and_metadata():
    host, bindings, handle, state = scenario()
    await host.startup()
    try:
        grant = await bindings.assistance.mint(state["lease"].principal, "task_a", AssistanceRequest(interaction_id="pause_a", generation=3))
        token = grant.assistance_url.rsplit("/", 1)[1]
        assert state["saved"][-1].lease.generation == 7
        assert state["saved"][-1].generation == 3
        assert grant.generation == 3
        assert token not in state["saved"][-1].model_dump_json()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.app), base_url="https://viewer.test") as client:
            response = await client.get("/help/" + token + "/state")
            assert response.status_code == 200
            assert response.json() == {
                "url": "https://example.test/form", "viewport_width": 1280, "viewport_height": 720,
                "interaction": state["view"].interaction.model_dump(mode="json"),
            }
            assert response.headers["Cache-Control"] == "no-store"
            assert "cdp" not in response.text.lower()
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_public_issuance_digest_only_rotation_and_bounded_expiry():
    host, bindings, handle, state = scenario()
    await host.startup()
    saved = []

    async def save(binding):
        saved.append(binding)
        state["binding"] = binding
        state["revoked"] = False

    bindings.control.save_capability.side_effect = save
    try:
        assistance = bindings.assistance
        grant = await assistance.mint(state["lease"].principal, "task_a", AssistanceRequest(interaction_id="pause_a", generation=3))
        token = grant.assistance_url.rsplit("/", 1)[1]
        assert len(token) == 43
        assert saved[-1].digest == hashlib.sha256(token.encode()).hexdigest()
        assert token not in saved[-1].model_dump_json()
        assert grant.expires_at <= state["view"].interaction.expires_at
        await assistance.authorize(token)
        newer = await assistance.mint(state["lease"].principal, "task_a", AssistanceRequest(interaction_id="pause_a", generation=3))
        assert newer.assistance_url != grant.assistance_url
        with pytest.raises(TaskFault):
            await assistance.authorize(token)
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_browser_exception_secrets_never_reach_response_or_server_logs(caplog):
    host, bindings, handle, state = scenario()
    await host.startup()
    handle.send_input.side_effect = RuntimeError("provider included private-otp-canary " + TOKEN)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.app), base_url="https://viewer.test") as client:
            response = await client.post(BASE + "/input", headers=ORIGIN, json={"kind": "type", "text": "private-otp-canary"})
            assert response.status_code == 503
            assert "private-otp-canary" not in response.text
            assert "private-otp-canary" not in caplog.text
            assert response.headers["Referrer-Policy"] == "no-referrer"
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_expiry_watcher_fences_held_writer_without_resuming():
    host, bindings, handle, state = scenario()
    await host.startup()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.app), base_url="https://viewer.test") as client:
            assert (await client.post(BASE + "/input", headers=ORIGIN, json={"kind": "keydown", "key": "Shift"})).status_code == 200
            state["revoked"] = True
            for _ in range(100):
                if handle.release.await_count:
                    break
                await asyncio.sleep(.01)
            handle.release.assert_awaited_once()
            bindings.control.respond.assert_not_awaited()
            bindings.control.cancel.assert_not_awaited()
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_shared_response_conflict_is_not_a_second_viewer_resolution():
    host, bindings, handle, state = scenario()
    await host.startup()
    bindings.control.respond.side_effect = TaskFault(task_error(ErrorCode.CONFLICT, "Decision already resolved"))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.app), base_url="https://viewer.test") as client:
            assert (await client.get(BASE + "/screen")).status_code == 200
            response = await client.post(BASE + "/respond", headers=ORIGIN,
                                         json={"generation": 3, "idempotency_key": "race-response-001", "kind": "browser", "browser_resolved": True})
            assert response.status_code == 409
            handle.release.assert_awaited_once()
            bindings.control.respond.assert_awaited_once()
            assert (await client.post(BASE + "/input", headers=ORIGIN, json={"kind": "release"})).status_code == 410
    finally:
        await host.shutdown()


@pytest.mark.parametrize("condition", ["revoked", "expired", "generation", "execution_generation", "running"])
def test_stream_binary_jpeg_and_expiry_while_already_connected(condition):
    host, bindings, handle, state = scenario()

    async def frames():
        yield b"\xff\xd8synthetic-jpeg"
        if condition == "revoked":
            state["revoked"] = True
        elif condition == "expired":
            state["binding"] = state["binding"].model_copy(update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
        elif condition == "generation":
            state["view"] = state["view"].model_copy(update={"interaction": state["view"].interaction.model_copy(update={"generation": 4})})
        elif condition == "execution_generation":
            state["lease"] = state["lease"].model_copy(update={"generation": 8})
        else:
            state["view"] = state["view"].model_copy(update={"state": TaskState.RUNNING, "interaction": None})
        await asyncio.sleep(10)

    handle.frames.side_effect = frames
    # TestClient's portal owns startup/shutdown and WebSocket event loop.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        await host.startup()
        yield
        await host.shutdown()

    host.app.router.lifespan_context = lifespan
    with TestClient(host.app, base_url="https://viewer.test") as client:
        with client.websocket_connect("wss://viewer.test" + BASE + "/stream", headers=ORIGIN) as socket:
            assert socket.receive_bytes() == b"\xff\xd8synthetic-jpeg"
            assert socket.receive()["type"] == "websocket.close"
    assert any(call.args[0].kind == "release" for call in handle.send_input.await_args_list)
