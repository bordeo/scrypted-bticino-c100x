"""SRTP round-trip and RTP helper tests (offline)."""

from __future__ import annotations

import secrets

import pylibsrtp

from c100x.srtp_media import is_h264_keyframe, parse_rtp, serialize_rtp


def test_rtp_serialize_parse_roundtrip() -> None:
    payload = b"\x00\x01\x02\x03"
    packet = serialize_rtp(
        payload_type=96,
        sequence=1234,
        timestamp=999,
        ssrc=0xAABBCCDD,
        payload=payload,
        marker=True,
    )
    header, out = parse_rtp(packet)
    assert header.payload_type == 96
    assert header.sequence == 1234
    assert header.timestamp == 999
    assert header.ssrc == 0xAABBCCDD
    assert header.marker is True
    assert out == payload


def test_srtp_protect_unprotect_roundtrip() -> None:
    key = secrets.token_bytes(30)
    outbound = pylibsrtp.Session(
        pylibsrtp.Policy(
            key=key,
            ssrc_type=pylibsrtp.Policy.SSRC_ANY_OUTBOUND,
            srtp_profile=pylibsrtp.Policy.SRTP_PROFILE_AES128_CM_SHA1_80,
        )
    )
    inbound = pylibsrtp.Session(
        pylibsrtp.Policy(
            key=key,
            ssrc_type=pylibsrtp.Policy.SSRC_ANY_INBOUND,
            srtp_profile=pylibsrtp.Policy.SRTP_PROFILE_AES128_CM_SHA1_80,
        )
    )
    plain = serialize_rtp(
        payload_type=8,
        sequence=1,
        timestamp=160,
        ssrc=0x12345678,
        payload=b"\xd5" * 160,
    )
    protected = outbound.protect(plain)
    assert protected != plain
    restored = inbound.unprotect(protected)
    assert restored == plain


def test_is_h264_keyframe() -> None:
    assert is_h264_keyframe(bytes([0x65]))  # IDR NAL
    assert not is_h264_keyframe(bytes([0x41]))  # non-IDR slice
    # FU-A start + IDR type
    assert is_h264_keyframe(bytes([0x1C, 0x85]))
    assert not is_h264_keyframe(b"")
