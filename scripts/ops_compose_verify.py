"""Validate resolved Compose contracts without reading/printing a real .env."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.task_settings import TaskSettings


def main():
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "JWT_SECRET": "synthetic-structural-not-for-service-use", "FLOWPILOT_TASKS_SANDBOX_MEMORY_MB": "768", "FLOWPILOT_TASKS_SANDBOX_CPU_CORES": "0.5", "FLOWPILOT_TASKS_SANDBOX_PIDS": "192"}
    resolved = json.loads(subprocess.run(["docker", "compose", "--env-file", "/dev/null", "--project-directory", str(ROOT), "-f", str(ROOT / "docker-compose.yml"), "config", "--format", "json"], cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout)
    backend = resolved["services"]["backend"]
    proxy = resolved["services"]["docker-proxy"]
    settings = TaskSettings.from_env(backend["environment"])
    assert settings.sandbox_memory_mb == 768 and settings.sandbox_cpu_cores == 0.5 and settings.sandbox_pids == 192
    assert (settings.max_active_browsers, settings.max_account_browsers) == (2, 1)
    assert all(p["host_ip"] == "127.0.0.1" and p["target"] == 8080 for p in backend["ports"])
    assert not proxy.get("ports") and not proxy.get("privileged")
    assert set(proxy["networks"]) == {"control"}
    assert resolved["networks"]["control"]["internal"]
    assert all(v.get("read_only") for v in proxy["volumes"] if v["source"] == "/var/run/docker.sock")
    assert backend["restart"] == "unless-stopped" and backend["pids_limit"] == 256
    assert backend["logging"]["options"] == {"max-size": "10m", "max-file": "3"}
    assert backend["stop_grace_period"] == "1m5s"
    print("PASS: resolved Compose settings/resources/control privacy/restart/log bounds (not host enforcement)")


if __name__ == "__main__":
    main()
