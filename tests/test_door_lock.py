"""HomeKit Lock / apriporta device (mocked SIP — never hits real hardware)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from c100x.const import CONF_EXPOSE_DOOR_OPENER, CONF_GATEWAY_ID, DOOR_RELOCK_SECONDS
from sdk_stub import install as install_sdk


def _visible_lock(lock_id: str, name: str = "Cancello", button_id: int = 1) -> dict:
    return {
        "id": lock_id,
        "name": name,
        "device": "lock",
        "tags": [
            {
                "key": "PrivateAddress",
                "value": {"visible": 1, "buttonId": button_id},
            }
        ],
    }


@pytest.mark.asyncio
async def test_unlock_sends_release_and_auto_relocks(monkeypatch) -> None:
    sdk, _ = install_sdk()
    import main as plugin_main

    class FakeStorage:
        def __init__(self) -> None:
            self.data = {
                CONF_GATEWAY_ID: '"gw-1"',
                CONF_EXPOSE_DOOR_OPENER: "true",
            }

        def getItem(self, key):
            return self.data.get(key)

        def setItem(self, key, value):
            self.data[key] = value

    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.storage = FakeStorage()
    plugin.device = None
    plugin.locks = {}
    plugin.lock_ids = ["lock-mod-1"]
    plugin._lock_names = {"lock-mod-1": "Cancello"}
    plugin.camera_ids = []
    plugin.gateway_id = "gw-1"
    plugin.home_id = "home-1"
    plugin._api = None
    plugin.print = lambda *a, **k: None

    sip = AsyncMock()
    sip.registered = True
    sip.release_door = AsyncMock()
    plugin.sip = sip

    # Speed up auto-relock for the test.
    monkeypatch.setattr(plugin_main, "DOOR_RELOCK_SECONDS", 0.05)

    lock = await plugin.getDevice("lock-lock-mod-1")
    assert lock is not None
    assert lock.lockState == plugin_main.LockState.Locked.value

    await lock.unlock()
    sip.release_door.assert_awaited_once_with("lock-mod-1", "gw-1")
    assert lock.lockState == plugin_main.LockState.Unlocked.value

    await asyncio.sleep(0.12)
    assert lock.lockState == plugin_main.LockState.Locked.value

    # lock() is a no-op that keeps Locked.
    await lock.lock()
    assert lock.lockState == plugin_main.LockState.Locked.value
    assert sip.release_door.await_count == 1


@pytest.mark.asyncio
async def test_unlock_refuses_without_sip() -> None:
    install_sdk()
    import main as plugin_main

    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.storage = type(
        "S",
        (),
        {"getItem": lambda self, k: None, "setItem": lambda self, k, v: None},
    )()
    plugin.sip = None
    plugin.gateway_id = "gw"
    plugin.print = lambda *a, **k: None
    lock = plugin_main.C100XDoorLock("lock-x", plugin, "x", "Door")
    with pytest.raises(RuntimeError, match="unavailable"):
        await lock.unlock()


@pytest.mark.asyncio
async def test_refresh_devices_exposes_lock_when_enabled() -> None:
    sdk, _ = install_sdk()
    import main as plugin_main

    class FakeStorage:
        def __init__(self, expose: bool = True) -> None:
            self.data = {
                CONF_GATEWAY_ID: '"gw-1"',
                CONF_EXPOSE_DOOR_OPENER: "true" if expose else "false",
            }

        def getItem(self, key):
            return self.data.get(key)

        def setItem(self, key, value):
            self.data[key] = value

    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.storage = FakeStorage(True)
    plugin.device = None
    plugin.locks = {}
    plugin.lock_ids = ["abc"]
    plugin._lock_names = {"abc": "Apriporta"}
    plugin.camera_ids = []
    plugin.sip = None
    plugin.gateway_id = "gw-1"
    plugin.home_id = "home"
    plugin._api = None
    plugin.print = lambda *a, **k: None

    await plugin.refreshDevices()
    changed = sdk.deviceManager.onDevicesChanged.await_args.args[0]
    ids = [d["nativeId"] for d in changed["devices"]]
    assert "doorbell" in ids
    assert "lock-abc" in ids
    lock_dev = next(d for d in changed["devices"] if d["nativeId"] == "lock-abc")
    assert lock_dev["type"] == "Lock"
    assert "Lock" in lock_dev["interfaces"]

    plugin.storage = FakeStorage(False)
    sdk.deviceManager.onDevicesChanged.reset_mock()
    await plugin.refreshDevices()
    changed = sdk.deviceManager.onDevicesChanged.await_args.args[0]
    assert [d["nativeId"] for d in changed["devices"]] == ["doorbell"]
    assert plugin.locks == {}


@pytest.mark.asyncio
async def test_refresh_lock_ids_from_topology() -> None:
    install_sdk()
    import main as plugin_main

    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.home_id = "home-1"
    plugin.camera_ids = []
    plugin.lock_ids = []
    plugin._lock_names = {}
    plugin._api = AsyncMock()
    plugin._api.modules = AsyncMock(
        return_value=[
            {"id": "gw", "device": "gateway", "tags": []},
            _visible_lock("lock-1", "Portone", 2),
            _visible_lock("lock-0", "Cancello", 1),
        ]
    )
    await plugin._refresh_camera_ids()
    assert plugin.lock_ids == ["lock-0", "lock-1"]
    assert plugin._lock_names["lock-0"] == "Cancello"
