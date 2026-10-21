"""Bounded artifact scanner and optional packaged-browser network probes."""

import base64
import hashlib
import ipaddress
import json
import sqlite3
import urllib.parse
from pathlib import Path

from .client import Client, local_url


def verify_key_hashes(database, plaintext_keys):
    """Read-only disposable SQLite snapshot; independent bcrypt verification."""
    import bcrypt
    database = Path(database)
    assert database.is_file() and not database.is_symlink()
    assert plaintext_keys and all(key.startswith('fp_live_') and len(key) >= 32 for key in plaintext_keys)
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        connection.execute('PRAGMA query_only=ON')
        hashes = [row[0] for row in connection.execute('SELECT key_hash FROM api_keys LIMIT 5001')]
    assert 0 < len(hashes) <= 5000, 'key inventory unavailable or exceeds scan bound'
    assert all(isinstance(value, str) and value.startswith(('$2a$', '$2b$', '$2y$')) for value in hashes)
    for key in plaintext_keys:
        assert any(bcrypt.checkpw(key.encode(), value.encode()) for value in hashes), 'expected key hash missing'
    artifacts = scan_artifacts(database.parent, plaintext_keys, max_bytes=4 * 1024 ** 3)
    assert not artifacts['findings'] and not artifacts['gaps'], 'plaintext key leak or incomplete snapshot scan'
    return {'verified_synthetic_keys': len(plaintext_keys), 'snapshot_files_scanned': artifacts['files_scanned']}


