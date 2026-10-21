"""Docker-network-only browser transport. No host CDP ports or help listener."""
import asyncio
import hashlib
import ipaddress
import json
import os
import tarfile

from ..repl_sandbox import ChatSandbox
from ..sandbox import CDP_CONTAINER_PORT, SANDBOX_NETWORK


def host_key(settings):
    return hashlib.sha256((str(settings.state_dir.resolve()) + '\0' + SANDBOX_NETWORK).encode()).hexdigest()


def mark_recovery(directory, settings, lease, container_id=None):
    marker = directory / 'recovery-required'
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, 'w') as output:
        json.dump({'task_host': host_key(settings), 'account_id': lease.principal.account_id,
                   'task_id': lease.task_id,
                   'session_id': lease.session_id, 'lease_generation': lease.generation,
                   'container_id': container_id}, output)


class TaskSandbox(ChatSandbox):
    def __init__(self, directory, user, *, settings, lease):
        super().__init__(directory, user)
        self.private_logs = True
        self.settings, self.lease = settings, lease

    async def start(self):
        # Do not abandon a Docker create/start thread on coroutine cancellation.
        # BrowserSession's failure cleanup must see the eventual container ID.
        start = asyncio.create_task(super().start())
        try:
            return await asyncio.shield(start)
        except asyncio.CancelledError:
            try:
                await start
            finally:
                await self.stop_writers()
            raise

    def _start(self):
        if (self.directory / 'recovery-required').exists():
            raise RuntimeError('Profile requires recovery')
        if self.archive.exists():
            if self.archive.stat().st_size > 256 * 1024 * 1024:
                raise ValueError('Profile archive exceeds recovery budget')
            with tarfile.open(self.archive) as archive:
                total = 0
                for member in archive:
                    from pathlib import PurePosixPath
                    path = PurePosixPath(member.name)
                    total += member.size
                    if (not member.isfile() or path.is_absolute() or '..' in path.parts
                            or path.parts[0] != 'chrome-profile' or total > 256 * 1024 * 1024):
                        raise ValueError('Unsafe profile archive')
        self.info = self.manager.start(
            user_id=self.user, settings=self.settings,
            task_labels={'flowpilot-task': '1', 'task-id': self.lease.task_id,
                         'task-host': host_key(self.settings),
                         'session-id': self.lease.session_id, 'profile-key': self.user,
                         'lease-generation': str(self.lease.generation)},
            profile_archive=str(self.archive) if self.archive.exists() else None)
        container = self.manager.client.containers.get(self.info['container_id'])
        container.reload()
        # Chromium also rejects DNS Host headers on the websocket upgrade. Use
        # Docker's authoritative bridge address, not the advertised loopback or
        # an untrusted discovery host. No host port needs publishing.
        address = ipaddress.ip_address(container.attrs['NetworkSettings']['Networks'][SANDBOX_NETWORK]['IPAddress'])
        return f'http://{address}:{CDP_CONTAINER_PORT}'

    def _close(self):
        try:
            super()._close()
        except Exception:
            mark_recovery(self.directory, self.settings, self.lease,
                          self.info['container_id'] if self.info else None)
            raise

    async def close(self):
        close = asyncio.create_task(super().close())
        try:
            await asyncio.shield(close)
        except asyncio.CancelledError:
            # Keep the profile lock held until the archive writer really exits.
            await close
            raise

    async def stop_writers(self):
        if self.info:
            container = await asyncio.to_thread(self.manager.client.containers.get, self.info['container_id'])
            await asyncio.to_thread(container.stop, timeout=5)
            await asyncio.to_thread(container.reload)
            if container.status in ('running', 'restarting', 'paused'):
                raise RuntimeError('Browser container did not stop')


def create_browser(settings, lease):
    return lambda directory, user: TaskSandbox(directory, user, settings=settings, lease=lease)
