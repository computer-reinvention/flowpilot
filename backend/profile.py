"""Browser profile management — save/restore Chromium profiles to/from sandbox containers."""

import os
import logging
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import docker
from docker.errors import NotFound

logger = logging.getLogger(__name__)

PROFILES_DIR = Path(os.environ.get("PROFILES_DIR", "/app/profiles"))
CHROME_PROFILE_CONTAINER_PATH = "/tmp/chrome-profile"


class ProfileManager:
    """Save and restore Chromium profiles between sandbox containers and host storage."""

    def __init__(self, profiles_dir: Path | None = None):
        self.profiles_dir = profiles_dir or PROFILES_DIR
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.client = docker.from_env()

    def save_profile(self, sandbox_id: str, profile_name: str) -> Path:
        """Extract the Chrome profile directory from a running container and save as tar.gz.

        Args:
            sandbox_id: The sandbox container name suffix (container is named uber-bookr-{sandbox_id}).
            profile_name: Name to save the profile under.

        Returns:
            Path to the saved profile directory.
        """
        container_name = f"uber-bookr-{sandbox_id}"
        profile_dir = self.profiles_dir / profile_name
        profile_dir.mkdir(parents=True, exist_ok=True)
        archive_path = profile_dir / "profile.tar.gz"

        try:
            container = self.client.containers.get(container_name)
        except NotFound:
            raise ValueError(f"Container {container_name} not found")

        # Get archive stream from container
        bits, _stat = container.get_archive(CHROME_PROFILE_CONTAINER_PATH)

        # Write the raw tar stream to a temporary file, then repack as gzip
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=True) as tmp:
            for chunk in bits:
                tmp.write(chunk)
            tmp.flush()
            tmp.seek(0)

            # Re-create as gzip-compressed tar
            with tarfile.open(tmp.name, "r:") as src_tar:
                with tarfile.open(str(archive_path), "w:gz") as dst_tar:
                    for member in src_tar.getmembers():
                        f = src_tar.extractfile(member)
                        if f is not None:
                            dst_tar.addfile(member, f)
                        else:
                            dst_tar.addfile(member)

        # Write metadata
        meta_path = profile_dir / "metadata.txt"
        meta_path.write_text(
            f"profile_name={profile_name}\n"
            f"sandbox_id={sandbox_id}\n"
            f"saved_at={datetime.now(timezone.utc).isoformat()}\n"
        )

        logger.info("Saved profile '%s' from sandbox %s (%s)", profile_name, sandbox_id, archive_path)
        return profile_dir

    def restore_profile(self, profile_name: str, sandbox_id: str) -> bool:
        """Untar a saved profile into a running container's Chrome profile directory.

        Args:
            profile_name: Name of the saved profile.
            sandbox_id: Target sandbox to restore into.

        Returns:
            True if successful, False if profile doesn't exist.
        """
        profile_dir = self.profiles_dir / profile_name
        archive_path = profile_dir / "profile.tar.gz"

        if not archive_path.exists():
            logger.warning("No saved profile found at %s", archive_path)
            return False

        container_name = f"uber-bookr-{sandbox_id}"

        try:
            container = self.client.containers.get(container_name)
        except NotFound:
            raise ValueError(f"Container {container_name} not found")

        # Ensure the parent directory exists in container
        container.exec_run(f"mkdir -p {CHROME_PROFILE_CONTAINER_PATH}")

        # docker SDK put_archive expects a raw tar stream, so we decompress gzip first
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=True) as tmp:
            with tarfile.open(str(archive_path), "r:gz") as gz:
                with tarfile.open(tmp.name, "w:") as plain:
                    for member in gz.getmembers():
                        f = gz.extractfile(member)
                        if f is not None:
                            plain.addfile(member, f)
                        else:
                            plain.addfile(member)

            with open(tmp.name, "rb") as tar_data:
                # Put archive into the parent of the profile dir
                parent = str(Path(CHROME_PROFILE_CONTAINER_PATH).parent)
                container.put_archive(parent, tar_data.read())

        logger.info("Restored profile '%s' into sandbox %s", profile_name, sandbox_id)
        return True

    def has_profile(self, profile_name: str) -> bool:
        """Check if a saved profile exists."""
        archive = self.profiles_dir / profile_name / "profile.tar.gz"
        return archive.exists()

    def get_profile_info(self, profile_name: str) -> dict:
        """Get metadata about a saved profile.

        Returns:
            Dict with name, exists, size_bytes, last_modified.
        """
        profile_dir = self.profiles_dir / profile_name
        archive_path = profile_dir / "profile.tar.gz"

        if not archive_path.exists():
            return {
                "name": profile_name,
                "exists": False,
                "size_bytes": None,
                "last_modified": None,
            }

        stat = archive_path.stat()
        return {
            "name": profile_name,
            "exists": True,
            "size_bytes": stat.st_size,
            "last_modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        }

    def list_profiles(self) -> list[dict]:
        """List all saved profiles."""
        profiles = []
        if not self.profiles_dir.exists():
            return profiles

        for entry in sorted(self.profiles_dir.iterdir()):
            if entry.is_dir() and (entry / "profile.tar.gz").exists():
                profiles.append(self.get_profile_info(entry.name))

        return profiles
