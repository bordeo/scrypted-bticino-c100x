"""0.1.9: retry a monitoring INVITE briefly when the door answers 486 (busy)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from c100x.sip import SipError
from sdk_stub import install as install_sdk


class FakeStorage:
    def __init__(self):
        self.data = {}

    def getItem(self, key):
        return self.data.get(key)

    def setItem(self, key, value):
        self.data[key] = value


class FakeSip:
    registered = True
    closed = False
    on_call_end = None


def _make(plugin_main, failures, *, status=486):
    class FakeMedia:
        created = []

        def __init__(self, sip, on_plain_rtp=None, on_keyframe=None):
            self.sip = sip
            self.negotiated = None
            self.attempts = 0
            self.started = False
            self.ended = False
            FakeMedia.created.append(self)

        @property
        def active(self):
            return self.started and not self.ended

        async def start(self, device_address):
            self.attempts += 1
            if self.attempts <= failures:
                raise SipError(f"Monitoring call failed with SIP status {status}")
            self.started = True

        async def end(self):
            self.ended = True

    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.storage = FakeStorage()
    plugin.device = None
    plugin.sip = FakeSip()
    plugin._supervise_task = None
    plugin.camera_ids = ["eu-1"]
    plugin.print = lambda *a, **k: None
    plugin.refreshDevices = AsyncMock()
    device = plugin_main.C100XDoorbell("doorbell", plugin)
    logs = []
    device.print = lambda *a, **k: logs.append(" ".join(map(str, a)))
    plugin.device = device
    return device, FakeMedia, logs


async def _open(url):
    host, port = url.removeprefix("rtsp://").rstrip("/").split(":")
    return await asyncio.open_connection(host, int(port))


@pytest.mark.asyncio
async def test_486_is_retried_until_the_door_is_free(monkeypatch) -> None:
    install_sdk()
    import main as plugin_main

    device, FakeMedia, logs = _make(plugin_main, failures=2)
    monkeypatch.setattr(plugin_main, "SrtpMediaSession", FakeMedia)
    monkeypatch.setattr(plugin_main, "MONITOR_BUSY_RETRY_INTERVAL", 0.01)
    await device.getVideoStream()
    _r, w = await _open(device._rtsp.url)
    for _ in range(200):
        if FakeMedia.created and FakeMedia.created[0].started:
            break
        await asyncio.sleep(0.01)
    media = FakeMedia.created[0]
    assert media.started and media.attempts == 3
    assert logs.count("Door busy (486); retrying monitoring call") == 2
    assert "Monitoring call started" in logs
    w.close()
    await device._rtsp.stop()


@pytest.mark.asyncio
async def test_non_486_fails_immediately(monkeypatch) -> None:
    install_sdk()
    import main as plugin_main

    device, FakeMedia, logs = _make(plugin_main, failures=5, status=488)
    monkeypatch.setattr(plugin_main, "SrtpMediaSession", FakeMedia)
    monkeypatch.setattr(plugin_main, "MONITOR_BUSY_RETRY_INTERVAL", 0.01)
    await device.getVideoStream()
    _r, w = await _open(device._rtsp.url)
    await asyncio.sleep(0.1)
    assert FakeMedia.created[0].attempts == 1
    assert any("Monitoring call failed" in line and "488" in line for line in logs)
    w.close()
    await device._rtsp.stop()


@pytest.mark.asyncio
async def test_486_gives_up_after_deadline(monkeypatch) -> None:
    install_sdk()
    import main as plugin_main

    device, FakeMedia, logs = _make(plugin_main, failures=10_000)
    monkeypatch.setattr(plugin_main, "SrtpMediaSession", FakeMedia)
    monkeypatch.setattr(plugin_main, "MONITOR_BUSY_RETRY_INTERVAL", 0.01)
    monkeypatch.setattr(plugin_main, "MONITOR_BUSY_RETRY_SECONDS", 0.1)
    await device.getVideoStream()
    _r, w = await _open(device._rtsp.url)
    await asyncio.sleep(0.4)
    media = FakeMedia.created[0]
    assert not media.started and 2 <= media.attempts < 50
    assert any("Monitoring call failed" in line and "486" in line for line in logs)
    w.close()
    await device._rtsp.stop()
