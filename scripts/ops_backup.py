"""Offline/quiesced state backup. Uses SQLite backup API and age encryption.

Stop admission AND all profile writers before invoking; no Docker commands here.
Restore only into a NEW isolated directory with the exact recorded revision.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def outside(source, destination):
    if destination.resolve().is_relative_to(source.resolve()):
        raise ValueError("recovery artifacts must be outside browser/profile state")


def snapshot(source, target, revision):
    source = source.resolve(strict=True)
    target.mkdir(mode=0o700)
    files = {}
    schemas = {}
    for item in sorted(source.rglob("*")):
        if item.is_symlink():
            raise ValueError("symlinks in profile state are not supported")
        if not item.is_file():
            continue
        rel = item.relative_to(source)
        if item.name.endswith(("-wal", "-shm", "-journal")):
            continue
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with item.open("rb") as stream:
            is_db = stream.read(16) == b"SQLite format 3\x00"
        if is_db:
            with closing(sqlite3.connect(item.as_uri() + "?mode=ro", uri=True)) as src, closing(sqlite3.connect(dest)) as dst:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("SQLite integrity check failed")
                schemas[str(rel)] = dst.execute("PRAGMA user_version").fetchone()[0]
        else:
            shutil.copyfile(item, dest)
        dest.chmod(0o600)
        files[str(rel)] = digest(dest)
    manifest = {"format": 1, "revision": revision, "sqlite": sqlite3.sqlite_version, "schemas": schemas, "files": files}
    (target / "ops-manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    (target / "ops-manifest.json").chmod(0o600)
    return manifest


def verify(root, revision):
    manifest = json.loads((root / "ops-manifest.json").read_text())
    if manifest["format"] != 1 or manifest["revision"] != revision:
        raise ValueError("restore requires exact backup format and application revision")
    for name, expected in manifest["files"].items():
        file = root / name
        if not file.resolve().is_relative_to(root.resolve()) or file.is_symlink() or digest(file) != expected:
            raise ValueError("backup checksum/path mismatch")
    for name, version in manifest["schemas"].items():
        with closing(sqlite3.connect((root / name).as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA user_version").fetchone()[0] != version:
                raise ValueError("restored SQLite schema/integrity mismatch")
    return manifest


def backup(source, output, revision, recipient):
    outside(source, output)
    if output.exists():
        raise ValueError("never overwrite a previous known-good archive")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.parent.stat().st_mode & 0o077:
        raise ValueError("backup parent must be mode 0700")
    # Restricted temp state stays outside browser mounts, on the backup filesystem.
    with tempfile.TemporaryDirectory(prefix=".ops-backup-", dir=output.parent) as tmp:
        tmp = Path(tmp)
        state = tmp / "state"
        snapshot(source, state, revision)
        archive = tmp / "state.tar"
        with tarfile.open(archive, "w") as tar:
            tar.add(state, arcname="state")
        encrypted = tmp / "encrypted.age"
        subprocess.run(["age", "-r", recipient, "-o", str(encrypted), str(archive)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        encrypted.chmod(0o600)
        # Atomic no-clobber publication; partial encryption cannot replace good archives.
        os.link(encrypted, output)


def restore(archive, destination, revision, identity):
    if destination.exists():
        raise ValueError("restore target must not exist")
    if identity.stat().st_mode & 0o077:
        raise ValueError("age identity must be mode 0600")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".ops-restore-", dir=destination.parent) as tmp:
        tmp = Path(tmp)
        plain = tmp / "state.tar"
        subprocess.run(["age", "-d", "-i", str(identity), "-o", str(plain), str(archive)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with tarfile.open(plain) as tar:
            for member in tar.getmembers():
                path = Path(member.name)
                if path.is_absolute() or ".." in path.parts or path.parts[0] != "state" or not (member.isfile() or member.isdir()):
                    raise ValueError("unsafe backup member")
            tar.extractall(tmp, filter="data")
        verify(tmp / "state", revision)
        os.rename(tmp / "state", destination)


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["backup", "restore"])
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--quiesced", action="store_true")
    parser.add_argument("--recipient")
    parser.add_argument("--identity", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "backup":
            if not args.quiesced or not args.recipient:
                parser.error("backup requires --quiesced and public age --recipient")
            backup(args.source, args.destination, args.revision, args.recipient)
        else:
            if not args.identity:
                parser.error("restore requires an offline --identity")
            restore(args.source, args.destination, args.revision, args.identity)
        print("Recovery artifact operation completed; no service started")
    except Exception:
        raise SystemExit("Recovery operation failed; details suppressed to protect state")
