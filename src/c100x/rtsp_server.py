"""Minimal one-client RTSP server (TCP interleaved) for Scrypted MediaStreamUrl.

Supports OPTIONS / DESCRIBE / SETUP / PLAY / TEARDOWN. On client connect the
caller places a monitoring INVITE; plain RTP is forwarded as interleaved
channels. Only one session at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)

OnConnect = Callable[["RtspSession"], Awaitable[None]]
OnTeardown = Callable[["RtspSession"], Awaitable[None]]
OnLog = Callable[[str], None]


@dataclass(slots=True)
class RtspSession:
    """One RTSP TCP client session."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    server: "RtspServer"
    cseq: int = 0
    session_id: str = "1"
    playing: bool = False
    video_channel: int = 0
    audio_channel: int = 2
    closed: bool = False
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def peer(self) -> str:
        try:
            addr = self.writer.get_extra_info("peername")
            return str(addr)
        except Exception:
            return "?"

    async def send_rtp(self, packet: bytes, *, channel: int) -> None:
        """Send one interleaved RTP packet ($ + channel + length + data)."""
        if self.closed or not self.playing:
            return
        header = b"$" + bytes([channel & 0xFF]) + len(packet).to_bytes(2, "big")
        async with self._send_lock:
            try:
                self.writer.write(header + packet)
                await self.writer.drain()
            except (ConnectionError, OSError):
                self.closed = True

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.playing = False
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


