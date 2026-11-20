"""Door audio gain helpers and transcoder wiring (offline)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from c100x.downlink_gain import (
    DoorAudioGainTranscoder,
    clamp_door_audio_gain_db,
    ffmpeg_volume_filter,
)
from sdk_stub import install as install_sdk


def test_clamp_door_audio_gain_db() -> None:
    assert clamp_door_audio_gain_db(12) == 12
    assert clamp_door_audio_gain_db(-5) == 0
    assert clamp_door_audio_gain_db(99) == 24
    assert clamp_door_audio_gain_db("18") == 18
    assert clamp_door_audio_gain_db(None, default=12) == 12
    assert clamp_door_audio_gain_db("nope", default=12) == 12
    # Autouse conftest patches production default to 0 for offline tests.
    assert clamp_door_audio_gain_db(None) == 0


def test_ffmpeg_volume_filter_includes_limiter() -> None:
    af = ffmpeg_volume_filter(12)
    assert "volume=12dB" in af
    assert "alimiter=" in af
    assert "limit=0.95" in af


@pytest.mark.asyncio
async def test_transcoder_skips_when_gain_zero() -> None:
    logs: list[str] = []
    sent: list[bytes] = []

    async def on_opus(packet: bytes) -> None:
        sent.append(packet)

    t = DoorAudioGainTranscoder(gain_db=0, on_opus_rtp=on_opus, on_log=logs.append)
    await t.start(SimpleNamespace(name="speex", payload_type=98, clock_rate=8000, channels=1))
    assert t.active is False
    assert any("passthrough" in line for line in logs)
    await t.feed_rtp(b"\x80" + b"\x00" * 20)
    assert sent == []
    await t.close()


@pytest.mark.asyncio
async def test_provider_gain_setting_clamped(monkeypatch) -> None:
    install_sdk()
    import main as plugin_main

    class FakeStorage:
        def __init__(self) -> None:
            self.data: dict = {}

        def getItem(self, key):
            return self.data.get(key)

        def setItem(self, key, value):
            self.data[key] = value

    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.storage = FakeStorage()
    plugin.device = None
    plugin.locks = {}
    plugin.lock_ids = []
    plugin._lock_names = {}
    plugin.camera_ids = []
    plugin.sip = None
    plugin.gateway_id = None
    plugin.home_id = None
    plugin._api = None
    plugin._plants_cache = []
    plugin._gateways_cache = []
    plugin.print = lambda *a, **k: None

    await plugin.putSetting("door_audio_gain_db", 99)
    assert plugin_main._json_get(plugin.storage, "door_audio_gain_db") == 24
    await plugin.putSetting("door_audio_gain_db", -3)
    assert plugin_main._json_get(plugin.storage, "door_audio_gain_db") == 0
