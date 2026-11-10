"""Talkback pipeline and Intercom gating tests (offline)."""

from __future__ import annotations

import asyncio
import shutil
import time

import pytest

from c100x.g711 import encode_g711
from c100x.srtp_media import serialize_rtp
from c100x.talkback import TalkbackError, TalkbackPipeline


class FakeAudio:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.talkback = False
        self.timestamps: list[float] = []

    def set_talkback(self, enabled: bool) -> None:
        self.talkback = enabled

    def send_pcm(self, pcm: bytes) -> None:
        self.frames.append(pcm)
        self.timestamps.append(time.monotonic())


class FakeMedia:
    def __init__(self) -> None:
        self._active = True
        self.audio = FakeAudio()
        self.talkback_enabled = False

    @property
    def active(self) -> bool:
        return self._active

    async def set_talkback(self, enabled: bool) -> None:
        self.talkback_enabled = enabled
        self.audio.set_talkback(enabled)

    def send_talkback_pcm(self, pcm: bytes) -> None:
        self.audio.send_pcm(pcm)


@pytest.mark.asyncio
async def test_talkback_pipeline_ffmpeg_sine_produces_20ms_frames() -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not installed")
    media = FakeMedia()
    pipeline = TalkbackPipeline(media)  # type: ignore[arg-type]
    # 0.2 s of sine → ~10 frames of 20 ms
    ffmpeg_input = {
        "inputArguments": [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=8000:duration=0.2",
        ]
    }
    await pipeline.start(ffmpeg, ffmpeg_input)
    # Wait for pump to consume
    for _ in range(50):
        if len(media.audio.frames) >= 5:
            break
        await asyncio.sleep(0.05)
    await pipeline.stop()
    assert len(media.audio.frames) >= 5
    assert all(len(frame) == 320 for frame in media.audio.frames)
    # Timing: consecutive 20 ms frames should be roughly spaced (allow slack).
    if len(media.audio.timestamps) >= 3:
        deltas = [
            media.audio.timestamps[i + 1] - media.audio.timestamps[i]
            for i in range(len(media.audio.timestamps) - 1)
        ]
        assert min(deltas) >= 0.0
        assert sum(deltas) / len(deltas) < 0.2
    # Encoded payload size for PCMA of one frame is 160
    encoded = encode_g711(media.audio.frames[0], "pcma")
    assert len(encoded) == 160
    packet = serialize_rtp(
        payload_type=8,
        sequence=1,
        timestamp=0,
        ssrc=1,
        payload=encoded,
    )
    assert packet[1] & 0x7F == 8


@pytest.mark.asyncio
async def test_talkback_refuses_without_active_call() -> None:
    media = FakeMedia()
    media._active = False
    pipeline = TalkbackPipeline(media)  # type: ignore[arg-type]
    with pytest.raises(TalkbackError):
        await pipeline.start("ffmpeg", {"url": "x"})


@pytest.mark.asyncio
async def test_device_talkback_disabled_raises_and_no_intercom(monkeypatch) -> None:
    """Device without Enable talkback must not expose Intercom; startIntercom raises."""
    from sdk_stub import install as install_sdk

    sdk, types_mod = install_sdk()
    import main as plugin_main

    storage = {}

    class FakeStorage:
        def getItem(self, key):
            return storage.get(key)

        def setItem(self, key, value):
            storage[key] = value

    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.storage = FakeStorage()
    plugin.sip = None
    plugin.gateway_id = "gw"
    plugin.device = None
    device = plugin_main.C100XDoorbell("doorbell", plugin)
    with pytest.raises(Exception) as exc:
        await device.startIntercom(object())
    assert "talkback" in str(exc.value).casefold()

    events = []

    async def capture(manifest):
        events.append(manifest)

    sdk.deviceManager.onDevicesChanged = capture
    plugin.storage.setItem("gateway_id", '"gw"')
    plugin.storage.setItem("enable_talkback", "false")
    await plugin.refreshDevices()
    assert events
    interfaces = events[0]["devices"][0]["interfaces"]
    assert types_mod.ScryptedInterface.Intercom.value not in interfaces
