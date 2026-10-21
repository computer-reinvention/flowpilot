"""Non-secret hosted preflight. Exit nonzero on missing security prerequisites."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import stat
import re
import shutil
import sys
import tempfile
import urllib.request
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
    try:
        peers = env.get("FLOWPILOT_TRUSTED_PROXY_IPS", "127.0.0.1").split(",")
        if not peers or len(peers) > 2:
            raise ValueError("invalid proxy peers")
        for peer in peers:
            address = ipaddress.ip_address(peer)
            if address.is_unspecified or not (address.is_loopback or str(address) == "172.30.240.1"):
                raise ValueError("unreviewed proxy address")
    except ValueError:
        errors.append("trusted proxies must be explicit loopback or packaged host gateway addresses, never wildcard/CIDR")
    url = urlsplit(env.get("FLOWPILOT_BASE_URL", ""))
    if hosted and (url.scheme != "https" or not url.hostname or url.username or url.query or url.fragment or url.path not in {"", "/"}):
        errors.append("public HTTPS origin required")
    if hosted and env.get("ALLOWED_ORIGINS") != env.get("FLOWPILOT_BASE_URL", "").rstrip("/"):
        errors.append("ALLOWED_ORIGINS must equal the public origin")
    if hosted and env.get("FLOWPILOT_EXTERNAL_HOST") != url.netloc:
        errors.append("proxy external Host must equal public URL authority")
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


def runtime_checks(env):
    errors = []
    root = Path(env.get("PROFILES_DIR", "/app/profiles"))
    try:
        if not root.is_dir() or root.is_symlink():
            raise ValueError("invalid state root")
        if shutil.disk_usage(root).free < 1024 ** 3:
            errors.append("DISK_LOW: at least 1 GiB free state space required")
        with tempfile.TemporaryFile(dir=root) as probe:
            probe.write(b"writability-check")
            probe.flush()
        for db in root.rglob("*.db"):
            if not os.access(db, os.R_OK | os.W_OK):
                errors.append("database permissions prevent read/write")
                break
    except Exception:
        errors.append("state directory not writable")
    host = env.get("DOCKER_HOST", "")
    if host != "tcp://docker-proxy:2375":
        errors.append("packaged Docker control must use private socket proxy")
    else:
        try:
            with urllib.request.urlopen("http://docker-proxy:2375/_ping", timeout=3) as response:
                if response.status != 200:
                    raise ValueError("unavailable")
        except Exception:
            errors.append("private Docker proxy unavailable")
    return errors


def diagnostics(env):
    revision = env.get("FLOWPILOT_REVISION", "unknown")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", revision):
        revision = "invalid"
    provider = env.get("LLM_PROVIDER", "gemini")
    if provider not in {"gemini", "claude", "anthropic", "openai", "codex"}:
        provider = "invalid"
    return {"revision": revision, "provider": provider, "jwt_present": bool(env.get("JWT_SECRET")), "tasks_enabled": env.get("FLOWPILOT_TASKS_ENABLED", "false").lower() in {"true", "1", "yes"}, "worker_count": 1}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="configuration-only; NOT hosted readiness")
    parser.add_argument("--runtime", action="store_true", help="probe state writability/free space and private Docker ping")
    parser.add_argument("--diagnostics", action="store_true", help="emit bounded non-secret build/config identity")
    args = parser.parse_args()
    failures = validate(os.environ, hosted=not args.local)
    if args.runtime:
        failures.extend(runtime_checks(os.environ))
    if args.diagnostics:
        print(json.dumps(diagnostics(os.environ), sort_keys=True))
    for failure in failures:
        print("BLOCKER: " + failure)
    raise SystemExit(bool(failures))
