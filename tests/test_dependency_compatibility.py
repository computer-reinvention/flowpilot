"""Actual dependency boundaries, using only synthetic local data and transports."""
import importlib.metadata
import ssl

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
