"""Docker container management for browser sandboxes."""

import uuid
import logging
import subprocess
from dataclasses import dataclass, field

import docker
from docker.errors import NotFound, APIError

logger = logging.getLogger(__name__)

SANDBOX_IMAGE = "uber-bookr-sandbox"
NOVNC_CONTAINER_PORT = 6080
CDP_CONTAINER_PORT = 9222


@dataclass
class SandboxInfo:
    sandbox_id: str
    container_id: str
    novnc_port: int
    cdp_port: int
    status: str


class SandboxManager:
    """Manages Docker containers that run headless Chromium + noVNC."""

    def __init__(self, profiles_host_dir: str = "/app/profiles"):
        self._client = None
        self.profiles_host_dir = profiles_host_dir
        self._sandboxes: dict[str, SandboxInfo] = {}

    @property
    def client(self):
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def start(self, profile_path: str | None = None) -> dict:
        """Create and start a new sandbox container.

        Args:
            profile_path: Optional host path to a saved browser profile to restore.

        Returns:
            Dict with sandbox_id, novnc_url, cdp_url, and status.
        """
        sandbox_id = str(uuid.uuid4())[:12]

        environment = {
            "VNC_PASSWORD": "bookr",
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
                    f"{NOVNC_CONTAINER_PORT}/tcp": None,  # random host port
                    f"{CDP_CONTAINER_PORT}/tcp": None,     # random host port for CDP
                },
                volumes=volumes,
                environment=environment,
                network_mode="bridge",
                labels={"uber-bookr": "sandbox", "sandbox-id": sandbox_id},
            )
        except APIError as e:
            logger.error("Failed to start sandbox container: %s", e)
            raise RuntimeError(f"Failed to start sandbox: {e}") from e

        # Reload to get port mappings
        container.reload()

        novnc_port = self._get_host_port(container, NOVNC_CONTAINER_PORT)
        cdp_port = self._get_host_port(container, CDP_CONTAINER_PORT)

        # If a profile was provided, copy it into the container
        if profile_path:
            try:
                subprocess.run(
                    ["docker", "cp", f"{profile_path}/.", f"{container.id}:/tmp/chrome-profile/"],
                    check=True,
                    capture_output=True,
                )
                logger.info("Restored profile from %s into sandbox %s", profile_path, sandbox_id)
            except subprocess.CalledProcessError as e:
                logger.warning("Failed to restore profile: %s", e)

        info = SandboxInfo(
            sandbox_id=sandbox_id,
            container_id=container.id,
            novnc_port=novnc_port,
            cdp_port=cdp_port,
            status="running",
        )
        self._sandboxes[sandbox_id] = info

        novnc_url = self.get_novnc_url(sandbox_id)
        cdp_url = f"ws://localhost:{cdp_port}"

        logger.info(
            "Started sandbox %s (container %s) — noVNC %s, CDP %s",
            sandbox_id, container.short_id, novnc_url, cdp_url,
        )

        return {
            "sandbox_id": sandbox_id,
            "novnc_url": novnc_url,
            "cdp_url": cdp_url,
            "status": "running",
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
        return f"http://localhost:{info.novnc_port}/vnc.html?autoconnect=true&password=bookr&resize=scale"

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

    def _get_host_port(self, container, container_port: int) -> int:
        """Extract the mapped host port from a container's port bindings."""
        port_key = f"{container_port}/tcp"
        ports = container.ports.get(port_key)
        if not ports:
            raise RuntimeError(f"No host port mapped for container port {container_port}")
        return int(ports[0]["HostPort"])
