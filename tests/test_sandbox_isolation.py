from unittest.mock import MagicMock

import pytest

from backend.sandbox import SandboxManager


def test_profile_restore_precedes_start_and_no_shared_mount(tmp_path):
    profile = tmp_path / "user" / "profile"
    profile.mkdir(parents=True)
    (profile / "Preferences").write_text('{}')
    manager = SandboxManager(str(tmp_path))
    manager._client = MagicMock()
    container = manager._client.containers.create.return_value
    events = []
    container.put_archive.side_effect = lambda *a: events.append("restore")
    container.start.side_effect = lambda: events.append("start")
    manager.start(str(profile), "user")
    assert events == ["restore", "start"]
    options = manager._client.containers.create.call_args.kwargs
    assert "volumes" not in options
    assert options["security_opt"] == ["no-new-privileges:true"]
    assert "ports" not in options


def test_profile_outside_root_fails_and_removes_container(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    manager = SandboxManager(str(root))
    manager._client = MagicMock()
    with pytest.raises(ValueError, match="within"):
        manager.start(str(other))
    container = manager._client.containers.create.return_value
    container.start.assert_not_called()
    container.remove.assert_called_once_with(force=True)
