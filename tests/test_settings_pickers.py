"""Offline tests for Installation/Gateway picker loading."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from c100x.api import ApiError
from c100x.const import CONF_EMAIL, CONF_GATEWAY_ID, CONF_HOME_ID, CONF_PASSWORD
from sdk_stub import install as install_sdk


class FakeStorage:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = data or {}

    def getItem(self, key: str) -> str | None:
        return self.data.get(key)

    def setItem(self, key: str, value: str) -> None:
        self.data[key] = value


def _make_plugin(messages: list[str]):
    install_sdk()
    import main as plugin_main

    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.storage = FakeStorage()
    plugin.device = None
    plugin.sip = None
    plugin.gateway_id = None
    plugin.home_id = None
    plugin._session = None
    plugin._auth = None
    plugin._api = None
    plugin._supervise_task = None
    plugin._material_dir = None
    plugin._plants_cache = []
    plugin._gateways_cache = []
    plugin.print = lambda *args, **kwargs: messages.append(" ".join(str(a) for a in args))
    plugin.refreshDevices = AsyncMock()
    return plugin, plugin_main


@pytest.mark.asyncio
async def test_put_setting_home_id_loads_gateways_via_existing_api() -> None:
    messages: list[str] = []
    plugin, plugin_main = _make_plugin(messages)
    api = AsyncMock()
    api.modules = AsyncMock(
        return_value=[
            {"id": "lock-1", "name": "Door", "device": "lock"},
            {"id": "gw-e4d7", "name": "Classe 100X", "device": "gateway"},
        ]
    )
    plugin._api = api
    plugin._plants_cache = [{"id": "plant-e4d7", "name": "Bordin (house)"}]
    plugin._gateways_cache = [{"id": "stale"}]

    await plugin.putSetting(CONF_HOME_ID, "Bordin (house) (plant-e4d7)")

    api.modules.assert_awaited_once_with("plant-e4d7")
    assert [item["id"] for item in plugin._gateways_cache] == ["gw-e4d7"]
    settings = await plugin.getSettings()
    gateway_setting = next(item for item in settings if item["key"] == CONF_GATEWAY_ID)
    assert gateway_setting["choices"] == ["Classe 100X (gw-e4d7)"]


@pytest.mark.asyncio
async def test_put_setting_home_id_uses_load_plants_when_api_missing(monkeypatch) -> None:
    messages: list[str] = []
    plugin, plugin_main = _make_plugin(messages)
    plugin_main._json_set(plugin.storage, CONF_EMAIL, "user@example.com")
    plugin_main._json_set(plugin.storage, CONF_PASSWORD, "secret")
    called: list[tuple[str, str]] = []

    async def fake_load_plants(email: str, password: str) -> None:
        called.append((email, password))
        plugin._gateways_cache = [{"id": "gw-from-plants", "name": "Gateway", "device": "gateway"}]

    monkeypatch.setattr(plugin, "_load_plants", fake_load_plants)

    await plugin.putSetting(CONF_HOME_ID, "plant-e4d7")

    assert called == [("user@example.com", "secret")]
    assert plugin._gateways_cache[0]["id"] == "gw-from-plants"
    # Password must not appear in plugin console output.
    assert all("secret" not in msg and "user@example.com" not in msg for msg in messages)


@pytest.mark.asyncio
async def test_load_gateways_prefers_ha_device_gateway() -> None:
    messages: list[str] = []
    plugin, _plugin_main = _make_plugin(messages)
    api = AsyncMock()
    api.modules = AsyncMock(
        return_value=[
            {"id": "eu-1", "name": "External unit", "device": "eu", "type": "vde"},
            {"id": "gw-1", "name": "Classe 100X", "device": "gateway"},
        ]
    )

    await plugin._load_gateways(api, "plant-e4d7")

    # Old vde-name filter would have preferred eu-1 and dropped the real gateway.
    assert [item["id"] for item in plugin._gateways_cache] == ["gw-1"]
    assert messages == []


@pytest.mark.asyncio
async def test_load_gateways_prints_when_modules_empty() -> None:
    messages: list[str] = []
    plugin, _plugin_main = _make_plugin(messages)
    api = AsyncMock()
    api.modules = AsyncMock(return_value=[])

    await plugin._load_gateways(api, "plant-empty")

    assert plugin._gateways_cache == []
    assert "No modules for this installation; pick another one" in messages


@pytest.mark.asyncio
async def test_load_gateways_logs_api_failure_without_secrets(monkeypatch) -> None:
    messages: list[str] = []
    logged: list[tuple[str, str]] = []
    plugin, plugin_main = _make_plugin(messages)
    api = AsyncMock()
    api.modules = AsyncMock(side_effect=ApiError(502, "GET modules", reason_code="null_body"))

    def capture(error: ApiError, *, where: str) -> None:
        logged.append((where, f"HTTP {error.status}"))

    monkeypatch.setattr(plugin_main, "log_api_failure", capture)

    with pytest.raises(ApiError):
        await plugin._load_gateways(api, "plant-e4d7")

    assert logged == [("load_gateways", "HTTP 502")]


@pytest.mark.asyncio
async def test_maybe_refresh_pickers_loads_gateways_when_plants_cached() -> None:
    messages: list[str] = []
    plugin, plugin_main = _make_plugin(messages)
    plugin_main._json_set(plugin.storage, CONF_HOME_ID, "plant-e4d7")
    plugin._plants_cache = [{"id": "plant-e4d7", "name": "Bordin"}]
    plugin._gateways_cache = []
    api = AsyncMock()
    api.modules = AsyncMock(
        return_value=[{"id": "gw-1", "name": "Classe 100X", "device": "gateway"}]
    )
    plugin._api = api

    await plugin._maybe_refresh_pickers()

    api.modules.assert_awaited_once_with("plant-e4d7")
    assert [item["id"] for item in plugin._gateways_cache] == ["gw-1"]
