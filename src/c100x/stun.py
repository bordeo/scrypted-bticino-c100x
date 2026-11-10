"""RFC 5389 STUN helpers for Classe 100X media NAT/relay priming."""

from __future__ import annotations

import asyncio
import secrets
import socket
import struct

from .const import (
    STUN_BINDING_REQUEST,
    STUN_BINDING_SUCCESS,
    STUN_COOKIE,
    STUN_SERVER,
    STUN_XOR_MAPPED_ADDRESS,
)


class StunError(RuntimeError):
    """STUN discovery or response failed."""


def binding_request() -> bytes:
    """Build an RFC 5389 Binding Request with a fresh transaction id."""
    return struct.pack(
        "!HHI12s", STUN_BINDING_REQUEST, 0, STUN_COOKIE, secrets.token_bytes(12)
    )


def is_stun(data: bytes) -> bool:
    """Return True when ``data`` looks like a STUN packet (not RTP)."""
    if len(data) < 20:
        return False
    _message_type, _length, cookie = struct.unpack("!HHI", data[:8])
    return cookie == STUN_COOKIE and (data[0] & 0xC0) == 0


def binding_success(request: bytes, addr: tuple[str, int]) -> bytes | None:
    """Build a Binding Success with XOR-MAPPED-ADDRESS for ``addr``."""
    if len(request) < 20:
        return None
    host = addr[0]
    if host.startswith("::ffff:"):
        host = host[7:]
    try:
        packed_ip = socket.inet_aton(host)
    except OSError:
        return None
    _message_type, _length, _cookie, transaction = struct.unpack("!HHI12s", request[:20])
    xor_port = addr[1] ^ (STUN_COOKIE >> 16)
    xor_address = struct.unpack("!I", packed_ip)[0] ^ STUN_COOKIE
    value = struct.pack("!BBHI", 0, 0x01, xor_port, xor_address)
    attribute = struct.pack("!HH", STUN_XOR_MAPPED_ADDRESS, len(value)) + value
    return (
        struct.pack("!HHI12s", STUN_BINDING_SUCCESS, len(attribute), STUN_COOKIE, transaction)
        + attribute
    )


def reply_binding(
    transport: asyncio.DatagramTransport | None, data: bytes, addr: tuple[str, int]
) -> bool:
    """Answer a Binding Request; return True when ``data`` was STUN (consumed)."""
    if not is_stun(data):
        return False
    if struct.unpack("!H", data[:2])[0] == STUN_BINDING_REQUEST and transport is not None:
        response = binding_success(data, addr)
        if response is not None:
            transport.sendto(response, addr)
    return True


def mapping_from_socket(media_socket: socket.socket) -> tuple[str, int]:
    """Discover the public UDP endpoint for ``media_socket`` via STUN."""
    candidates = socket.getaddrinfo(
        STUN_SERVER[0], STUN_SERVER[1], socket.AF_INET, socket.SOCK_DGRAM
    )
    transaction = secrets.token_bytes(12)
    request = struct.pack("!HHI12s", STUN_BINDING_REQUEST, 0, STUN_COOKIE, transaction)
    previous_timeout = media_socket.gettimeout()
    try:
        media_socket.settimeout(3)
        media_socket.sendto(request, candidates[0][4])
        response, _source = media_socket.recvfrom(2048)
    except (OSError, TimeoutError) as err:
        raise StunError("STUN media discovery failed") from err
    finally:
        media_socket.settimeout(previous_timeout)
    if len(response) < 20:
        raise StunError("STUN returned a truncated response")
    message_type, length, cookie, response_transaction = struct.unpack(
        "!HHI12s", response[:20]
    )
    if (
        message_type != STUN_BINDING_SUCCESS
        or cookie != STUN_COOKIE
        or response_transaction != transaction
        or len(response) < 20 + length
    ):
        raise StunError("STUN returned an invalid response")
    offset = 20
    while offset + 4 <= 20 + length:
        attribute_type, attribute_length = struct.unpack("!HH", response[offset : offset + 4])
        value = response[offset + 4 : offset + 4 + attribute_length]
        if attribute_type == STUN_XOR_MAPPED_ADDRESS and len(value) >= 8 and value[1] == 0x01:
            xor_port = struct.unpack("!H", value[2:4])[0]
            xor_address = struct.unpack("!I", value[4:8])[0]
            port = xor_port ^ (STUN_COOKIE >> 16)
            address = socket.inet_ntoa(struct.pack("!I", xor_address ^ STUN_COOKIE))
            return address, port
        offset += 4 + ((attribute_length + 3) & ~3)
    raise StunError("STUN response has no IPv4 mapped address")


def discover_mappings(
    sockets: tuple[socket.socket, ...],
) -> tuple[tuple[str, int], ...]:
    """Discover the public UDP endpoint for every RTP/RTCP socket using STUN."""
    return tuple(mapping_from_socket(item) for item in sockets)
