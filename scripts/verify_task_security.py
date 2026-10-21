#!/usr/bin/env python3
"""Explicit offline artifact scan and packaged-browser network diagnostics."""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.task_acceptance_support.evidence import Evidence
from tests.task_acceptance_support.security import probe_network, scan_artifacts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, help="read-only snapshot directory from this labelled test run")
    parser.add_argument("--canaries-env", default="TASK_ACCEPTANCE_CANARIES_JSON", help="environment variable containing JSON array of synthetic secret canaries")
    parser.add_argument("--network-manifest", type=Path, help="explicit disposable packaged topology probe manifest")
    parser.add_argument("--cdp-env", default="TASK_ACCEPTANCE_CDP_URL", help="environment variable holding loopback CDP endpoint")
    args = parser.parse_args()
    if not args.artifacts and not args.network_manifest:
        parser.error("select --artifacts and/or --network-manifest")
    evidence = Evidence("security")
    if args.artifacts:
        def scan():
            canaries = json.loads(os.environ[args.canaries_env])
            assert isinstance(canaries, list) and all(isinstance(c, str) for c in canaries)
            result = scan_artifacts(args.artifacts, canaries)
            outcome = "failed" if result["findings"] else "blocked" if result["gaps"] else "passed"
            evidence.record("artifact_scan", outcome, "bounded raw/URL/JSON/base64 canary scan; compressed artifacts require expansion", **result)
            # A clean arbitrary directory alone does not prove DB/history/trace coverage.
        try:
            scan()
        except Exception as exc:
            evidence.record("artifact_scan", "failed", "scan configuration invalid", exception_type=type(exc).__name__)
    if args.network_manifest:
        evidence.check("packaged_browser_navigation", lambda: probe_network(os.environ[args.cdp_env], json.loads(args.network_manifest.read_text())))
    print(evidence.write().relative_to(ROOT))
    return evidence.exit_code(require_complete=True)


if __name__ == "__main__":
    raise SystemExit(main())
