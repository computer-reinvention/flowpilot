"""Capability-scoped assistance mounted in the task API, never a public CDP proxy.

The frozen v1 ports currently omit lease lookup for minting and browser metadata
for state. Those operations fail closed rather than guessing execution generations
or fabricating URLs. See plan/003_remote_human_assistance.md for amendment needs.
"""

import asyncio
import hashlib
import hmac
import re
import secrets
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import ValidationError

from ..task_contracts import (
    AssistanceGrant, AssistanceRequest, BrowserInput, ERROR_POLICY, ErrorCode, ErrorEnvelope, InteractionResponse,
    MAX_INPUT_BYTES, PollRequest, TaskFault, TaskState, task_error,
)
from ..task_ports import AssistanceBinding
from .assistance_page import render_page, security_headers


def now():
    return datetime.now(timezone.utc)


def fault(code=ErrorCode.ASSISTANCE_EXPIRED):
    return TaskFault(task_error(code, "Assistance is unavailable"))


def digest_token(token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise fault()
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def check_origin(connection, *, required):
    """Trust ASGI scheme/Host, not client-supplied Forwarded/X-Forwarded headers.

    A trusted TLS proxy must normalize these at the server boundary. This module
    cannot select trusted proxy addresses; that belongs to deployment settings.
    """
    hosts = connection.headers.getlist("host")
    origins = connection.headers.getlist("origin")
    if len(hosts) != 1 or len(origins) > 1:
        raise fault(ErrorCode.NOT_FOUND)
    host = hosts[0]
    if not host or not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", host):
        raise fault(ErrorCode.NOT_FOUND)
    scheme = {"ws": "http", "wss": "https"}.get(connection.scope["scheme"], connection.scope["scheme"])

    def normalize(value):
        try:
            parsed = urlsplit(value)
            if (parsed.scheme not in {"https", "http"} or not parsed.hostname or
                    parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
                raise ValueError
            return (parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80))
        except ValueError:
            raise fault(ErrorCode.NOT_FOUND) from None

    expected = normalize(f"{scheme}://{host}")
    if not origins:
        if required:
            raise fault(ErrorCode.NOT_FOUND)
    elif normalize(origins[0]) != expected:
        raise fault(ErrorCode.NOT_FOUND)
    # Fetch Metadata also protects GETs requested by a foreign website.
    if connection.headers.get("sec-fetch-site") == "cross-site":
        raise fault(ErrorCode.NOT_FOUND)


class Assistance:
    def __init__(self, settings):
        self.settings = settings
        self.bindings = None
        self._active = False
        self._lock = asyncio.Lock()
        self._handles = {}
        self._revoked = set()
        self._watcher = None

    def bind(self, bindings):
        self.bindings = bindings

    async def startup(self):
        self._active = True
        self._watcher = asyncio.create_task(self._watch())

    async def shutdown(self):
        self._active = False
        if self._watcher:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
        async with self._lock:
            for binding, handle in tuple(self._handles.values()):
                self._revoked.add(binding.digest)
                with suppress(Exception):
                    await handle.release()
            self._handles.clear()

    async def mint(self, principal, task_id, request: AssistanceRequest) -> AssistanceGrant:
        async with self._lock:
            view = await self.bindings.control.get(principal, task_id, PollRequest())
            interaction = view.interaction
            if (view.state != TaskState.WAITING_FOR_BROWSER or not interaction or
                    interaction.interaction_id != request.interaction_id or
                    interaction.generation != request.generation or interaction.expires_at <= now()):
                raise fault(ErrorCode.STALE_GENERATION)
            # TaskView has no lease generation. Never substitute pause generation:
            # it would bypass the executor's execution fence after a restart.
            raise TaskFault(task_error(ErrorCode.UNAVAILABLE, "Assistance minting requires execution lease lookup"))

    async def _issue(self, lease, interaction):
        """Mint once a future foundation amendment supplies the verified lease.

        Internal only: never infer the lease from a browser-supplied identifier.
        Caller holds _lock and has checked the current account-owned pause.
        """
        expiry = min(interaction.expires_at, now() + timedelta(seconds=self.settings.assistance_ttl_seconds))
        if expiry <= now():
            raise fault(ErrorCode.STALE_GENERATION)
        token = secrets.token_urlsafe(32)
        binding = AssistanceBinding(lease=lease, interaction_id=interaction.interaction_id,
                                    generation=interaction.generation, digest=digest_token(token), expires_at=expiry)
        for old, _ in tuple(self._handles.values()):
            if old.lease == lease:
                await self._retire(old)
        await self.bindings.control.revoke_capabilities(lease)
        await self.bindings.control.save_capability(binding)
        return AssistanceGrant(assistance_url=f"/api/v1/browser-tasks/help/{token}", expires_at=expiry,
                               interaction_id=binding.interaction_id, generation=binding.generation)

    async def _validate(self, digest):
        if not self._active or digest in self._revoked:
            raise fault()
        binding = await self.bindings.control.capability(digest, now())
        if not hmac.compare_digest(binding.digest, digest) or binding.expires_at <= now():
            raise fault()
        view = await self.bindings.control.get(binding.lease.principal, binding.lease.task_id, PollRequest())
        interaction = view.interaction
        if (view.state != TaskState.WAITING_FOR_BROWSER or not interaction or
                view.task_id != binding.lease.task_id or view.session_id != binding.lease.session_id or view.profile_id != binding.lease.profile_id or
                interaction.interaction_id != binding.interaction_id or interaction.generation != binding.generation or
                interaction.expires_at <= now()):
            raise fault()
        return binding

    async def authorize(self, token) -> AssistanceBinding:
        return await self._validate(digest_token(token))

    async def browser(self, binding):
        current = await self._validate(binding.digest)
        if current != binding:
            raise fault()
        if binding.digest not in self._handles:
            handle = await self.bindings.executor.browser(binding.lease, binding.interaction_id, binding.generation)
            self._handles[binding.digest] = (binding, handle)
        return self._handles[binding.digest][1]

    async def _retire(self, binding):
        # Fence locally before I/O; failed revocation must never reopen this writer.
        self._revoked.add(binding.digest)
        entries = [(other, handle) for other, handle in self._handles.values() if other.lease == binding.lease]
        self._revoked.update(other.digest for other, _ in entries)
        for other, handle in entries:
            await handle.release()
            self._handles.pop(other.digest, None)
        await self.bindings.control.revoke_capabilities(binding.lease)

    async def _watch(self):
        while True:
            await asyncio.sleep(.25)
            async with self._lock:
                for binding, handle in tuple(self._handles.values()):
                    try:
                        await self._validate(binding.digest)
                    except Exception:
                        self._revoked.add(binding.digest)
                        # Keep failed releases for retry; never silently resume.
                        try:
                            await handle.release()
                        except Exception:
                            continue
                        self._handles.pop(binding.digest, None)


async def read_contract(request, schema):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_INPUT_BYTES:
            raise fault(ErrorCode.INVALID_INPUT)
    try:
        return schema.model_validate_json(body)
    except (ValidationError, ValueError):
        raise fault(ErrorCode.INVALID_INPUT) from None


def create_assistance(settings):
    return Assistance(settings)


class AssistanceRoute(APIRoute):
    """Prevent ASGI servers from logging raw browser exceptions and secret input."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def sanitized(request):
            try:
                return await handler(request)
            except TaskFault as exc:
                error = exc.error
            except Exception:
                error = task_error(ErrorCode.UNAVAILABLE, "Assistance is unavailable")
            headers = security_headers()
            if error.retry_after_seconds:
                headers["Retry-After"] = str(error.retry_after_seconds)
            return JSONResponse(ErrorEnvelope(error=error).model_dump(mode="json"),
                                status_code=ERROR_POLICY[error.code][0], headers=headers)
        return sanitized


def create_router(bindings):
    router = APIRouter(route_class=AssistanceRoute)
    assistance = bindings.assistance

    @router.get("/help/{token}")
    async def viewer(token: str, request: Request):
        check_origin(request, required=False)
        await assistance.authorize(token)
        page, headers = render_page()
        return HTMLResponse(page, headers=headers)

    @router.get("/help/{token}/state")
    async def state(token: str, request: Request):
        check_origin(request, required=False)
        await assistance.authorize(token)
        raise TaskFault(task_error(ErrorCode.UNAVAILABLE, "Browser viewport metadata is unavailable"))

    @router.get("/help/{token}/screen")
    async def screen(token: str, request: Request):
        check_origin(request, required=False)
        async with assistance._lock:
            binding = await assistance.authorize(token)
            handle = await assistance.browser(binding)
            image = await handle.screenshot()
            await assistance.authorize(token)
            return Response(image, media_type="image/png", headers=security_headers())

    @router.post("/help/{token}/input")
    async def input_command(token: str, request: Request):
        check_origin(request, required=True)
        command = await read_contract(request, BrowserInput)
        async with assistance._lock:
            binding = await assistance.authorize(token)
            handle = await assistance.browser(binding)
            try:
                await handle.send_input(command)
            except ValueError:
                raise fault(ErrorCode.INVALID_INPUT) from None
            return JSONResponse({"accepted": True}, headers=security_headers())

    @router.post("/help/{token}/respond", status_code=202)
    async def respond(token: str, request: Request):
        check_origin(request, required=True)
        response = await read_contract(request, InteractionResponse)
        async with assistance._lock:
            binding = await assistance.authorize(token)
            view = await bindings.control.get(binding.lease.principal, binding.lease.task_id, PollRequest())
            response.validate_for(view.interaction, now())
            await assistance._retire(binding)
            # The shared control service alone settles API/viewer response races.
            result = await bindings.control.respond(binding.lease.principal, binding.lease.task_id, binding.interaction_id, response)
            return JSONResponse(result.model_dump(mode="json"), status_code=202, headers=security_headers())

    @router.post("/help/{token}/cancel", status_code=202)
    async def cancel(token: str, request: Request):
        check_origin(request, required=True)
        async with assistance._lock:
            binding = await assistance.authorize(token)
            await assistance._retire(binding)
            result = await bindings.control.cancel(binding.lease.principal, binding.lease.task_id)
            return JSONResponse(result.model_dump(mode="json"), status_code=202, headers=security_headers())

    @router.websocket("/help/{token}/stream")
    async def stream(socket: WebSocket, token: str):
        frames = next_frame = disconnected = None
        binding = handle = None
        try:
            check_origin(socket, required=True)
            async with assistance._lock:
                binding = await assistance.authorize(token)
                handle = await assistance.browser(binding)
            await socket.accept(headers=[(key.encode(), value.encode()) for key, value in security_headers().items()])
            # No incoming WS commands: input has a bounded, Origin-checked HTTP path.
            disconnected = asyncio.create_task(socket.receive())
            frames = handle.frames()
            next_frame = asyncio.create_task(anext(frames))
            while True:
                done, _ = await asyncio.wait([next_frame, disconnected], timeout=.25, return_when=asyncio.FIRST_COMPLETED)
                await assistance.authorize(token)
                if disconnected in done:
                    break
                if next_frame in done:
                    image = next_frame.result()
                    await socket.send_bytes(image)
                    next_frame = asyncio.create_task(anext(frames))
        except (TaskFault, WebSocketDisconnect, StopAsyncIteration):
            pass
        except Exception:
            # Browser/provider errors must not disclose text, URLs or capabilities.
            pass
        finally:
            for task in (next_frame, disconnected):
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            if frames:
                with suppress(Exception):
                    await frames.aclose()
            if handle:
                async with assistance._lock:
                    with suppress(Exception):
                        await handle.send_input(BrowserInput(kind="release"))
            with suppress(RuntimeError, WebSocketDisconnect):
                await socket.close(code=1008)
    return router
