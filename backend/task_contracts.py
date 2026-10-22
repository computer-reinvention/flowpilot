"""Version 1 browser-task wire and state contracts, independent of execution."""

import hashlib
import hmac
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, JsonValue,
    StrictBool, StringConstraints, field_validator, model_validator,
)

PROTOCOL_VERSION = "1"
API_PREFIX = "/api/v1/browser-tasks"
PROFILE_PREFIX = f"{API_PREFIX}/profiles"
ASSISTANCE_PREFIX = f"{API_PREFIX}/help"
MAX_BODY_BYTES = 131_072
MAX_INPUT_BYTES = 32_768
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
IdempotencyKey = Annotated[str, StringConstraints(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=2_000)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class Principal(Contract):
    """Server-resolved account identity; API keys never select a different owner."""

    account_id: Identifier


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    WAITING_FOR_BROWSER = "waiting_for_browser"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_STATES = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED, TaskState.INTERRUPTED})
WAITING_STATES = frozenset({TaskState.WAITING_FOR_INPUT, TaskState.WAITING_FOR_CONFIRMATION, TaskState.WAITING_FOR_BROWSER})
TRANSITIONS = {
    TaskState.QUEUED: frozenset({TaskState.RUNNING, TaskState.CANCELLING, TaskState.FAILED, TaskState.INTERRUPTED}),
    TaskState.RUNNING: WAITING_STATES | {TaskState.CANCELLING, TaskState.COMPLETED, TaskState.FAILED, TaskState.INTERRUPTED},
    **{state: frozenset({TaskState.RUNNING, TaskState.CANCELLING, TaskState.FAILED, TaskState.INTERRUPTED}) for state in WAITING_STATES},
    TaskState.CANCELLING: frozenset({TaskState.CANCELLED, TaskState.INTERRUPTED}),
    **{state: frozenset() for state in TERMINAL_STATES},
}


def require_transition(before: TaskState, after: TaskState) -> None:
    if after not in TRANSITIONS[before]:
        raise ValueError(f"Invalid task transition: {before} -> {after}")


class ErrorCode(StrEnum):
    UNAUTHENTICATED = "unauthenticated"
    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    CONFLICT = "conflict"
    STALE_GENERATION = "stale_generation"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    SHUTDOWN = "shutdown"
    UNAVAILABLE = "unavailable"
    INTERRUPTED = "interrupted"
    PROFILE_RECOVERY_REQUIRED = "profile_recovery_required"
    PROVIDER_FAILURE = "provider_failure"
    ASSISTANCE_EXPIRED = "assistance_expired"
    READINESS_TIMEOUT = "readiness_timeout"
    NO_PROGRESS = "no_progress"


ERROR_POLICY = {
    ErrorCode.UNAUTHENTICATED: (401, False),
    ErrorCode.NOT_FOUND: (404, False),
    ErrorCode.INVALID_INPUT: (422, False),
    ErrorCode.METHOD_NOT_ALLOWED: (405, False),
    ErrorCode.CONFLICT: (409, False),
    ErrorCode.STALE_GENERATION: (409, False),
    ErrorCode.CAPACITY_EXHAUSTED: (429, True),
    ErrorCode.SHUTDOWN: (503, True),
    ErrorCode.UNAVAILABLE: (503, True),
    ErrorCode.INTERRUPTED: (409, False),
    ErrorCode.PROFILE_RECOVERY_REQUIRED: (409, False),
    ErrorCode.PROVIDER_FAILURE: (502, False),
    ErrorCode.ASSISTANCE_EXPIRED: (410, False),
    ErrorCode.READINESS_TIMEOUT: (409, False),
    ErrorCode.NO_PROGRESS: (409, False),
}


class TaskError(Contract):
    """Retryable means retry the same HTTP operation/key, never replay site effects."""

    code: ErrorCode
    message: ShortText
    retryable: bool
    retry_after_seconds: int | None = Field(default=None, ge=1, le=300)

    @model_validator(mode="after")
    def policy(self):
        if self.retryable != ERROR_POLICY[self.code][1]:
            raise ValueError("Error retryability does not match protocol policy")
        if self.retry_after_seconds is not None and not self.retryable:
            raise ValueError("Non-retryable errors cannot advertise retry-after")
        return self


