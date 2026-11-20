"""0.1.8: media must follow the *current* SipClient across SIP reconnects.

Regression for 0.1.7: a provider settings save (e.g. "Enable talkback") ran
_connect_stack, replacing plugin.sip, while the reused RTSP server kept an
SrtpMediaSession bound to the closed client -> "SIP connection is not open".
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from c100x.const import (
    CONF_EMAIL,
    CONF_ENABLE_TALKBACK,
    CONF_GATEWAY_ID,
    CONF_HOME_ID,
    CONF_PASSWORD,
)
from sdk_stub import install as install_sdk


class FakeStorage:
    def __init__(self, data=None):
        self.data = data or {}

    def getItem(self, key):
        return self.data.get(key)

    def setItem(self, key, value):
        self.data[key] = value


class FakeSip:
    def __init__(self, name: str, registered: bool = True) -> None:
        self.name = name
        self.registered = registered
        self.closed = False
        self.on_call_end = None

    async def close(self) -> None:
        self.closed = True
        self.registered = False


class FakeMedia:
    created: list["FakeMedia"] = []

    def __init__(self, sip, on_plain_rtp=None, on_keyframe=None) -> None:
        self.sip = sip
        self.negotiated = None
        self.started_with: str | None = None
        self.ended = False
        FakeMedia.created.append(self)

    @property
    def active(self) -> bool:
        return self.started_with is not None and not self.ended

    async def start(self, device_address: str):
        if self.sip.closed:
            raise RuntimeError("SIP connection is not open")
        self.started_with = device_address
        self.negotiated = SimpleNamespace(
            audio=SimpleNamespace(
                port=6000,
                direction="sendrecv",
                protocol="RTP/SAVP",
                codec=lambda _names: SimpleNamespace(payload_type=8, name="PCMA"),
            )
        )
        return self.negotiated

    async def end(self) -> None:
        self.ended = True


def _plugin(plugin_main, storage=None):
    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.storage = storage or FakeStorage()
    plugin.device = None
    plugin.sip = None
    plugin._supervise_task = None
    plugin.camera_ids = ["eu-1"]
    plugin.print = lambda *a, **k: None
    plugin.refreshDevices = AsyncMock()
    return plugin


async def _connect_and_wait(url: str, media_count: int):
    host, port = url.removeprefix("rtsp://").rstrip("/").split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    for _ in range(100):
        if len(FakeMedia.created) >= media_count and FakeMedia.created[-1].started_with:
            break
        await asyncio.sleep(0.01)
    return reader, writer


async def _close(writer) -> None:
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_reused_rtsp_server_uses_current_sip_client(monkeypatch) -> None:
    install_sdk()
    import main as plugin_main

    FakeMedia.created = []
    monkeypatch.setattr(plugin_main, "SrtpMediaSession", FakeMedia)
    plugin = _plugin(plugin_main)
    sip_a = FakeSip("a")
    plugin.sip = sip_a
    device = plugin_main.C100XDoorbell("doorbell", plugin)
    device.print = lambda *a, **k: None
    plugin.device = device

    await device.getVideoStream()
    url = device._rtsp.url
    _r, w = await _connect_and_wait(url, 1)
    assert FakeMedia.created[0].sip is sip_a
    assert FakeMedia.created[0].started_with == "eu-1"
    await _close(w)
    assert FakeMedia.created[0].ended

    # SIP replaced underneath a still-listening RTSP server (supervisor path).
    await sip_a.close()
    sip_b = FakeSip("b")
    plugin.sip = sip_b
    await device.getVideoStream()
    assert device._rtsp.url == url  # server reused
    _r, w = await _connect_and_wait(url, 2)
    assert FakeMedia.created[1].sip is sip_b
    assert FakeMedia.created[1].started_with == "eu-1"
    await _close(w)
    await device._rtsp.stop()


@pytest.mark.asyncio
async def test_shutdown_sip_drops_rtsp_server_and_media(monkeypatch) -> None:
    install_sdk()
    import main as plugin_main

    FakeMedia.created = []
    monkeypatch.setattr(plugin_main, "SrtpMediaSession", FakeMedia)
    plugin = _plugin(plugin_main)
    sip_a = FakeSip("a")
    plugin.sip = sip_a
    device = plugin_main.C100XDoorbell("doorbell", plugin)
    device.print = lambda *a, **k: None
    plugin.device = device
    await device.getVideoStream()
    _r, w = await _connect_and_wait(device._rtsp.url, 1)
    media = FakeMedia.created[0]
    talkback = SimpleNamespace(stop=AsyncMock())
    device._talkback = talkback

    await plugin._shutdown_sip()

    assert device._rtsp is None
    assert device._media is None
    assert device._talkback is None
    talkback.stop.assert_awaited()
    assert media.ended
    assert sip_a.closed and plugin.sip is None
    w.close()


@pytest.mark.asyncio
async def test_provider_talkback_toggle_does_not_reconnect_sip() -> None:
    install_sdk()
    import main as plugin_main

    storage = FakeStorage(
        {
            CONF_EMAIL: json.dumps("a@example.com"),
            CONF_PASSWORD: json.dumps("x"),
            CONF_HOME_ID: json.dumps("home"),
            CONF_GATEWAY_ID: json.dumps("gw"),
        }
    )
    plugin = _plugin(plugin_main, storage)
    plugin._connect_stack = AsyncMock()
    plugin._shutdown_sip = AsyncMock()

    await plugin.putSetting(CONF_ENABLE_TALKBACK, "true")
    assert json.loads(storage.data[CONF_ENABLE_TALKBACK]) is True
    await plugin.putSetting(CONF_ENABLE_TALKBACK, False)
    assert json.loads(storage.data[CONF_ENABLE_TALKBACK]) is False

    plugin._connect_stack.assert_not_awaited()
    plugin._shutdown_sip.assert_not_awaited()
    assert plugin.refreshDevices.await_count == 2

    # Credential/installation saves still reconnect, as before.
    await plugin.putSetting(CONF_GATEWAY_ID, "Gateway (gw)")
    plugin._connect_stack.assert_awaited_once()
