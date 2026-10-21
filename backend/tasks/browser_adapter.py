"""Docker-network-only browser transport. No host CDP ports or help listener."""
import asyncio

from ..repl_sandbox import ChatSandbox


class TaskSandbox(ChatSandbox):
    def __init__(self, directory, user, *, settings, lease):
        super().__init__(directory, user)
        self.settings, self.lease = settings, lease

    def _start(self):
        if (self.directory / 'recovery-required').exists():
            raise RuntimeError('Profile requires recovery')
        self.info = self.manager.start(
            user_id=self.user, settings=self.settings,
            task_labels={'flowpilot-task': '1', 'task-id': self.lease.task_id,
                         'session-id': self.lease.session_id, 'profile-key': self.user,
                         'lease-generation': str(self.lease.generation)},
            profile_archive=str(self.archive) if self.archive.exists() else None)
        return self.info['cdp_url'].replace('ws:', 'http:')

    def _close(self):
        try:
            super()._close()
        except Exception:
            (self.directory / 'recovery-required').touch(mode=0o600)
            raise

    async def stop_writers(self):
        if self.info:
            container = await asyncio.to_thread(self.manager.client.containers.get, self.info['container_id'])
            await asyncio.to_thread(container.stop, timeout=1)


def create_browser(settings, lease):
    return lambda directory, user: TaskSandbox(directory, user, settings=settings, lease=lease)
