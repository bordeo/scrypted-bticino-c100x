"""Offline STUN packet builder / parser tests (no network)."""

from __future__ import annotations

import struct

from c100x.const import STUN_BINDING_REQUEST, STUN_BINDING_SUCCESS, STUN_COOKIE
from c100x.stun import binding_request, binding_success, is_stun


def test_binding_request_shape() -> None:
    packet = binding_request()
    assert len(packet) == 20
    message_type, length, cookie = struct.unpack("!HHI", packet[:8])
    assert message_type == STUN_BINDING_REQUEST
    assert length == 0
    assert cookie == STUN_COOKIE
    assert is_stun(packet)


def test_is_stun_rejects_rtp() -> None:
    # RTP version 2 sets the top bits differently from STUN.
    rtp = bytes([0x80, 0x60]) + b"\x00" * 18
    assert not is_stun(rtp)
    assert not is_stun(b"short")


def test_binding_success_xor_mapped_address() -> None:
    request = binding_request()
    response = binding_success(request, ("203.0.113.50", 40000))
    assert response is not None
    message_type, length, cookie, transaction = struct.unpack("!HHI12s", response[:20])
    assert message_type == STUN_BINDING_SUCCESS
    assert cookie == STUN_COOKIE
    assert transaction == request[8:20]
    # Attribute: XOR-MAPPED-ADDRESS
    attr_type, attr_len = struct.unpack("!HH", response[20:24])
    assert attr_type == 0x0020
    value = response[24 : 24 + attr_len]
    assert value[1] == 0x01  # IPv4
    xor_port = struct.unpack("!H", value[2:4])[0]
    port = xor_port ^ (STUN_COOKIE >> 16)
    assert port == 40000
