"""Day/night placeholders, crop math, and snapshot sizing."""

from __future__ import annotations

import io
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from c100x.assets import (
    PLACEHOLDER_DAY_PATH,
    PLACEHOLDER_NIGHT_PATH,
    PLACEHOLDER_SIZE,
    load_placeholder_jpeg,
)
from c100x.snapshot import (
    choose_placeholder_jpeg,
    cover_crop_box,
    fit_within_long_side,
    parse_picture_size,
    render_snapshot_jpeg,
    resize_jpeg_cover,
)
from c100x.sun import DEFAULT_TIMEZONE, is_daylight, solar_events


def test_placeholder_day_night_assets_exist() -> None:
    assert PLACEHOLDER_DAY_PATH.is_file()
    assert PLACEHOLDER_NIGHT_PATH.is_file()
    day = load_placeholder_jpeg(night=False)
    night = load_placeholder_jpeg(night=True)
    for blob in (day, night):
        assert blob.startswith(b"\xff\xd8\xff") and blob.endswith(b"\xff\xd9")
        assert len(blob) < 100_000
    with Image.open(PLACEHOLDER_DAY_PATH) as img:
        assert img.size == PLACEHOLDER_SIZE == (1280, 720)
    with Image.open(PLACEHOLDER_NIGHT_PATH) as img:
        assert img.size == (1280, 720)


def test_cover_crop_box_wider_source() -> None:
    left, top, right, bottom = cover_crop_box(1920, 1080, 800, 800)
    assert top == 0 and bottom == 1080
    assert right - left == 1080
    assert left == (1920 - 1080) // 2


def test_cover_crop_box_taller_source() -> None:
    # Landscape 16:9 into portrait → crop sides.
    left, top, right, bottom = cover_crop_box(1280, 720, 720, 1280)
    assert top == 0 and bottom == 720
    crop_w = right - left
    assert abs(crop_w / 720 - 720 / 1280) < 0.02


def test_fit_within_long_side_caps_1280() -> None:
    assert fit_within_long_side(640, 480) == (640, 480)
    w, h = fit_within_long_side(2560, 1440)
    assert max(w, h) == 1280
    assert abs(w / h - 2560 / 1440) < 0.02


def test_parse_picture_size() -> None:
    assert parse_picture_size(None) is None
    assert parse_picture_size({"picture": {"width": 320, "height": 240}}) == (320, 240)
    assert parse_picture_size({"width": 100, "height": 0}) is None


def test_resize_jpeg_cover_matches_requested_size() -> None:
    raw = load_placeholder_jpeg(night=False)
    out = resize_jpeg_cover(raw, 320, 240)
    with Image.open(io.BytesIO(out)) as img:
        assert img.size == (320, 240)
        assert img.format == "JPEG"


def _red_marker_aspect(jpeg: bytes) -> float:
    with Image.open(io.BytesIO(jpeg)) as img:
        rgb = img.convert("RGB")
        pixels = rgb.load()
        xs: list[int] = []
        ys: list[int] = []
        for y in range(rgb.height):
            for x in range(rgb.width):
                r, g, b = pixels[x, y]
                if r > 150 and g < 80 and b < 80:
                    xs.append(x)
                    ys.append(y)
        assert xs and ys, "expected red marker in stub asset"
        return (max(xs) - min(xs) + 1) / float(max(ys) - min(ys) + 1)


def test_render_1280x720_no_distortion() -> None:
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    out = render_snapshot_jpeg(
        when=datetime(2024, 6, 21, 12, 0, tzinfo=tz),
        options={"picture": {"width": 1280, "height": 720}},
    )
    with Image.open(io.BytesIO(out)) as img:
        assert img.size == (1280, 720)
    assert abs(_red_marker_aspect(out) - 1.0) < 0.08


def test_render_720x1280_cover_crops_landscape() -> None:
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    out = render_snapshot_jpeg(
        when=datetime(2024, 6, 21, 12, 0, tzinfo=tz),
        options={"picture": {"width": 720, "height": 1280}},
    )
    with Image.open(io.BytesIO(out)) as img:
        assert img.size == (720, 1280)


def test_render_default_returns_landscape() -> None:
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    out = render_snapshot_jpeg(when=datetime(2024, 6, 21, 12, 0, tzinfo=tz))
    assert out == load_placeholder_jpeg(night=False)
    with Image.open(io.BytesIO(out)) as img:
        assert img.size == (1280, 720)


def test_solar_events_peraga_summer_winter() -> None:
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    rise, set_ = solar_events(datetime(2024, 6, 21).date())
    assert rise.astimezone(tz).hour <= 6
    assert set_.astimezone(tz).hour >= 20
    rise_w, set_w = solar_events(datetime(2024, 12, 21).date())
    assert rise_w.astimezone(tz).hour >= 7
    assert set_w.astimezone(tz).hour <= 17


def test_is_daylight_noon_vs_midnight() -> None:
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    noon = datetime(2024, 6, 21, 12, 0, tzinfo=tz)
    midnight = datetime(2024, 6, 21, 0, 30, tzinfo=tz)
    assert is_daylight(noon) is True
    assert is_daylight(midnight) is False


def test_choose_placeholder_switches_day_night() -> None:
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    day = choose_placeholder_jpeg(when=datetime(2024, 6, 21, 12, 0, tzinfo=tz))
    night = choose_placeholder_jpeg(when=datetime(2024, 6, 21, 0, 30, tzinfo=tz))
    assert day == load_placeholder_jpeg(night=False)
    assert night == load_placeholder_jpeg(night=True)


@pytest.mark.asyncio
async def test_take_picture_honors_requested_size() -> None:
    from sdk_stub import install as install_sdk

    install_sdk()
    import main as plugin_main

    class FakeStorage:
        def getItem(self, key):
            return None

        def setItem(self, key, value):
            pass

    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.storage = FakeStorage()
    device = plugin_main.C100XDoorbell("doorbell", plugin)
    device._last_jpeg = None
    jpeg = await device.takePicture({"picture": {"width": 200, "height": 100}})
    with Image.open(io.BytesIO(jpeg)) as img:
        assert img.size == (200, 100)


@pytest.mark.asyncio
async def test_get_picture_options_advertise_wide() -> None:
    from sdk_stub import install as install_sdk

    install_sdk()
    import main as plugin_main

    class FakeStorage:
        def getItem(self, key):
            return None

        def setItem(self, key, value):
            pass

    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.storage = FakeStorage()
    device = plugin_main.C100XDoorbell("doorbell", plugin)
    opts = await device.getPictureOptions()
    assert opts[0]["picture"] == {"width": 1280, "height": 720}
    assert opts[0]["canResize"] is True


@pytest.mark.asyncio
async def test_take_picture_default_is_wide_16x9() -> None:
    from sdk_stub import install as install_sdk

    install_sdk()
    import main as plugin_main

    class FakeStorage:
        def getItem(self, key):
            return None

        def setItem(self, key, value):
            pass

    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.storage = FakeStorage()
    device = plugin_main.C100XDoorbell("doorbell", plugin)
    device._last_jpeg = None
    jpeg = await device.takePicture(None)
    with Image.open(io.BytesIO(jpeg)) as img:
        assert img.size == (1280, 720)
