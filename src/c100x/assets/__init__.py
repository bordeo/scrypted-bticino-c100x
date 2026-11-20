"""Static assets shipped with the plugin."""

from __future__ import annotations

from pathlib import Path

_ASSETS = Path(__file__).resolve().parent

# Owner drops final 576×1024 portrait JPEGs here (subject = left half of house).
PLACEHOLDER_DAY_PATH = _ASSETS / "placeholder_day.jpg"
PLACEHOLDER_NIGHT_PATH = _ASSETS / "placeholder_night.jpg"
# Legacy single placeholder kept as fallback if day/night files are missing.
CITOFONO_PLACEHOLDER_PATH = _ASSETS / "citofono_placeholder.jpg"


def load_placeholder_jpeg(*, night: bool = False) -> bytes:
    """Return the day or night Casa-tile JPEG (no SIP / no monitoring call)."""
    path = PLACEHOLDER_NIGHT_PATH if night else PLACEHOLDER_DAY_PATH
    if path.is_file():
        return path.read_bytes()
    if CITOFONO_PLACEHOLDER_PATH.is_file():
        return CITOFONO_PLACEHOLDER_PATH.read_bytes()
    raise FileNotFoundError(f"Missing snapshot placeholder: {path}")


def load_citofono_placeholder() -> bytes:
    """Backward-compatible alias — prefers day image, then legacy file."""
    if PLACEHOLDER_DAY_PATH.is_file():
        return PLACEHOLDER_DAY_PATH.read_bytes()
    return CITOFONO_PLACEHOLDER_PATH.read_bytes()
