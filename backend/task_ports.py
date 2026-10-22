"""Implementation seams shared by independent task workstreams.

All state mutations are account-bound atomic compare-and-swap operations. A lease
generation fences late callbacks; revisions fence competing writes. No transaction
may span browser/model I/O. Every accepted mutation advances revision. Response
values stay transient; only decision metadata is durable. No port may log raw
request values, DOM, credentials or capabilities. Safe correlation fields are
account_id, task_id, session_id, profile_id, revision and lease generation.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Literal, Protocol

from .task_contracts import (
    AssistanceGrant, AssistanceRequest, BrowserInput, BrowserMetadata, Contract, Identifier, Interaction,
    InteractionResponse, PollRequest, Principal, ProfileCreate, ProfileList,
    ProfileView, TaskAdmission, TaskError, TaskResult, TaskStart, TaskState, TaskView,
)
from pydantic import AwareDatetime, Field, model_validator


class Lease(Contract):
    principal: Principal
    task_id: Identifier
    session_id: Identifier
    profile_id: Identifier
    generation: int = Field(ge=1)


class Execution(Contract):
    lease: Lease
    revision: int = Field(ge=1)
    request: TaskStart = Field(repr=False)
    admitted_at: AwareDatetime
    deadline: AwareDatetime

    @model_validator(mode="after")
    def valid_deadline(self):
        if self.deadline <= self.admitted_at:
            raise ValueError("Execution deadline must follow durable admission")
        return self


class AssistanceBinding(Contract):
    """Persist a digest, never the clear capability. Check expiry on every use."""

    lease: Lease
    interaction_id: Identifier
    generation: int = Field(ge=1)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: AwareDatetime


# Element references one snapshot may hold; captures stop collecting controls here.
MAX_REFERENCES = 2_000


class ElementReference(Contract):
    reference: Identifier
    snapshot_id: Identifier
    fingerprint: str = Field(min_length=1, max_length=128)


class Observation(Contract):
    """One coherent, ephemeral snapshot; not a credential-safe persistence format."""

    snapshot_id: Identifier
    url: str = Field(max_length=8_192)
    target_id: Identifier
    document_id: Identifier
    elements: str = Field(max_length=24_000, repr=False)
    references: tuple[ElementReference, ...] = Field(default=(), max_length=MAX_REFERENCES)
    structural_fingerprint: str = Field(min_length=1, max_length=128)
    progress_fingerprint: str = Field(min_length=1, max_length=128)
    readiness: Literal["ready", "timeout", "unstable", "unknown"]
    part: int = Field(default=0, ge=0)
    parts: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def coherent_references(self):
        if any(reference.snapshot_id != self.snapshot_id for reference in self.references):
            raise ValueError("Element references must belong to this snapshot")
        if len({reference.reference for reference in self.references}) != len(self.references):
            raise ValueError("Element references must be unique within a snapshot")
        return self


class ObservationPolicy(Protocol):
    """Never silently accept timeout as readiness or rematch an ambiguous target."""

    async def observe(self, browser: Any, *, part: int = 0, find: str | None = None) -> Observation: ...
    async def require_fresh(self, browser: Any, observation: Observation) -> None: ...
    async def after_action(self, browser: Any, observation: Observation | None, action: str) -> None: ...


class Lifecycle(Protocol):
    def bind(self, bindings: "TaskBindings") -> None: ...
    async def startup(self) -> None: ...
    async def shutdown(self) -> None: ...


class TaskRepository(Protocol):
    """Admission reserves capacity/profile/session and idempotency in one commit.

    Idempotency is account+operation+key, independent of API key rotation. Matching
    admission returns the same receipt; fingerprint mismatch conflicts. Foreign
    and missing IDs both yield not_found. Claim is exclusive; repeated dispatch
    cannot acquire a second lease. Terminal records are immutable. Respond stores
    descriptors/decision identity and receipt_digest only, never input values or
    their guessable hash. Digest checks use constant-time comparison. Secret
    rotation retires old receipts; it never silently accepts changed input values.
    Delete profile must reject a lease and coordinate durable memory removal.
    """

    async def admit(self, principal: Principal, request: TaskStart) -> TaskAdmission: ...
    async def get(self, principal: Principal, task_id: str) -> TaskView: ...
    async def claim(self, principal: Principal, task_id: str) -> Execution | None: ...
    async def transition(self, lease: Lease, expected_revision: int, state: TaskState, *, interaction: Interaction | None = None, result: TaskResult | None = None, error: TaskError | None = None, progress: str = "") -> TaskView: ...
    async def set_progress(self, lease: Lease, expected_revision: int, summary: str) -> TaskView: ...
    async def release_browser(self, lease: Lease, *, profile_saved: bool) -> None: ...
    async def request_cancel(self, principal: Principal, task_id: str) -> TaskView: ...
    async def resolve(self, principal: Principal, task_id: str, interaction_id: str, response: InteractionResponse) -> TaskView: ...
    async def interrupt_active(self) -> tuple[Lease, ...]: ...
    async def create_profile(self, principal: Principal, request: ProfileCreate) -> ProfileView: ...
    async def list_profiles(self, principal: Principal, cursor: str | None = None) -> ProfileList: ...
    async def delete_profile(self, principal: Principal, profile_id: str) -> None: ...
    async def save_capability(self, binding: AssistanceBinding) -> None: ...
    async def capability(self, digest: str, now: datetime) -> AssistanceBinding: ...
    async def revoke_capabilities(self, lease: Lease) -> None: ...
    async def assistance_lease(self, principal: Principal, task_id: str, request: AssistanceRequest) -> Lease:
        """Atomically authorize the owned active browser pause and return its execution lease.

        Check task state, interaction ID/generation/expiry and the current execution
        generation in the same transaction. Never substitute interaction generation
        for lease generation. save_capability must repeat those checks at commit
        because cancellation or resolution can race the lookup.
        """
        ...


class TaskControl(Lifecycle, Protocol):
    """001 owns the public service and dispatch; bind never starts work.

    Startup interrupts/revokes old execution then asks executor.recover before
    enabling admission. Start commits before dispatching executor.execute. Each
    execute returns only after cleanup and terminal settlement. Cancelling persists
    intent before requesting stop. Input response delivery is transient and fenced;
    duplicate decisions use a persisted receipt, not a second wake. A denied
    confirmation cannot authorize its proposed effect. Host-declared critical
    effects require a current affirmative decision; arbitrary JS is not a typed
    critical-effect classifier or a comprehensive authorization sandbox.
    """

    async def start(self, principal: Principal, request: TaskStart) -> TaskAdmission: ...
    async def get(self, principal: Principal, task_id: str, poll: PollRequest) -> TaskView: ...
    async def cancel(self, principal: Principal, task_id: str) -> TaskView: ...
    async def respond(self, principal: Principal, task_id: str, interaction_id: str, response: InteractionResponse) -> TaskView: ...
    async def progress(self, lease: Lease, expected_revision: int, summary: str) -> TaskView: ...
    async def release_browser(self, lease: Lease, *, profile_saved: bool) -> None: ...
    async def wait_for_interaction(self, lease: Lease, interaction: Interaction) -> InteractionResponse: ...
    async def require_approval(self, lease: Lease, interaction_id: str, generation: int, action_id: str) -> None: ...
    async def finish(self, lease: Lease, expected_revision: int, state: TaskState, *, result: TaskResult | None = None, error: TaskError | None = None) -> TaskView: ...
    async def create_profile(self, principal: Principal, request: ProfileCreate) -> ProfileView: ...
    async def list_profiles(self, principal: Principal, cursor: str | None = None) -> ProfileList: ...
    async def delete_profile(self, principal: Principal, profile_id: str) -> None: ...
    async def save_capability(self, binding: AssistanceBinding) -> None: ...
    async def capability(self, digest: str, now: datetime) -> AssistanceBinding: ...
    async def revoke_capabilities(self, lease: Lease) -> None: ...
    async def assistance_lease(self, principal: Principal, task_id: str, request: AssistanceRequest) -> Lease:
        """Return only the repository-authorized current pause's fenced execution lease."""
        ...


