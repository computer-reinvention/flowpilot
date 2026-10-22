"""Sanitized evidence: never serialize request/response bodies or exception text."""

import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone

from .wire import ROOT

# No item defaults to passed. Remaining requirements survive partial invocations.
REQUIREMENTS = {
    "onboarding_http": "001/004: signup/login, ordinary API-key client and once-only disclosure",
    "auth_key_no_store": "001 repair: signup/login/key creation/list/revocation responses declare Cache-Control no-store",
    "same_account_key_scope": "001: same-account keys share authority/idempotency; profiles remain explicit distinct namespaces, not key-scoped identities",
    "auth_ownership": "001: invalid/forged identity, foreign tasks/profiles and revoked keys",
    "http_retry_followup": "001/002: concurrent admission, conflict, fresh-client reattach and website counters",
    "profile_website_isolation": "002: two-account/two-profile cookie, storage, scripts and history isolation",
    "questions_secrets": "001/002: transient secret delivery and duplicate/conflicting/stale response receipts",
    "confirmation_decline": "001/002: approve/decline/expired/wrong-owner with actual effect counters",
    "assistance_tls": "003/005: actual TLS pointer/keyboard/stream/fallback and capability expiry fencing",
    "assistance_generation_metadata": "001/002/003 amendment 2201b5a: public mint/state succeeds across distinct pause generations within one execution; no public lease generation",
    "assistance_wallclock_expiry": "003: active capability rejects after actual elapsed TTL, not only cancel/remint",
    "assistance_atomic_commit": "001/003: deterministic resolve/cancel between assistance_lease lookup and save_capability commit rejects stale binding",
    "direct_api_resume_drain": "001/002/003 amendment 561383d: direct authenticated respond awaits real pause-wide human drain with active stream and owned in-flight input gate before agent effects",
    "resume_wrong_generation": "001/002: wrong-generation direct response never releases the legitimate active human handle",
    "resume_release_failure": "001/002: injected release_human failure leaves pause unresolved, capability current and agent effects absent",
    "resume_release_timeout": "001/002: injected release_human timeout leaves pause unresolved and never wakes agent effects",
    "capacity_paused": "001/002: global/account/session/profile caps count paused/idle browser leases",
    "cancel_fencing": "001/002/003: stop proof, terminal immutability and late callback/input fencing",
    "crash_after_effect": "001/002: owned process kill after website effect, interrupted state and no replay",
    "profile_save_failure": "002: failed save retains prior archive and bounded quarantine evidence",
    "script_reuse_repair": "002/007: replay/repair with no repeated prerequisite, model proof separate",
    "pi_readiness": "007: real browser same-URL freshness/readiness/no-progress guards",
    "packaged_network": "005: real packaged-browser private/admin/metadata deny plus positive control",
    "artifact_secret_scan": "001/002/003/005: DB/history/log/trace/result canaries absent",
    "key_hash_at_rest": "001: real disposable database stores key hashes, never plaintext",
    "operations": "005: image IDs, builds, restart, log/disk bounds, backup/restore",
    "compatibility": "001-007: existing flow/run/UI/chat/Pi regressions and frontend onboarding",
    "standalone_example": "004: execute published standalone client against real local service",
    "provider_backed": "002/007: separately authorized bounded synthetic model-backed cases",
    "hosted_postdeployment": "manual only: hosted TLS, cloud runtime and real provider validation",
}


class Evidence:
    def __init__(self, kind):
        self.run_id = "task-acceptance-" + uuid.uuid4().hex
        self.kind = kind
        self.checks = []
        self.requirements = {name: {"outcome": "blocked", "owner_and_gap": gap}
                             for name, gap in REQUIREMENTS.items()}

    def record(self, name, outcome, detail, **safe):
        assert outcome in {"passed", "failed", "skipped", "blocked"}
        self.checks.append({"name": name, "outcome": outcome, "detail": detail, **safe})

    def check(self, name, action, requirement=None):
        try:
            safe = action() or {}
        except Exception as exc:
            self.record(name, "failed", "assertion or transport failed; rerun named check locally",
                        exception_type=type(exc).__name__)
            if requirement:
                self.requirements[requirement]["outcome"] = "failed"
            return False
        self.record(name, "passed", "assertions verified", **safe)
        if requirement:
            self.requirements[requirement]["outcome"] = "passed"
        return True

    def write(self):
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
        fixture_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted((ROOT / "tests/fixtures/browser_tasks").glob("*.json"))}
        document = {"schema_version": 1, "run_id": self.run_id, "kind": self.kind,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "revision": revision, "working_tree_dirty": dirty,
                    "foundation_revision": "0ccc06a005c3ff680110de6b66a6606a4e48d3d3",
                    "fixture_sha256": fixture_hashes,
                    # A development subset can never complete: unselected gates stay blocked.
                    "scenario_selection": getattr(self, "scenario_selection", None) or "all",
                    "checks": self.checks,
                    "requirements": self.requirements,
                    "readiness": "not_established" if any(v["outcome"] != "passed" for v in self.requirements.values()) else "local_only",
                    "limitations": ["Fixture/consumer tests are not service acceptance.",
                                    "No deployment or provider calls authorized by this harness.",
                                    "No raw bodies, credentials, website state or traces included."]}
        target = ROOT / "reports/task-acceptance" / self.run_id
        target.mkdir(parents=True, exist_ok=False)
        path = target / "evidence.json"
        path.write_text(json.dumps(document, indent=2) + "\n")
        return path

    def exit_code(self, require_complete=False):
        if any(check["outcome"] == "failed" for check in self.checks):
            return 1
        if require_complete and any(item["outcome"] != "passed" for item in self.requirements.values()):
            return 2
        return 0
