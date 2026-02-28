"""Browser profile management — save/restore Chromium profiles to/from sandbox containers."""

import os
import logging
import json
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
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = docker.from_env()
        return self._client

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
        """Check if a saved profile exists (tar archive or imported cookies)."""
        profile_dir = self.profiles_dir / profile_name
        archive = profile_dir / "profile.tar.gz"
        cookies = profile_dir / "cookies.json"
        return archive.exists() or cookies.exists()

    def get_profile_info(self, profile_name: str) -> dict:
        """Get metadata about a saved profile.

        Returns:
            Dict with name, exists, size_bytes, last_modified, source (tar or extension).
        """
        profile_dir = self.profiles_dir / profile_name
        archive_path = profile_dir / "profile.tar.gz"
        cookies_path = profile_dir / "cookies.json"

        # Check tar archive first, then cookies.json from extension import
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

    def list_profiles(self) -> list[dict]:
        """List all saved profiles."""
        profiles = []
        if not self.profiles_dir.exists():
            return profiles

        for entry in sorted(self.profiles_dir.iterdir()):
            if entry.is_dir() and ((entry / "profile.tar.gz").exists() or (entry / "cookies.json").exists()):
                profiles.append(self.get_profile_info(entry.name))

        return profiles

    # ------------------------------------------------------------------
    # Extension-based session import
    # ------------------------------------------------------------------

    def import_session(self, profile_name: str, session_data: dict) -> Path:
        """Import session data captured by the Chrome extension.

        Saves cookies as cookies.json (compatible with Playwright add_cookies)
        and the full session payload as session.json.

        Args:
            profile_name: Name to save the profile under.
            session_data: Dict with cookies, localStorage, sessionStorage, url, userAgent, timestamp.

        Returns:
            Path to the profile directory.
        """
        profile_dir = self.profiles_dir / profile_name
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Save cookies as a list suitable for Playwright's add_cookies()
        raw_cookies = session_data.get("cookies", [])
        playwright_cookies = []
        for c in raw_cookies:
            cookie = {
                "name": c.get("name", ""),
                "value": c.get("value", ""),
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
            }
            # Playwright wants 'url' or 'domain'. Domain from Chrome cookies API starts with '.'
            # which Playwright handles fine.
            if c.get("secure"):
                cookie["secure"] = True
            if c.get("httpOnly"):
                cookie["httpOnly"] = True
            if c.get("sameSite"):
                # Chrome uses "lax"/"strict"/"no_restriction"; Playwright uses "Lax"/"Strict"/"None"
                sam = c["sameSite"].lower()
                if sam == "no_restriction":
                    cookie["sameSite"] = "None"
                elif sam == "lax":
                    cookie["sameSite"] = "Lax"
                elif sam == "strict":
                    cookie["sameSite"] = "Strict"
            if c.get("expirationDate"):
                cookie["expires"] = c["expirationDate"]
            playwright_cookies.append(cookie)

        cookies_path = profile_dir / "cookies.json"
        cookies_path.write_text(json.dumps(playwright_cookies, indent=2))

        # Save full session data as backup
        session_path = profile_dir / "session.json"
        session_path.write_text(json.dumps(session_data, indent=2))

        # Write metadata
        meta_path = profile_dir / "metadata.txt"
        meta_path.write_text(
            f"profile_name={profile_name}\n"
            f"source=extension\n"
            f"imported_at={datetime.now(timezone.utc).isoformat()}\n"
            f"url={session_data.get('url', '')}\n"
            f"cookie_count={len(playwright_cookies)}\n"
        )

        logger.info(
            "Imported session for profile '%s': %d cookies, %d localStorage keys, %d sessionStorage keys",
            profile_name,
            len(playwright_cookies),
            len(session_data.get("localStorage", {})),
            len(session_data.get("sessionStorage", {})),
        )
        return profile_dir

    def get_session_cookies(self, profile_name: str) -> list[dict] | None:
        """Load cookies from an imported session profile.

        Returns:
            List of cookie dicts suitable for Playwright's add_cookies(), or None if not found.
        """
        cookies_path = self.profiles_dir / profile_name / "cookies.json"
        if not cookies_path.exists():
            return None
        return json.loads(cookies_path.read_text())

    def get_session_storage(self, profile_name: str) -> dict | None:
        """Load localStorage and sessionStorage from an imported session profile.

        Returns:
            Dict with 'localStorage' and 'sessionStorage' keys, or None if not found.
        """
        session_path = self.profiles_dir / profile_name / "session.json"
        if not session_path.exists():
            return None
        data = json.loads(session_path.read_text())
        return {
            "localStorage": data.get("localStorage", {}),
            "sessionStorage": data.get("sessionStorage", {}),
        }