class ErrorEnvelope(Contract):
    error: TaskError


def task_error(code: ErrorCode, message: str, retry_after_seconds: int | None = None) -> TaskError:
    return TaskError(code=code, message=message, retryable=ERROR_POLICY[code][1], retry_after_seconds=retry_after_seconds)


class TaskFault(Exception):
    """Only sanitized protocol errors may cross the HTTP boundary."""

    def __init__(self, error: TaskError):
        self.error = error
        super().__init__(error.code.value)


class ScriptTimeout(TaskFault):
    """A page script was stopped at its deadline; its effects are unknown."""


def bounded_values(value: dict) -> dict:
    if len(value) > 64 or any(not key or len(key) > 128 for key in value):
        raise ValueError("At most 64 bounded named values are allowed")
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_INPUT_BYTES:
        raise ValueError("Values exceed the wire size limit")
    return value


class TaskStart(Contract):
    task: str = Field(min_length=1, max_length=16_000, repr=False)
    inputs: dict[str, JsonValue] = Field(default_factory=dict, repr=False)
    session_id: Identifier | None = None
    profile_id: Identifier | None = None
    idempotency_key: IdempotencyKey

    @field_validator("task")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Task cannot be blank")
        return value

    @field_validator("inputs")
    @classmethod
    def bound_inputs(cls, value):
        return bounded_values(value)

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"idempotency_key"})
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class TaskAdmission(Contract):
    task_id: Identifier
    session_id: Identifier
    profile_id: Identifier
    status_url: str
    revision: int = Field(ge=1)

    @model_validator(mode="after")
    def owned_status_url(self):
        if self.status_url != f"{API_PREFIX}/{self.task_id}":
            raise ValueError("Status URL must be the canonical relative task path")
        return self


class PollRequest(Contract):
    after_revision: int | None = Field(default=None, ge=0)
    wait_seconds: float = Field(default=0, ge=0, le=25, allow_inf_nan=False)

    @model_validator(mode="after")
    def wait_needs_revision(self):
        if self.wait_seconds and self.after_revision is None:
            raise ValueError("Waiting requires after_revision")
        return self


class InputField(Contract):
    name: Identifier
    label: ShortText
    kind: Literal["text", "secret", "select"] = "text"
    options: tuple[ShortText, ...] = Field(default=(), max_length=32)


class InteractionKind(StrEnum):
    INPUT = "input"
    CONFIRMATION = "confirmation"
    BROWSER = "browser"


INTERACTION_STATE = {
    InteractionKind.INPUT: TaskState.WAITING_FOR_INPUT,
    InteractionKind.CONFIRMATION: TaskState.WAITING_FOR_CONFIRMATION,
    InteractionKind.BROWSER: TaskState.WAITING_FOR_BROWSER,
}


class Interaction(Contract):
    interaction_id: Identifier
    generation: int = Field(ge=1)
    kind: InteractionKind
    message: ShortText
    action_id: Identifier | None = None
    fields: tuple[InputField, ...] = Field(default=(), max_length=64)
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def field_shape(self):
        if (self.kind == InteractionKind.INPUT) != bool(self.fields):
            raise ValueError("Only input interactions have a nonempty field list")
        if len({field.name for field in self.fields}) != len(self.fields):
            raise ValueError("Interaction fields must have unique names")
        if (self.kind == InteractionKind.CONFIRMATION) != (self.action_id is not None):
            raise ValueError("Confirmations must bind a proposed action ID")
        return self


