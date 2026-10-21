"""Only harness-created process groups and unique directories may be stopped."""

import os
import signal
import subprocess
import time
from pathlib import Path


class OwnedProcess:
    def __init__(self, argv, directory, cwd, extra_env=None):
        self.directory = Path(directory)
        assert self.directory.name.startswith('task-acceptance-')
        self.directory.mkdir(parents=True, exist_ok=False)
        # Do not inherit provider keys, proxy configuration, or deployment secrets.
        env = {name: os.environ[name] for name in ('PATH', 'HOME', 'TMPDIR', 'LANG') if name in os.environ}
        env.update(extra_env or {})
        self.process = subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=True,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        self.pid = self.process.pid

    def kill(self):
        if self.process.poll() is None:
            os.killpg(self.pid, signal.SIGKILL)
            self.process.wait(timeout=5)

    def close(self):
        if self.process.poll() is None:
            os.killpg(self.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.kill()


def wait_file(path, process, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).exists():
            return Path(path).read_text()
        assert process.process.poll() is None, 'owned process exited before readiness'
        time.sleep(.02)
    raise TimeoutError('owned resource did not become ready')
