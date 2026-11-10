"""Minimal scrypted_sdk stub for offline plugin tests."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


def install() -> tuple[types.ModuleType, types.ModuleType]:
    if getattr(sys.modules.get("scrypted_sdk"), "_c100x_stub", False):
        return sys.modules["scrypted_sdk"], sys.modules["scrypted_sdk.types"]

    sdk = types.ModuleType("scrypted_sdk")
    sdk._c100x_stub = True

    class Base:
        def __init__(self, nativeId=None):
            self.nativeId = nativeId
            self.id = nativeId or "plugin"
            self.interfaces = []
            self.binaryState = False

        async def onDeviceEvent(self, *a, **k):
            return None

        def print(self, *a, **k):
            return None

    sdk.ScryptedDeviceBase = Base

    types_mod = types.ModuleType("scrypted_sdk.types")

    class _Enum:
        def __init__(self, value):
            self.value = value

    class ScryptedInterface:
        VideoCamera = _Enum("VideoCamera")
        Camera = _Enum("Camera")
        BinarySensor = _Enum("BinarySensor")
        Settings = _Enum("Settings")
        Intercom = _Enum("Intercom")

    class ScryptedDeviceType:
        Doorbell = _Enum("Doorbell")

    class ScryptedMimeTypes:
        MediaStreamUrl = _Enum("text/x-media-url")
        FFmpegInput = _Enum("x-scrypted/x-ffmpeg-input")

    # Unique base classes — Python forbids duplicate bases.
    stubs = {
        "BinarySensor": type("BinarySensor", (), {}),
        "Camera": type("Camera", (), {}),
        "DeviceProvider": type("DeviceProvider", (), {}),
        "Intercom": type("Intercom", (), {}),
        "MediaObject": type("MediaObject", (), {}),
        "RequestMediaStreamOptions": dict,
        "ResponseMediaStreamOptions": dict,
        "ScryptedDeviceType": ScryptedDeviceType,
        "ScryptedInterface": ScryptedInterface,
        "ScryptedMimeTypes": ScryptedMimeTypes,
        "ScryptedSystemDevice": type("ScryptedSystemDevice", (), {}),
        "Setting": dict,
        "Settings": type("Settings", (), {}),
        "VideoCamera": type("VideoCamera", (), {}),
    }
    for name, obj in stubs.items():
        setattr(types_mod, name, obj)

    sdk.types = types_mod
    sdk.deviceManager = SimpleNamespace(
        onDevicesChanged=AsyncMock(),
        onDeviceEvent=AsyncMock(),
        getDeviceStorage=MagicMock(return_value={}),
        getDeviceState=MagicMock(),
    )
    sdk.mediaManager = SimpleNamespace(
        createMediaObject=AsyncMock(side_effect=lambda data, mime, options=None: data),
        convertMediaObjectToJSON=AsyncMock(return_value={}),
        getFFmpegPath=AsyncMock(return_value="ffmpeg"),
    )
    sys.modules["scrypted_sdk"] = sdk
    sys.modules["scrypted_sdk.types"] = types_mod
    # Force re-import of main against this stub.
    sys.modules.pop("main", None)
    return sdk, types_mod
