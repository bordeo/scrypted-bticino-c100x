from datetime import datetime, timezone
from types import SimpleNamespace

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from c100x.certificate import _expiry_from_tbs, certificate_expiry, certificate_not_valid_after


def _cert(not_after: datetime) -> x509.Certificate:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "t")])
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime(2026, 1, 1))
        .not_valid_after(not_after.replace(tzinfo=None) if not_after.tzinfo else not_after)
        .sign(key, hashes.SHA256())
    )


def test_expiry_matches_builder_date() -> None:
    expected = datetime(2027, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    cert = _cert(expected)
    assert certificate_not_valid_after(cert) == expected
    pem = cert.public_bytes(__import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.PEM).decode()
    assert certificate_expiry(pem) == expected


def test_tbs_fallback_matches_property() -> None:
    expected = datetime(2028, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    cert = _cert(expected)
    assert _expiry_from_tbs(cert) == expected


def test_prefers_utc_property_then_naive_then_tbs(monkeypatch) -> None:
    expected = datetime(2029, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    cert = _cert(expected)

    def boom_utc(_self):
        raise AttributeError("no not_valid_after")

    # Simulate mixed install: accessing *_utc raises, naive also missing, TBS works.
    monkeypatch.setattr(type(cert), "not_valid_after_utc", property(boom_utc), raising=False)
    try:
        monkeypatch.delattr(type(cert), "not_valid_after")
    except AttributeError:
        monkeypatch.setattr(type(cert), "not_valid_after", property(boom_utc), raising=False)
    assert certificate_not_valid_after(cert) == expected
