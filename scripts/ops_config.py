"""Non-secret hosted preflight. Exit nonzero on missing security prerequisites."""
import argparse
import os
from pathlib import Path
import stat
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def validate(env, *, hosted=True):
    from backend.task_settings import TaskSettings
    errors = []
    try:
        settings = TaskSettings.from_env(env)
        if settings.shutdown_seconds > 45:
            errors.append("shutdown budget exceeds packaged 45 seconds")
    except Exception:
        errors.append("invalid task settings (values suppressed)")
    secret = env.get("JWT_SECRET", "")
    if len(secret.encode()) < 32:
        errors.append("stable JWT_SECRET of at least 32 bytes required")
    if env.get("WEB_CONCURRENCY", "1") != "1" or env.get("UVICORN_WORKERS", "1") != "1":
        errors.append("exactly one backend worker required")
    url = urlsplit(env.get("FLOWPILOT_BASE_URL", ""))
    if hosted and (url.scheme != "https" or not url.hostname or url.username or url.query or url.fragment or url.path not in {"", "/"}):
        errors.append("public HTTPS origin required")
    if hosted and env.get("ALLOWED_ORIGINS") != env.get("FLOWPILOT_BASE_URL", "").rstrip("/"):
        errors.append("ALLOWED_ORIGINS must equal the public origin")
    provider = env.get("LLM_PROVIDER", "gemini")
    key = {"gemini": "GOOGLE_API_KEY", "claude": "ANTHROPIC_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider)
    if key:
        if not env.get(key):
            errors.append("selected provider authentication missing")
    elif provider == "codex":
        auth = Path(env.get("PI_AUTH_FILE", "/app/.pi-auth/auth.json"))
        if env.get("AGENT_DRIVER") != "pi" or not auth.is_file():
            errors.append("codex requires pi and operator-supplied auth file")
        elif stat.S_IMODE(auth.stat().st_mode) & 0o077:
            errors.append("subscription auth file permissions must be 0600")
    else:
        errors.append("unsupported provider")
    if hosted:
        errors.append("HOST_NETWORK_POLICY_UNVERIFIED: isolated Linux enforcement/probe evidence required")
        errors.append("ASSEMBLED_ACCEPTANCE_UNVERIFIED: restart, rate limits and runtime resource checks required")
    return errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="configuration-only; NOT hosted readiness")
    args = parser.parse_args()
    failures = validate(os.environ, hosted=not args.local)
    for failure in failures:
        print("BLOCKER: " + failure)
    raise SystemExit(bool(failures))
