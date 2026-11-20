"""Downlink Speex/Opus/G.711 → optional SpeexDSP AEC → gain → Opus RTP.

Used when **Echo cancellation** is enabled. Decodes the door track to PCM,
cancels acoustic echo using uplink (talkback) PCM as the far-end reference,
applies the same dB gain as the non-AEC path, then encodes Opus for RTSP.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import logging
import os
import secrets
import socket
import tempfile
from collections.abc import Awaitable, Callable
from typing import Any

from .audio_codec import OpusEncoder, resolve_ffmpeg
from .const import DOOR_AUDIO_GAIN_MAX_DB, DOOR_AUDIO_GAIN_MIN_DB
from .downlink_gain import clamp_door_audio_gain_db, ffmpeg_volume_filter
from .echo_cancel import EchoCanceller, speexdsp_available
from .srtp_media import serialize_rtp

_LOGGER = logging.getLogger(__name__)

OnOpusRtp = Callable[[bytes], Awaitable[None] | None]


def _rtpmap_for_codec(codec: Any) -> tuple[int, str]:
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


def apply_pcm_gain(pcm: bytes, gain_db: int) -> bytes:
    """Apply a digital gain to s16le PCM with soft clip."""
    gain_db = max(DOOR_AUDIO_GAIN_MIN_DB, min(DOOR_AUDIO_GAIN_MAX_DB, int(gain_db)))
    if gain_db <= 0 or not pcm:
        return pcm
    factor = 10 ** (gain_db / 20.0)
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    for i, sample in enumerate(samples):
        scaled = int(sample * factor)
        if scaled > 32767:
            scaled = 32767
        elif scaled < -32768:
            scaled = -32768
        samples[i] = scaled
    return samples.tobytes()


def upsample_s16le(pcm: bytes, *, factor: int) -> bytes:
    """Integer-factor upsample by sample repeat (8 kHz → 48 kHz uses factor 6)."""
    if factor <= 1 or not pcm:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    out = array.array("h")
    for sample in samples:
        out.extend([sample] * factor)
    return out.tobytes()


class DoorAudioAecTranscoder:
    """Decode door RTP → AEC (+gain) → Opus RTP for HomeKit RTSP."""

    OUTPUT_PAYLOAD_TYPE = 96
    OUTPUT_RTPMAP = "opus/48000/2"

    def __init__(
        self,
        *,
        gain_db: int,
        echo: EchoCanceller,
        on_opus_rtp: OnOpusRtp,
        ffmpeg_path: str | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.gain_db = clamp_door_audio_gain_db(gain_db)
        self.echo = echo
        self.on_opus_rtp = on_opus_rtp
        self.ffmpeg_path = resolve_ffmpeg(ffmpeg_path)
        self._on_log = on_log
        self._process: asyncio.subprocess.Process | None = None
        self._feed_sock: socket.socket | None = None
        self._in_addr: tuple[str, int] | None = None
        self._sdp_path: str | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._active = False
        self._opus: OpusEncoder | None = None
        self._sequence = secrets.randbelow(65536)
        self._timestamp = secrets.randbelow(2**32)
        self._ssrc = secrets.randbelow(2**32)
        self._pcm_rate = 8000
        self._frame_bytes = 320
        self._upsample = 6
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
        if not speexdsp_available():
            raise RuntimeError("libspeexdsp is not available")
        if not self.ffmpeg_path:
            raise RuntimeError("ffmpeg not found")
        name = str(codec.name).casefold()
        rate = int(getattr(codec, "clock_rate", 8000) or 8000)
        if name == "opus":
            self._pcm_rate = 48000
            self._upsample = 1
        else:
            self._pcm_rate = rate if rate in (8000, 16000) else 8000
            self._upsample = max(1, 48000 // self._pcm_rate)
        self._frame_bytes = self._pcm_rate // 50 * 2
        self._opus = OpusEncoder(sample_rate=48000, channels=1)

        pt, rtpmap = _rtpmap_for_codec(codec)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        in_port = probe.getsockname()[1]
        probe.close()
        self._in_addr = ("127.0.0.1", in_port)
        sdp = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 127.0.0.1\r\n"
            "s=c100x-door-aec\r\n"
            "c=IN IP4 127.0.0.1\r\n"
            "t=0 0\r\n"
            f"m=audio {in_port} RTP/AVP {pt}\r\n"
            f"a=rtpmap:{pt} {rtpmap}\r\n"
        )
        fd, self._sdp_path = tempfile.mkstemp(prefix="c100x-aec-", suffix=".sdp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(sdp)

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
            "-probesize",
            "32",
            "-analyzeduration",
            "0",
            "-i",
            self._sdp_path,
            "-ac",
            "1",
            "-ar",
            str(self._pcm_rate),
            "-f",
            "s16le",
            "pipe:1",
        ]
        self._feed_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._feed_sock.setblocking(False)
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.sleep(0.05)
        if self._process.returncode is not None:
            err = b""
            if self._process.stderr:
                with contextlib.suppress(Exception):
                    err = await self._process.stderr.read()
            await self.close()
            raise RuntimeError(
                f"Door AEC ffmpeg exited early: {err.decode('utf-8', 'replace')[:200]}"
            )
        self._active = True
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._pump_task = asyncio.create_task(self._pump())
        af = ffmpeg_volume_filter(self.gain_db) if self.gain_db > 0 else "gain=0(passthrough)"
        self._emit(
            f"Door audio AEC transcoder started "
            f"(aec on, +{self.gain_db} dB digital, {rtpmap} → {self.OUTPUT_RTPMAP}; {af})"
        )

    async def feed_rtp(self, packet: bytes) -> None:
        if not self._active or not self._feed_sock or not self._in_addr:
            return
        with contextlib.suppress(OSError, BlockingIOError):
            self._feed_sock.sendto(packet, self._in_addr)

    async def _drain_stderr(self) -> None:
        process = self._process
        if not process or not process.stderr:
            return
        try:
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return

    async def _pump(self) -> None:
        assert self._process and self._process.stdout and self._opus
        buf = bytearray()
        try:
            while self._active:
                chunk = await self._process.stdout.read(self._frame_bytes)
                if not chunk:
                    break
                buf.extend(chunk)
                while len(buf) >= self._frame_bytes:
                    frame = bytes(buf[: self._frame_bytes])
                    del buf[: self._frame_bytes]
                    cleaned = self.echo.process(frame)
                    gained = apply_pcm_gain(cleaned, self.gain_db)
                    pcm48 = upsample_s16le(gained, factor=self._upsample)
                    # OpusEncoder expects exactly 20 ms at 48 kHz.
                    need = self._opus.frame_samples * 2
                    if len(pcm48) < need:
                        pcm48 = pcm48 + b"\x00" * (need - len(pcm48))
                    elif len(pcm48) > need:
                        pcm48 = pcm48[:need]
                    try:
                        payload = self._opus.encode(pcm48)
                    except Exception:  # noqa: BLE001
                        continue
                    packet = serialize_rtp(
                        payload_type=self.OUTPUT_PAYLOAD_TYPE,
                        sequence=self._sequence,
                        timestamp=self._timestamp,
                        ssrc=self._ssrc,
                        payload=payload,
                        marker=not self._logged_first,
                    )
                    self._sequence = (self._sequence + 1) & 0xFFFF
                    self._timestamp = (self._timestamp + self._opus.frame_samples) & 0xFFFFFFFF
                    if not self._logged_first:
                        self._logged_first = True
                        self._emit(
                            f"Door audio AEC: first Opus frame after +{self.gain_db} dB "
                            f"({len(packet)} bytes RTP)"
                        )
                    result = self.on_opus_rtp(packet)
                    if asyncio.iscoroutine(result):
                        await result
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            self._emit(f"Door audio AEC pump stopped: {type(err).__name__}: {err}")
        finally:
            self._active = False

    async def close(self) -> None:
        self._active = False
        for attr in ("_pump_task", "_stderr_task"):
            task = getattr(self, attr)
            setattr(self, attr, None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        process, self._process = self._process, None
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(process.wait(), timeout=1.0)
        if self._feed_sock:
            with contextlib.suppress(OSError):
                self._feed_sock.close()
            self._feed_sock = None
        if self._sdp_path:
            with contextlib.suppress(OSError):
                os.unlink(self._sdp_path)
            self._sdp_path = None
        self._opus = None
