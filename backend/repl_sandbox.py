"""Local chat adapter for the existing full Chromium/Xvfb Docker sandbox."""
import asyncio
import os
from pathlib import Path
import tarfile
import tempfile

import httpx

from .sandbox import SandboxManager, SANDBOX_NETWORK


class ChatSandbox:
    def __init__(self, directory: Path, user: str):
        self.directory = directory
        self.user = user
        self.manager = SandboxManager(str(directory))
        self.info = None
        self.archive = directory / 'sandbox-profile.tar'

    def _start(self):
        from docker.errors import NotFound
        try:
            self.manager.client.networks.get(SANDBOX_NETWORK)
        except NotFound:
            self.manager.client.networks.create(SANDBOX_NETWORK, driver='bridge')
        self.info = self.manager.start(user_id=self.user, publish_local_cdp=True,
                                       profile_archive=str(self.archive) if self.archive.exists() else None)
        return self.info['local_cdp_url']

    async def start(self):
        url = await asyncio.to_thread(self._start)
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(120):
                try:
                    response = await client.get(url + '/json/version', timeout=1)
                    response.raise_for_status()
                    # Chromium advertises its internal debugger port; use the
                    # actual loopback-published port for every client.
                    path = response.json()['webSocketDebuggerUrl'].split('/devtools/', 1)[1]
                    return url, url.replace('http:', 'ws:') + '/devtools/' + path
                except (httpx.HTTPError, KeyError):
                    await asyncio.sleep(.5)
        raise RuntimeError('Virtual-display Chromium did not become ready within 60 seconds')

    def _close(self):
        if not self.info:
            return
        container = self.manager.client.containers.get(self.info['container_id'])
        # Graceful Supervisor shutdown lets Chromium flush its profile first.
        container.stop(timeout=20)
        try:
            bits, _ = container.get_archive('/tmp/chrome-profile')
            with tempfile.TemporaryFile() as source:
                for chunk in bits:
                    source.write(chunk)
                source.seek(0)
                temporary = self.archive.with_suffix('.tar.tmp')
                with tarfile.open(fileobj=source, mode='r:') as original, tarfile.open(temporary, 'w:') as saved:
                    for member in original:
                        path = Path(member.name)
                        if path.is_absolute() or '..' in path.parts or path.parts[0] != 'chrome-profile':
                            raise ValueError('Unsafe browser profile archive')
                        if member.isfile() and not path.name.startswith('Singleton'):
                            saved.addfile(member, original.extractfile(member))
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.archive)
        except Exception as exc:
            # Keep the stopped container as a recovery source if persistence fails.
            raise RuntimeError(f'Profile save failed; retained container {container.id} for recovery') from exc
        self.manager.stop(self.info['sandbox_id'])
        self.info = None

    async def close(self):
        await asyncio.to_thread(self._close)
