"""RTSP handshake with a fake RTP forward (offline, loopback only)."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from c100x.rtsp_server import RtspServer
from c100x.srtp_media import serialize_rtp


async def _rtsp_exchange(reader, writer, lines: list[str], body: bytes = b"") -> bytes:
    payload = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
    writer.write(payload)
    await writer.drain()
    # Read until Content-Length satisfied
    header = b""
    while b"\r\n\r\n" not in header:
        chunk = await asyncio.wait_for(reader.read(1), timeout=2)
        if not chunk:
            break
        header += chunk
    headers_blob, _, rest = header.partition(b"\r\n\r\n")
    length = 0
    for line in headers_blob.decode().split("\r\n")[1:]:
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
    body_out = rest
    while len(body_out) < length:
        body_out += await reader.readexactly(length - len(body_out))
    return headers_blob + b"\r\n\r\n" + body_out


@pytest.mark.asyncio
async def test_rtsp_options_describe_setup_play_teardown_and_rtp() -> None:
    received = []

    async def on_connect(session):
        received.append("connect")

    async def on_teardown(session):
        received.append("teardown")

    server = RtspServer(on_connect=on_connect, on_teardown=on_teardown, idle_seconds=5)
    url = await server.start()
    host, port = "127.0.0.1", server.port

    reader, writer = await asyncio.open_connection(host, port)
    try:
        resp = await _rtsp_exchange(
            reader,
            writer,
            [f"OPTIONS {url} RTSP/1.0", "CSeq: 1"],
        )
        assert b"200 OK" in resp
        assert b"DESCRIBE" in resp

        resp = await _rtsp_exchange(
            reader,
            writer,
            [f"DESCRIBE {url} RTSP/1.0", "CSeq: 2", "Accept: application/sdp"],
        )
        assert b"200 OK" in resp
        assert b"H264" in resp

        resp = await _rtsp_exchange(
            reader,
            writer,
            [
                f"SETUP {url}trackID=0 RTSP/1.0",
                "CSeq: 3",
                "Transport: RTP/AVP/TCP;unicast;interleaved=0-1",
            ],
        )
        assert b"200 OK" in resp
        assert b"interleaved=0-1" in resp

        resp = await _rtsp_exchange(
            reader,
            writer,
            [
                f"SETUP {url}trackID=1 RTSP/1.0",
                "CSeq: 4",
                "Transport: RTP/AVP/TCP;unicast;interleaved=2-3",
                "Session: 1",
            ],
        )
        assert b"200 OK" in resp

        resp = await _rtsp_exchange(
            reader,
            writer,
            [f"PLAY {url} RTSP/1.0", "CSeq: 5", "Session: 1"],
        )
        assert b"200 OK" in resp

        packet = serialize_rtp(
            payload_type=96,
            sequence=1,
            timestamp=0,
            ssrc=1,
            payload=b"\x65\x00\x01",
            marker=True,
        )
        await server.forward_rtp(packet, "video")
        # Read interleaved frame
        dollar = await asyncio.wait_for(reader.readexactly(1), timeout=2)
        assert dollar == b"$"
        ch_len = await reader.readexactly(3)
        channel = ch_len[0]
        length = int.from_bytes(ch_len[1:3], "big")
        data = await reader.readexactly(length)
        assert channel == 0
        assert data == packet

        resp = await _rtsp_exchange(
            reader,
            writer,
            [f"TEARDOWN {url} RTSP/1.0", "CSeq: 6", "Session: 1"],
        )
        assert b"200 OK" in resp
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        await server.stop()

    assert "connect" in received
    assert "teardown" in received


@pytest.mark.asyncio
async def test_rtsp_udp_setup_accepted() -> None:
    """Scrypted/HomeKit try UDP first; accept client_port SETUP and forward RTP."""
    server = RtspServer(idle_seconds=5)
    url = await server.start()
    loop = asyncio.get_running_loop()

    class Collector(asyncio.DatagramProtocol):
        def __init__(self):
            self.packets = []

        def datagram_received(self, data, addr):
            self.packets.append(data)

    transport, protocol = await loop.create_datagram_endpoint(
        Collector, local_addr=("127.0.0.1", 0)
    )
    client_port = transport.get_extra_info("sockname")[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        resp = await _rtsp_exchange(
            reader,
            writer,
            [
                f"SETUP {url}trackID=0 RTSP/1.0",
                "CSeq: 1",
                f"Transport: RTP/AVP;unicast;client_port={client_port}-{client_port + 1}",
            ],
        )
        assert b"200 OK" in resp
        assert b"client_port=" in resp
        assert b"server_port=" in resp

        resp = await _rtsp_exchange(
            reader,
            writer,
            [f"PLAY {url} RTSP/1.0", "CSeq: 2", "Session: 1"],
        )
        assert b"200 OK" in resp

        packet = serialize_rtp(
            payload_type=96,
            sequence=1,
            timestamp=0,
            ssrc=1,
            payload=b"\x65\x00\x01",
            marker=True,
        )
        await server.forward_rtp(packet, "video")
        for _ in range(50):
            if protocol.packets:
                break
            await asyncio.sleep(0.02)
        assert protocol.packets
        assert protocol.packets[0] == packet
    finally:
        transport.close()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        await server.stop()


async def _drain_interleaved(reader: asyncio.StreamReader, *, rounds: int = 32) -> None:
    """Read interleaved RTP so the TCP send buffer does not stall the server."""
    _eof = (TimeoutError, asyncio.TimeoutError, asyncio.exceptions.IncompleteReadError)
    for _ in range(rounds):
        try:
            dollar = await asyncio.wait_for(reader.readexactly(1), timeout=0.02)
        except _eof:
            return
        if dollar != b"$":
            return
        try:
            ch_len = await reader.readexactly(3)
            length = int.from_bytes(ch_len[1:3], "big")
            await reader.readexactly(length)
        except _eof:
            return


@pytest.mark.asyncio
async def test_rtsp_playing_idle_refreshed_by_rtp() -> None:
    """RTP activity must keep the session alive past a short idle window."""
    server = RtspServer(idle_seconds=0.8)
    url = await server.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        await _rtsp_exchange(
            reader,
            writer,
            [
                f"SETUP {url}trackID=0 RTSP/1.0",
                "CSeq: 1",
                "Transport: RTP/AVP/TCP;unicast;interleaved=0-1",
            ],
        )
        await _rtsp_exchange(
            reader, writer, [f"PLAY {url} RTSP/1.0", "CSeq: 2", "Session: 1"]
        )
        assert server.session is not None and server.session.playing

        packet = serialize_rtp(
            payload_type=96, sequence=1, timestamp=0, ssrc=1, payload=b"\x65"
        )
        # Keep RTP flowing longer than idle_seconds; drain so TCP does not back-pressure.
        for _ in range(10):
            await server.forward_rtp(packet, "video")
            await _drain_interleaved(reader)
            await asyncio.sleep(0.12)
            assert server.session is not None and not server.session.closed

        resp = await _rtsp_exchange(
            reader,
            writer,
            [f"GET_PARAMETER {url} RTSP/1.0", "CSeq: 3", "Session: 1"],
        )
        assert b"200 OK" in resp
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        await server.stop()


@pytest.mark.asyncio
async def test_rtsp_describe_advertises_sendonly() -> None:
    server = RtspServer(idle_seconds=5)
    url = await server.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        resp = await _rtsp_exchange(
            reader,
            writer,
            [f"DESCRIBE {url} RTSP/1.0", "CSeq: 1", "Accept: application/sdp"],
        )
        assert b"200 OK" in resp
        assert b"a=sendonly" in resp
        assert b"a=recvonly" not in resp
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        await server.stop()


@pytest.mark.asyncio
async def test_rtsp_buffers_rtp_until_play() -> None:
    server = RtspServer(idle_seconds=5)
    url = await server.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        await _rtsp_exchange(reader, writer, [f"OPTIONS {url} RTSP/1.0", "CSeq: 1"])
        await _rtsp_exchange(
            reader, writer, [f"DESCRIBE {url} RTSP/1.0", "CSeq: 2", "Accept: application/sdp"]
        )
        await _rtsp_exchange(
            reader,
            writer,
            [
                f"SETUP {url}trackID=0 RTSP/1.0",
                "CSeq: 3",
                "Transport: RTP/AVP/TCP;unicast;interleaved=0-1",
            ],
        )

        packet = serialize_rtp(
            payload_type=96,
            sequence=1,
            timestamp=0,
            ssrc=1,
            payload=b"\x65\x00\x01",
            marker=True,
        )
        await server.forward_rtp(packet, "video")

        # Nothing should be on the wire before PLAY.
        writer.write(b"")  # no-op; just ensure we can wait briefly
        await writer.drain()
        try:
            early = await asyncio.wait_for(reader.readexactly(1), timeout=0.15)
        except (TimeoutError, asyncio.TimeoutError):
            # On 3.10 asyncio.TimeoutError is still distinct from builtin TimeoutError.
            early = b""
        assert early == b""

        resp = await _rtsp_exchange(
            reader, writer, [f"PLAY {url} RTSP/1.0", "CSeq: 4", "Session: 1"]
        )
        assert b"200 OK" in resp

        dollar = await asyncio.wait_for(reader.readexactly(1), timeout=2)
        assert dollar == b"$"
        ch_len = await reader.readexactly(3)
        length = int.from_bytes(ch_len[1:3], "big")
        data = await reader.readexactly(length)
        assert ch_len[0] == 0
        assert data == packet
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        await server.stop()
