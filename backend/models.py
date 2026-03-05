"""Data models for the browser automation platform."""

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Flow:
    id: str
    name: str
    domain: str
    task: str
    inputs: str  # JSON array of input definitions
    created_by: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    entry_url: str | None = None
    icon: str | None = None
    exploration_status: str = "pending"  # pending | exploring | ready | failed
    exploration_error: str | None = None
    exploration_notes: str | None = None
    exploration_tree: str = "[]"  # JSON array of exploration stage nodes
    exploration_input_request: str | None = None  # JSON array of {name, label, description?}
    exploration_confirmation_request: str | None = None  # summary string
    auth_url: str | None = None
    auth_before_explore: bool = False


@dataclass
class Run:
    id: str
    flow_id: str
    user_id: str
    status: str = "pending"  # pending/connecting/running/paused_for_hitl/completed/failed/cancelled
    inputs: str = "{}"  # JSON dict of user-provided values
    sandbox_id: str | None = None
    steps: str = "[]"  # JSON array of step messages
    current_step: str | None = None
    result: str | None = None  # JSON
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str | None = None
    profile_id: str = ""


@dataclass
class Session:
    id: str
    user_id: str
    domain: str
    cookies: str = "[]"  # JSON
    local_storage: str = "{}"  # JSON
    session_storage: str = "{}"  # JSON
    user_agent: str | None = None
    profile_name: str = "default"  # Named auth profile (e.g., "test-user-1")
    captured_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
