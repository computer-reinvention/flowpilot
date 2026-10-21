#!/usr/bin/env python3
"""Run from any cwd; only loopback disposable services are accepted."""

import argparse
import asyncio
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.task_acceptance_support.client import local_url
from tests.task_acceptance_support.evidence import Evidence
from tests.task_acceptance_support.site import synthetic_site
from tests.task_acceptance_support.system import run_service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="consumer and actual local Chromium fixture tests only")
    parser.add_argument("--base-url", type=local_url, help="explicit disposable local service; may create run-labelled accounts")
    parser.add_argument("--require-complete", action="store_true", help="exit 2 on any unmet acceptance requirement")
    parser.add_argument("--deterministic-local", action="store_true", help="authorize disposable account/task writes; service MUST use deterministic local inference, no provider credentials")
    parser.add_argument('--direct-resume', action='store_true', help='owned constructor-injected direct API resume/failure/timeout targets; blocked until assembled adapter supplied')
    parser.add_argument('--assembled-docker', action='store_true', help='actual composed services and disposable Docker browsers, deterministic model boundary only')
    parser.add_argument("--site-browser-host", choices=['127.0.0.1', 'localhost', 'host.docker.internal'], default='127.0.0.1',
                        help="host mapping for this run's synthetic fixture, not an arbitrary external website")
    args = parser.parse_args()
    if not args.offline and not args.base_url and not args.direct_resume and not args.assembled_docker:
        parser.error("select --offline, --base-url, --assembled-docker and/or --direct-resume")
    if args.direct_resume and not args.deterministic_local:
        parser.error('--direct-resume requires --deterministic-local and an owned disposable constructor adapter')
    if args.assembled_docker and not args.deterministic_local:
        parser.error('--assembled-docker requires --deterministic-local; real providers are forbidden')
    evidence = Evidence("system")
    (ROOT / "reports/task-acceptance").mkdir(parents=True, exist_ok=True)
    if args.offline:
        result = subprocess.run([sys.executable, "-m", "pytest", "tests/test_task_acceptance_contract.py",
                                 "tests/test_task_acceptance_browser.py", "tests/test_task_acceptance_security.py",
                                 "tests/test_task_acceptance_baseline.py", "tests/test_task_acceptance_assistance.py",
                                 "tests/test_task_acceptance_identity.py", "tests/test_task_acceptance_release_fence.py", "-q"],
                                cwd=ROOT, capture_output=True, text=True, timeout=120)
        counts = {name: int(value) for value, name in re.findall(r"(\d+) (passed|failed|skipped|error)", result.stdout)}
        evidence.record("offline_harness", "passed" if result.returncode == 0 else "failed",
                        "independent consumers, real-browser fixture and scanner self-tests only",
                        exit_code=result.returncode, counts=counts)
        print("offline harness exit:", result.returncode)
    if args.base_url:
        # Unique directory and two loopback listeners; no global resource cleanup.
        with tempfile.TemporaryDirectory(prefix=evidence.run_id + "-", dir=ROOT / "reports/task-acceptance") as directory:
            with synthetic_site(directory, args.site_browser_host) as site:
                try:
                    run_service(evidence, args.base_url, site, args.deterministic_local)
                except Exception as exc:
                    evidence.record("service_probe", "failed", "local service probe could not complete",
                                    exception_type=type(exc).__name__)
    if args.direct_resume:
        from tests.task_acceptance_support.release_fence import run_constructor_resume_targets
        try:
            asyncio.run(run_constructor_resume_targets(evidence))
        except Exception as exc:
            evidence.record('direct_resume_setup', 'failed', 'owned local constructor adapter setup failed', exception_type=type(exc).__name__)
    if args.assembled_docker:
        from tests.task_acceptance_support.docker_run import run_assembled_docker
        try:
            run_assembled_docker(evidence)
        except Exception as exc:
            evidence.record('assembled_docker_setup', 'failed', 'owned assembled Docker execution failed', exception_type=type(exc).__name__)
    print(evidence.write().relative_to(ROOT))
    return evidence.exit_code(args.require_complete or bool(args.base_url) or args.direct_resume or args.assembled_docker)


if __name__ == "__main__":
    raise SystemExit(main())
