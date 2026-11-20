"""Audio codec helpers for Classe 100X monitoring (Opus / Speex / G.711)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TalkbackFormat:
    """PCM format expected by ``send_talkback_pcm`` / silence keepalive."""

    sample_rate: int
    channels: int = 1
    frame_ms: int = 20

    @property
    def frame_samples(self) -> int:
        return self.sample_rate * self.frame_ms // 1000

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * self.channels * 2  # s16le


def talkback_format_for(codec: Any) -> TalkbackFormat:
    """PCM rate/frame size for encoding talkback toward ``codec``."""
    name = str(codec.name).casefold()
    if name == "opus":
        return TalkbackFormat(48000)
    if name == "speex":
        return TalkbackFormat(int(getattr(codec, "clock_rate", 8000) or 8000))
    return TalkbackFormat(8000)


def ffmpeg_has_encoder(ffmpeg_path: str, name: str) -> bool:
    """Return True when ``ffmpeg -encoders`` lists ``name`` (e.g. libspeex)."""
    try:
        out = subprocess.check_output(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return name in out


def resolve_ffmpeg(path: str | None = None) -> str | None:
    if path:
        return path
    return shutil.which("ffmpeg")


class OpusEncoder:
    """Thin wrapper around opuslib for 20 ms VoIP frames."""

    def __init__(self, *, sample_rate: int = 48000, channels: int = 1) -> None:
        import opuslib

        self._encoder = opuslib.Encoder(sample_rate, channels, opuslib.APPLICATION_VOIP)
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_samples = sample_rate // 50

    def encode(self, pcm_s16le: bytes) -> bytes:
        if len(pcm_s16le) != self.frame_samples * self.channels * 2:
            raise ValueError("Opus PCM frame has unexpected length")
        return self._encoder.encode(pcm_s16le, self.frame_samples)

    def encode_silence(self) -> bytes:
        return self.encode(b"\x00" * (self.frame_samples * self.channels * 2))


class SpeexFfmpegEncoder:
    """Encode 20 ms Speex frames via ffmpeg libspeex (raw ``spx`` demux is awkward).

    We ask ffmpeg for RTP packets on a local UDP port, then return the RTP
    payload for the caller to re-packetize / SRTP-protect.
    """

    def __init__(
        self,
        *,
        ffmpeg_path: str,
        sample_rate: int,
        payload_type: int,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.sample_rate = sample_rate
        self.payload_type = payload_type
        self.frame_samples = sample_rate // 50
        self._process: asyncio.subprocess.Process | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _RtpCollector | None = None
        self._local_port = 0

    async def start(self) -> None:
        if not ffmpeg_has_encoder(self.ffmpeg_path, "libspeex"):
            raise RuntimeError("ffmpeg libspeex encoder is not available")
        loop = asyncio.get_running_loop()
        self._protocol = _RtpCollector()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: self._protocol,
            local_addr=("127.0.0.1", 0),
        )
        self._local_port = self._transport.get_extra_info("sockname")[1]
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(self.sample_rate),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-c:a",
            "libspeex",
            "-ar",
            str(self.sample_rate),
            "-ac",
            "1",
            "-payload_type",
            str(self.payload_type),
            "-f",
            "rtp",
            f"rtp://127.0.0.1:{self._local_port}",
        ]
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

    async def encode(self, pcm_s16le: bytes) -> bytes | None:
        if not self._process or not self._process.stdin or not self._protocol:
            return None
        self._protocol.clear()
        self._process.stdin.write(pcm_s16le)
        await self._process.stdin.drain()
        return await self._protocol.wait_payload(timeout=0.5)

    async def encode_silence(self) -> bytes | None:
        return await self.encode(b"\x00" * (self.frame_samples * 2))

    async def close(self) -> None:
        process, self._process = self._process, None
        if process and process.stdin:
            with contextlib.suppress(Exception):
                process.stdin.close()
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(process.wait(), timeout=1)
        if self._transport:
            self._transport.close()
            self._transport = None


class _RtpCollector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self._payload: bytes | None = None
        self._event = asyncio.Event()

    def clear(self) -> None:
        self._payload = None
        self._event = asyncio.Event()

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        if len(data) < 12:
            return
        # Skip RTP header (no CSRC/extension assumed from ffmpeg).
        self._payload = data[12:]
        self._event.set()

    async def wait_payload(self, *, timeout: float) -> bytes | None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        return self._payload
