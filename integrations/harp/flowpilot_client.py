"""Reference FlowPilot browser-task client for Harp (httpx, async).

Shaped like harp/browser_use_infra.py: one class, typed errors, no retries that
could repeat website effects. Copy into harp/flowpilot_client.py and read the key
and base URL from Settings (flowpilot_api_key / flowpilot_base_url).
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

TERMINAL = {"completed", "failed", "interrupted", "cancelled"}
WAITING = {"waiting_for_input", "waiting_for_confirmation", "waiting_for_browser"}


@dataclass
class FlowPilotError(Exception):
    status: int
    code: str
    message: str
    retryable: bool = False
    retry_after: int | None = None


def admission_key(*parts: str) -> str:
    """Stable idempotency key: a retried turn re-attaches instead of starting twice."""
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:40]


class FlowPilotClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 40.0):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=timeout,
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, body: dict | None = None,
                       params: dict | None = None) -> Any:
        try:
            response = await self._http.request(method, path, json=body, params=params)
        except httpx.TimeoutException:
            raise FlowPilotError(504, "timeout", "FlowPilot did not answer in time", True) from None
        except httpx.TransportError:
            raise FlowPilotError(503, "unavailable", "FlowPilot is unreachable", True) from None
        if response.status_code >= 400:
            try:
                error = response.json().get("error") or {}
            except ValueError:
                error = {}
            retry_after = response.headers.get("Retry-After")
            raise FlowPilotError(response.status_code, error.get("code", "http_error"),
                                 error.get("message", response.reason_phrase),
                                 bool(error.get("retryable")), int(retry_after) if retry_after else None)
        return response.json() if response.content else None

    async def create_profile(self, label: str, key: str) -> dict:
        return await self._request("POST", "/api/v1/browser-tasks/profiles",
                                   {"label": label[:120], "idempotency_key": key})

    async def start(self, task: str, key: str, *, profile_id: str | None = None,
                    session_id: str | None = None, inputs: dict | None = None) -> dict:
        body: dict[str, Any] = {"task": task, "idempotency_key": key}
        if session_id:
            body["session_id"] = session_id
        elif profile_id:
            body["profile_id"] = profile_id
        if inputs:
            body["inputs"] = inputs
        return await self._request("POST", "/api/v1/browser-tasks", body)

    async def status(self, task_id: str, after_revision: int | None = None, wait: int = 0) -> dict:
        params = {"after_revision": after_revision, "wait_seconds": wait} if after_revision is not None and wait else None
        return await self._request("GET", f"/api/v1/browser-tasks/{task_id}", params=params)

    async def wait(self, task_id: str, revision: int, budget_seconds: float) -> dict:
        """Long-poll until terminal, waiting on a human, or the budget is spent."""
        import time
        deadline = time.monotonic() + budget_seconds
        view = await self.status(task_id)
        while view["state"] not in TERMINAL | WAITING and time.monotonic() < deadline:
            wait = int(max(1, min(25, deadline - time.monotonic())))
            try:
                view = await self.status(task_id, view["revision"], wait)
            except FlowPilotError as exc:
                if exc.status in (503, 504):
                    continue
                raise
        return view

    async def respond_input(self, task_id: str, interaction: dict, values: dict[str, str]) -> dict:
        return await self._respond(task_id, interaction, {"kind": "input", "values": values})

    async def respond_confirmation(self, task_id: str, interaction: dict, confirmed: bool) -> dict:
        return await self._respond(task_id, interaction, {"kind": "confirmation", "confirmed": confirmed is True})

    async def respond_browser_resolved(self, task_id: str, interaction: dict) -> dict:
        return await self._respond(task_id, interaction, {"kind": "browser", "browser_resolved": True})

    async def _respond(self, task_id: str, interaction: dict, body: dict) -> dict:
        body = {**body, "generation": interaction["generation"], "idempotency_key": uuid.uuid4().hex}
        return await self._request(
            "POST", f"/api/v1/browser-tasks/{task_id}/interactions/{interaction['interaction_id']}/respond", body)

    async def assistance_link(self, task_id: str, interaction: dict) -> dict:
        """Private, short-lived viewer link for waiting_for_browser. Send only to the owner, privately."""
        grant = await self._request("POST", f"/api/v1/browser-tasks/{task_id}/assistance",
                                    {"interaction_id": interaction["interaction_id"],
                                     "generation": interaction["generation"]})
        grant["assistance_url"] = str(self._http.base_url).rstrip("/") + grant["assistance_url"]
        return grant

    async def cancel(self, task_id: str) -> dict:
        return await self._request("POST", f"/api/v1/browser-tasks/{task_id}/cancel")
