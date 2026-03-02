"""Data models for the browser automation platform."""

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Flow:
    id: str
    name: str
    domain: str
    description: str
    inputs: str  # JSON array of input definitions
    created_by: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    entry_url: str | None = None
    icon: str | None = None


@dataclass
class Run:
    id: str
    flow_id: str
    user_id: str
    status: str = "pending"  # pending/connecting/running/paused_for_auth/completed/failed/cancelled
    inputs: str = "{}"  # JSON dict of user-provided values
    sandbox_id: str | None = None
    steps: str = "[]"  # JSON array of step messages
    current_step: str | None = None
    result: str | None = None  # JSON
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str | None = None


@dataclass
class Session:
    id: str
    user_id: str
    domain: str
    cookies: str = "[]"  # JSON
    local_storage: str = "{}"  # JSON
    session_storage: str = "{}"  # JSON
    user_agent: str | None = None
    captured_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
