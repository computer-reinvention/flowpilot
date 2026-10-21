"""Typed single-host task limits; construction is the only environment boundary."""

import os
from pathlib import Path
from typing import Mapping

from pydantic import Field, model_validator

from .task_contracts import Contract, MAX_BODY_BYTES


class TaskSettings(Contract):
    enabled: bool = False
    state_dir: Path = Path("/app/profiles/browser-tasks")
    max_active_browsers: int = Field(default=3, ge=1, le=100)
    max_account_browsers: int = Field(default=2, ge=1, le=100)
    max_body_bytes: int = Field(default=MAX_BODY_BYTES, ge=1_024, le=MAX_BODY_BYTES)
    poll_wait_seconds: float = Field(default=25, ge=0, le=25, allow_inf_nan=False)
    task_timeout_seconds: int = Field(default=900, ge=1, le=14_400)
    human_wait_seconds: int = Field(default=600, ge=1, le=3_600)
    idle_session_seconds: int = Field(default=300, ge=0, le=3_600)
    cancellation_seconds: int = Field(default=30, ge=1, le=120)
    shutdown_seconds: int = Field(default=45, ge=1, le=300)
    assistance_ttl_seconds: int = Field(default=300, ge=1, le=600)
    sandbox_memory_mb: int = Field(default=1_024, ge=256, le=16_384)
    sandbox_cpu_cores: float = Field(default=1, gt=0, le=32, allow_inf_nan=False)
    sandbox_pids: int = Field(default=1_024, ge=128, le=4_096)
    sandbox_shm_mb: int = Field(default=256, ge=64, le=2_048)
    readiness_timeout_seconds: float = Field(default=10, gt=0, le=60, allow_inf_nan=False)
    readiness_window_seconds: float = Field(default=0.5, gt=0, le=5, allow_inf_nan=False)
    no_progress_limit: int = Field(default=3, ge=2, le=20)
    history_retention_days: int = Field(default=30, ge=1, le=365)
    trace_retention_days: int = Field(default=7, ge=1, le=30)
    api_requests_per_minute: int = Field(default=120, ge=1, le=10_000)
    login_requests_per_minute: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def coherent_limits(self):
        if self.max_account_browsers > self.max_active_browsers:
            raise ValueError("Account capacity cannot exceed global capacity")
        if self.assistance_ttl_seconds > self.human_wait_seconds:
            raise ValueError("Assistance TTL cannot exceed the human wait budget")
        if self.shutdown_seconds < self.cancellation_seconds:
            raise ValueError("Shutdown budget must include cancellation")
        if self.readiness_window_seconds >= self.readiness_timeout_seconds:
            raise ValueError("Readiness window must fit within its deadline")
        return self

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None):
        environ = os.environ if environ is None else environ
        values = {name: environ[f"FLOWPILOT_TASKS_{name.upper()}"] for name in cls.model_fields if f"FLOWPILOT_TASKS_{name.upper()}" in environ}
        if "state_dir" not in values and "PROFILES_DIR" in environ:
            values["state_dir"] = Path(environ["PROFILES_DIR"]) / "browser-tasks"
        return cls.model_validate(values)
