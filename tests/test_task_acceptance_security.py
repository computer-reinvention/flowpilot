import base64
import json
import sqlite3
import urllib.parse

import pytest

from tests.task_acceptance_support.evidence import Evidence
from tests.task_acceptance_support.security import network_target, scan_artifacts, verify_key_hashes


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


def test_independent_key_hash_verification_rejects_plaintext_and_missing_hashes(tmp_path):
    import bcrypt
    key = 'fp_live_synthetic_acceptance_key_00000001'
    database = tmp_path / 'keys.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE api_keys(key_hash TEXT)')
        connection.execute('INSERT INTO api_keys VALUES (?)', (bcrypt.hashpw(key.encode(), bcrypt.gensalt(rounds=4)).decode(),))
    assert verify_key_hashes(database, [key])['verified_synthetic_keys'] == 1
    with pytest.raises(AssertionError):
        verify_key_hashes(database, [key + 'missing'])
    (tmp_path / 'unsafe.log').write_text(key)
    with pytest.raises(AssertionError):
        verify_key_hashes(database, [key])


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


TRANSFORMED = 'synthetic-OTP-acceptance-"/+ é\\123456'
TRANSFORMS = {
    'raw': lambda v: v.encode(),
    'url': lambda v: urllib.parse.quote(v, safe='').encode(),
    'url_plus': lambda v: urllib.parse.quote_plus(v).encode(),
    'json_ascii': lambda v: json.dumps(v)[1:-1].encode(),
    'json_unicode': lambda v: json.dumps(v, ensure_ascii=False)[1:-1].encode(),
    'json_double': lambda v: json.dumps(json.dumps(v))[1:-1].encode(),
    'hex': lambda v: v.encode().hex().encode(),
    'utf16le': lambda v: v.encode('utf-16-le'),
    'base64_whole': lambda v: base64.b64encode(v.encode()),
    'base64_offset1': lambda v: base64.b64encode(b'x' + v.encode() + b'tail'),
    'base64_offset2': lambda v: base64.b64encode(b'xy' + v.encode() + b'tail!'),
    'base64url_offset1': lambda v: base64.urlsafe_b64encode(b'{' + v.encode() + b'}'),
}


@pytest.mark.parametrize('name', sorted(TRANSFORMS))
def test_transformed_variants_detected_inside_larger_blobs(tmp_path, name):
    (tmp_path / 'blob.bin').write_bytes(b'prefix-noise ' + TRANSFORMS[name](TRANSFORMED) + b' suffix-noise')
    report = scan_artifacts(tmp_path, [TRANSFORMED])
    assert len(report['findings']) == 1, name
    assert TRANSFORMED not in json.dumps(report)


def test_different_transformed_value_is_not_a_false_positive(tmp_path):
    for index, encode in enumerate(TRANSFORMS.values()):
        (tmp_path / f'other-{index}').write_bytes(encode('synthetic-OTP-acceptance-unrelated-99999'))
    assert not scan_artifacts(tmp_path, [TRANSFORMED])['findings']


@pytest.mark.parametrize('kind', ['tar', 'tar.gz', 'gzip', 'zip', 'nested'])
def test_archives_are_expanded_in_memory(tmp_path, kind):
    import gzip
    import io
    import tarfile
    import zipfile

    def tar(payload, mode='w'):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode=mode) as archive:
            info = tarfile.TarInfo('chrome-profile/Default/Local Storage/000003.log')
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        return buffer.getvalue()

    payload = b'\x00leveldb\x00' + TRANSFORMED.encode('utf-16-le') + b'\x00'
    if kind == 'tar':
        data = tar(payload)
    elif kind == 'tar.gz':
        data = tar(payload, 'w:gz')
    elif kind == 'gzip':
        data = gzip.compress(b'trace:' + base64.b64encode(b'ab' + TRANSFORMED.encode() + b'z'))
    elif kind == 'zip':
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('trace/events.json', json.dumps({'value': TRANSFORMED}))
        data = buffer.getvalue()
    else:
        data = tar(gzip.compress(tar(payload)))
    (tmp_path / 'sandbox-profile.tar').write_bytes(data)
    report = scan_artifacts(tmp_path, [TRANSFORMED])
    assert len(report['findings']) == 1 and not report['gaps']
    assert report['archive_members_scanned'] >= 1
    clean = scan_artifacts(tmp_path, ['synthetic-OTP-acceptance-absent-000'])
    assert not clean['findings'] and not clean['gaps']


def test_oversized_compressed_stream_is_a_gap_not_a_pass(tmp_path):
    import gzip
    from tests.task_acceptance_support.security import scan_bytes, _needles
    bomb = gzip.compress(b'\0' * (4 * 1024 * 1024))
    found, _, unreadable = scan_bytes(bomb, _needles([TRANSFORMED]), budget=1024)
    assert not found and unreadable


def test_in_memory_log_blobs_are_scanned_without_retention():
    from tests.task_acceptance_support.security import scan_blobs
    report = scan_blobs([('browser-container.log', b'chromium: value=' + urllib.parse.quote(TRANSFORMED).encode()),
                         ('caddy.log', b'{"level":"info"}')], [TRANSFORMED])
    assert len(report['findings']) == 1 and report['blobs_scanned'] == 2
    assert 'browser-container' not in json.dumps(report) and TRANSFORMED not in json.dumps(report)
    assert scan_blobs([], [TRANSFORMED])['gaps']