class InteractionResponse(Contract):
    generation: int = Field(ge=1)
    idempotency_key: IdempotencyKey
    kind: InteractionKind
    values: dict[str, str] | None = Field(default=None, repr=False)
    confirmed: StrictBool | None = None
    browser_resolved: Literal[True] | None = None

    @model_validator(mode="after")
    def response_shape(self):
        present = (self.values is not None, self.confirmed is not None, self.browser_resolved is not None)
        expected = {
            InteractionKind.INPUT: (True, False, False),
            InteractionKind.CONFIRMATION: (False, True, False),
            InteractionKind.BROWSER: (False, False, True),
        }
        if present != expected[self.kind]:
            raise ValueError("Response must match its interaction kind")
        if self.values is not None:
            bounded_values(self.values)
        return self

    def validate_for(self, interaction: Interaction, now: datetime) -> None:
        if interaction.expires_at <= now:
            raise TaskFault(task_error(ErrorCode.STALE_GENERATION, "Interaction is no longer active"))
        if (self.generation, self.kind) != (interaction.generation, interaction.kind):
            raise TaskFault(task_error(ErrorCode.STALE_GENERATION, "Interaction is no longer active"))
        if self.values is not None and set(self.values) != {field.name for field in interaction.fields}:
            raise TaskFault(task_error(ErrorCode.INVALID_INPUT, "Supply exactly the requested fields"))

    def receipt_digest(self, secret: bytes, principal: Principal, task_id: str, interaction_id: str) -> str:
        """Keyed equality check for retries without storing guessable OTP hashes.

        Use the stable server JWT secret (at least 32 bytes), domain-separated here.
        Persist only the digest and decision metadata; rotating that secret requires
        retiring old receipts, not accepting mismatched response values.
        """
        if len(secret) < 32:
            raise ValueError("Response receipt key must be at least 32 bytes")
        key = hmac.digest(secret, b"flowpilot/browser-tasks/v1/interaction-receipt", "sha256")
        payload = [principal.account_id, task_id, interaction_id, self.model_dump(mode="json", exclude={"idempotency_key"})]
        return hmac.new(key, json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(), "sha256").hexdigest()


class TaskMetrics(Contract):
    model_turns: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    reused: int = Field(default=0, ge=0)
    saved: int = Field(default=0, ge=0)
    replay_failures: int = Field(default=0, ge=0)
    wall_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    human_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)


class TaskResult(Contract):
    answer: str = Field(max_length=32_000)
    source_urls: tuple[HttpUrl, ...] = Field(default=(), max_length=64)
    metrics: TaskMetrics = Field(default_factory=TaskMetrics)


class TaskView(Contract):
    task_id: Identifier
    session_id: Identifier
    profile_id: Identifier
    state: TaskState
    revision: int = Field(ge=1)
    progress: str = Field(default="", max_length=2_000)
    interaction: Interaction | None = None
    result: TaskResult | None = None
    error: TaskError | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def state_payload(self):
        if (self.state in WAITING_STATES) != (self.interaction is not None):
            raise ValueError("Only waiting tasks expose a pending interaction")
        if self.interaction and INTERACTION_STATE[self.interaction.kind] != self.state:
            raise ValueError("Interaction does not match task state")
        if (self.state == TaskState.COMPLETED) != (self.result is not None):
            raise ValueError("Only completed tasks have a result")
        if self.state in {TaskState.FAILED, TaskState.INTERRUPTED} and self.error is None:
            raise ValueError("Failed/interrupted tasks need a typed error")
        if self.error is not None and self.state not in {TaskState.FAILED, TaskState.INTERRUPTED}:
            raise ValueError("Only failed/interrupted tasks expose an error")
        if self.updated_at < self.created_at:
            raise ValueError("Task timestamps are out of order")
        return self


class ProfileCreate(Contract):
    label: ShortText
    idempotency_key: IdempotencyKey


class ProfileView(Contract):
    profile_id: Identifier
    label: ShortText
    state: Literal["available", "leased", "recovery_required"]
    created_at: AwareDatetime


class ProfileList(Contract):
    items: tuple[ProfileView, ...] = Field(max_length=100)
    next_cursor: Identifier | None = None


class AssistanceRequest(Contract):
    interaction_id: Identifier
    generation: int = Field(ge=1)


class AssistanceGrant(Contract):
    """Explicitly minted bearer secret; never put this in task status or telemetry."""

    assistance_url: str = Field(repr=False)
    expires_at: AwareDatetime
    interaction_id: Identifier
    generation: int = Field(ge=1)

    @field_validator("assistance_url")
    @classmethod
    def canonical_viewer(cls, value):
        prefix = ASSISTANCE_PREFIX + "/"
        if not value.startswith(prefix) or "/" in value[len(prefix):] or "?" in value or "#" in value:
            raise ValueError("Viewer URL must be a relative capability path")
        token = value[len(prefix):]
        if len(token) < 32 or len(token) > 128 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in token):
            raise ValueError("Invalid capability encoding")
        return value