class BrowserHandle(Protocol):
    """Only current waiting_for_browser leases may acquire a human writer.

    Input and resume serialize. Release drains in-flight input and held keys/buttons
    before agent writes restart. Streams revalidate capability generation/expiry;
    screenshot fallback is viewport-only, never an OS-desktop promise.
    """

    async def screenshot(self) -> bytes: ...
    async def metadata(self) -> BrowserMetadata:
        """Read the current page URL and viewport while the human writer is authorized."""
        ...
    def frames(self) -> AsyncIterator[bytes]: ...
    async def send_input(self, command: BrowserInput) -> None: ...
    async def release(self) -> None: ...


class TaskExecutor(Lifecycle, Protocol):
    """002 owns one writer/profile and bounded agent/browser shutdown.

    execute never retries unknown website effects. Cancellation stops children and
    browser JS, fences callbacks, drains human input, saves/quarantines state and
    settles cancelled only after stop is proven; otherwise interrupted. Idle
    browsers retain capacity reservations until saved/evicted. recover touches only
    task-labelled resources and never resumes an interrupted Python stack.
    Use an additional flowpilot-task label so the legacy SandboxManager orphan
    sweep can exclude task-owned containers before this service reconciles them.
    """

    async def execute(self, execution: Execution) -> None: ...
    async def cancel(self, lease: Lease) -> None: ...
    async def recover(self, leases: tuple[Lease, ...]) -> None: ...
    async def browser(self, lease: Lease, interaction_id: str, generation: int) -> BrowserHandle: ...
    async def release_human(self, lease: Lease, interaction_id: str, generation: int) -> None:
        """Fence/drain all human handles for the verified pause before API resume.

        Never acquire a new writer merely to release it. This is idempotent for
        the same valid pause even if no viewer was opened. Fence future handle
        acquisition/input for that pause, await in-flight input and release held
        controls before returning. Other task/lease/pause generations are rejected.
        Control validates a live response, awaits this within its cancellation
        budget, then commits resolution/revocation and wakes the executor. Failure
        or timeout must not resolve the interaction or wake agent writes.
        """
        ...
    async def delete_profile(self, principal: Principal, profile_id: str) -> None: ...


class AssistanceAccess(Lifecycle, Protocol):
    async def mint(self, principal: Principal, task_id: str, request: AssistanceRequest) -> AssistanceGrant: ...
    async def authorize(self, token: str) -> AssistanceBinding: ...
    async def browser(self, binding: AssistanceBinding) -> BrowserHandle: ...


class InteractionChannel(Protocol):
    """Injected local/hosted REPL transport; hosted implementations never use stdin."""

    async def start(self) -> None: ...
    async def request(self, reason: str, category: str = "other", kind: str = "browser", fields: list | None = None) -> dict: ...
    async def close(self) -> None: ...


class HandoffFactory(Protocol):
    def __call__(self, context: Any, cdp_url: str, user: str, notify: Any = None) -> InteractionChannel: ...


@dataclass(frozen=True)
class TaskBindings:
    control: TaskControl
    executor: TaskExecutor
    assistance: AssistanceAccess
    observation: ObservationPolicy
