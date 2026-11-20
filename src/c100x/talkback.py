"""Talkback uplink: FFmpegInput → s16le PCM → negotiated codec → SRTP.

HomeKit return audio arrives as an FFmpegInput MediaObject. We spawn ffmpeg to
pipe PCM at the rate required by the negotiated codec (48 kHz Opus, 8/16 kHz
Speex, 8 kHz G.711), encode in 20 ms frames, SRTP-protect, and send.

Uplink silence keepalive (in ``SrtpMediaSession``) gap-fills any 20 ms slot
without a mic frame so TX never stalls while ffmpeg connects or buffers.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import logging
import shlex
import time
from collections.abc import Callable
from typing import Any

from .srtp_media import SrtpMediaSession

_LOGGER = logging.getLogger(__name__)

# Bound blocking reads so a stalled HomeKit RTSP source cannot freeze the pump;
# silence keepalive covers the gap.
_STDOUT_READ_TIMEOUT_S = 0.05
# Treat PCM as silence when peak |sample| stays below this (s16).
_SILENCE_PEAK = 96


def pcm_peak_abs(pcm: bytes) -> int:
    if len(pcm) < 2:
        return 0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    return max((abs(s) for s in samples), default=0)


def inject_low_latency_input_args(input_args: list[str]) -> list[str]:
    """Ensure low-latency / no-probe flags appear before the first ``-i``.

    HomeKit return audio is usually Opus over RTSP. Declaring the codec and
    skipping probe cuts seconds off startIntercom → first PCM.
    """
    args = [str(a) for a in input_args]
    hay = " ".join(args).casefold()
    if "-i" not in args:
        return args
    i_at = args.index("-i")
    head, tail = args[:i_at], args[i_at:]
    extras: list[str] = []
    # Always prefer tiny probe for RTSP/HTTP return audio.
    if "rtsp://" in hay or "-rtsp_transport" in hay:
        for flag, value in (
            ("-fflags", "nobuffer"),
            ("-flags", "low_delay"),
            ("-probesize", "32"),
            ("-analyzeduration", "0"),
            ("-thread_queue_size", "64"),
        ):
            if flag not in head and flag not in tail:
                extras.extend([flag, value])
        # Declare Opus when HomeKit did not (avoids demuxer probe delay).
        if "-acodec" not in head and "-acodec" not in tail and "-c:a" not in head:
            extras.extend(["-acodec", "libopus"])
    return extras + head + tail


class TalkbackError(RuntimeError):
    """Talkback could not start or stay running."""


class TalkbackPipeline:
    """Own one ffmpeg PCM producer feeding an active SrtpMediaSession."""

    def __init__(
        self,
        media: SrtpMediaSession,
        on_log: Callable[[str], None] | None = None,
        on_pcm: Callable[[bytes], None] | None = None,
    ) -> None:
        self.media = media
        self.on_log = on_log
        self.on_pcm = on_pcm
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._active = False
        self._frame_bytes = 320
        self._frames_sent = 0
        self._started_mono = 0.0
        self._first_nonsilence_logged = False

    def _emit(self, message: str) -> None:
        _LOGGER.info("%s", message)
        if self.on_log:
            with contextlib.suppress(Exception):
                self.on_log(message)

    @property
    def active(self) -> bool:
        return self._active

    async def start(self, ffmpeg_path: str, ffmpeg_input: dict[str, Any]) -> None:
        """Start talkback from a Scrypted FFmpegInput JSON object."""
        self._emit("startIntercom pipeline: starting")
        if not self.media.active:
            raise TalkbackError("No active call for talkback")
        fmt = self.media.talkback_format()
        if fmt is None:
            raise TalkbackError("No negotiated audio codec for talkback")
        await self.stop()
        self._frame_bytes = fmt.frame_bytes
        self._frames_sent = 0
        self._first_nonsilence_logged = False
        self._started_mono = time.monotonic()
        url = ffmpeg_input.get("url") or ""
        input_args = list(ffmpeg_input.get("inputArguments") or [])
        command = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
        ]
        if input_args:
            command.extend(inject_low_latency_input_args(input_args))
        elif url:
            hay = str(url).casefold()
            if hay.startswith("rtsp://"):
                command.extend(
                    [
                        "-fflags",
                        "nobuffer",
                        "-flags",
                        "low_delay",
                        "-probesize",
                        "32",
                        "-analyzeduration",
                        "0",
                        "-thread_queue_size",
                        "64",
                        "-rtsp_transport",
                        "tcp",
                        "-acodec",
                        "libopus",
                    ]
                )
            command.extend(["-i", str(url)])
        else:
            raise TalkbackError("FFmpegInput has no url or inputArguments")
        # Output PCM at the negotiated uplink clock only (no extra resamples).
        command.extend(
            [
                "-vn",
                "-sn",
                "-dn",
                "-ac",
                str(fmt.channels),
                "-ar",
                str(fmt.sample_rate),
                "-f",
                "s16le",
                "pipe:1",
            ]
        )
        redacted = " ".join(
            shlex.quote(part) for part in command if "password" not in part.lower()
        )
        self._emit(f"talkback ffmpeg: {redacted}")
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Arm talkback so mic frames are accepted; silence keepalive keeps
        # gap-filling until real PCM actually flows (and whenever it stalls).
        await self.media.set_talkback(True)
        self._active = True
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._task = asyncio.create_task(self._pump())
        self._emit(
            f"talkback pipeline running (pcm {fmt.sample_rate} Hz / {fmt.frame_ms} ms frames)"
        )

    async def stop(self) -> None:
        was_active = self._active
        self._active = False
        task, self._task = self._task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        stderr_task, self._stderr_task = self._stderr_task, None
        if stderr_task:
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
        process, self._process = self._process, None
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(process.wait(), timeout=1)
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                    await process.wait()
        with contextlib.suppress(Exception):
            await self.media.set_talkback(False)
        if was_active:
            self._emit(f"talkback stopped (frames_sent={self._frames_sent})")

    async def _drain_stderr(self) -> None:
        """Prevent ffmpeg from blocking when the stderr pipe fills."""
        process = self._process
        if not process or not process.stderr:
            return
        try:
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    return
                text = chunk.decode("utf-8", errors="replace").strip()
                if text:
                    self._emit(f"talkback ffmpeg stderr: {text[:500]}")
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            self._emit(f"talkback stderr drain stopped: {type(err).__name__}: {err}")

    async def _pump(self) -> None:
        assert self._process and self._process.stdout
        buf = bytearray()
        try:
            while self._active:
                try:
                    chunk = await asyncio.wait_for(
                        self._process.stdout.read(self._frame_bytes),
                        timeout=_STDOUT_READ_TIMEOUT_S,
                    )
                except (TimeoutError, asyncio.TimeoutError):
                    # No PCM this slot — silence keepalive gap-fills uplink.
                    # Py3.10: wait_for raises asyncio.TimeoutError (not builtin).
                    if self._process.returncode is not None:
                        self._emit(
                            f"talkback ffmpeg exited (code={self._process.returncode})"
                        )
                        break
                    continue
                if not chunk:
                    self._emit("talkback ffmpeg stdout EOF")
                    break
                buf.extend(chunk)
                while len(buf) >= self._frame_bytes:
                    frame = bytes(buf[: self._frame_bytes])
                    del buf[: self._frame_bytes]
                    if self.on_pcm:
                        with contextlib.suppress(Exception):
                            self.on_pcm(frame)
                    self.media.send_talkback_pcm(frame)
                    self._frames_sent += 1
                    if self._frames_sent == 1:
                        elapsed_ms = int((time.monotonic() - self._started_mono) * 1000)
                        audio = self.media.audio
                        self._emit(
                            "talkback first uplink frame sent "
                            f"(bytes={len(frame)} peak={pcm_peak_abs(frame)} "
                            f"after_ms={elapsed_ms} tx_pkts="
                            f"{getattr(audio, 'tx_packets', '?')})"
                        )
                    if (
                        not self._first_nonsilence_logged
                        and pcm_peak_abs(frame) >= _SILENCE_PEAK
                    ):
                        self._first_nonsilence_logged = True
                        elapsed_ms = int((time.monotonic() - self._started_mono) * 1000)
                        self._emit(
                            "talkback first non-silence uplink frame "
                            f"(peak={pcm_peak_abs(frame)} after_ms={elapsed_ms})"
                        )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self._emit(f"talkback pump stopped: {type(err).__name__}: {err}")
        finally:
            self._active = False
            with contextlib.suppress(Exception):
                await self.media.set_talkback(False)
