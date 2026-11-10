"""Doorbell binaryState pulse + debounce tests."""

from __future__ import annotations

import asyncio

import pytest

from c100x.const import RING_ACTIVE_SECONDS
from sdk_stub import install as install_sdk


@pytest.mark.asyncio
async def test_ring_sets_binary_state_for_debounce_window(monkeypatch) -> None:
    install_sdk()
    import main as plugin_main

    monkeypatch.setattr(plugin_main, "RING_ACTIVE_SECONDS", 0.05)

    class FakeStorage:
        def __init__(self):
            self.data = {}

        def getItem(self, key):
            return self.data.get(key)

        def setItem(self, key, value):
            self.data[key] = value

    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.storage = FakeStorage()
    plugin.sip = None
    plugin.gateway_id = "gw"
    device = plugin_main.C100XDoorbell("doorbell", plugin)
    events = []

    async def capture(iface, value):
        events.append((iface, value))

    device.onDeviceEvent = capture

    await device.on_ring({"call_id": "c1"})
    assert device.binaryState is True
    await device.on_ring({"call_id": "c1"})  # retransmit ignored
    await device.on_ring({"call_id": "c2"})  # debounce while ringing
    assert device.binaryState is True
    await asyncio.sleep(0.1)
    assert device.binaryState is False
    await device.on_ring({"call_id": "c3"})
    assert device.binaryState is True
    await asyncio.sleep(0.1)
    assert RING_ACTIVE_SECONDS == 5
