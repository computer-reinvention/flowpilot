"""Offline lane005 tests: python -m unittest discover -s scripts -p ops_tests.py -v."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup = load("ops_backup")
retention = load("ops_retention")
config = load("ops_config")
health = load("ops_health")


class OperationalTests(unittest.TestCase):
    def test_settings_and_redaction(self):
        env = {"JWT_SECRET": "x" * 32, "GOOGLE_API_KEY": "SECRET_CANARY"}
        self.assertEqual(config.validate(env, hosted=False), [])
        env["FLOWPILOT_TASKS_SANDBOX_MEMORY_MB"] = "SECRET_CANARY"
        failures = config.validate(env)
        self.assertNotIn("SECRET_CANARY", str(failures))
        self.assertTrue(any("HOST_NETWORK_POLICY_UNVERIFIED" in e for e in failures))

    def test_provider_and_worker_fail_closed(self):
        for env in ({"LLM_PROVIDER": "codex"}, {"UVICORN_WORKERS": "4"}, {"LLM_PROVIDER": "unknown"}):
            self.assertTrue(config.validate(env, hosted=False))

    def test_proxy_trust_and_packaged_receive_bound(self):
        env = {"JWT_SECRET": "x" * 32, "GOOGLE_API_KEY": "fixture"}
        for peer in ("*", "172.30.240.0/24", "172.30.240.3", "0.0.0.0"):
            failures = config.validate({**env, "FLOWPILOT_TRUSTED_PROXY_IPS": peer}, hosted=False)
            self.assertTrue(any("trusted proxies" in item for item in failures))
        self.assertEqual(config.validate({**env, "FLOWPILOT_TRUSTED_PROXY_IPS": "172.30.240.1"}, hosted=False), [])
        startup = (ROOT / "scripts" / "ops_start.sh").read_text()
        self.assertIn("--ws websockets --ws-max-size 65536 --ws-max-queue 16", startup)
        self.assertIn("--no-access-log", startup)

    def test_health_checks_enabled_composition(self):
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(503 if self.path.endswith("browser-tasks") else 200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with patch.dict(os.environ, {"FLOWPILOT_TASKS_ENABLED": "true"}):
                with self.assertRaises(Exception):
                    health.check(base)
            with patch.dict(os.environ, {"FLOWPILOT_TASKS_ENABLED": "false"}):
                health.check(base)
            self.assertEqual(paths, ["/health/browser-tasks", "/health"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_sqlite_wal_and_profile_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "profiles"
            source.mkdir()
            (source / "account-a").mkdir()
            (source / "account-a" / "profile.tar").write_bytes(b"private-profile-fixture")
            with sqlite3.connect(source / "platform.db") as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA user_version=7")
                db.execute("CREATE TABLE accounts (id TEXT)")
                db.execute("INSERT INTO accounts VALUES ('owner-a')")
                db.commit()
                manifest = backup.snapshot(source, root / "snapshot", "fixture-rev")
                self.assertEqual(manifest["schemas"], {"platform.db": 7})
                backup.verify(root / "snapshot", "fixture-rev")
                with sqlite3.connect(root / "snapshot" / "platform.db") as restored:
                    self.assertEqual(restored.execute("SELECT id FROM accounts").fetchone()[0], "owner-a")
                self.assertEqual((root / "snapshot" / "account-a" / "profile.tar").read_bytes(), b"private-profile-fixture")
                self.assertFalse((root / "snapshot" / "platform.db-wal").exists())
                with self.assertRaises(ValueError):
                    backup.verify(root / "snapshot", "wrong-rev")
                (root / "snapshot" / "account-a" / "profile.tar").write_bytes(b"corrupt")
                with self.assertRaises(ValueError):
                    backup.verify(root / "snapshot", "fixture-rev")

    def test_backup_rejects_symlinks_and_unsafe_destinations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                backup.outside(root, root / "backup")
            (root / "link").symlink_to("/etc/passwd")
            with self.assertRaises(ValueError):
                backup.snapshot(root, root.parent / (root.name + "-snapshot"), "rev")
            # Remove only this test's created snapshot.
            shutil.rmtree(root.parent / (root.name + "-snapshot"))

    def test_retention_dry_run_and_protected_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("old.trace", "0001_execute_reflex.call.json", "profile.json", "profile.tar", "platform.db", "recent.trace"):
                (root / name).write_text("fixture")
                os.utime(root / name, (0, 0))
            os.utime(root / "recent.trace", (200000, 200000))
            (root / "link.trace").symlink_to(root / "platform.db")
            self.assertEqual(retention.clean(root, 1, now=200000), 2)
            self.assertTrue((root / "old.trace").exists())
            self.assertEqual(retention.clean(root, 1, apply=True, now=200000), 2)
            self.assertFalse((root / "0001_execute_reflex.call.json").exists())
            self.assertTrue((root / "profile.json").exists())
            self.assertTrue((root / "profile.tar").exists())
            self.assertTrue((root / "platform.db").exists())
            self.assertTrue((root / "recent.trace").exists())

    def test_failed_backup_does_not_publish_or_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "profiles"
            source.mkdir()
            (source / "fixture").write_text("state")
            output = root / "backup.age"
            with patch.object(backup.subprocess, "run", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    backup.backup(source, output, "rev", "public-recipient")
            self.assertFalse(output.exists())
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["profiles"])
            output.write_bytes(b"previous-good-archive")
            with self.assertRaises(ValueError):
                backup.backup(source, output, "rev", "public-recipient")
            self.assertEqual(output.read_bytes(), b"previous-good-archive")

    def test_restore_rejects_traversal_before_extraction(self):
        import io
        import tarfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = root / "identity"
            identity.write_text("fixture")
            identity.chmod(0o600)

            def fake_decrypt(arguments, **kwargs):
                output = Path(arguments[arguments.index("-o") + 1])
                with tarfile.open(output, "w") as tar:
                    info = tarfile.TarInfo("state/../../escaped")
                    info.size = 1
                    tar.addfile(info, io.BytesIO(b"x"))

            with patch.object(backup.subprocess, "run", side_effect=fake_decrypt):
                with self.assertRaises(ValueError):
                    backup.restore(root / "unused.age", root / "new", "rev", identity)
            self.assertFalse((root / "new").exists())
            self.assertFalse((root / "escaped").exists())

    def test_runtime_low_disk_and_proxy_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            usage = shutil._ntuple_diskusage(100, 99, 1)
            with patch.object(config.shutil, "disk_usage", return_value=usage):
                failures = config.runtime_checks({"PROFILES_DIR": tmp, "DOCKER_HOST": "unix:///var/run/docker.sock"})
            self.assertTrue(any("DISK_LOW" in item for item in failures))
            self.assertTrue(any("private socket proxy" in item for item in failures))

    def test_deploy_commands_are_mocked_and_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copyfile(ROOT / "deploy", root / "deploy")
            (root / "docker-compose.yml").write_text("services: {}")
            log = root / "calls"
            (root / "docker").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$MOCK_LOG"\n')
            (root / "docker").chmod(0o700)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"], "MOCK_LOG": str(log)}
            env.pop("FLOWPILOT_OPERATOR_CONFIRM", None)
            for command in ("setup", "cleanup", "up", "stop", "restart"):
                result = subprocess.run(["bash", str(root / "deploy"), command], cwd=root, env=env, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
            self.assertFalse(log.exists())
            env["FLOWPILOT_OPERATOR_CONFIRM"] = "manual-host-operation"
            result = subprocess.run(["bash", str(root / "deploy"), "stop"], cwd=root, env=env, capture_output=True)
            self.assertEqual(result.returncode, 0)
            self.assertIn("stop --timeout 65", log.read_text())
            self.assertNotIn("rm", log.read_text())
            self.assertNotIn("prune", log.read_text())

    def test_packaged_settings_cover_contract(self):
        from backend.task_settings import TaskSettings
        compose = (ROOT / "docker-compose.yml").read_text()
        example = (ROOT / ".env.example").read_text()
        for field in TaskSettings.model_fields:
            if field == "state_dir":
                continue  # derived from PROFILES_DIR
            name = "FLOWPILOT_TASKS_" + field.upper()
            self.assertIn(name, compose)
            self.assertIn(name, example)

    def test_image_audit_gate_requires_justified_unexpired_acceptance(self):
        import datetime
        audit = load("ops_image_audit")
        feeds = {"chromium": "156.0.8078.47", "node": [("22.23.4", True), ("22.23.3", False), ("24.22.1", True)],
                 "python": ["3.12.15", "3.12.14", "3.13.9"]}
        report = {"Results": [
            {"Class": "os-pkgs", "Type": "debian", "Target": "img", "Vulnerabilities": [
                {"VulnerabilityID": "CVE-1", "PkgName": "libx", "InstalledVersion": "1", "Severity": "HIGH", "Status": "affected"},
                {"VulnerabilityID": "CVE-2", "PkgName": "liby", "InstalledVersion": "1", "FixedVersion": "2", "Severity": "CRITICAL", "Status": "fixed"},
                {"VulnerabilityID": "CVE-3", "PkgName": "libz", "InstalledVersion": "1", "Severity": "MEDIUM", "Status": "affected"}]},
            {"Class": "lang-pkgs", "Type": "python-pkg", "Target": "venv", "Vulnerabilities": [
                {"VulnerabilityID": "GHSA-1", "PkgName": "pkg", "InstalledVersion": "1", "Severity": "CRITICAL"}]}]}
        runtimes = audit.parse_runtimes("chromium\t/c\tGoogle Chrome for Testing 156.0.8078.4 \nnode\t/n\tv22.23.3\n"
                                        "node\t/m\tv22.23.4\npython\t/p\t3.12.15\n")
        self.assertEqual(runtimes["node"], {"/n": "22.23.3", "/m": "22.23.4"})
        findings, context = audit.normalize("sandbox", report, runtimes, feeds)
        self.assertEqual(context, {"python-pkg": {"CRITICAL": 1}})  # gated by ops_dependency_audit, not here
        ids = sorted(f["id"] for f in findings)
        self.assertEqual(ids, ["CHROMIUM-BEHIND-STABLE", "CVE-1", "CVE-2", "CVE-3", "NODE-SECURITY-RELEASE-MISSED"])
        today = datetime.date(2026, 10, 22)
        with tempfile.TemporaryDirectory() as tmp:
            allow = Path(tmp) / "allow.json"
            allow.write_text(json.dumps({"accepted": [
                {"id": "CVE-1", "package": "libx", "images": ["sandbox"], "justification": "unfixed upstream", "review_by": "2026-12-01"},
                {"id": "CVE-2", "package": "liby", "images": ["sandbox"], "justification": "", "review_by": "2026-12-01"},
                {"id": "NODE-SECURITY-RELEASE-MISSED", "package": "node", "images": ["sandbox"], "installed": "22.23.3",
                 "justification": "old", "review_by": "2026-10-01"},
                {"id": "CHROMIUM-BEHIND-STABLE", "package": "chromium", "images": ["backend"], "installed": "156.0.8078.4",
                 "justification": "other image only", "review_by": "2026-12-01"}]}))
            entries, problems = audit.load_allowlist(allow, today)
        self.assertEqual(len(problems), 2)  # empty justification and expired review date are rejected
        summary, unaccepted = audit.evaluate(findings, entries)
        self.assertEqual(sorted(f["id"] for f in unaccepted), ["CHROMIUM-BEHIND-STABLE", "CVE-2", "NODE-SECURITY-RELEASE-MISSED"])
        self.assertEqual(summary["sandbox"]["gated_accepted"], 1)
        self.assertEqual(summary["sandbox"]["by_severity"]["MEDIUM"], 1)  # reported, never gated
        chromium = next(f for f in findings if f["package"] == "chromium")
        pinned = dict(entries[("CHROMIUM-BEHIND-STABLE", "chromium")], images=["sandbox"])
        self.assertTrue(audit.accepted(chromium, {("CHROMIUM-BEHIND-STABLE", "chromium"): pinned}))
        # A runtime acceptance is bound to the exact version; any other build is re-gated.
        stale = dict(pinned, installed="155.0.1.1")
        self.assertFalse(audit.accepted(chromium, {("CHROMIUM-BEHIND-STABLE", "chromium"): stale}))


if __name__ == "__main__":
    unittest.main()
