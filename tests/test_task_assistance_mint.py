"""Public assistance routes with a strict, explicitly test-only account router.

001 owns real authentication and the mint HTTP route. This peer router exposes
the frozen path using fixed synthetic credentials, not a second implementation.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request

from backend.task_contracts import (
    API_PREFIX, AssistanceRequest, ErrorCode, Principal, TaskFault, TaskState, task_error,
)
from tests.test_task_assistance_security import scenario


AUTH = {"Authorization": "Bearer fp_live_lane003_fixture_a"}


@asynccontextmanager
async def public_client():
    host, bindings, handle, state = scenario()
    router = APIRouter()
    principals = {
        "Bearer fp_live_lane003_fixture_a": Principal(account_id="account_a"),
        "Bearer fp_live_lane003_fixture_b": Principal(account_id="account_b"),
    }

    @router.post("/{task_id}/assistance", status_code=201)
    async def peer_mint(task_id: str, body: AssistanceRequest, request: Request):
        principal = principals.get(request.headers.get("Authorization"))
        if principal is None:
            raise TaskFault(task_error(ErrorCode.UNAUTHENTICATED, "Authentication required"))
        return await bindings.assistance.mint(principal, task_id, body)

    host.routers += (router,)
    app = FastAPI()
    app.mount(API_PREFIX, host.app)
    await host.startup()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://viewer.test") as client:
            yield client, bindings, handle, state
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_mounted_public_mint_viewer_state_input_response_and_reuse():
    async with public_client() as (client, bindings, handle, state):
        mint = await client.post(API_PREFIX + "/task_a/assistance", headers=AUTH,
                                 json={"interaction_id": "pause_a", "generation": 3})
        assert mint.status_code == 201
        grant = mint.json()
        base = grant["assistance_url"]
        assert base.startswith(API_PREFIX + "/help/")
        assert "fp_live_" not in base
        token = base.rsplit("/", 1)[1]
        assert state["saved"][-1].lease.generation == 7
        assert token not in state["saved"][-1].model_dump_json()
        assert (await client.get(base)).status_code == 200
        response = await client.get(base + "/state")
        assert response.status_code == 200
        assert response.json()["interaction"]["generation"] == 3
        assert response.json()["url"] == "https://example.test/form"
        assert (await client.get(base + "/screen")).status_code == 200
        # The copied live link is deliberately a bearer credential, not an account
        # cookie: no Authorization header is needed on capability routes.
        origin = {"Origin": "https://viewer.test"}
        assert (await client.post(base + "/input", headers=origin,
                                  json={"kind": "type", "text": "synthetic-secret-canary"})).status_code == 200
        response = await client.post(base + "/respond", headers=origin, json={
            "generation": 3, "kind": "browser", "browser_resolved": True,
            "idempotency_key": "public-response-001",
        })
        assert response.status_code == 202
        assert response.json()["state"] == "running"
        assert "synthetic-secret-canary" not in response.text
        for suffix in ("", "/state", "/screen"):
            assert (await client.get(base + suffix)).status_code == 410
        assert (await client.post(base + "/cancel", headers=origin)).status_code == 410
        handle.release.assert_awaited_once()
        bindings.control.respond.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing_auth", "foreign", "foreign_with_identity_header", "missing_task", "wrong_interaction", "stale_generation", "expired", "not_paused"])
async def test_public_mint_rejects_unauthorized_and_noncurrent_requests(kind):
    async with public_client() as (client, bindings, handle, state):
        path = API_PREFIX + "/task_a/assistance"
        headers = dict(AUTH)
        body = {"interaction_id": "pause_a", "generation": 3}
        expected = 409
        if kind == "missing_auth":
            headers = {}
            expected = 401
        elif kind.startswith("foreign"):
            headers = {"Authorization": "Bearer fp_live_lane003_fixture_b"}
            if kind == "foreign_with_identity_header":
                headers.update({"X-Account-ID": "account_a", "X-User-ID": "account_a"})
            expected = 404
        elif kind == "missing_task":
            path = API_PREFIX + "/missing/assistance"
            expected = 404
        elif kind == "wrong_interaction":
            body["interaction_id"] = "old_pause"
        elif kind == "stale_generation":
            body["generation"] = 2
        elif kind == "expired":
            state["view"] = state["view"].model_copy(update={"interaction": state["view"].interaction.model_copy(
                update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})})
        else:
            state["view"] = state["view"].model_copy(update={"state": TaskState.RUNNING, "interaction": None})
        response = await client.post(path, headers=headers, json=body)
        assert response.status_code == expected
        assert "assistance_url" not in response.text
        assert state["saved"] == []
        bindings.control.save_capability.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["resolved", "cancelled", "new_pause", "new_execution", "expired"])
async def test_resolution_or_generation_change_during_mint_save_cannot_publish_link(race):
    async with public_client() as (client, bindings, handle, state):
        save = bindings.control.save_capability.side_effect

        async def racing_save(binding):
            # Models the other control-plane transaction winning between the
            # authorized lease lookup and atomic save_capability commit check.
            if race == "new_execution":
                state["lease"] = state["lease"].model_copy(update={"generation": 8})
            elif race == "new_pause":
                state["view"] = state["view"].model_copy(update={"interaction": state["view"].interaction.model_copy(update={"generation": 4})})
            elif race == "expired":
                state["view"] = state["view"].model_copy(update={"interaction": state["view"].interaction.model_copy(
                    update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})})
            else:
                state["view"] = state["view"].model_copy(update={"interaction": None,
                    "state": TaskState.RUNNING if race == "resolved" else TaskState.CANCELLING})
            await save(binding)

        bindings.control.save_capability.side_effect = racing_save
        response = await client.post(API_PREFIX + "/task_a/assistance", headers=AUTH,
                                     json={"interaction_id": "pause_a", "generation": 3})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "stale_generation"
        assert "assistance_url" not in response.text
        assert state["saved"] == []


@pytest.mark.asyncio
async def test_public_remint_rotates_live_link_and_drains_old_writer():
    async with public_client() as (client, bindings, handle, state):
        async def mint():
            response = await client.post(API_PREFIX + "/task_a/assistance", headers=AUTH,
                                         json={"interaction_id": "pause_a", "generation": 3})
            assert response.status_code == 201
            return response.json()["assistance_url"]

        old = await mint()
        assert (await client.get(old + "/screen")).status_code == 200
        newer = await mint()
        assert newer != old
        handle.release.assert_awaited_once()
        assert (await client.get(old + "/state")).status_code == 410
        # Inspect the current link without asking this double to supply another
        # writer after release; executor ownership of acquisition is a peer seam.
        assert (await client.get(newer)).status_code == 200
        assert state["saved"][-1].generation == 3
