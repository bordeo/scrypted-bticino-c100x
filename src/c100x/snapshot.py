"""Snapshot JPEG helpers: day/night placeholders and HAP size cover-crop.

HomeKit requests a width/height; HAP expects a JPEG that matches. We center-crop
(cover, never stretch) then resize, capping the long side at 1280. Without a
requested size the portrait asset is returned as-is. Pillow is preferred;
without it we return the original bytes.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from .assets import load_placeholder_jpeg
from .sun import (
    DEFAULT_LATITUDE,
    DEFAULT_LONGITUDE,
    DEFAULT_TIMEZONE,
    clamp_latitude,
    clamp_longitude,
    is_daylight,
)

_LOGGER = logging.getLogger(__name__)

SNAPSHOT_MAX_LONG_SIDE = 1280
SNAPSHOT_JPEG_QUALITY = 80


def cover_crop_box(
    src_w: int, src_h: int, dst_w: int, dst_h: int
) -> tuple[int, int, int, int]:
    """Return a centered cover crop ``(left, top, right, bottom)`` in source pixels."""
    if src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
        raise ValueError("dimensions must be positive")
    src_aspect = src_w / src_h
    dst_aspect = dst_w / dst_h
    if src_aspect > dst_aspect:
        # Source is wider — crop left/right.
        crop_w = max(1, int(round(src_h * dst_aspect)))
        left = max(0, (src_w - crop_w) // 2)
        right = min(src_w, left + crop_w)
        return left, 0, right, src_h
    # Source is taller (or equal) — crop top/bottom.
    crop_h = max(1, int(round(src_w / dst_aspect)))
    top = max(0, (src_h - crop_h) // 2)
    bottom = min(src_h, top + crop_h)
    return 0, top, src_w, bottom


def fit_within_long_side(
    width: int, height: int, *, max_long: int = SNAPSHOT_MAX_LONG_SIDE
) -> tuple[int, int]:
    """Scale so max(width, height) <= max_long, preserving aspect."""
    width = max(1, int(width))
    height = max(1, int(height))
    long_side = max(width, height)
    if long_side <= max_long:
        return width, height
    scale = max_long / float(long_side)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def parse_picture_size(options: Any) -> tuple[int, int] | None:
    """Extract positive (width, height) from Scrypted RequestPictureOptions."""
    if not options:
        return None
    picture = options
    if isinstance(options, dict):
        picture = options.get("picture") or options
    if not isinstance(picture, dict):
        return None
    try:
        width = int(picture.get("width") or 0)
        height = int(picture.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def resize_jpeg_cover(
    jpeg: bytes,
    width: int,
    height: int,
    *,
    max_long: int = SNAPSHOT_MAX_LONG_SIDE,
    quality: int = SNAPSHOT_JPEG_QUALITY,
) -> bytes:
    """Center-crop (cover) and resize ``jpeg`` to the requested size.

    Falls back to the original bytes when Pillow is unavailable or decoding fails.
    """
    out_w, out_h = fit_within_long_side(width, height, max_long=max_long)
    try:
        from PIL import Image
    except ImportError:
        _LOGGER.debug("Pillow unavailable — returning original snapshot JPEG")
        return jpeg
    try:
        with Image.open(io.BytesIO(jpeg)) as image:
            image = image.convert("RGB")
            box = cover_crop_box(image.width, image.height, out_w, out_h)
            cropped = image.crop(box)
            resized = cropped.resize((out_w, out_h), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue()
    except Exception as err:
        _LOGGER.warning("Snapshot resize failed (%s); returning original", type(err).__name__)
        return jpeg


def choose_placeholder_jpeg(
    *,
    latitude: float = DEFAULT_LATITUDE,
    longitude: float = DEFAULT_LONGITUDE,
    tz_name: str = DEFAULT_TIMEZONE,
    when=None,
) -> bytes:
    """Load day or night placeholder based on local sunrise/sunset."""
    day = is_daylight(
        when,
        latitude=clamp_latitude(latitude),
        longitude=clamp_longitude(longitude),
        tz_name=tz_name,
    )
    return load_placeholder_jpeg(night=not day)