class Acknowledgement(Contract):
    accepted: Literal[True] = True


class BrowserMetadata(Contract):
    url: HttpUrl
    viewport_width: int = Field(ge=1, le=16_384)
    viewport_height: int = Field(ge=1, le=16_384)


class AssistanceState(BrowserMetadata):
    interaction: Interaction


class Readiness(Contract):
    enabled: bool
    ready: bool
    protocol_version: Literal["1"] = PROTOCOL_VERSION
    reason: Literal["disabled", "components_missing", "starting", "ready", "shutdown", "startup_failed"]


class Endpoint(Contract):
    method: str
    path: str
    request: str | None
    response: str | None
    success_status: int
    auth: Literal["account", "capability"] = "account"


ENDPOINTS = (
    Endpoint(method="POST", path=API_PREFIX, request="TaskStart", response="TaskAdmission", success_status=202),
    Endpoint(method="GET", path=API_PREFIX + "/{task_id}", request="PollRequest", response="TaskView", success_status=200),
    Endpoint(method="POST", path=API_PREFIX + "/{task_id}/cancel", request=None, response="TaskView", success_status=202),
    Endpoint(method="POST", path=API_PREFIX + "/{task_id}/interactions/{interaction_id}/respond", request="InteractionResponse", response="TaskView", success_status=202),
    Endpoint(method="POST", path=API_PREFIX + "/{task_id}/assistance", request="AssistanceRequest", response="AssistanceGrant", success_status=201),
    Endpoint(method="POST", path=PROFILE_PREFIX, request="ProfileCreate", response="ProfileView", success_status=201),
    Endpoint(method="GET", path=PROFILE_PREFIX, request=None, response="ProfileList", success_status=200),
    Endpoint(method="DELETE", path=PROFILE_PREFIX + "/{profile_id}", request=None, response=None, success_status=204),
    *(Endpoint(method=method, path=ASSISTANCE_PREFIX + "/{token}" + suffix, request=request, response=response, success_status=101 if method == "WEBSOCKET" else 202 if response == "TaskView" else 200, auth="capability") for method, suffix, request, response in (
        ("GET", "", None, "HTML"), ("GET", "/state", None, "AssistanceState"),
        ("GET", "/screen", None, "PNG"), ("WEBSOCKET", "/stream", None, "JPEGFrames"),
        ("POST", "/input", "BrowserInput", "Acknowledgement"),
        ("POST", "/respond", "InteractionResponse", "TaskView"),
        ("POST", "/cancel", None, "TaskView"),
    )),
)


class BrowserInput(Contract):
    kind: Literal["move", "down", "up", "scroll", "keydown", "keyup", "type", "release"]
    x: float | None = Field(default=None, ge=0, le=16_384, allow_inf_nan=False)
    y: float | None = Field(default=None, ge=0, le=16_384, allow_inf_nan=False)
    dx: float | None = Field(default=None, ge=-16_384, le=16_384, allow_inf_nan=False)
    dy: float | None = Field(default=None, ge=-16_384, le=16_384, allow_inf_nan=False)
    button: Literal["left", "middle", "right"] | None = None
    key: str | None = Field(default=None, min_length=1, max_length=64, repr=False)
    text: str | None = Field(default=None, max_length=8_000, repr=False)

    @model_validator(mode="after")
    def input_shape(self):
        supplied = {name for name in ("x", "y", "dx", "dy", "button", "key", "text") if getattr(self, name) is not None}
        shapes = {
            "move": {"x", "y"}, "down": {"x", "y", "button"}, "up": {"x", "y", "button"},
            "scroll": {"dx", "dy"}, "keydown": {"key"}, "keyup": {"key"},
            "type": {"text"}, "release": set(),
        }
        if supplied != shapes[self.kind]:
            raise ValueError("Input fields do not match the command")
        return self