class RtspServer:
    """Single-client RTSP server bound to 127.0.0.1."""

    def __init__(
        self,
        *,
        on_connect: OnConnect | None = None,
        on_teardown: OnTeardown | None = None,
        sdp_builder: Callable[["RtspSession"], str] | None = None,
        idle_seconds: float = 30.0,
        on_log: OnLog | None = None,
    ) -> None:
        self.on_connect = on_connect
        self.on_teardown = on_teardown
        self.sdp_builder = sdp_builder or _default_sdp
        self.idle_seconds = idle_seconds
        self.on_log = on_log
        self._pending: deque[tuple[str, bytes]] = deque()
        self._pending_limit = 256
        self._server: asyncio.AbstractServer | None = None
        self._session: RtspSession | None = None
        self._lock = asyncio.Lock()
        self.host = "127.0.0.1"
        self.port = 0

    @property
    def url(self) -> str:
        return f"rtsp://{self.host}:{self.port}/"

    @property
    def session(self) -> RtspSession | None:
        return self._session

    def _log(self, message: str) -> None:
        _LOGGER.info("%s", message)
        if self.on_log:
            with contextlib.suppress(Exception):
                self.on_log(message)

    async def _flush_pending(self, session: RtspSession) -> None:
        while self._pending:
            kind, packet = self._pending.popleft()
            if session.closed or not session.playing:
                self._pending.clear()
                return
            channel = session.video_channel if kind == "video" else session.audio_channel
            await session.send_rtp(packet, channel=channel)

    async def start(self) -> str:

        self._server = await asyncio.start_server(
            self._handle_client, self.host, 0
        )
        socks = self._server.sockets
        if not socks:
            raise RuntimeError("RTSP server failed to bind")
        self.port = socks[0].getsockname()[1]
        asyncio.create_task(self._server.serve_forever())
        return self.url

    async def stop(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def forward_rtp(self, packet: bytes, kind: str) -> None:
        session = self._session
        if not session or session.closed:
            return
        if not session.playing:
            self._pending.append((kind, packet))
            while len(self._pending) > self._pending_limit:
                self._pending.popleft()
            return
        channel = session.video_channel if kind == "video" else session.audio_channel
        await session.send_rtp(packet, channel=channel)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        async with self._lock:
            if self._session and not self._session.closed:
                # One session at a time: refuse a second client clearly.
                try:
                    writer.write(
                        b"RTSP/1.0 453 Not Enough Bandwidth\r\n"
                        b"CSeq: 0\r\n"
                        b"Content-Length: 0\r\n\r\n"
                    )
                    await writer.drain()
                finally:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
                self._log("RTSP: rejected second client (one session at a time)")
                return
            session = RtspSession(reader=reader, writer=writer, server=self)
            self._session = session

        try:
            if self.on_connect:
                await self.on_connect(session)
            await self._serve(session)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self._log(f"RTSP session ended: {type(err).__name__}: {err}")
        finally:
            if self.on_teardown:
                with contextlib.suppress(Exception):
                    await self.on_teardown(session)
            await session.close()
            if self._session is session:
                self._session = None
            self._pending.clear()

    async def _serve(self, session: RtspSession) -> None:
        while not session.closed:
            try:
                request = await asyncio.wait_for(
                    _read_rtsp_request(session.reader),
                    timeout=self.idle_seconds if session.playing else 60.0,
                )
            except TimeoutError as err:
                if session.playing:
                    _LOGGER.info("RTSP idle timeout; tearing down")
                    raise
                raise ConnectionError("RTSP handshake timeout") from err
            if request is None:
                break
            method, url, headers, _body = request
            session.cseq = int(headers.get("cseq", "0"))
            if method == "OPTIONS":
                await _respond(
                    session,
                    200,
                    "OK",
                    extra={"Public": "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN"},
                )
            elif method == "DESCRIBE":
                sdp = self.sdp_builder(session)
                await _respond(
                    session,
                    200,
                    "OK",
                    body=sdp.encode(),
                    extra={
                        "Content-Type": "application/sdp",
                        "Content-Base": self.url,
                    },
                )
            elif method == "SETUP":
                transport = headers.get("transport", "")
                transport_l = transport.lower()
                # Only TCP interleaved is implemented. Scrypted's RtspClient
                # tries UDP first and falls back to TCP only on RtspStatusError.
                # A 200 with a TCP Transport for a UDP SETUP makes it skip the
                # fallback, so the player never receives RTP.
                if "interleaved=" not in transport_l and "/tcp" not in transport_l:
                    self._log("RTSP SETUP rejected: 461 Unsupported Transport")
                    await _respond(session, 461, "Unsupported Transport")
                    continue
                channel = 0
                match = re.search(r"interleaved=(\d+)(?:-(\d+))?", transport)
                if match:
                    channel = int(match.group(1))
                if "trackID=1" in url or "track/audio" in url or "streamid=1" in url:
                    session.audio_channel = channel
                    track = "audio"
                else:
                    session.video_channel = channel
                    track = "video"
                reply_transport = (
                    f"RTP/AVP/TCP;unicast;interleaved={channel}-{channel + 1};mode=play"
                )
                await _respond(
                    session,
                    200,
                    "OK",
                    extra={
                        "Transport": reply_transport,
                        "Session": session.session_id,
                        "X-Track": track,
                    },
                )
            elif method == "PLAY":
                session.playing = True
                await _respond(
                    session,
                    200,
                    "OK",
                    extra={
                        "Session": session.session_id,
                        "RTP-Info": f"url={self.url}trackID=0,seq=0",
                    },
                )
                self._log("RTSP PLAY")
                await self._flush_pending(session)
            elif method == "TEARDOWN":
                await _respond(session, 200, "OK", extra={"Session": session.session_id})
                session.playing = False
                break
            else:
                await _respond(session, 501, "Not Implemented")


def _default_sdp(session: RtspSession) -> str:
    return "\r\n".join(
        [
            "v=0",
            "o=- 0 0 IN IP4 127.0.0.1",
            "s=BTicino C100X",
            "c=IN IP4 127.0.0.1",
            "t=0 0",
            "m=video 0 RTP/AVP 96",
            "a=rtpmap:96 H264/90000",
            "a=fmtp:96 packetization-mode=1;profile-level-id=42e01f",
            "a=control:trackID=0",
            "a=sendonly",
            "m=audio 0 RTP/AVP 8",
            "a=rtpmap:8 PCMA/8000",
            "a=control:trackID=1",
            "a=sendonly",
            "",
        ]
    )


async def _read_rtsp_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], bytes] | None:
    # Interleaved RTP from client is rare for our receive-only path; skip $-framed.
    while True:
        first = await reader.read(1)
        if not first:
            return None
        if first == b"$":
            header = await reader.readexactly(3)
            length = int.from_bytes(header[1:3], "big")
            await reader.readexactly(length)
            continue
        line = first + await reader.readline()
        break
    start = line.decode("utf-8", errors="replace").strip()
    if not start:
        return None
    parts = start.split(" ", 2)
    if len(parts) < 2:
        raise ConnectionError(f"Malformed RTSP request: {start!r}")
    method, url = parts[0], parts[1]
    headers: dict[str, str] = {}
    while True:
        raw = await reader.readline()
        if raw in (b"\r\n", b"\n", b""):
            break
        line_s = raw.decode("utf-8", errors="replace").strip()
        if ":" in line_s:
            name, value = line_s.split(":", 1)
            headers[name.lower().strip()] = value.strip()
    length = int(headers.get("content-length", "0"))
    body = await reader.readexactly(length) if length else b""
    return method, url, headers, body


async def _respond(
    session: RtspSession,
    status: int,
    reason: str,
    *,
    body: bytes = b"",
    extra: dict[str, str] | None = None,
) -> None:
    lines = [f"RTSP/1.0 {status} {reason}", f"CSeq: {session.cseq}"]
    if extra:
        for key, value in extra.items():
            lines.append(f"{key}: {value}")
    lines.append(f"Content-Length: {len(body)}")
    payload = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
    async with session._send_lock:
        session.writer.write(payload)
        await session.writer.drain()

