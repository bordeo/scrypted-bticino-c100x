"""SRTP media session for Classe 100X monitoring (no aiortc / PyAV).

Owns UDP sockets, STUN priming, SRTP decrypt/encrypt via pylibsrtp, G.711
silence keepalive, and optional talkback uplink. Plain RTP is forwarded to an
RTSP client callback.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import socket
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pylibsrtp

from .g711 import encode_g711, silence_frame
from .sdp import Codec, NegotiatedSession, build_monitoring_offer, parse_answer
from .sip import SipClient
from .stun import StunError, binding_request, discover_mappings, is_stun, reply_binding

_LOGGER = logging.getLogger(__name__)

RtpCallback = Callable[[bytes, str], Awaitable[None] | None]
# kind is "video" | "audio"


@dataclass(slots=True)
class RtpHeader:
    """Parsed RTP header fields used by the media path."""

    version: int
    padding: bool
    extension: bool
    csrc_count: int
    marker: bool
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    header_length: int


class MediaSessionError(RuntimeError):
    """The monitoring media pipeline failed safely."""


def parse_rtp(packet: bytes) -> tuple[RtpHeader, bytes]:
    """Parse a plain RTP packet into header + payload."""
    if len(packet) < 12:
        raise ValueError("RTP packet too short")
    b0, b1 = packet[0], packet[1]
    version = b0 >> 6
    if version != 2:
        raise ValueError("Unsupported RTP version")
    padding = bool(b0 & 0x20)
    extension = bool(b0 & 0x10)
    csrc_count = b0 & 0x0F
    marker = bool(b1 & 0x80)
    payload_type = b1 & 0x7F
    sequence, timestamp, ssrc = struct.unpack("!HII", packet[2:12])
    offset = 12 + (csrc_count * 4)
    if extension:
        if len(packet) < offset + 4:
            raise ValueError("RTP extension truncated")
        ext_len = struct.unpack("!H", packet[offset + 2 : offset + 4])[0]
        offset += 4 + (ext_len * 4)
    if offset > len(packet):
        raise ValueError("RTP header truncated")
    payload = packet[offset:]
    if padding and payload:
        pad = payload[-1]
        if pad and pad <= len(payload):
            payload = payload[:-pad]
    header = RtpHeader(
        version=version,
        padding=padding,
        extension=extension,
        csrc_count=csrc_count,
        marker=marker,
        payload_type=payload_type,
        sequence=sequence,
        timestamp=timestamp,
        ssrc=ssrc,
        header_length=offset,
    )
    return header, payload


def serialize_rtp(
    *,
    payload_type: int,
    sequence: int,
    timestamp: int,
    ssrc: int,
    payload: bytes,
    marker: bool = False,
) -> bytes:
    """Serialize one RTP packet (no CSRC / extension)."""
    b0 = 0x80
    b1 = payload_type & 0x7F
    if marker:
        b1 |= 0x80
    return struct.pack("!BBHII", b0, b1, sequence & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc) + payload


def is_h264_keyframe(payload: bytes) -> bool:
    """Return True when an H.264 NAL / FU-A payload contains a keyframe (IDR)."""
    if not payload:
        return False
    nal_type = payload[0] & 0x1F
    if nal_type == 5:  # IDR
        return True
    if nal_type == 28 and len(payload) >= 2:  # FU-A
        # Start bit and IDR type in FU header
        fu = payload[1]
        if (fu & 0x80) and (fu & 0x1F) == 5:
            return True
    if nal_type == 24:  # STAP-A
        offset = 1
        while offset + 2 <= len(payload):
            size = struct.unpack("!H", payload[offset : offset + 2])[0]
            offset += 2
            if size <= 0 or offset + size > len(payload):
                break
            if (payload[offset] & 0x1F) == 5:
                return True
            offset += size
    return False


def reserve_even_pair(bind_host: str = "0.0.0.0") -> tuple[tuple[socket.socket, socket.socket], int]:
    """Bind an even RTP + odd RTCP UDP port pair."""
    for _ in range(100):
        port = secrets.randbelow(10000) * 2 + 40000
        first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        second = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            first.bind((bind_host, port))
            second.bind((bind_host, port + 1))
            return (first, second), port
        except OSError:
            first.close()
            second.close()
    raise MediaSessionError("No RTP port pair is available")


class _StunResponderProtocol(asyncio.DatagramProtocol):
    """Answer relay STUN Binding Requests on an otherwise unused RTCP port."""

    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        reply_binding(self.transport, data, addr)


class _MediaRtpProtocol(asyncio.DatagramProtocol):
    """Receive SRTP on one port, unprotect, and forward plain RTP."""

    def __init__(
        self,
        kind: str,
        on_plain_rtp: Callable[[bytes, str], None],
        on_first_media: Callable[[], None] | None = None,
    ) -> None:
        self.kind = kind
        self.on_plain_rtp = on_plain_rtp
        self.on_first_media = on_first_media
        self.transport: asyncio.DatagramTransport | None = None
        self._inbound: pylibsrtp.Session | None = None
        self._outbound: pylibsrtp.Session | None = None
        self._codec: Codec | None = None
        self._remote: tuple[str, int] | None = None
        self._talkback = False
        self._pcm = bytearray()
        self._sequence = secrets.randbelow(65536)
        self._timestamp = secrets.randbelow(2**32)
        self._ssrc = secrets.randbelow(2**32)
        self._first_packet = True
        self._got_media = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def configure_video(self, session: NegotiatedSession) -> None:
        media = session.video
        if media is None or media.crypto is None:
            raise MediaSessionError("No negotiated video")
        self._remote = (media.connection, media.port)
        self._codec = media.codec({"h264"})
        inbound_policy = pylibsrtp.Policy(
            key=media.crypto.key,
            ssrc_type=pylibsrtp.Policy.SSRC_ANY_INBOUND,
            srtp_profile=pylibsrtp.Policy.SRTP_PROFILE_AES128_CM_SHA1_80,
        )
        self._inbound = pylibsrtp.Session(inbound_policy)

    def configure_audio(self, session: NegotiatedSession) -> None:
        media = session.audio
        if media is None or media.crypto is None:
            return
        codec = media.codec({"pcma", "pcmu"})
        if codec is None:
            raise MediaSessionError("No supported negotiated audio codec")
        self._codec = codec
        self._remote = (media.connection, media.port)
        inbound_policy = pylibsrtp.Policy(
            key=media.crypto.key,
            ssrc_type=pylibsrtp.Policy.SSRC_ANY_INBOUND,
            srtp_profile=pylibsrtp.Policy.SRTP_PROFILE_AES128_CM_SHA1_80,
        )
        outbound_policy = pylibsrtp.Policy(
            key=session.offer.audio_crypto.key,
            ssrc_type=pylibsrtp.Policy.SSRC_ANY_OUTBOUND,
            srtp_profile=pylibsrtp.Policy.SRTP_PROFILE_AES128_CM_SHA1_80,
        )
        self._inbound = pylibsrtp.Session(inbound_policy)
        self._outbound = pylibsrtp.Session(outbound_policy)

    def prime_remote(self) -> None:
        if self.transport is not None and self._remote is not None:
            self.transport.sendto(binding_request(), self._remote)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if reply_binding(self.transport, data, addr):
            return
        if self._inbound is None:
            return
        try:
            plain = self._inbound.unprotect(data)
        except pylibsrtp.Error:
            return
        if not self._got_media:
            self._got_media = True
            if self.on_first_media:
                self.on_first_media()
        self.on_plain_rtp(plain, self.kind)

    def set_talkback(self, enabled: bool) -> None:
        self._talkback = enabled
        if not enabled:
            self._pcm.clear()

    def send_silence(self) -> None:
        if self._talkback or self._codec is None:
            return
        try:
            payload = silence_frame(self._codec.name)
            self._send_g711_payload(payload, samples=160)
        except (ValueError, pylibsrtp.Error):
            return

    def send_pcm(self, pcm: bytes) -> None:
        if not self._talkback or self._codec is None:
            return
        self._pcm.extend(pcm)
        packet_bytes = self._codec.clock_rate // 50 * 2  # 20 ms mono s16
        while len(self._pcm) >= packet_bytes:
            chunk = bytes(self._pcm[:packet_bytes])
            del self._pcm[:packet_bytes]
            samples = len(chunk) // 2
            payload = encode_g711(chunk, self._codec.name)
            self._send_g711_payload(payload, samples=samples)

    def _send_g711_payload(self, payload: bytes, *, samples: int) -> None:
        if (
            self._codec is None
            or self._outbound is None
            or self._remote is None
            or self.transport is None
        ):
            return
        packet = serialize_rtp(
            payload_type=self._codec.payload_type,
            sequence=self._sequence,
            timestamp=self._timestamp,
            ssrc=self._ssrc,
            payload=payload,
            marker=self._first_packet,
        )
        protected = self._outbound.protect(packet)
        self.transport.sendto(protected, self._remote)
        self._first_packet = False
        self._sequence = (self._sequence + 1) & 0xFFFF
        self._timestamp = (self._timestamp + samples) & 0xFFFFFFFF

    @property
    def codec(self) -> Codec | None:
        return self._codec

    def close(self) -> None:
        self.set_talkback(False)
        if self.transport:
            self.transport.close()
            self.transport = None


class SrtpMediaSession:
    """One monitoring call's media: STUN, SRTP, silence, talkback, RTP forward."""

    def __init__(
        self,
        sip: SipClient,
        on_plain_rtp: RtpCallback | None = None,
        on_keyframe: Callable[[bytes], None] | None = None,
    ) -> None:
        self.sip = sip
        self.on_plain_rtp = on_plain_rtp
        self.on_keyframe = on_keyframe
        self.negotiated: NegotiatedSession | None = None
        self.audio: _MediaRtpProtocol | None = None
        self.video: _MediaRtpProtocol | None = None
        self.talkback_enabled = False
        self._audio_rtcp: asyncio.DatagramTransport | None = None
        self._video_rtcp: asyncio.DatagramTransport | None = None
        self._primer: asyncio.Task[None] | None = None
        self._keepalive: asyncio.Task[None] | None = None
        self._idle: asyncio.Task[None] | None = None
        self._first_video = asyncio.Event()
        self._last_packet = 0.0
        self._ending = False
        self._last_keyframe: bytes | None = None
        self.sip.on_call_end = self.remote_ended

    @property
    def active(self) -> bool:
        return self.negotiated is not None and not self._ending

    @property
    def last_keyframe(self) -> bytes | None:
        return self._last_keyframe

    def _dispatch_rtp(self, packet: bytes, kind: str) -> None:
        self._last_packet = time.monotonic()
        if kind == "video":
            try:
                _header, payload = parse_rtp(packet)
            except ValueError:
                payload = b""
            else:
                if is_h264_keyframe(payload):
                    # Cache raw annex-B-ish NAL for snapshot helpers; store RTP payload.
                    self._last_keyframe = payload
                    if self.on_keyframe:
                        self.on_keyframe(payload)
                if not self._first_video.is_set():
                    self._first_video.set()
        if self.on_plain_rtp:
            result = self.on_plain_rtp(packet, kind)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)

    async def start(self, device_address: str) -> NegotiatedSession:
        if self.negotiated:
            raise MediaSessionError("A media session is already active")
        self._ending = False
        self.talkback_enabled = False
        audio_sockets, audio_port = await asyncio.to_thread(reserve_even_pair)
        video_sockets, video_port = await asyncio.to_thread(reserve_even_pair)
        try:
            mappings = await asyncio.to_thread(
                discover_mappings, audio_sockets + video_sockets
            )
        except StunError as err:
            for reserved in audio_sockets + video_sockets:
                reserved.close()
            raise MediaSessionError(str(err)) from err
        except Exception:
            for reserved in audio_sockets + video_sockets:
                reserved.close()
            raise
        public_addresses = {item[0] for item in mappings}
        if len(public_addresses) != 1:
            for reserved in audio_sockets + video_sockets:
                reserved.close()
            raise MediaSessionError("STUN returned inconsistent public media addresses")
        address = public_addresses.pop()
        loop = asyncio.get_running_loop()
        try:
            _audio_transport, audio_proto = await loop.create_datagram_endpoint(
                lambda: _MediaRtpProtocol(
                    "audio",
                    self._dispatch_rtp,
                    on_first_media=lambda: _LOGGER.debug("inbound audio RTP"),
                ),
                sock=audio_sockets[0],
            )
            audio_rtcp, _ = await loop.create_datagram_endpoint(
                _StunResponderProtocol, sock=audio_sockets[1]
            )
            _video_transport, video_proto = await loop.create_datagram_endpoint(
                lambda: _MediaRtpProtocol("video", self._dispatch_rtp),
                sock=video_sockets[0],
            )
            video_rtcp, _ = await loop.create_datagram_endpoint(
                _StunResponderProtocol, sock=video_sockets[1]
            )
        except OSError as err:
            for reserved in audio_sockets + video_sockets:
                with contextlib.suppress(OSError):
                    reserved.close()
            raise MediaSessionError("RTP socket activation failed") from err

        self.audio = audio_proto
        self.video = video_proto
        self._audio_rtcp = audio_rtcp
        self._video_rtcp = video_rtcp
        offer = build_monitoring_offer(
            address=address,
            audio_port=audio_port,
            video_port=video_port,
            device_address=device_address,
            advertised_audio_port=mappings[0][1],
            advertised_audio_rtcp_port=mappings[1][1],
            advertised_video_port=mappings[2][1],
            advertised_video_rtcp_port=mappings[3][1],
        )
        try:
            answer = await self.sip.start_monitoring(offer.sdp)
            session = parse_answer(answer, offer)
            self.negotiated = session
            self.audio.configure_audio(session)
            self.video.configure_video(session)
            self.audio.prime_remote()
            self.video.prime_remote()
            if session.audio:
                audio_rtcp.sendto(
                    binding_request(), (session.audio.connection, session.audio.port + 1)
                )
            if session.video:
                video_rtcp.sendto(
                    binding_request(), (session.video.connection, session.video.port + 1)
                )
            self._first_video = asyncio.Event()
            self._last_packet = time.monotonic()
            self._primer = asyncio.create_task(self._prime_relay())
            self._keepalive = asyncio.create_task(self._audio_keepalive())
            return session
        except Exception:
            await self._teardown(send_bye=True)
            raise

    async def end(self) -> None:
        await self._teardown(send_bye=True)

    async def remote_ended(self) -> None:
        await self._teardown(send_bye=False)

    async def set_talkback(self, enabled: bool) -> None:
        if enabled and (self.negotiated is None or self.negotiated.audio is None or self.audio is None):
            raise MediaSessionError("Talkback requires an active negotiated call")
        if (
            enabled
            and self.negotiated
            and self.negotiated.audio
            and self.negotiated.audio.direction not in {"sendrecv", "recvonly"}
        ):
            raise MediaSessionError("The outdoor unit did not negotiate microphone reception")
        self.talkback_enabled = enabled
        if self.audio:
            self.audio.set_talkback(enabled)

    def send_talkback_pcm(self, pcm: bytes) -> None:
        if self.talkback_enabled and self.audio:
            self.audio.send_pcm(pcm)

    async def _prime_relay(self) -> None:
        try:
            while not self._first_video.is_set() and not self._ending:
                if self.audio:
                    self.audio.prime_remote()
                if self.video:
                    self.video.prime_remote()
                session = self.negotiated
                if session and session.audio and self._audio_rtcp:
                    self._audio_rtcp.sendto(
                        binding_request(),
                        (session.audio.connection, session.audio.port + 1),
                    )
                if session and session.video and self._video_rtcp:
                    self._video_rtcp.sendto(
                        binding_request(),
                        (session.video.connection, session.video.port + 1),
                    )
                try:
                    await asyncio.wait_for(self._first_video.wait(), timeout=1.5)
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise

    async def _audio_keepalive(self) -> None:
        try:
            while not self._ending:
                session = self.negotiated
                if (
                    not self.talkback_enabled
                    and self.audio
                    and session
                    and session.audio
                    and session.audio.direction in {"sendrecv", "recvonly"}
                ):
                    self.audio.send_silence()
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            raise

    async def _teardown(self, *, send_bye: bool) -> None:
        if self._ending:
            return
        self._ending = True
        try:
            self.talkback_enabled = False
            if self.audio:
                self.audio.set_talkback(False)
            for attr in ("_keepalive", "_primer", "_idle"):
                task = getattr(self, attr)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    setattr(self, attr, None)
            if self.audio:
                self.audio.close()
                self.audio = None
            if self.video:
                self.video.close()
                self.video = None
            if self._audio_rtcp:
                self._audio_rtcp.close()
                self._audio_rtcp = None
            if self._video_rtcp:
                self._video_rtcp.close()
                self._video_rtcp = None
            if send_bye:
                with contextlib.suppress(Exception):
                    await self.sip.end_monitoring()
            self.negotiated = None
        finally:
            self._ending = False


def next_reconnect_delay(attempt: int) -> float:
    """Return the backoff delay for reconnect attempt ``attempt`` (0-based)."""
    from .const import SIP_RECONNECT_BACKOFF

    if attempt < 0:
        attempt = 0
    if attempt >= len(SIP_RECONNECT_BACKOFF):
        return float(SIP_RECONNECT_BACKOFF[-1])
    return float(SIP_RECONNECT_BACKOFF[attempt])


def new_client_id() -> str:
    """Allocate a fresh Legrand SIP clientId (never reuse HA's)."""
    return str(secrets.randbelow(9) + 1) + "".join(
        str(secrets.randbelow(10)) for _ in range(21)
    )
