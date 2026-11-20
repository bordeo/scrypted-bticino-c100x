"""Talkback uplink: FFmpegInput → s16le PCM → negotiated codec → SRTP.

HomeKit return audio arrives as an FFmpegInput MediaObject. We spawn ffmpeg to
pipe PCM at the rate required by the negotiated codec (48 kHz Opus, 8/16 kHz
Speex, 8 kHz G.711), encode in 20 ms frames, SRTP-protect, and send.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
from typing import Any

from .srtp_media import SrtpMediaSession

_LOGGER = logging.getLogger(__name__)


class TalkbackError(RuntimeError):
    """Talkback could not start or stay running."""


class TalkbackPipeline:
    """Own one ffmpeg PCM producer feeding an active SrtpMediaSession."""

    def __init__(self, media: SrtpMediaSession) -> None:
        self.media = media
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._active = False
        self._frame_bytes = 320

    @property
    def active(self) -> bool:
        return self._active

    async def start(self, ffmpeg_path: str, ffmpeg_input: dict[str, Any]) -> None:
        """Start talkback from a Scrypted FFmpegInput JSON object."""
        if not self.media.active:
            raise TalkbackError("No active call for talkback")
        fmt = self.media.talkback_format()
        if fmt is None:
            raise TalkbackError("No negotiated audio codec for talkback")
        await self.stop()
        self._frame_bytes = fmt.frame_bytes
        url = ffmpeg_input.get("url") or ""
        input_args = list(ffmpeg_input.get("inputArguments") or [])
        command = [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin"]
        if input_args:
            command.extend(str(arg) for arg in input_args)
        elif url:
            command.extend(["-i", str(url)])
        else:
            raise TalkbackError("FFmpegInput has no url or inputArguments")
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
        _LOGGER.debug(
            "talkback ffmpeg: %s",
            " ".join(shlex.quote(part) for part in command if "password" not in part.lower()),
        )
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self.media.set_talkback(True)
        self._active = True
        self._task = asyncio.create_task(self._pump())

    async def stop(self) -> None:
        self._active = False
        task, self._task = self._task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
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

    async def _pump(self) -> None:
        assert self._process and self._process.stdout
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
                    self.media.send_talkback_pcm(frame)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.warning("talkback pump stopped: %s", type(err).__name__)
        finally:
            self._active = False
            with contextlib.suppress(Exception):
                await self.media.set_talkback(False)
