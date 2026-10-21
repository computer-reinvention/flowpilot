"""Probe composition rather than just liveness; never print response bodies."""
import os
import urllib.request


def check(base="http://127.0.0.1:8080"):
    enabled = os.getenv("FLOWPILOT_TASKS_ENABLED", "false").lower() in {"1", "true", "yes"}
    path = "/health/browser-tasks" if enabled else "/health"
    with urllib.request.urlopen(base + path, timeout=3) as response:
        if response.status != 200:
            raise RuntimeError("readiness unavailable")


if __name__ == "__main__":
    try:
        check()
    except Exception:
        raise SystemExit("readiness unavailable")
