"""Fail-closed aggregate quarantine quotas (lane002 R1)."""
import pytest
from pydantic import ValidationError

from backend.task_settings import TaskSettings


GIB = 1024 ** 3


def test_quarantine_quota_settings_have_safe_defaults_and_env_names():
    settings = TaskSettings.from_env({})
    assert (settings.quarantine_max_count, settings.quarantine_max_account_count) == (16, 4)
    assert (settings.quarantine_max_bytes, settings.quarantine_max_account_bytes) == (16 * GIB, 4 * GIB)
    assert settings.quarantine_reserve_bytes == GIB
    configured = TaskSettings.from_env({
        'FLOWPILOT_TASKS_QUARANTINE_MAX_COUNT': '3',
        'FLOWPILOT_TASKS_QUARANTINE_MAX_ACCOUNT_COUNT': '1',
        'FLOWPILOT_TASKS_QUARANTINE_MAX_BYTES': str(2 * GIB),
        'FLOWPILOT_TASKS_QUARANTINE_MAX_ACCOUNT_BYTES': str(GIB),
        'FLOWPILOT_TASKS_QUARANTINE_RESERVE_BYTES': str(512 * 1024 ** 2),
    })
    assert (configured.quarantine_max_count, configured.quarantine_max_account_count) == (3, 1)
    assert configured.quarantine_reserve_bytes == 512 * 1024 ** 2


@pytest.mark.parametrize('overrides', [
    {'quarantine_max_count': 0},
    {'quarantine_max_count': 2, 'quarantine_max_account_count': 3},
    {'quarantine_max_bytes': GIB, 'quarantine_max_account_bytes': 2 * GIB, 'quarantine_reserve_bytes': GIB},
    {'quarantine_max_account_bytes': GIB, 'quarantine_reserve_bytes': 2 * GIB},
    {'quarantine_reserve_bytes': 1024},
])
def test_incoherent_quarantine_quotas_are_rejected(overrides):
    with pytest.raises(ValidationError):
        TaskSettings(**overrides)
