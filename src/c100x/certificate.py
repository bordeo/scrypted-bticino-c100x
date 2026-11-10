"""mTLS certificate provisioning."""

from __future__ import annotations

import base64
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .api import LegrandApi
from .const import CERTIFICATE_ORGANIZATIONAL_UNIT, CERTIFICATE_TEMPLATE
from .models import CertificateBundle


async def provision_certificate(
    api: LegrandApi,
    *,
    plant: dict,
    gateway_id: str,
    client_id: str,
    user_oid: str,
) -> CertificateBundle:
    """Generate a local private key and submit only its CSR to Legrand."""
    owner_email = str(plant.get("ownerEmail", ""))
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, client_id),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, CERTIFICATE_ORGANIZATIONAL_UNIT),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "LEGRAND"),
            x509.NameAttribute(NameOID.LOCALITY_NAME, "Paris"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "France"),
            x509.NameAttribute(NameOID.COUNTRY_NAME, "FR"),
            *([x509.NameAttribute(NameOID.EMAIL_ADDRESS, owner_email)] if owner_email else []),
        ]
    )
    sip_uri = f"sip:{user_oid}_{client_id}@{gateway_id}.bs.iotleg.com"
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(subject)
        .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(sip_uri)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    response = await api.provision_certificate(
        {
            "csr": base64.b64encode(csr.public_bytes(serialization.Encoding.DER)).decode(),
            "sender": {
                "addressType": "addressLocation",
                "plant": {
                    "_gatewayDbIdx": -1,
                    "country": plant.get("country", "fr"),
                    "dbIdx": -1,
                    "id": plant["id"],
                    "name": plant.get("name", "Home"),
                    "ownerEmail": owner_email,
                    "ownerId": plant.get("ownerId", user_oid),
                    "type": plant.get("type"),
                },
                "system": "information",
            },
            "template": CERTIFICATE_TEMPLATE,
        }
    )
    ca_response = await api.certificate_authority()
    encoded_certificate = str(response["cert"])
    certificate_pem = (
        "-----BEGIN CERTIFICATE-----\n"
        + "\n".join(encoded_certificate[index : index + 64] for index in range(0, len(encoded_certificate), 64))
        + "\n-----END CERTIFICATE-----\n"
    )
    private_key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    certificate = x509.load_pem_x509_certificate(certificate_pem.encode())
    encoded_ca = str(ca_response["chain"])
    ca_pem = (
        "-----BEGIN CERTIFICATE-----\n"
        + "\n".join(
            encoded_ca[index : index + 64]
            for index in range(0, len(encoded_ca), 64)
        )
        + "\n-----END CERTIFICATE-----\n"
    )
    expires_at = certificate_not_valid_after(certificate)
    return CertificateBundle(certificate_pem, private_key_pem, ca_pem, expires_at)


def certificate_expiry(certificate_pem: str) -> datetime:
    """Return the timezone-aware certificate expiry."""
    return certificate_not_valid_after(x509.load_pem_x509_certificate(certificate_pem.encode()))


def certificate_not_valid_after(certificate: x509.Certificate) -> datetime:
    """Return certificate expiry as timezone-aware UTC.

    cryptography 42 added not_valid_after_utc and deprecated not_valid_after.
    cryptography 46 removed not_valid_after. Some mixed installs expose
    not_valid_after_utc as a Python wrapper that still reads not_valid_after
    on a Rust object that no longer has it. Fall back to parsing the TBS.
    """
    for name in ("not_valid_after_utc", "not_valid_after"):
        try:
            value = getattr(certificate, name)
        except AttributeError:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return _expiry_from_tbs(certificate)


def _der_len(data: bytes, index: int) -> tuple[int, int]:
    first = data[index]
    if first < 0x80:
        return first, index + 1
    count = first & 0x7F
    start = index + 1
    return int.from_bytes(data[start : start + count], "big"), start + count


def _der_tlv(data: bytes, index: int) -> tuple[int, bytes, int]:
    tag = data[index]
    length, start = _der_len(data, index + 1)
    end = start + length
    return tag, data[start:end], end


def _der_seq_elements(body: bytes) -> list[tuple[int, bytes]]:
    index = 0
    elements: list[tuple[int, bytes]] = []
    while index < len(body):
        tag, value, index = _der_tlv(body, index)
        elements.append((tag, value))
    return elements


def _parse_asn1_time(tag: int, value: bytes) -> datetime:
    text = value.decode("ascii")
    if tag == 0x17:  # UTCTime YYMMDDHHMMSSZ
        year = int(text[0:2])
        year += 1900 if year >= 50 else 2000
        rest = text[2:]
    elif tag == 0x18:  # GeneralizedTime YYYYMMDDHHMMSSZ
        year = int(text[0:4])
        rest = text[4:]
    else:
        raise ValueError(f"unsupported ASN.1 time tag {tag:#x}")
    return datetime(
        year,
        int(rest[0:2]),
        int(rest[2:4]),
        int(rest[4:6]),
        int(rest[6:8]),
        int(rest[8:10]),
        tzinfo=timezone.utc,
    )


def _expiry_from_tbs(certificate: x509.Certificate) -> datetime:
    tag, body, _ = _der_tlv(certificate.tbs_certificate_bytes, 0)
    if tag != 0x30:
        raise ValueError("TBS certificate is not a SEQUENCE")
    elements = _der_seq_elements(body)
    index = 1 if elements and elements[0][0] == 0xA0 else 0
    _validity_tag, validity_body = elements[index + 3]
    times = _der_seq_elements(validity_body)
    return _parse_asn1_time(*times[1])
