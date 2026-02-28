"""Docker container management for browser sandboxes."""

import os
import re
import uuid
import secrets
import logging
import subprocess
from dataclasses import dataclass, field

import docker
from docker.errors import NotFound, APIError

logger = logging.getLogger(__name__)

SANDBOX_IMAGE = "uber-bookr-sandbox"
NOVNC_CONTAINER_PORT = 6080
CDP_CONTAINER_PORT = 9222
SANDBOX_NETWORK = "uber-bookr_sandbox-net"
MAX_SANDBOXES_PER_USER = 2
PROFILE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass
class SandboxInfo:
    sandbox_id: str
    container_id: str
    novnc_port: int
    cdp_port: int
    status: str
    user_id: str = ""
    vnc_password: str = ""


class SandboxManager:
    """Manages Docker containers that run headless Chromium + noVNC."""

    def __init__(self, profiles_host_dir: str = "/app/profiles"):
        self._client = None
        self.profiles_host_dir = profiles_host_dir
        self._sandboxes: dict[str, SandboxInfo] = {}
        self._user_sandboxes: dict[str, set[str]] = {}  # user_id -> {sandbox_id, ...}

    @property
    def client(self):
        if self._client is None:
            docker_host = os.environ.get("DOCKER_HOST")
            if docker_host:
                self._client = docker.DockerClient(base_url=docker_host)
            else:
                self._client = docker.from_env()
        return self._client

    def start(self, profile_path: str | None = None, user_id: str = "") -> dict:
        """Create and start a new sandbox container.

        Args:
            profile_path: Optional host path to a saved browser profile to restore.
            user_id: ID of the user requesting the sandbox.

        Returns:
            Dict with sandbox_id, novnc_url, cdp_url, and status.
        """
        # H2: Per-user sandbox limit
        if user_id:
            current = self._user_sandboxes.get(user_id, set())
            # Clean up stopped sandboxes from tracking
            active = {sid for sid in current if sid in self._sandboxes}
            self._user_sandboxes[user_id] = active
            if len(active) >= MAX_SANDBOXES_PER_USER:
                raise RuntimeError(
                    f"User {user_id} already has {len(active)} active sandboxes (max {MAX_SANDBOXES_PER_USER})"
                )

        sandbox_id = str(uuid.uuid4())[:12]

        # C4: Random VNC password per sandbox
        vnc_password = secrets.token_urlsafe(12)

        environment = {
            "VNC_PASSWORD": vnc_password,
            "DISPLAY": ":1",
        }

        volumes = {
            self.profiles_host_dir: {"bind": "/profiles", "mode": "rw"},
        }

        try:
            container = self.client.containers.run(
                image=SANDBOX_IMAGE,
                name=f"uber-bookr-{sandbox_id}",
                detach=True,
                ports={
                    f"{NOVNC_CONTAINER_PORT}/tcp": None,  # random host port — noVNC only
                    # C2: CDP port NOT published to host; accessed via sandbox-net
                },
                volumes=volumes,
                environment=environment,
                network=SANDBOX_NETWORK,
                labels={
                    "uber-bookr": "sandbox",
                    "sandbox-id": sandbox_id,
                    "user-id": user_id,
                },
                # H1: Resource limits
                mem_limit="1g",
                cpu_quota=100000,
                pids_limit=256,
                shm_size="256m",
            )
        except APIError as e:
            logger.error("Failed to start sandbox container: %s", e)
            raise RuntimeError(f"Failed to start sandbox: {e}") from e

        # Reload to get port mappings
        container.reload()

        novnc_port = self._get_host_port(container, NOVNC_CONTAINER_PORT)

        # C2: CDP accessible via container name on the internal network
        cdp_host = f"uber-bookr-{sandbox_id}"
        cdp_port = CDP_CONTAINER_PORT  # internal port, not published

        # If a profile was provided, copy it into the container
        if profile_path:
            try:
                subprocess.run(
                    ["docker", "cp", f"{profile_path}/.", f"{container.id}:/tmp/chrome-profile/"],
                    check=True,
                    capture_output=True,
                )
                # Fix ownership for non-root user
                container.exec_run("chown -R sandboxuser:sandboxuser /tmp/chrome-profile")
                logger.info("Restored profile from %s into sandbox %s", profile_path, sandbox_id)
            except subprocess.CalledProcessError as e:
                logger.warning("Failed to restore profile: %s", e)

        info = SandboxInfo(
            sandbox_id=sandbox_id,
            container_id=container.id,
            novnc_port=novnc_port,
            cdp_port=cdp_port,
            status="running",
            user_id=user_id,
            vnc_password=vnc_password,
        )
        self._sandboxes[sandbox_id] = info

        # Track per-user
        if user_id:
            self._user_sandboxes.setdefault(user_id, set()).add(sandbox_id)

        novnc_url = self.get_novnc_url(sandbox_id)
        cdp_url = f"ws://{cdp_host}:{cdp_port}"

        logger.info(
            "Started sandbox %s (container %s) — noVNC %s, CDP %s",
            sandbox_id, container.short_id, novnc_url, cdp_url,
        )

        return {
            "sandbox_id": sandbox_id,
            "novnc_url": novnc_url,
            "cdp_url": cdp_url,
            "status": "running",
            "vnc_password": vnc_password,
        }

    def stop(self, sandbox_id: str) -> None:
        """Stop and remove a sandbox container."""
        info = self._sandboxes.get(sandbox_id)
        if not info:
            raise ValueError(f"Unknown sandbox: {sandbox_id}")

        try:
            container = self.client.containers.get(info.container_id)
            container.stop(timeout=5)
            container.remove(force=True)
        except NotFound:
            logger.warning("Container for sandbox %s already gone", sandbox_id)
        except APIError as e:
            logger.error("Error stopping sandbox %s: %s", sandbox_id, e)
            raise

        # Clean up user tracking
        if info.user_id and info.user_id in self._user_sandboxes:
            self._user_sandboxes[info.user_id].discard(sandbox_id)

        info.status = "stopped"
        del self._sandboxes[sandbox_id]
        logger.info("Stopped sandbox %s", sandbox_id)

    def save_profile(self, sandbox_id: str, profile_name: str) -> str:
        """Copy the Chromium profile out of a running container.

        Returns the host path where the profile was saved.
        """
        from pathlib import Path

        info = self._sandboxes.get(sandbox_id)
        if not info:
            raise ValueError(f"Unknown sandbox: {sandbox_id}")

        dest = Path(self.profiles_host_dir) / profile_name
        dest.mkdir(parents=True, exist_ok=True)

        chrome_profile_path = "/tmp/chrome-profile"

        try:
            subprocess.run(
                ["docker", "cp", f"{info.container_id}:{chrome_profile_path}/.", str(dest)],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            logger.error("Failed to save profile: %s", e.stderr.decode())
            raise RuntimeError(f"Failed to save profile: {e}") from e

        logger.info("Saved profile '%s' from sandbox %s to %s", profile_name, sandbox_id, dest)
        return str(dest)

    def get_novnc_url(self, sandbox_id: str) -> str:
        """Return the noVNC URL for a sandbox."""
        info = self._sandboxes.get(sandbox_id)
        if not info:
            raise ValueError(f"Unknown sandbox: {sandbox_id}")
        return f"http://localhost:{info.novnc_port}/vnc.html?autoconnect=true&password={info.vnc_password}&resize=scale"

    def get_cdp_url(self, sandbox_id: str) -> str:
        """Return the Chrome DevTools Protocol WebSocket URL."""
        info = self._sandboxes.get(sandbox_id)
        if not info:
            raise ValueError(f"Unknown sandbox: {sandbox_id}")
        return f"ws://localhost:{info.cdp_port}"

    def is_running(self, sandbox_id: str) -> bool:
        """Check if a sandbox container is still running."""
        info = self._sandboxes.get(sandbox_id)
        if not info:
            return False
        try:
            container = self.client.containers.get(info.container_id)
            return container.status == "running"
        except NotFound:
            return False

    def get_sandbox_owner(self, sandbox_id: str) -> str:
        """Return the user_id that owns this sandbox."""
        info = self._sandboxes.get(sandbox_id)
        if not info:
            raise ValueError(f"Unknown sandbox: {sandbox_id}")
        return info.user_id

    def cleanup_orphans(self) -> int:
        """H3: Stop and remove any orphaned uber-bookr sandbox containers."""
        removed = 0
        try:
            containers = self.client.containers.list(
                all=True,
                filters={"label": "uber-bookr=sandbox"},
            )
            for container in containers:
                sandbox_id = container.labels.get("sandbox-id", "unknown")
                if sandbox_id not in self._sandboxes:
                    logger.info("Cleaning up orphaned sandbox container: %s (%s)", container.name, sandbox_id)
                    try:
                        container.stop(timeout=5)
                        container.remove(force=True)
                        removed += 1
                    except Exception as e:
                        logger.warning("Failed to remove orphan %s: %s", container.name, e)
        except Exception as e:
            logger.error("Failed to list containers for orphan cleanup: %s", e)
        return removed

    def stop_all(self) -> None:
        """Stop all tracked sandboxes (for graceful shutdown)."""
        sandbox_ids = list(self._sandboxes.keys())
        for sandbox_id in sandbox_ids:
            try:
                self.stop(sandbox_id)
            except Exception as e:
                logger.warning("Failed to stop sandbox %s during shutdown: %s", sandbox_id, e)

    @staticmethod
    def validate_profile_name(profile_name: str) -> bool:
        """H5: Validate profile name is safe (alphanumeric + hyphens + underscores)."""
        return bool(PROFILE_NAME_PATTERN.match(profile_name))

    def _get_host_port(self, container, container_port: int) -> int:
        """Extract the mapped host port from a container's port bindings."""
        port_key = f"{container_port}/tcp"
        ports = container.ports.get(port_key)
        if not ports:
            raise RuntimeError(f"No host port mapped for container port {container_port}")
        return int(ports[0]["HostPort"])
