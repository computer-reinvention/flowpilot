"""Strict, programmable port doubles, not an alternate task implementation.

Each stream supplies behavior only for its peers. Unconfigured awaited methods
raise instead of accidentally returning truthy mocks. Autospec enforces the frozen
call signatures; no Docker, database, provider or future component is imported.
"""

from unittest.mock import create_autospec

from backend.task_ports import (
    AssistanceAccess, BrowserHandle, InteractionChannel, Observation, ObservationPolicy,
    TaskBindings, TaskControl, TaskExecutor, TaskRepository,
)


def port_double(protocol):
    double = create_autospec(protocol, instance=True, spec_set=True)
    for name in dir(protocol):
        if not name.startswith("_") and callable(getattr(protocol, name)):
            method = getattr(double, name)
            method.side_effect = NotImplementedError(f"Configure {protocol.__name__}.{name} in the scenario")
    return double


def lifecycle_double(protocol):
    double = port_double(protocol)
    for name in ("bind", "startup", "shutdown"):
        method = getattr(double, name)
        method.side_effect = None
        method.return_value = None
    return double


def bindings_double():
    return TaskBindings(
        control=lifecycle_double(TaskControl),
        executor=lifecycle_double(TaskExecutor),
        assistance=lifecycle_double(AssistanceAccess),
        observation=port_double(ObservationPolicy),
    )


def repository_double():
    return port_double(TaskRepository)


def browser_double():
    return port_double(BrowserHandle)


def channel_double():
    channel = port_double(InteractionChannel)
    for name in ("start", "close"):
        getattr(channel, name).side_effect = None
        getattr(channel, name).return_value = None
    return channel


def observation_fixture():
    return Observation(
        snapshot_id="snapshot_demo", url="https://example.test/form", target_id="tab_demo",
        document_id="document_demo", elements="A synthetic button", structural_fingerprint="structure_demo",
        progress_fingerprint="progress_demo", readiness="ready",
    )
