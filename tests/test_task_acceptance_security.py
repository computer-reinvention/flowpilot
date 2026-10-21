import base64
import json
import sqlite3
import urllib.parse

import pytest

from tests.task_acceptance_support.evidence import Evidence
from tests.task_acceptance_support.security import network_target, scan_artifacts


CANARY = 'synthetic-OTP-acceptance-"/123456'


@pytest.mark.parametrize("encode", [lambda v: v, lambda v: urllib.parse.quote(v, safe=""),
                                    lambda v: json.dumps(v)[1:-1],
                                    lambda v: base64.b64encode(v.encode()).decode()])
def test_scan_detects_canaries_without_echoing_values(tmp_path, encode):
    (tmp_path / 'trace.log').write_text(encode(CANARY))
    report = scan_artifacts(tmp_path, [CANARY])
    assert len(report['findings']) == 1 and not report['gaps']
    assert CANARY not in json.dumps(report) and 'trace.log' not in json.dumps(report)


def test_scan_database_wal_and_clean_artifacts(tmp_path):
    connection = sqlite3.connect(tmp_path / 'state.sqlite3')
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('CREATE TABLE transient(value TEXT)')
    connection.execute('INSERT INTO transient VALUES (?)', (CANARY,))
    connection.commit()
    try:
        assert scan_artifacts(tmp_path, [CANARY])['findings']
    finally:
        connection.close()
    other = tmp_path / 'clean'
    other.mkdir()
    (other / 'safe.json').write_text('{"state":"completed"}')
    assert not scan_artifacts(other, [CANARY])['findings']


def test_empty_bounded_compressed_and_symlink_scans_are_gaps(tmp_path):
    assert scan_artifacts(tmp_path, [CANARY])['gaps']
    (tmp_path / 'large').write_text('bounded')
    assert scan_artifacts(tmp_path, [CANARY], max_bytes=1)['gaps']
    (tmp_path / 'archive').write_bytes(b'PK-unexpanded')
    (tmp_path / 'link').symlink_to(tmp_path / 'large')
    reasons = {item['reason'] for item in scan_artifacts(tmp_path, [CANARY])['gaps']}
    assert reasons == {'symlink_not_followed', 'archive_requires_expansion'}


@pytest.mark.parametrize('url', ['https://example.com/', 'http://example.com/', 'http://backend/delete',
                                 'http://backend/?token=secret', 'http://user:pass@backend/'])
def test_network_probes_cannot_target_external_effects(url):
    with pytest.raises((AssertionError, ValueError)):
        network_target(url)


def test_blocked_requirements_cannot_become_pass_by_running_harness_checks():
    evidence = Evidence('self-test')
    evidence.record('fake_peer', 'passed', 'consumer only')
    assert evidence.exit_code() == 0
    assert evidence.exit_code(require_complete=True) == 2
    assert all(item['outcome'] == 'blocked' for item in evidence.requirements.values())
    evidence.record('real_failure', 'failed', 'counter mismatch')
    assert evidence.exit_code(require_complete=True) == 1
