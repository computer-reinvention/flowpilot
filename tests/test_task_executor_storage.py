from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.sandbox import SandboxManager
from backend.task_contracts import Principal
from backend.task_settings import TaskSettings
from backend.tasks.memory import ProfileStore, storage_key
from backend.tasks.browser_adapter import TaskSandbox
from backend.task_ports import Lease


def test_profile_identity_lock_and_redacted_history(tmp_path):
    settings = TaskSettings(state_dir=tmp_path)
    alice, bob = Principal(account_id='alice'), Principal(account_id='bob')
    assert storage_key(alice, 'p') != storage_key(bob, 'p')
    assert storage_key(alice, 'p') != storage_key(alice, 'q')
    store = ProfileStore(settings, alice, 'p')
    with pytest.raises(ValueError):
        ProfileStore(settings, alice, 'p')
    store.record_turn('use secret123', {'password': 'secret123'}, 'secret123 accepted', {})
    store.close()
    store = ProfileStore(settings, alice, 'p')
    assert 'secret123' not in str(store.history())
    assert store.history()[0]['inputs'] == '{}'
    store.close()


def test_legacy_cleanup_excludes_task_containers():
    manager = SandboxManager()
    manager._client = MagicMock()
    task = MagicMock(labels={'flowpilot-task': '1'})
    legacy = MagicMock(labels={'sandbox-id': 'orphan'})
    manager.client.containers.list.return_value = [task, legacy]
    assert manager.cleanup_orphans() == 1
    task.stop.assert_not_called()
    legacy.remove.assert_called_once_with(force=True)


def test_task_transport_has_no_host_publication(tmp_path):
    manager = SandboxManager(str(tmp_path))
    manager._client = MagicMock()
    settings = TaskSettings(sandbox_pids=512, sandbox_memory_mb=768)
    manager.start(task_labels={'flowpilot-task': '1'}, settings=settings)
    kwargs = manager.client.containers.create.call_args.kwargs
    assert 'ports' not in kwargs
    assert kwargs['pids_limit'] == 512
    assert kwargs['mem_limit'] == '768m'
    assert kwargs['labels']['flowpilot-task'] == '1'
    assert kwargs['environment']['CHROME_AUTORESTART'] == 'false'


def test_save_failure_preserves_archive_and_quarantines(tmp_path):
    settings = TaskSettings(state_dir=tmp_path)
    lease = Lease(principal=Principal(account_id='alice'), profile_id='p', task_id='t',
                  session_id='s', generation=1)
    sandbox = TaskSandbox(tmp_path, 'key', settings=settings, lease=lease)
    sandbox.archive.write_bytes(b'previous-good-archive')
    sandbox.info = {'container_id': 'container', 'sandbox_id': 'sandbox'}
    sandbox.manager._client = MagicMock()
    container = sandbox.manager.client.containers.get.return_value
    container.get_archive.side_effect = RuntimeError('archive unavailable')
    with pytest.raises(RuntimeError, match='Profile save failed'):
        sandbox._close()
    assert sandbox.archive.read_bytes() == b'previous-good-archive'
    assert (tmp_path / 'recovery-required').exists()
    container.stop.assert_called()
    container.remove.assert_not_called()


def test_archive_restore_rejects_links_before_container_launch(tmp_path):
    import tarfile
    settings = TaskSettings(state_dir=tmp_path)
    lease = Lease(principal=Principal(account_id='alice'), profile_id='p', task_id='t',
                  session_id='s', generation=1)
    sandbox = TaskSandbox(tmp_path, 'key', settings=settings, lease=lease)
    sandbox.manager._client = MagicMock()
    with tarfile.open(sandbox.archive, 'w') as archive:
        link = tarfile.TarInfo('chrome-profile/symlink')
        link.type, link.linkname = tarfile.SYMTYPE, '/etc/passwd'
        archive.addfile(link)
    with pytest.raises(ValueError, match='Unsafe profile archive'):
        sandbox._start()
    sandbox.manager.client.containers.create.assert_not_called()
