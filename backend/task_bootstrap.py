"""Fail-closed composition for optional, independently implemented task services."""

import asyncio
import importlib
import logging
from contextlib import suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.routing import Route
from starlette.exceptions import HTTPException as StarletteHTTPException

from .task_contracts import (
    API_PREFIX, ERROR_POLICY, ErrorCode, ErrorEnvelope, Readiness, TaskFault, task_error,
)
from .task_ports import TaskBindings
from .task_settings import TaskSettings

logger = logging.getLogger(__name__)
COMPONENTS = (
    ("backend.tasks.control", "create_control"),
    ("backend.tasks.executor", "create_executor"),
    ("backend.tasks.assistance", "create_assistance"),
    ("backend.observation", "create_observation"),
    ("backend.tasks.routes", "create_router"),
    ("backend.tasks.assistance", "create_router"),
)


def error_response(code: ErrorCode, message: str, retry_after: int | None = None):
    error = task_error(code, message, retry_after)
    headers = {"Cache-Control": "no-store"}
    if code == ErrorCode.UNAUTHENTICATED:
        headers["WWW-Authenticate"] = "Bearer"
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(ErrorEnvelope(error=error).model_dump(mode="json"), status_code=ERROR_POLICY[code][0], headers=headers)


class TaskBoundary:
    """Bound bodies before parsing, including chunked requests, without echoing them."""

    def __init__(self, app, host):
        self.app, self.host = app, host

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        readiness = self.host.readiness
        if not readiness.ready:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013 if readiness.enabled else 1008})
                return
            code = ErrorCode.UNAVAILABLE if readiness.enabled else ErrorCode.NOT_FOUND
            response = error_response(code, "Browser tasks are unavailable" if readiness.enabled else "Not found", 5 if readiness.enabled else None)
            return await response(scope, receive, send)
        if scope["type"] == "websocket":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        body = bytearray()
        try:
            length = int(headers.get(b"content-length", b"0"))
            if length < 0 or length > self.host.settings.max_body_bytes:
                raise ValueError
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > self.host.settings.max_body_bytes:
                    raise ValueError
                if not message.get("more_body", False):
                    break
        except ValueError:
            return await error_response(ErrorCode.INVALID_INPUT, "Request body exceeds protocol bounds")(scope, receive, send)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        async def no_store(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [(key, value) for key, value in message.get("headers", []) if key.lower() != b"cache-control"] + [(b"cache-control", b"no-store")]
            await send(message)

        await self.app(scope, replay, no_store)


class TaskHost:
    def __init__(self, settings: TaskSettings, bindings: TaskBindings | None = None, routers: tuple = ()):
        self.settings = settings
        self.bindings = bindings
        self.routers = routers
        self._started = []
        self._mounted = False
        self._startup_attempted = False
        self.readiness = Readiness(enabled=settings.enabled, ready=False, reason="starting" if settings.enabled else "disabled")
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
        self.app.add_middleware(TaskBoundary, host=self)

        @self.app.exception_handler(TaskFault)
        async def protocol_failure(request: Request, exc: TaskFault):
            return error_response(exc.error.code, exc.error.message, exc.error.retry_after_seconds)

        @self.app.exception_handler(RequestValidationError)
        async def invalid_request(request: Request, exc: RequestValidationError):
            return error_response(ErrorCode.INVALID_INPUT, "Request does not match the browser-task protocol")

        @self.app.exception_handler(StarletteHTTPException)
        async def http_failure(request: Request, exc: StarletteHTTPException):
            if exc.status_code == 401:
                return error_response(ErrorCode.UNAUTHENTICATED, "Authentication required")
            if exc.status_code in (403, 404):
                return error_response(ErrorCode.NOT_FOUND, "Not found")
            if exc.status_code == 405:
                return error_response(ErrorCode.METHOD_NOT_ALLOWED, "Method not allowed")
            return error_response(ErrorCode.INVALID_INPUT, "Request is not supported by the browser-task protocol")

        @self.app.exception_handler(Exception)
        async def unexpected_failure(request: Request, exc: Exception):
            logger.error("Browser-task operation failed; exception details withheld")
            return error_response(ErrorCode.UNAVAILABLE, "Browser-task operation is unavailable", 5)

    def _load(self):
        exports = []
        for module, symbol in COMPONENTS:
            try:
                loaded = importlib.import_module(module)
            except ModuleNotFoundError as exc:
                if exc.name == module:
                    return False
                raise
            factory = getattr(loaded, symbol, None)
            if not callable(factory):
                return False
            exports.append(factory)
        self.bindings = TaskBindings(*(factory(self.settings) for factory in exports[:4]))
        self.routers = tuple(factory(self.bindings) for factory in exports[4:])
        return True

    async def startup(self):
        if not self.settings.enabled or self._startup_attempted:
            return
        self._startup_attempted = True
        try:
            if self.bindings is None and not self._load():
                self.readiness = Readiness(enabled=True, ready=False, reason="components_missing")
                return
            if not self.routers:
                raise RuntimeError("Task route factories are required")
            services = (self.bindings.executor, self.bindings.assistance, self.bindings.control)
            for service in services:
                service.bind(self.bindings)
            if not self._mounted:
                for router in self.routers:
                    self.app.include_router(router)
                self._mounted = True
            for service in services:
                self._started.append(service)
                await service.startup()
            self.readiness = Readiness(enabled=True, ready=True, reason="ready")
        except Exception:
            logger.error("Browser task composition failed; admission remains closed")
            await self._stop_services()
            self.readiness = Readiness(enabled=True, ready=False, reason="startup_failed")

    async def _stop_services(self):
        services, self._started = list(reversed(self._started)), []

        async def stop():
            for service in services:
                with suppress(Exception):
                    await service.shutdown()

        with suppress(TimeoutError):
            await asyncio.wait_for(stop(), timeout=self.settings.shutdown_seconds)

    async def shutdown(self):
        self.readiness = Readiness(enabled=self.settings.enabled, ready=False, reason="shutdown" if self.settings.enabled else "disabled")
        await self._stop_services()


class TaskRoot:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope = dict(scope)
        scope["root_path"] = scope.get("root_path", "") + API_PREFIX
        scope["path"] = scope["path"].rstrip("/") + "/"
        await self.app(scope, receive, send)


def install_task_api(app: FastAPI, settings: TaskSettings | None = None, *, bindings: TaskBindings | None = None, routers: tuple = ()) -> TaskHost:
    """Register once before the frontend catch-all. No component I/O occurs here."""
    if hasattr(app.state, "browser_tasks"):
        raise ValueError("Browser-task API already installed")
    host = TaskHost(settings if settings is not None else TaskSettings.from_env(), bindings, routers)
    app.state.browser_tasks = host
    app.router.routes.append(Route(API_PREFIX, endpoint=TaskRoot(host.app), methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]))
    app.mount(API_PREFIX, host.app, name="browser-tasks")

    @app.get("/health/browser-tasks", response_model=Readiness)
    async def browser_task_readiness():
        status = 200 if host.readiness.ready or not host.settings.enabled else 503
        return JSONResponse(host.readiness.model_dump(mode="json"), status_code=status, headers={"Cache-Control": "no-store"})

    return host
