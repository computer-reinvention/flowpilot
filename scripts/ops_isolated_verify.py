"""Disposable-container rehearsal: real age round trip, Caddy parse, nft check.

Run with --network none --cap-add NET_ADMIN; never mount host state or Docker socket.
This is NOT an end-to-end sandbox network proof or production readiness attestation.
"""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs)


def main():
    os.umask(0o077)
    scripts = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("ops_backup", scripts / "ops_backup.py")
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        identity = root / "identity"
        run("age-keygen", "-o", str(identity))
        recipient = run("age-keygen", "-y", str(identity)).stdout.decode().strip()
        source = root / "profiles"
        source.mkdir()
        (source / "account-a").mkdir()
        (source / "account-b").mkdir()
        (source / "account-a" / "profile.tar").write_bytes(b"profile-a-fixture")
        (source / "account-b" / "profile.tar").write_bytes(b"profile-b-fixture")
        with sqlite3.connect(source / "platform.db") as db:
            db.execute("CREATE TABLE tasks (id TEXT, state TEXT)")
            db.execute("INSERT INTO tasks VALUES ('fixture-task', 'interrupted')")
            db.commit()
        output = root / "backup.age"
        backup.backup(source, output, "fixture-revision", recipient)
        assert b"profile-a-fixture" not in output.read_bytes()
        restored = root / "restored"
        backup.restore(output, restored, "fixture-revision", identity)
        assert (restored / "account-a" / "profile.tar").read_bytes() == b"profile-a-fixture"
        assert (restored / "account-b" / "profile.tar").read_bytes() == b"profile-b-fixture"
        with sqlite3.connect(restored / "platform.db") as db:
            assert db.execute("SELECT state FROM tasks").fetchone()[0] == "interrupted"
        before = output.read_bytes()
        try:
            backup.backup(source, output, "fixture-revision", recipient)
            raise AssertionError("overwrote archive")
        except ValueError:
            pass
        assert output.read_bytes() == before
        output.write_bytes(before[:-20] + b"CORRUPT")
        try:
            backup.restore(output, root / "corrupt-restore", "fixture-revision", identity)
            raise AssertionError("accepted corruption")
        except subprocess.CalledProcessError:
            pass
        assert not (root / "corrupt-restore").exists()
    run("caddy", "validate", "--config", str(scripts / "ops_Caddyfile"), "--adapter", "caddyfile", env={**os.environ, "FLOWPILOT_DOMAIN": "http://localhost:18080"})
    result = subprocess.run(["nft", "--check", "--file", str(scripts / "ops_network_policy.nft")], capture_output=True)
    print(json.dumps({"age_restore_records_profiles_tamper_no_clobber": "PASS", "caddy_parse": "PASS", "nft_namespace_check": "PASS" if result.returncode == 0 else "BLOCKED", "nft_error": result.stderr.decode()[:1500], "sandbox_reachability": "UNVERIFIED"}, indent=2))
    return bool(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
