"""Browser profile management — save/restore Chromium profiles to/from sandbox containers."""

import logging
import os
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import docker
from docker.errors import NotFound

from .config import PROFILES_DIR

logger = logging.getLogger(__name__)
CHROME_PROFILE_CONTAINER_PATH = "/tmp/chrome-profile"
PROFILE_ARCHIVE_FILENAME = "profile.tar.gz"
PROFILE_COOKIES_FILENAME = "cookies.json"


class ProfileManager:
    """Save and restore Chromium profiles between sandbox containers and host storage."""

    def __init__(self, profiles_dir: Path | None = None):
        self.profiles_dir = profiles_dir or PROFILES_DIR
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self._client = None

    @property
    def client(self):
        if self._client is None:
            docker_host = os.environ.get("DOCKER_HOST")
            if docker_host:
                self._client = docker.DockerClient(base_url=docker_host)
            else:
                self._client = docker.from_env()
        return self._client

    def _user_profile_dir(self, user_id: str, profile_name: str) -> Path:
        """Get the profile directory for a user: profiles_dir / user_id / profile_name."""
        return self.profiles_dir / user_id / profile_name

    def save_profile(self, sandbox_id: str, profile_name: str, user_id: str, retries: int = 2) -> Path:
        """Extract the Chrome profile directory from a running container and save as tar.gz.

        Retries on transient Docker/network errors. Raises on persistent failure.
        """
        container_name = f"flowpilot-{sandbox_id}"
        profile_dir = self._user_profile_dir(user_id, profile_name)
        profile_dir.mkdir(parents=True, exist_ok=True)
        archive_path = profile_dir / PROFILE_ARCHIVE_FILENAME

        last_err: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                container = self.client.containers.get(container_name)
                # Verify container is still running
                container.reload()
                if container.status != "running":
                    raise RuntimeError(f"Container {container_name} is {container.status}, not running")

                bits, _stat = container.get_archive(CHROME_PROFILE_CONTAINER_PATH)

                with tempfile.NamedTemporaryFile(suffix=".tar", delete=True) as tmp:
                    for chunk in bits:
                        tmp.write(chunk)
                    tmp.flush()
                    tmp.seek(0)

                    with tarfile.open(tmp.name, "r:") as src_tar:
                        with tarfile.open(str(archive_path), "w:gz") as dst_tar:
                            for member in src_tar.getmembers():
                                if member.name.startswith('..') or member.name.startswith('/'):
                                    raise ValueError(f'Unsafe tar member: {member.name}')
                                f = src_tar.extractfile(member)
                                if f is not None:
                                    dst_tar.addfile(member, f)
                                else:
                                    dst_tar.addfile(member)

                # Verify the archive was actually written
                if not archive_path.exists() or archive_path.stat().st_size == 0:
                    raise RuntimeError("Archive file is empty after write")

                meta_path = profile_dir / "metadata.txt"
                meta_path.write_text(
                    f"profile_name={profile_name}\n"
                    f"user_id={user_id}\n"
                    f"sandbox_id={sandbox_id}\n"
                    f"saved_at={datetime.now(timezone.utc).isoformat()}\n"
                )

                logger.info("Saved profile '%s' for user %s from sandbox %s", profile_name, user_id, sandbox_id)
                return profile_dir

            except NotFound:
                # Container gone — no point retrying
                logger.warning("Profile save failed: container %s not found", container_name)
                raise
            except Exception as e:
                last_err = e
                logger.warning("Profile save attempt %d/%d failed: %s", attempt, retries, e)
                if attempt < retries:
                    time.sleep(0.5)

        raise RuntimeError(f"Profile save failed after {retries} attempts: {last_err}")

    def restore_profile(self, user_id: str, profile_name: str, sandbox_id: str) -> bool:
        """Untar a saved profile into a running container's Chrome profile directory."""
        profile_dir = self._user_profile_dir(user_id, profile_name)
        archive_path = profile_dir / PROFILE_ARCHIVE_FILENAME

        if not archive_path.exists():
            logger.warning("No saved profile found at %s", archive_path)
            return False

        container_name = f"flowpilot-{sandbox_id}"

        try:
            container = self.client.containers.get(container_name)
        except NotFound:
            raise ValueError(f"Container {container_name} not found")

        container.exec_run(f"mkdir -p {CHROME_PROFILE_CONTAINER_PATH}")

        with tempfile.NamedTemporaryFile(suffix=".tar", delete=True) as tmp:
            with tarfile.open(str(archive_path), "r:gz") as gz:
                with tarfile.open(tmp.name, "w:") as plain:
                    for member in gz.getmembers():
                        if member.name.startswith('..') or member.name.startswith('/'):
                            raise ValueError(f'Unsafe tar member: {member.name}')
                        f = gz.extractfile(member)
                        if f is not None:
                            plain.addfile(member, f)
                        else:
                            plain.addfile(member)

            with open(tmp.name, "rb") as tar_data:
                parent = str(Path(CHROME_PROFILE_CONTAINER_PATH).parent)
                container.put_archive(parent, tar_data.read())

        logger.info("Restored profile '%s' for user %s into sandbox %s", profile_name, user_id, sandbox_id)
        return True

    def list_profiles(self, user_id: str) -> list[dict]:
        """List all profiles for a user."""
        user_dir = self.profiles_dir / user_id
        if not user_dir.is_dir():
            return []
        profiles = []
        for child in sorted(user_dir.iterdir()):
            if child.is_dir():
                info = self.get_profile_info(user_id, child.name)
                if info.get("exists"):
                    profiles.append(info)
        return profiles

    def delete_profile(self, user_id: str, profile_name: str) -> bool:
        """Delete a profile directory."""
        import shutil
        profile_dir = self._user_profile_dir(user_id, profile_name)
        if not profile_dir.is_dir():
            return False
        shutil.rmtree(profile_dir)
        logger.info("Deleted profile '%s' for user %s", profile_name, user_id)
        return True

    def has_profile(self, user_id: str, profile_name: str) -> bool:
        """Check if a saved profile exists (tar archive or imported cookies)."""
        profile_dir = self._user_profile_dir(user_id, profile_name)
        archive = profile_dir / PROFILE_ARCHIVE_FILENAME
        cookies = profile_dir / PROFILE_COOKIES_FILENAME
        return archive.exists() or cookies.exists()

    def get_profile_info(self, user_id: str, profile_name: str) -> dict:
        """Get metadata about a saved profile."""
        profile_dir = self._user_profile_dir(user_id, profile_name)
        archive_path = profile_dir / PROFILE_ARCHIVE_FILENAME
        cookies_path = profile_dir / PROFILE_COOKIES_FILENAME

        check_path = None
        source = None
        if archive_path.exists():
            check_path = archive_path
            source = "tar"
        elif cookies_path.exists():
            check_path = cookies_path
            source = "extension"

        if check_path is None:
            return {
                "name": profile_name,
                "exists": False,
                "size_bytes": None,
                "last_modified": None,
                "source": None,
            }

        stat = check_path.stat()
        return {
            "name": profile_name,
            "exists": True,
            "size_bytes": stat.st_size,
            "last_modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "source": source,
        }


