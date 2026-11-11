"""Official-topology helpers for visible external units."""

from __future__ import annotations

import json

from c100x.topology import visible_external_units


def _eu(module_id: str, *, visible: int, button_id: int) -> dict:
    return {
        "id": module_id,
        "deviceType": "eu",
        "tags": [
            {
                "key": "PrivateAddress",
                "value": json.dumps({"visible": visible, "buttonId": button_id}),
            }
        ],
    }


def test_visible_external_units_orders_by_button_id() -> None:
    modules = [
        {"id": "gateway", "device": "gateway", "tags": []},
        _eu("eu-second", visible=1, button_id=2),
        _eu("eu-first", visible=2, button_id=1),
        _eu("eu-hidden", visible=0, button_id=0),
        {"id": "lock-1", "deviceType": "lock", "tags": []},
    ]
    units = visible_external_units(modules)
    assert [item["id"] for item in units] == ["eu-first", "eu-second"]


def test_visible_external_units_accepts_device_field() -> None:
    modules = [
        {
            "id": "eu-via-device",
            "device": "eu",
            "tags": [
                {
                    "key": "PrivateAddress",
                    "value": {"visible": 1, "buttonId": 3},
                }
            ],
        }
    ]
    assert [item["id"] for item in visible_external_units(modules)] == ["eu-via-device"]
