import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from pydantic import ValidationError

from backend import task_contracts as wire
from backend.task_bootstrap import COMPONENTS, TaskHost, install_task_api
from backend.task_ports import AssistanceBinding, Execution, Lease
from backend.task_ports import Observation
from backend.task_settings import TaskSettings
from tests.fixtures.browser_tasks.doubles import (
    bindings_double, browser_double, channel_double, observation_fixture, repository_double,
)

FIXTURES = Path(__file__).parent / "fixtures" / "browser_tasks"
EXCHANGES = json.loads((FIXTURES / "exchanges.json").read_text())["cases"]


@pytest.mark.parametrize("case", EXCHANGES, ids=lambda case: case["name"])
def test_golden_exchanges_validate(case):
    for direction in ("request", "response"):
        model_name = case.get(direction + "_model")
        if model_name:
            model = getattr(wire, model_name)
            parsed = model.model_validate(case[direction])
            assert model.model_validate_json(parsed.model_dump_json()) == parsed
    if case.get("response_model") == "ErrorEnvelope":
        assert wire.ERROR_POLICY[wire.ErrorCode(case["response"]["error"]["code"])][0] == case["status"]


def test_duplicate_admission_fixture_is_identical():
    first, duplicate = EXCHANGES[:2]
    assert first["request"] == duplicate["request"]
    assert first["response"] == duplicate["response"]


def test_fingerprint_binds_behavior_not_key_or_mapping_order():
    original = wire.TaskStart(task="Inspect", inputs={"a": 1, "b": 2}, idempotency_key="original-key")
    reordered = wire.TaskStart(task="Inspect", inputs={"b": 2, "a": 1}, idempotency_key="different-key")
    assert original.fingerprint() == reordered.fingerprint()
    for update in ({"task": "Other"}, {"profile_id": "other"}, {"session_id": "other"}, {"inputs": {"a": 2}}):
        assert original.fingerprint() != original.model_copy(update=update).fingerprint()


@pytest.mark.parametrize("payload", [
    {"task": " "}, {"task": "a" * 16_001}, {"inputs": {"a": "x" * 32_769}},
    {"inputs": {str(i): i for i in range(65)}}, {"profile_id": "../other"},
    {"account_id": "other"}, {"inputs": {"x": float("nan")}},
])
def test_start_rejects_unbounded_or_spoofed_fields(payload):
    with pytest.raises(ValidationError):
        wire.TaskStart.model_validate({"task": "Inspect", "idempotency_key": "sample-key", **payload})


def test_transition_matrix_fences_terminal_and_cancellation():
    assert set(wire.TRANSITIONS) == set(wire.TaskState)
    for before in wire.TaskState:
        for after in wire.TaskState:
            if after in wire.TRANSITIONS[before]:
                wire.require_transition(before, after)
            else:
                with pytest.raises(ValueError):
                    wire.require_transition(before, after)
    assert not any(wire.TRANSITIONS[state] for state in wire.TERMINAL_STATES)
    assert wire.TaskState.COMPLETED not in wire.TRANSITIONS[wire.TaskState.CANCELLING]


