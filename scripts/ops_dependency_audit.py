"""Public-advisory checks in bounded disposable images. No host tool installs/keys.

Auditor installation exists only in a discarded container overlay, never production
images. Network access is to package/advisory registries, not application accounts.
"""
import json
import subprocess


def run(image, *command):
    result = subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64", "--label", "flowpilot.ops=lane005", "--cpus", "1", "--memory", "768m", "--pids-limit", "128", "--entrypoint", "/bin/sh", image, "-c", " ".join(command)], capture_output=True, text=True)
    if result.returncode not in {0, 1}:
        raise RuntimeError("audit infrastructure unavailable (details suppressed)")
    try:
        return json.loads(result.stdout)
    except Exception:
        raise RuntimeError("audit did not produce a valid advisory inventory") from None


def main():
    python = run("flowpilot-lane005-backend:verify", "python -m pip install --no-cache-dir --target /tmp/audit-tools pip-audit==2.9.0 >/tmp/install.log 2>&1 && uv export --frozen --no-dev --no-emit-project --no-hashes --format requirements-txt -o /tmp/requirements.txt >/tmp/export.log 2>&1 && PYTHONPATH=/tmp/audit-tools /app/.venv/bin/python -m pip_audit --disable-pip --no-deps -r /tmp/requirements.txt -f json")
    frontend = run("flowpilot-lane005-frontend:verify", "npm audit --json")
    agent = run("flowpilot-lane005-agent:verify", "npm audit --omit=dev --json")
    findings = [{"package": entry["name"], "version": entry["version"], "advisories": [{key: vuln.get(key) for key in ("id", "fix_versions", "aliases")} for vuln in entry.get("vulns", [])]} for entry in python["dependencies"] if entry.get("vulns")]
    print(json.dumps({"python": findings, "frontend": {"summary": frontend["metadata"]["vulnerabilities"], "packages": sorted(frontend.get("vulnerabilities", {}))}, "agent": agent["metadata"]["vulnerabilities"], "os_chromium": "UNVERIFIED"}, sort_keys=True, indent=2))
    return bool(findings or frontend["metadata"]["vulnerabilities"]["total"] or agent["metadata"]["vulnerabilities"]["total"])


if __name__ == "__main__":
    raise SystemExit(main())
