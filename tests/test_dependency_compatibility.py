"""Actual dependency boundaries, using only synthetic local data and transports."""
import importlib.metadata
import ssl
import hashlib
import json
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.x509.oid import NameOID


def test_certifi_revoked_root_removed_and_verification_stays_strict():
    assert importlib.metadata.version("certifi") == "2026.10.19"
    with open(certifi.where(), "rb") as source:
        certificates = x509.load_pem_x509_certificates(source.read())
    names = {
        value.value
        for certificate in certificates
        for value in certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    }
    assert "emSign ECC Root CA - G3" not in names
    context = ssl.create_default_context(cafile=certifi.where())
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert len(context.get_ca_certs()) > 100


def test_vendored_stylesheet_is_exact_licensed_release_without_cli():
    root = Path(__file__).resolve().parents[1]
    css = (root / 'frontend/src/shadcn-tailwind.css').read_bytes()
    assert len(css) == 16340
    assert hashlib.sha256(css).hexdigest() == '4c371f7a1ff5d219ae2f7ff28bd256b4346fd546fe46fbae22092e57db2f0fae'
    license_text = (root / 'frontend/src/shadcn-tailwind.LICENSE').read_text()
    assert 'MIT License' in license_text
    assert 'Copyright (c) 2023 shadcn' in license_text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in license_text
    package = json.loads((root / 'frontend/package.json').read_text())
    assert 'shadcn' not in package['devDependencies']
    lock = json.loads((root / 'frontend/package-lock.json').read_text())
    for name in ('shadcn', 'braces', 'micromatch', 'fast-glob', 'ts-morph'):
        assert not any(key.endswith('node_modules/' + name) for key in lock['packages'])
    assert '@import "./shadcn-tailwind.css";' in (root / 'frontend/src/index.css').read_text()