def test_interaction_decline_is_explicit_and_generation_bound():
    interaction_data = next(c for c in EXCHANGES if c["name"] == "confirmation")["response"]["interaction"]
    interaction = wire.Interaction.model_validate(interaction_data)
    response = wire.InteractionResponse(generation=2, idempotency_key="decline-key", kind="confirmation", confirmed=False)
    response.validate_for(interaction, datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert response.confirmed is False
    for bad in ({"generation": 3}, {"kind": "browser", "confirmed": None, "browser_resolved": True}):
        with pytest.raises(wire.TaskFault):
            response.model_copy(update=bad).validate_for(interaction, datetime(2030, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(wire.TaskFault):
        response.validate_for(interaction, datetime(2030, 1, 2, tzinfo=timezone.utc))
    with pytest.raises(ValidationError):
        wire.InteractionResponse(generation=2, idempotency_key="decline-key", kind="confirmation", confirmed="yes")


def test_secret_values_are_not_in_safe_representations_or_status_schema():
    secret = "synthetic-secret-canary"
    response = wire.InteractionResponse(generation=1, idempotency_key="response-key", kind="input", values={"otp": secret})
    assert secret not in repr(response)
    assert "values" not in wire.TaskView.model_json_schema()["properties"]
    assert "assistance_url" not in wire.TaskView.model_json_schema()["properties"]
    with pytest.raises(ValidationError) as error:
        wire.BrowserInput(kind="type", text=secret, account_id="foreign")
    assert secret not in str(error.value)


def test_response_receipts_detect_changed_values_without_plaintext_hashes():
    response = wire.InteractionResponse(generation=1, idempotency_key="receipt-key", kind="input", values={"otp": "123456"})
    principal = wire.Principal(account_id="account_demo")
    key = b"synthetic-server-key-not-a-secret" * 2
    digest = response.receipt_digest(key, principal, "task_demo", "interaction_demo")
    assert len(digest) == 64
    assert digest == response.model_copy(update={"idempotency_key": "different-key"}).receipt_digest(key, principal, "task_demo", "interaction_demo")
    assert digest != response.model_copy(update={"values": {"otp": "654321"}}).receipt_digest(key, principal, "task_demo", "interaction_demo")
    assert digest != response.receipt_digest(key, wire.Principal(account_id="account_other"), "task_demo", "interaction_demo")
    assert digest != response.receipt_digest(b"different-synthetic-key" * 3, principal, "task_demo", "interaction_demo")
    with pytest.raises(ValueError):
        response.receipt_digest(b"short", principal, "task_demo", "interaction_demo")


def test_observation_references_cannot_cross_snapshots():
    snapshot = observation_fixture().model_dump()
    with pytest.raises(ValidationError):
        Observation.model_validate({**snapshot, "references": [{"reference": "element_demo", "snapshot_id": "different_snapshot", "fingerprint": "element_hash"}]})
    assert Observation.model_validate({**snapshot, "references": [{"reference": "element_demo", "snapshot_id": snapshot["snapshot_id"], "fingerprint": "element_hash"}]}).references[0].reference == "element_demo"


def test_viewer_transport_fixtures_are_bounded():
    viewer = json.loads((FIXTURES / "viewer_commands.json").read_text())
    for command in viewer["commands"]:
        wire.BrowserInput.model_validate(command)
    assert wire.InteractionResponse.model_validate(viewer["resume"]).browser_resolved is True
    wire.AssistanceState.model_validate(viewer["state"])
    wire.Acknowledgement.model_validate(viewer["transport"]["input_acknowledgement"])


@pytest.mark.parametrize("command", [
    {"kind": "evaluate", "text": "anything"}, {"kind": "type"},
    {"kind": "move", "x": -1, "y": 0}, {"kind": "release", "text": "oops"},
    {"kind": "scroll", "dx": float("inf"), "dy": 0},
])
def test_browser_commands_are_narrow_and_bounded(command):
    with pytest.raises(ValidationError):
        wire.BrowserInput.model_validate(command)


def test_polling_error_policy_and_profile_paths():
    with pytest.raises(ValidationError):
        wire.PollRequest(wait_seconds=1)
    with pytest.raises(ValidationError):
        wire.PollRequest(after_revision=1, wait_seconds=26)
    for code, (_, retryable) in wire.ERROR_POLICY.items():
        assert wire.task_error(code, "Safe message").retryable is retryable
        with pytest.raises(ValidationError):
            wire.TaskError(code=code, message="Safe message", retryable=not retryable)
    endpoints = {(e.method, e.path) for e in wire.ENDPOINTS}
    assert ("POST", wire.API_PREFIX) in endpoints
    assert ("DELETE", wire.PROFILE_PREFIX + "/{profile_id}") in endpoints
    assert len(endpoints) == len(wire.ENDPOINTS)


def test_settings_are_typed_bounded_and_disabled_by_default():
    assert TaskSettings.from_env({}).enabled is False
    assert TaskSettings.from_env({"PROFILES_DIR": "/synthetic/profiles"}).state_dir == Path("/synthetic/profiles/browser-tasks")
    assert TaskSettings.from_env({"FLOWPILOT_TASKS_ENABLED": "true", "FLOWPILOT_TASKS_MAX_ACTIVE_BROWSERS": "4"}).max_active_browsers == 4
    for overrides in ({"max_active_browsers": 0}, {"max_account_browsers": 4}, {"shutdown_seconds": 1}, {"assistance_ttl_seconds": 601}, {"readiness_window_seconds": 20}):
        with pytest.raises(ValidationError):
            TaskSettings(**overrides)


def test_policy_fixtures_pin_isolation_and_no_crash_replay():
    policy = json.loads((FIXTURES / "policy.json").read_text())
    assert policy["protocol_version"] == wire.PROTOCOL_VERSION
    assert policy["restart"]["automatic_website_replay"] is False
    assert policy["authentication"]["identity_headers_trusted"] is False
    assert policy["capacity"]["paused_browser_counts"] is True


async def test_port_doubles_are_independent_and_strict():
    bindings = bindings_double()
    for service in (bindings.control, bindings.executor, bindings.assistance):
        service.bind(bindings)
        await service.startup()
        await service.shutdown()
    with pytest.raises(NotImplementedError):
        await repository_double().get(wire.Principal(account_id="account_demo"), "task_demo")
    with pytest.raises(TypeError):
        await bindings.executor.execute()
    with pytest.raises(NotImplementedError):
        await browser_double().screenshot()
    await channel_double().start()
    assert observation_fixture().readiness == "ready"


def stub_router(bindings):
    router = APIRouter()

    @router.post("/", response_model=wire.TaskAdmission, status_code=202)
    async def admit(request: wire.TaskStart):
        return await bindings.control.start(wire.Principal(account_id="account_demo"), request)

    return router


async def request_app(app, method, path, **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


async def test_disabled_surface_is_not_legacy_flow_or_frontend():
    app = FastAPI()
    host = install_task_api(app, TaskSettings())
    await host.startup()
    for path in (wire.API_PREFIX, wire.PROFILE_PREFIX, wire.API_PREFIX + "/task_demo"):
        response = await request_app(app, "POST", path)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    response = await request_app(app, "GET", "/health/browser-tasks")
    assert response.json() == {"enabled": False, "ready": False, "reason": "disabled", "protocol_version": "1"}
    with pytest.raises(ValueError):
        install_task_api(app)


async def test_enabled_missing_components_fail_readiness_and_admission():
    app = FastAPI()
    host = install_task_api(app, TaskSettings(enabled=True))
    await host.startup()
    assert host.readiness.reason == "components_missing"
    response = await request_app(app, "POST", wire.API_PREFIX, json=EXCHANGES[0]["request"])
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert (await request_app(app, "GET", "/health/browser-tasks")).status_code == 503


async def test_composition_wires_reserved_factories_once(monkeypatch):
    bindings = bindings_double()
    modules = {}
    services = (bindings.control, bindings.executor, bindings.assistance, bindings.observation)
    for (module, symbol), service in zip(COMPONENTS[:4], services):
        modules.setdefault(module, SimpleNamespace()).__dict__[symbol] = lambda settings, service=service: service
    modules["backend.tasks.routes"] = SimpleNamespace(create_router=stub_router)
    modules["backend.tasks.assistance"].create_router = lambda bindings: APIRouter()
    real_import = importlib.import_module
    monkeypatch.setattr("backend.task_bootstrap.importlib.import_module", lambda module: modules[module] if module in modules else real_import(module))
    app = FastAPI()
    host = install_task_api(app, TaskSettings(enabled=True))
    bindings.control.start.side_effect = None
    bindings.control.start.return_value = wire.TaskAdmission.model_validate(EXCHANGES[0]["response"])
    await host.startup()
    await host.startup()
    assert host.readiness.ready
    response = await request_app(app, "POST", wire.API_PREFIX, json=EXCHANGES[0]["request"])
    assert response.status_code == 202
    assert response.json() == {**EXCHANGES[0]["response"]}
    assert response.headers["Cache-Control"] == "no-store"
    for service in services[:3]:
        service.bind.assert_called_once()
        service.startup.assert_awaited_once()
    await host.shutdown()
    for service in services[:3]:
        service.shutdown.assert_awaited_once()
    assert (await request_app(app, "POST", wire.API_PREFIX)).status_code == 503


async def test_validation_and_chunked_body_errors_never_echo_inputs():
    app = FastAPI()
    bindings = bindings_double()
    host = install_task_api(app, TaskSettings(enabled=True, max_body_bytes=1_024), bindings=bindings, routers=(stub_router(bindings),))
    await host.startup()
    response = await request_app(app, "POST", wire.API_PREFIX, json={"task": "canary", "idempotency_key": "canary"})
    assert response.status_code == 422
    assert "canary" not in response.text

    async def chunks():
        for _ in range(3):
            yield b"x" * 600

    response = await request_app(app, "POST", wire.API_PREFIX, content=chunks())
    assert response.status_code == 422
    bindings.control.start.assert_not_awaited()
    await host.shutdown()


async def test_startup_failure_rolls_back_and_stays_closed():
    bindings = bindings_double()
    bindings.assistance.startup.side_effect = RuntimeError("synthetic-secret-canary")
    host = TaskHost(TaskSettings(enabled=True), bindings, (stub_router(bindings),))
    await host.startup()
    assert host.readiness.reason == "startup_failed"
    assert not host.readiness.ready
    bindings.control.startup.assert_not_awaited()
    bindings.executor.shutdown.assert_awaited_once()
    bindings.assistance.shutdown.assert_awaited_once()


async def test_task_errors_are_sanitized_without_changing_legacy_errors():
    bindings = bindings_double()
    router = APIRouter()

    @router.get("/bad")
    async def bad():
        raise wire.TaskFault(wire.task_error(wire.ErrorCode.CAPACITY_EXHAUSTED, "Capacity reserved", 5))

    @router.get("/broken")
    async def broken():
        raise RuntimeError("synthetic-sensitive-exception")

    app = FastAPI()
    host = install_task_api(app, TaskSettings(enabled=True), bindings=bindings, routers=(router,))
    await host.startup()
    response = await request_app(app, "GET", wire.API_PREFIX + "/bad")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        response = await client.get(wire.API_PREFIX + "/broken")
    assert response.status_code == 503
    assert "synthetic-sensitive" not in response.text
    wire.ErrorEnvelope.model_validate(response.json())
    response = await request_app(app, "GET", wire.API_PREFIX + "/missing")
    assert response.json()["error"]["code"] == "not_found"
    response = await request_app(app, "GET", "/legacy-missing")
    assert response.json() == {"detail": "Not Found"}
    await host.shutdown()


async def test_disabled_websocket_and_default_app_registration(client):
    app = FastAPI()
    host = install_task_api(app, TaskSettings())
    messages = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        messages.append(message)

    await host.app({"type": "websocket", "path": "/help/synthetic", "headers": [], "scheme": "ws", "query_string": b"", "server": ("test", 80), "client": ("test", 1), "root_path": ""}, receive, send)
    assert messages == [{"type": "websocket.close", "code": 1008}]
    from backend.main import app as existing_app
    assert existing_app.state.browser_tasks.readiness.enabled is False
    assert sum(getattr(route, "name", None) == "browser-tasks" for route in existing_app.routes) == 1


async def test_pi_injected_observation_is_consumed_without_separate_dump(monkeypatch):
    from backend import pi_browser, runner

    bindings = bindings_double()
    snapshot = observation_fixture()
    for method, result in (("observe", snapshot), ("require_fresh", None), ("after_action", None)):
        getattr(bindings.observation, method).side_effect = None
        getattr(bindings.observation, method).return_value = result
    browser = SimpleNamespace(get_current_page_url=AsyncMock(return_value=snapshot.url), agent_focus_target_id=snapshot.target_id)
    execute = AsyncMock(return_value=(True, "ok"))
    monkeypatch.setattr(runner, "_execute_reflex", execute)

    async def agent(task, system, definitions, dispatch, **kwargs):
        with pytest.raises(RuntimeError):
            await dispatch("execute", {"script": "async()=>true"})
        result = await dispatch("observe", {})
        assert result["snapshot_id"] == snapshot.snapshot_id
        await dispatch("execute", {"script": "async()=>true"})
        await dispatch("complete", {"verify": "async()=>true", "summary": "Verified"})

    monkeypatch.setattr(pi_browser, "run_pi", agent)
    result = await pi_browser.drive_browser(browser, "Inspect", {}, "", SimpleNamespace(checkpoint=AsyncMock()), None, None, None, observation_policy=bindings.observation)
    assert result == "Verified"
    bindings.observation.require_fresh.assert_awaited_once_with(browser, snapshot)
    assert all(call.kwargs["observation_policy"] is bindings.observation for call in execute.await_args_list)


def test_execution_and_capabilities_are_lease_generation_bound():
    lease = Lease(principal=wire.Principal(account_id="account_demo"), task_id="task_demo", session_id="session_demo", profile_id="profile_demo", generation=1)
    execution = Execution(lease=lease, revision=2, request=wire.TaskStart.model_validate(EXCHANGES[0]["request"]), admitted_at="2030-01-01T00:00:00Z", deadline="2030-01-01T00:15:00Z")
    binding = AssistanceBinding(lease=lease, interaction_id="interaction_demo", generation=1, digest="a" * 64, expires_at="2030-01-01T00:05:00Z")
    assert execution.lease == binding.lease
    assert "token" not in binding.model_dump()


async def test_local_repl_keeps_defaults_and_accepts_transport_injection(tmp_path):
    from backend.repl import BrowserSession

    default = BrowserSession("local_demo", state_dir=tmp_path)
    assert default.handoff_factory is None
    assert default.observation_policy is None
    default.store.close()
    factory = lambda *args: channel_double()
    injected = BrowserSession("local_injected", state_dir=tmp_path, handoff_factory=factory, observation_policy=bindings_double().observation)
    assert injected.handoff_factory is factory
    injected.store.close()
