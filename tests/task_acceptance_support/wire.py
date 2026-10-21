"""Small, independent assertions derived from the frozen v1 wire contract."""

import json
import re
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "/api/v1/browser-tasks"
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
WAITING = {"waiting_for_input": "input", "waiting_for_confirmation": "confirmation", "waiting_for_browser": "browser"}
STATES = TERMINAL | set(WAITING) | {"queued", "running", "cancelling"}
ERRORS = {
    "unauthenticated": (401, False), "not_found": (404, False),
    "invalid_input": (422, False), "method_not_allowed": (405, False),
    "conflict": (409, False), "stale_generation": (409, False),
    "capacity_exhausted": (429, True), "shutdown": (503, True),
    "unavailable": (503, True), "interrupted": (409, False),
    "profile_recovery_required": (409, False), "provider_failure": (502, False),
    "assistance_expired": (410, False), "readiness_timeout": (409, False),
    "no_progress": (409, False),
}


def fixture(name):
    return json.loads((ROOT / "tests/fixtures/browser_tasks" / name).read_text())


def identifier(value):
    assert isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)


def admission(body):
    for key in ("task_id", "session_id", "profile_id"):
        identifier(body[key])
    assert body["status_url"] == f"{PREFIX}/{body['task_id']}"
    assert type(body["revision"]) is int and body["revision"] >= 1


def error(status, body, headers=None):
    err = body["error"]
    expected_status, retryable = ERRORS[err["code"]]
    assert status == expected_status
    assert err["retryable"] is retryable
    assert isinstance(err["message"], str) and 0 < len(err["message"]) <= 2000
    if err.get("retry_after_seconds") is not None:
        assert retryable and 1 <= err["retry_after_seconds"] <= 300
        if headers is not None:
            headers = {k.lower(): v for k, v in headers.items()}
            assert int(headers["retry-after"]) == err["retry_after_seconds"]


def view(body, previous=None):
    for key in ("task_id", "session_id", "profile_id"):
        identifier(body[key])
    assert body["state"] in STATES
    assert type(body["revision"]) is int and body["revision"] >= 1
    interaction = body.get("interaction")
    assert bool(interaction) == (body["state"] in WAITING)
    if interaction:
        assert interaction["kind"] == WAITING[body["state"]]
        assert interaction["generation"] >= 1
        identifier(interaction["interaction_id"])
        assert bool(interaction.get("fields")) == (interaction["kind"] == "input")
        assert bool(interaction.get("action_id")) == (interaction["kind"] == "confirmation")
    assert (body.get("result") is not None) == (body["state"] == "completed")
    assert (body.get("error") is not None) == (body["state"] in {"failed", "interrupted"})
    if body.get("error"):
        error(ERRORS[body["error"]["code"]][0], {"error": body["error"]})
    if previous:
        assert all(body[k] == previous[k] for k in ("task_id", "session_id", "profile_id"))
        assert body["revision"] >= previous["revision"]
        if previous["state"] in TERMINAL:
            assert body == previous, "terminal task mutated"
        if body["revision"] == previous["revision"]:
            assert body == previous, "same revision has different payload"
        # Polls can miss intermediate states; do not demand adjacent transitions.
        if previous["state"] == "cancelling":
            assert body["state"] in {"cancelling", "cancelled", "interrupted"}


def ordinary_safe(body):
    forbidden = {"assistance_url", "capability", "cdp_url", "api_key", "password", "otp", "values",
                 "lease", "lease_generation", "execution_generation", "principal"}
    def walk(item):
        if isinstance(item, dict):
            assert not forbidden.intersection(item), "secret-bearing field in ordinary response"
            for value in item.values():
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)
        elif isinstance(item, str):
            assert "/browser-tasks/help/" not in item and "fp_live_" not in item
    walk(body)


def assistance_state(body, interaction, expected_url=None):
    """Public metadata consumer; never equate pause and internal execution epochs."""
    assert set(body) == {'interaction', 'url', 'viewport_width', 'viewport_height'}
    assert body['interaction'] == interaction
    parsed = urllib.parse.urlsplit(body['url'])
    assert parsed.scheme in {'http', 'https'} and parsed.hostname
    if expected_url is not None:
        assert body['url'].rstrip('/') == expected_url.rstrip('/')
    for name in ('viewport_width', 'viewport_height'):
        assert type(body[name]) is int and 1 <= body[name] <= 16384
    ordinary_safe(body)
