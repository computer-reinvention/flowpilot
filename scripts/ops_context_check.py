"""Build synthetic secret-canary context; scan saved image layers, not host secrets."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    tag = "flowpilot-lane005-context:" + uuid.uuid4().hex
    marker = ("LANE005_SYNTHETIC_SECRET_" + uuid.uuid4().hex).encode()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        shutil.copyfile(ROOT / ".dockerignore", root / ".dockerignore")
        for name in (".env", ".env.production", ".pi-auth/auth.json", "profiles/private", "traces/private", ".repl-state/private", "backend/.env.local", "nested/.pi-auth/auth.json", "nested/profiles/private", "nested/credentials.key", "backups/state.age", "repl.local.json"):
            file = root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(marker)
        (root / "allowed.txt").write_text("public-fixture")
        (root / "Dockerfile").write_text("FROM scratch\nCOPY . /context/\nLABEL flowpilot.ops=lane005\n")
        try:
            subprocess.run(["docker", "build", "-t", tag, str(root)], check=True, stdout=subprocess.DEVNULL)
            archive = root / "image.tar"
            with archive.open("wb") as target:
                subprocess.run(["docker", "image", "save", tag], stdout=target, check=True)
            with tarfile.open(archive) as tar:
                for member in tar.getmembers():
                    if member.isfile():
                        assert marker not in tar.extractfile(member).read(), "secret canary reached image content/layers"
            print("PASS: synthetic secret canaries excluded from saved image/layers")
        finally:
            subprocess.run(["docker", "image", "rm", tag], stdout=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    main()
