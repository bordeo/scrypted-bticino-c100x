"""OPTIONS must not wait on SIP BYE/INVITE (0.1.10 / Bugbot)."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sdk_stub import install as install_sdk


class FakeStorage:
    def getItem(self, key):
        return None

    def setItem(self, key, value):
        pass


@pytest.mark.asyncio
async def test_options_not_blocked_by_slow_previous_bye(monkeypatch) -> None:
    """on_connect must not await a slow BYE before the RTSP handshake loop."""
    install_sdk()
    import main as plugin_main

    bye_released = asyncio.Event()

    class SlowMedia:
        created = []

        def __init__(self, sip, on_plain_rtp=None, on_keyframe=None, ffmpeg_path=None):
            self.sip = sip
            self.negotiated = None
            self.ended = False
            SlowMedia.created.append(self)

        @property
        def active(self):
            return self.negotiated is not None and not self.ended

        async def start(self, device_address):
            await bye_released.wait()
            opus = SimpleNamespace(
                payload_type=96, name="opus", clock_rate=48000, channels=2, fmtp=None
            )
            self.negotiated = SimpleNamespace(
                audio=SimpleNamespace(
                    port=6000,
                    direction="sendrecv",
                    protocol="RTP/SAVP",
                    codecs=[opus],
                    codec=lambda _names: opus,
                )
            )
            return self.negotiated

        async def end(self):
            self.ended = True

    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.storage = FakeStorage()
    plugin.device = None
    plugin.sip = SimpleNamespace(registered=True, closed=False, on_call_end=None)
    plugin.camera_ids = ["eu-1"]
    plugin.print = lambda *a, **k: None
    plugin.refreshDevices = AsyncMock()
    device = plugin_main.C100XDoorbell("doorbell", plugin)
    device.print = lambda *a, **k: None
    plugin.device = device

    # Seed a previous media session whose end() is slow — if on_connect awaited
    # it before OPTIONS, the handshake would stall past Scrypted's 10s budget.
    previous = SlowMedia(plugin.sip)
    previous.negotiated = SimpleNamespace(audio=None)

    async def slow_end():
        await asyncio.sleep(2.0)
        previous.ended = True

    previous.end = slow_end  # type: ignore[method-assign]
    device._media = previous

    monkeypatch.setattr(plugin_main, "SrtpMediaSession", SlowMedia)
    SlowMedia.created = [previous]
    await device.getVideoStream()
    url = device._rtsp.url
    host, port = url.removeprefix("rtsp://").rstrip("/").split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    try:
        writer.write(f"OPTIONS {url} RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
        await writer.drain()
        resp = await asyncio.wait_for(reader.read(4096), timeout=0.5)
        assert b"RTSP/1.0 200 OK" in resp
        assert b"CSeq: 1" in resp
    finally:
        bye_released.set()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        await device._rtsp.stop()