def _aligned_base64(data, encoder):
    """Base64 fragments that depend only on `data`, at all three byte offsets.

    A secret embedded inside a larger encoded blob does not start on a 3-byte
    boundary in general; whole-value encoding alone misses those occurrences.
    """
    fragments = set()
    for offset in range(3):
        encoded = encoder(b'\0' * offset + data).rstrip(b'=')
        start = 4 if offset else 0
        stop = ((offset + len(data)) // 3) * 4
        fragment = encoded[start:stop]
        if len(fragment) >= 8:
            fragments.add(fragment)
    return fragments


def canary_variants(secret):
    """Raw plus transformed spellings a log, trace, DB or browser may persist."""
    import html
    assert len(secret) >= 12, "use distinctive synthetic canaries, not real credentials"
    raw = secret.encode()
    variants = {raw, urllib.parse.quote(secret, safe="").encode(), urllib.parse.quote_plus(secret).encode(),
                urllib.parse.quote(secret, safe="/").encode(),
                json.dumps(secret, ensure_ascii=True)[1:-1].encode(),
                json.dumps(secret, ensure_ascii=False)[1:-1].encode(),
                json.dumps(json.dumps(secret, ensure_ascii=True))[1:-1].encode(),
                html.escape(secret, quote=True).encode(),
                raw.hex().encode(), raw.hex().upper().encode(),
                secret.encode('utf-16-le'), secret.encode('utf-16-be'),
                base64.b64encode(raw), base64.urlsafe_b64encode(raw).rstrip(b"=")}
    variants |= _aligned_base64(raw, base64.b64encode)
    variants |= _aligned_base64(raw, base64.urlsafe_b64encode)
    return variants


def _is_tar(content):
    return len(content) > 262 and content[257:262] == b'ustar'


def _expand(content, depth, budget):
    """Return member byte strings of tar/gzip/zip/bz2/xz content, bounded.

    Raises for unreadable or over-budget archives; callers record a gap.
    """
    import bz2
    import gzip
    import io
    import lzma
    import tarfile
    import zipfile
    if depth > 3:
        raise ValueError('archive nesting bound')
    if content[:2] == b'\x1f\x8b' or content[:3] == b'BZh' or content[:6] == b'\xfd7zXZ\x00':
        opener = gzip.GzipFile if content[:2] == b'\x1f\x8b' else bz2.BZ2File if content[:3] == b'BZh' else lzma.LZMAFile
        with opener(fileobj=io.BytesIO(content)) if opener is gzip.GzipFile else opener(io.BytesIO(content)) as stream:
            data = stream.read(budget + 1)  # Never inflate a hostile stream unboundedly.
        if len(data) > budget:
            raise ValueError('expanded size bound')
        return [data]
    if content[:2] == b'PK':
        members = []
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if info.file_size > budget:
                    raise ValueError('expanded size bound')
                members.append(archive.read(info))
                budget -= info.file_size
        return members
    if _is_tar(content):
        members = []
        with tarfile.open(fileobj=io.BytesIO(content), mode='r:') as archive:
            for member in archive:
                if not member.isfile():
                    continue
                if member.size > budget:
                    raise ValueError('expanded size bound')
                members.append(archive.extractfile(member).read())
                budget -= member.size
        return members
    return []


def scan_bytes(content, needles, budget=256 * 1024 * 1024, depth=0):
    """Return (matched canary hashes, expanded member count, unreadable archive?).

    Raw bytes are always scanned; tar/zip/gzip/bz2/xz members are additionally
    scanned in memory (never extracted to disk), recursively to a fixed depth.
    """
    found = {canary_hash for canary_hash, variants in needles if any(needle in content for needle in variants)}
    members = 0
    unreadable = False
    if content[:2] in {b'PK', b'\x1f\x8b'} or content[:3] == b'BZh' or content[:6] == b'\xfd7zXZ\x00' or _is_tar(content):
        try:
            expanded = _expand(content, depth, budget)
        except Exception:
            return found, members, True
        for member in expanded:
            members += 1
            inner, inner_members, inner_unreadable = scan_bytes(member, needles, budget - len(member), depth + 1)
            found |= inner
            members += inner_members
            unreadable = unreadable or inner_unreadable
    return found, members, unreadable


def _needles(canaries):
    assert canaries, "empty secret scan is not proof"
    return [(hashlib.sha256(secret.encode()).hexdigest(), canary_variants(secret)) for secret in canaries]


def scan_blobs(blobs, canaries):
    """Scan named in-memory artifacts (container logs, HTTP results) without retaining them."""
    needles = _needles(canaries)
    findings, gaps, members = [], [], 0
    for name, content in blobs:
        ref = hashlib.sha256(name.encode()).hexdigest()
        found, expanded, unreadable = scan_bytes(content, needles)
        members += expanded
        if unreadable:
            gaps.append({"artifact_ref": ref, "reason": "archive_requires_expansion"})
        findings.extend({"artifact_ref": ref, "canary_sha256": item} for item in sorted(found))
    if not blobs:
        gaps.append({"reason": "no_artifacts"})
    return {"blobs_scanned": len(blobs), "bytes_scanned": sum(len(c) for _, c in blobs),
            "archive_members_scanned": members, "findings": findings, "gaps": gaps}


def scan_artifacts(directory, canaries, max_bytes=32 * 1024 * 1024):
    directory = Path(directory)
    assert directory.is_dir() and not directory.is_symlink()
    needles = _needles(canaries)
    findings, gaps = [], []
    total = 0
    files = 0
    members = 0
    for path in sorted(directory.rglob("*")):
        ref = hashlib.sha256(str(path.relative_to(directory)).encode()).hexdigest()
        if path.is_symlink():
            gaps.append({"artifact_ref": ref, "reason": "symlink_not_followed"})
            continue
        if not path.is_file():
            continue
        size = path.stat().st_size
        if total + size > max_bytes:
            gaps.append({"artifact_ref": ref, "reason": "scan_size_bound"})
            continue
        total += size
        files += 1
        content = path.read_bytes()
        found, expanded, unreadable = scan_bytes(content, needles)
        members += expanded
        # Compressed content that cannot be expanded in memory stays an explicit gap.
        if unreadable:
            gaps.append({"artifact_ref": ref, "reason": "archive_requires_expansion"})
        findings.extend({"artifact_ref": ref, "canary_sha256": item} for item in sorted(found))
    if not files:
        gaps.append({"reason": "no_artifacts"})
    return {"files_scanned": files, "bytes_scanned": total, "archive_members_scanned": members,
            "findings": findings, "gaps": gaps}


def network_target(url):
    parsed = urllib.parse.urlsplit(url)
    assert parsed.scheme == "http" and not parsed.username and not parsed.password
    assert not parsed.query and not parsed.fragment
    host = parsed.hostname
    allowed_names = {"localhost", "backend", "docker-proxy", "host.docker.internal", "metadata.google.internal"}
    if host not in allowed_names:
        address = ipaddress.ip_address(host)
        assert address.is_private or address.is_loopback or address.is_link_local
    assert parsed.path in {"/", "/health", "/version", "/latest/meta-data/", "/computeMetadata/v1/"}
    return url


def probe_network(cdp_url, manifest):
    """Run in the packaged browser, NOT a replacement locally launched browser.

    Positive control must succeed or deny results are inconclusive. All targets
    are GET-only local/private diagnostic paths; no cloud credential endpoints.
    """
    from playwright.sync_api import sync_playwright
    local_url(cdp_url)
    assert manifest["run_label"].startswith("task-acceptance-")
    targets = manifest["deny"]
    assert targets and len(targets) <= 20
    positive = network_target(manifest["allow"])
    for target in targets:
        network_target(target['browser_url'])
        # A nonexistent target is not network isolation. The same disposable
        # endpoint must demonstrably listen through an explicit host-loopback map.
        local_url(target['host_control_url'])
        control = Client(target['host_control_url']).request('GET', '/health')
        assert control.status == 200, 'denied-target host control is unavailable'
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        context = browser.new_context(ignore_https_errors=False)
        try:
            page = context.new_page()
            # Navigation avoids CORS/CSP false denials. Success includes HTTP 4xx/5xx.
            response = page.goto(positive, timeout=5000)
            assert response and response.ok, "positive reachability control failed"
            outcomes = []
            for index, target in enumerate(targets):
                denied = False
                try:
                    page.goto(target['browser_url'], timeout=3000)
                except Exception:
                    denied = True
                outcomes.append({"target_index": index, "denied": denied})
            assert all(item["denied"] for item in outcomes), "private endpoint reachable"
            return {"positive_control": True, "probes": outcomes,
                    "limitation": "navigation probes only; DNS-rebinding, redirects, WebSocket and exfiltration still unverified"}
        finally:
            context.close()  # Only the newly created probe context is closed.
            # Do not close the remote task browser or unrelated contexts.
