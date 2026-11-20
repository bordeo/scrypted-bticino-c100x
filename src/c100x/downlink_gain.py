"""Door→HomeKit downlink gain: Speex/Opus/G.711 → volume+limiter → Opus.

Downlink from the panel is SRTP-decrypt + plain RTP into our RTSP server.
Scrypted/HomeKit remux that RTP without a plugin-controlled ``volume=`` stage,
so quiet Speex cannot be boosted via MediaStreamUrl args. When gain > 0 we
transcode here: ffmpeg decodes the negotiated codec, applies ``volume=NdB``
plus ``alimiter``, and re-encodes Opus for the RTSP audio track.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import tempfile
from collections.abc import Awaitable, Callable  # Awaitable used in OnOpusRtp
from typing import Any

from .audio_codec import resolve_ffmpeg
from .const import DOOR_AUDIO_GAIN_MAX_DB, DOOR_AUDIO_GAIN_MIN_DB

_LOGGER = logging.getLogger(__name__)

OnOpusRtp = Callable[[bytes], Awaitable[None] | None]


def clamp_door_audio_gain_db(value: Any, default: int | None = None) -> int:
    """Parse and clamp the door-audio gain setting to 0..24 dB."""
    if default is None:
        # Resolve at call time so tests can patch ``c100x.const.DEFAULT_…``.
        from .const import DEFAULT_DOOR_AUDIO_GAIN_DB as _default

        default = _default
    try:
        if isinstance(value, bool):
            raise TypeError
        gain = int(float(value))
    except (TypeError, ValueError):
        gain = int(default)
    return max(DOOR_AUDIO_GAIN_MIN_DB, min(DOOR_AUDIO_GAIN_MAX_DB, gain))


def ffmpeg_volume_filter(gain_db: int) -> str:
    """Build the ffmpeg ``-af`` chain for gain + peak limiter."""
    gain = clamp_door_audio_gain_db(gain_db)
    # level=false: do not auto-make-up after limiting (we already applied gain).
    return f"volume={gain}dB,alimiter=limit=0.95:level=false"


def _rtpmap_for_codec(codec: Any) -> tuple[int, str]:
    """Return (payload_type, rtpmap body without pt prefix) for an SDP m=audio."""
    pt = int(codec.payload_type)
    name = str(codec.name).casefold()
    rate = int(getattr(codec, "clock_rate", 8000) or 8000)
    channels = int(getattr(codec, "channels", 1) or 1)
    if name == "opus":
        ch = channels if channels > 1 else 2
        return pt, f"opus/{rate}/{ch}"
    if name == "speex":
        return pt, f"speex/{rate}"
    if name == "pcmu":
        return pt, "PCMU/8000"
    return pt, "PCMA/8000"


class DoorAudioGainTranscoder:
    """ffmpeg RTP→(volume+alimiter)→Opus RTP bridge for one monitoring call."""

    # PT 96 matches the pre-negotiation RTSP DESCRIBE default (Opus). Using a
    # different PT (e.g. 97) raced DESCRIBE: HomeKit locked pt=96 then dropped
    # gained packets when the transcoder came up later (default gain is +12).
    OUTPUT_PAYLOAD_TYPE = 96
    OUTPUT_RTPMAP = "opus/48000/2"

    def __init__(
        self,
        *,
        gain_db: int,
        on_opus_rtp: OnOpusRtp,
        ffmpeg_path: str | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.gain_db = clamp_door_audio_gain_db(gain_db)
        self.on_opus_rtp = on_opus_rtp
        self.ffmpeg_path = resolve_ffmpeg(ffmpeg_path)
        self._on_log = on_log
        self._process: asyncio.subprocess.Process | None = None
        self._out_transport: asyncio.DatagramTransport | None = None
        self._feed_sock: socket.socket | None = None
        self._in_addr: tuple[str, int] | None = None
        self._sdp_path: str | None = None
        self._active = False
        self._logged_first = False

    @property
    def active(self) -> bool:
        return self._active

    def _emit(self, message: str) -> None:
        _LOGGER.info("%s", message)
        if self._on_log:
            with contextlib.suppress(Exception):
                self._on_log(message)

    async def start(self, codec: Any) -> None:
        if self.gain_db <= 0:
            self._emit("Door audio gain 0 dB — RTP passthrough (no transcoder)")
            return
        if not self.ffmpeg_path:
            self._emit("Door audio gain skipped — ffmpeg not found")
            return
        pt, rtpmap = _rtpmap_for_codec(codec)

        loop = asyncio.get_running_loop()
        protocol = _OpusRtpReceiver(self._handle_out)
        self._out_transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            local_addr=("127.0.0.1", 0),
        )
        out_port = self._out_transport.get_extra_info("sockname")[1]

        # Ephemeral input port for ffmpeg's RTP demuxer (SDP m= line).
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        in_port = probe.getsockname()[1]
        probe.close()
        self._in_addr = ("127.0.0.1", in_port)

        sdp = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 127.0.0.1\r\n"
            "s=c100x-door-gain\r\n"
            "c=IN IP4 127.0.0.1\r\n"
            "t=0 0\r\n"
            f"m=audio {in_port} RTP/AVP {pt}\r\n"
            f"a=rtpmap:{pt} {rtpmap}\r\n"
        )
        fd, self._sdp_path = tempfile.mkstemp(prefix="c100x-gain-", suffix=".sdp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(sdp)

        af = ffmpeg_volume_filter(self.gain_db)
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,udp,rtp",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-i",
            self._sdp_path,
            "-af",
            af,
            "-acodec",
            "libopus",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-b:a",
            "24k",
            "-frame_duration",
            "20",
            "-payload_type",
            str(self.OUTPUT_PAYLOAD_TYPE),
            "-f",
            "rtp",
            f"rtp://127.0.0.1:{out_port}?pkt_size=1200",
        ]
        self._feed_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._feed_sock.setblocking(False)
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception:
            await self.close()
            raise
        # Brief settle so ffmpeg binds the SDP listen port before first feed.
        await asyncio.sleep(0.05)
        if self._process.returncode is not None:
            err = b""
            if self._process.stderr:
                with contextlib.suppress(Exception):
                    err = await self._process.stderr.read()
            await self.close()
            raise RuntimeError(
                f"Door audio gain ffmpeg exited early: {err.decode('utf-8', 'replace')[:200]}"
            )
        self._active = True
        self._emit(
            f"Door audio gain transcoder started (+{self.gain_db} dB, "
            f"{rtpmap} → {self.OUTPUT_RTPMAP}, -af {af})"
        )

    async def feed_rtp(self, packet: bytes) -> None:
        if not self._active or not self._feed_sock or not self._in_addr:
            return
        with contextlib.suppress(OSError, BlockingIOError):
            self._feed_sock.sendto(packet, self._in_addr)

    async def _handle_out(self, packet: bytes) -> None:
        if not self._logged_first:
            self._logged_first = True
            self._emit(
                f"Door audio gain: first Opus frame after +{self.gain_db} dB "
                f"({len(packet)} bytes RTP)"
            )
        result = self.on_opus_rtp(packet)
        if asyncio.iscoroutine(result):
            await result

    async def close(self) -> None:
        self._active = False
        process, self._process = self._process, None
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(process.wait(), timeout=1.0)
        if self._out_transport:
            self._out_transport.close()
            self._out_transport = None
        if self._feed_sock:
            with contextlib.suppress(OSError):
                self._feed_sock.close()
            self._feed_sock = None
        if self._sdp_path:
            with contextlib.suppress(OSError):
                os.unlink(self._sdp_path)
            self._sdp_path = None


class _OpusRtpReceiver(asyncio.DatagramProtocol):
    def __init__(self, callback: Callable[[bytes], Awaitable[None] | None]) -> None:
        self._callback = callback

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        if len(data) < 12:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        result = self._callback(data)
        if asyncio.iscoroutine(result):
            loop.create_task(result)
