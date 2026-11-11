"""Monitoring INVITE DEVADDR must use the EU module id, not the gateway id."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sdk_stub import install as install_sdk


def _visible_eu(module_id: str, *, button_id: int = 1) -> dict:
    return {
        "id": module_id,
        "deviceType": "eu",
        "tags": [
            {
                "key": "PrivateAddress",
                "value": json.dumps({"visible": 1, "buttonId": button_id}),
            }
        ],
    }


def _plugin_for_topology(modules: list[dict], *, gateway_id: str, home_id: str = "home-1"):
    install_sdk()
    import main as plugin_main

    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.gateway_id = gateway_id
    plugin.home_id = home_id
    plugin.camera_ids = []
    plugin._api = SimpleNamespace(modules=AsyncMock(return_value=modules))
    return plugin_main, plugin


@pytest.mark.asyncio
async def test_monitoring_device_address_uses_eu_not_gateway() -> None:
    # Distinct ids: gateway must never be chosen as DEVADDR.
    gateway_id = "gateway-module-id"
    eu_id = "external-unit-module-id"
    _plugin_main, plugin = _plugin_for_topology(
        [
            {"id": gateway_id, "device": "gateway", "tags": []},
            _visible_eu(eu_id, button_id=1),
            _visible_eu("eu-later", button_id=2),
        ],
        gateway_id=gateway_id,
    )

    address = await plugin.monitoring_device_address()

    assert address == eu_id
    assert address != gateway_id
    assert plugin.camera_ids == [eu_id, "eu-later"]
    plugin._api.modules.assert_awaited_once_with("home-1")


@pytest.mark.asyncio
async def test_monitoring_device_address_missing_external_unit() -> None:
    gateway_id = "gateway-only"
    _plugin_main, plugin = _plugin_for_topology(
        [
            {"id": gateway_id, "device": "gateway", "tags": []},
            {
                "id": "hidden-eu",
                "deviceType": "eu",
                "tags": [
                    {
                        "key": "PrivateAddress",
                        "value": json.dumps({"visible": 0, "buttonId": 1}),
                    }
                ],
            },
        ],
        gateway_id=gateway_id,
    )

    with pytest.raises(
        RuntimeError,
        match="No official-app-visible external unit was found in the topology",
    ):
        await plugin.monitoring_device_address()
    assert plugin.camera_ids == []


@pytest.mark.asyncio
async def test_rtsp_monitoring_start_passes_eu_module_id(monkeypatch) -> None:
    """getVideoStream's RTSP on_connect path must start media with the EU id."""
    gateway_id = "gateway-uuid"
    eu_id = "eu-module-uuid"
    plugin_main, plugin = _plugin_for_topology(
        [{"id": gateway_id, "device": "gateway", "tags": []}, _visible_eu(eu_id)],
        gateway_id=gateway_id,
    )
    plugin.sip = SimpleNamespace(registered=True)
    device = plugin_main.C100XDoorbell("doorbell", plugin)
    device.print = lambda *_a, **_k: None

    started: list[str] = []

    class FakeMedia:
        negotiated = None

        async def start(self, device_address: str):
            started.append(device_address)
            return SimpleNamespace(audio=None, video=None)

        async def end(self):
            return None

    captured: dict = {}

    class FakeRtspServer:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.port = 15555
            self.url = "rtsp://127.0.0.1:15555/c100x"

        async def start(self):
            session = SimpleNamespace(closed=False)

            async def close():
                session.closed = True

            session.close = close
            await captured["on_connect"](session)
            for _ in range(20):
                if started:
                    break
                await asyncio.sleep(0)
            return self.url

    monkeypatch.setattr(plugin_main, "SrtpMediaSession", lambda *a, **k: FakeMedia())
    monkeypatch.setattr(plugin_main, "RtspServer", FakeRtspServer)

    url = await device._start_rtsp_session()

    assert url == "rtsp://127.0.0.1:15555/c100x"
    assert started == [eu_id]
    assert started[0] != gateway_id
