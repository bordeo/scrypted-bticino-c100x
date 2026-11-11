"""getVideoStream must hand WebRTC a JSON MediaStreamUrl, not a Python dict."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sdk_stub import install as install_sdk


@pytest.mark.asyncio
async def test_get_video_stream_serializes_media_stream_url_as_json() -> None:
    sdk, types_mod = install_sdk()
    import main as plugin_main

    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.sip = SimpleNamespace(registered=True)
    device = plugin_main.C100XDoorbell("doorbell", plugin)
    device._rtsp = SimpleNamespace(port=12345, url="rtsp://127.0.0.1:12345/c100x")

    result = await device.getVideoStream()

    sdk.mediaManager.createMediaObject.assert_awaited_once()
    args, kwargs = sdk.mediaManager.createMediaObject.call_args
    data, mime = args[0], args[1]
    options = args[2] if len(args) > 2 else kwargs.get("options")
    assert mime == types_mod.ScryptedMimeTypes.MediaStreamUrl.value
    assert not isinstance(data, dict)
    payload = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    assert payload["url"] == "rtsp://127.0.0.1:12345/c100x"
    assert payload["mediaStreamOptions"]["id"] == "default"
    assert payload["mediaStreamOptions"]["video"]["codec"] == "h264"
    assert payload["mediaStreamOptions"]["audio"]["codec"] == "pcm_alaw"
    assert options["sourceId"] == device.id
    assert result == data


@pytest.mark.asyncio
async def test_get_video_stream_requires_sip_registration() -> None:
    install_sdk()
    import main as plugin_main

    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.sip = SimpleNamespace(registered=False)
    device = plugin_main.C100XDoorbell("doorbell", plugin)
    with pytest.raises(RuntimeError, match="SIP is not registered yet"):
        await device.getVideoStream()
