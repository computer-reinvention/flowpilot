"""Build synthetic secret-canary context; scan saved image layers, not host secrets."""
import io
import gzip
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
        for name in (".env", ".env.production", ".pi-auth/auth.json", "profiles/private", "traces/private", ".repl-state/private", "backend/.env.local", "nested/.pi-auth/auth.json", "nested/profiles/private", "nested/credentials.key", "backups/state.age", "repl.local.json", "frontend/src/components/profiles/.env"):
            file = root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(marker)
        (root / "allowed.txt").write_text("public-fixture")
        (root / "frontend/src/components/profiles/fixture.tsx").write_text("public-source-fixture")
        (root / "Dockerfile").write_text("FROM scratch\nCOPY . /context/\nLABEL flowpilot.ops=lane005\n")
        try:
            subprocess.run(["docker", "build", "-t", tag, str(root)], check=True, stdout=subprocess.DEVNULL)
            archive = root / "image.tar"
            with archive.open("wb") as target:
                subprocess.run(["docker", "image", "save", tag], stdout=target, check=True)
            public_seen = source_seen = False
            with tarfile.open(archive) as tar:
                for member in tar.getmembers():
                    if member.isfile():
                        data = tar.extractfile(member).read()
                        if data.startswith(b"\x1f\x8b"):
                            data = gzip.decompress(data)
                        assert marker not in data, "secret canary reached image content/layers"
                        public_seen |= b"public-fixture" in data
                        source_seen |= b"public-source-fixture" in data
            assert public_seen and source_seen, "canary inspection did not read real source-containing layers"
            print("PASS: synthetic secret canaries excluded from saved image/layers")
        finally:
            subprocess.run(["docker", "image", "rm", tag], stdout=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    main()
