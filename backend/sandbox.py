"""Docker container management for browser sandboxes."""

import io
import os
import re
import uuid
import logging
import tarfile
from dataclasses import dataclass
from pathlib import Path

import docker
from docker.errors import NotFound, APIError

logger = logging.getLogger(__name__)

SANDBOX_IMAGE = "flowpilot-sandbox"
CDP_CONTAINER_PORT = 9223  # socat: 0.0.0.0:9223 → 127.0.0.1:9222 (Chromium ignores --remote-debugging-address on ARM)
SANDBOX_NETWORK = os.environ.get("SANDBOX_NETWORK", "flowpilot_sandbox-net")
PROFILE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass
class SandboxInfo:
    sandbox_id: str
    container_id: str
    cdp_port: int
    status: str
    user_id: str = ""


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

    def start(self, profile_path: str | None = None, user_id: str = "", *,
              publish_local_cdp: bool = False, profile_archive: str | None = None,
              task_labels: dict | None = None, settings=None) -> dict:
        """Create and start a new sandbox container.

        Args:
            profile_path: Optional host path to a saved browser profile to restore.
            user_id: ID of the user requesting the sandbox.

        Returns:
            Dict with sandbox_id, cdp_url, and status.
        """
        sandbox_id = str(uuid.uuid4())[:12]

        environment = {
            "DISPLAY": ":1",
            **({'CHROME_AUTORESTART': 'false'} if publish_local_cdp or task_labels else {}),
        }

        container = None
        try:
            container = self.client.containers.create(
                image=SANDBOX_IMAGE,
                name=f"flowpilot-{sandbox_id}",
                detach=True,
                # CDP port is NOT published to the host; accessed via sandbox-net
                environment=environment,
                network=SANDBOX_NETWORK,
                **({"ports": {f"{CDP_CONTAINER_PORT}/tcp": ("127.0.0.1", None)}} if publish_local_cdp else {}),
                labels={
                    "flowpilot": "sandbox",
                    "sandbox-id": sandbox_id,
                    "user-id": user_id,
                    **(task_labels or {}),
                },
                # Resource limits
                mem_limit=f"{settings.sandbox_memory_mb}m" if settings else "1g",
                cpu_quota=int(settings.sandbox_cpu_cores * 100000) if settings else 100000,
                pids_limit=settings.sandbox_pids if settings else 256,
                shm_size=f"{settings.sandbox_shm_mb}m" if settings else "256m",
                security_opt=["no-new-privileges:true"],
                **({"runtime": os.environ["SANDBOX_RUNTIME"]} if os.getenv("SANDBOX_RUNTIME") else {}),
            )
            if profile_archive:
                archive = Path(profile_archive).resolve(strict=True)
                if not archive.is_relative_to(Path(self.profiles_host_dir).resolve()):
                    raise ValueError('Profile archive must be within the configured profile directory')
                container.put_archive('/tmp', archive.read_bytes())
            if profile_path:
                src = Path(profile_path).resolve(strict=True)
                root = Path(self.profiles_host_dir).resolve()
                if not src.is_relative_to(root):
                    raise ValueError("Profile must be within the configured profile directory")
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w") as tar:
                    for item in src.rglob("*"):
                        if item.is_file() and not item.is_symlink():
                            tar.add(str(item), arcname="chrome-profile/" + str(item.relative_to(src)), recursive=False)
                buf.seek(0)
                container.put_archive("/tmp", buf)
            # Restore before Chromium starts: no shared profile mount or startup race.
            container.start()
            if publish_local_cdp:
                container.reload()
                local_port = container.attrs['NetworkSettings']['Ports'][f'{CDP_CONTAINER_PORT}/tcp'][0]['HostPort']
        except APIError as e:
            if container is not None:
                container.remove(force=True)
            logger.error("Failed to start sandbox container: %s", e)
            raise RuntimeError(f"Failed to start sandbox: {e}") from e
        except Exception:
            if container is not None:
                container.remove(force=True)
            raise

        # CDP accessible via container name on the internal network
        cdp_host = f"flowpilot-{sandbox_id}"
        cdp_port = CDP_CONTAINER_PORT  # internal port, not published

        info = SandboxInfo(
            sandbox_id=sandbox_id,
            container_id=container.id or "",
            cdp_port=cdp_port,
            status="running",
            user_id=user_id,
        )
        self._sandboxes[sandbox_id] = info

        # Track per-user
        if user_id:
            self._user_sandboxes.setdefault(user_id, set()).add(sandbox_id)

        cdp_url = f"ws://{cdp_host}:{cdp_port}"
        logger.info("Started sandbox %s (container %s) — CDP %s", sandbox_id, container.short_id, cdp_url)

        return {
            "sandbox_id": sandbox_id,
            "container_id": container.id,
            "cdp_url": cdp_url,
            "status": "running",
            **({"local_cdp_url": f"http://127.0.0.1:{local_port}"} if publish_local_cdp else {}),
        }

    def stop(self, sandbox_id: str) -> None:
        """Stop and remove a sandbox container."""
        info = self._sandboxes.get(sandbox_id)
        if not info:
            raise ValueError(f"Unknown sandbox: {sandbox_id}")

        try:
            container = self.client.containers.get(info.container_id)
            # Capture logs before destroying the container
            try:
                logs = container.logs(tail=80, stdout=True, stderr=True).decode(errors="replace")
                if logs.strip():
                    logger.info("Sandbox %s logs:\n%s", sandbox_id, logs)
            except Exception as log_err:
                logger.warning("Could not capture logs for sandbox %s: %s", sandbox_id, log_err)
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

    def get_cdp_url(self, sandbox_id: str) -> str:
        """Return the internal CDP URL for a sandbox (accessible via Docker network)."""
        info = self._sandboxes.get(sandbox_id)
        if not info:
            raise ValueError(f"Unknown sandbox: {sandbox_id}")
        return f"ws://flowpilot-{sandbox_id}:{info.cdp_port}"

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

    def reconcile_from_db(self, db_rows: list[dict]) -> list[str]:
        """Rebuild _sandboxes from DB rows. Returns sandbox_ids whose containers are gone."""
        dead = []
        for row in db_rows:
            try:
                container = self.client.containers.get(row["container_id"])
                if container.labels.get("flowpilot-task"):
                    dead.append(row["id"])
                    continue
                if container.status == "running":
                    info = SandboxInfo(
                        sandbox_id=row["id"],
                        container_id=row["container_id"],
                        cdp_port=CDP_CONTAINER_PORT,
                        status="running",
                        user_id=row["user_id"],
                    )
                    self._sandboxes[row["id"]] = info
                    logger.info("Reconciled sandbox %s (container %s)", row["id"], row["container_id"][:12])
                else:
                    dead.append(row["id"])
            except NotFound:
                dead.append(row["id"])
            except APIError as e:
                logger.warning("Reconcile error for sandbox %s: %s", row["id"], e)
        return dead

    def cleanup_orphans(self) -> int:
        """H3: Stop and remove any orphaned flowpilot sandbox containers."""
        removed = 0
        try:
            containers = self.client.containers.list(
                all=True,
                filters={"label": "flowpilot=sandbox"},
            )
            for container in containers:
                # The task host reconciles these after legacy startup cleanup.
                if container.labels.get("flowpilot-task"):
                    continue
                sandbox_id = container.labels.get("sandbox-id", "unknown")
                if sandbox_id not in self._sandboxes:
                    logger.info("Cleaning up orphaned sandbox container: %s (%s)", container.name, sandbox_id)
                    try:
                        container.stop(timeout=5)
                        container.remove(force=True)
                        removed += 1
                    except (APIError, NotFound) as e:
                        logger.warning("Failed to remove orphan %s: %s", container.name, e)
        except APIError as e:
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
