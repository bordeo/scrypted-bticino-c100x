"""Static assets shipped with the plugin."""

from __future__ import annotations

from pathlib import Path

_ASSETS = Path(__file__).resolve().parent

# Portrait (≈9:16). Owner replaces with final photos (subject = left half of house).
PLACEHOLDER_DAY_PATH = _ASSETS / "placeholder_day.jpg"
PLACEHOLDER_NIGHT_PATH = _ASSETS / "placeholder_night.jpg"
# Landscape / wide (≈16:9) for HomeKit camera tiles.
PLACEHOLDER_DAY_WIDE_PATH = _ASSETS / "placeholder_day_wide.jpg"
PLACEHOLDER_NIGHT_WIDE_PATH = _ASSETS / "placeholder_night_wide.jpg"
# Legacy single placeholder kept as last-resort fallback.
CITOFONO_PLACEHOLDER_PATH = _ASSETS / "citofono_placeholder.jpg"

# Declared native sizes (stubs and real assets should match).
PLACEHOLDER_PORTRAIT_SIZE = (576, 1024)
PLACEHOLDER_WIDE_SIZE = (1280, 720)


def load_placeholder_jpeg(*, night: bool = False, wide: bool = False) -> bytes:
    """Return a day/night Casa-tile JPEG (portrait or wide)."""
    if wide:
        path = PLACEHOLDER_NIGHT_WIDE_PATH if night else PLACEHOLDER_DAY_WIDE_PATH
        fallbacks = (
            path,
            PLACEHOLDER_NIGHT_PATH if night else PLACEHOLDER_DAY_PATH,
            CITOFONO_PLACEHOLDER_PATH,
        )
    else:
        path = PLACEHOLDER_NIGHT_PATH if night else PLACEHOLDER_DAY_PATH
        fallbacks = (
            path,
            PLACEHOLDER_NIGHT_WIDE_PATH if night else PLACEHOLDER_DAY_WIDE_PATH,
            CITOFONO_PLACEHOLDER_PATH,
        )
    for candidate in fallbacks:
        if candidate.is_file():
            return candidate.read_bytes()
    raise FileNotFoundError(f"Missing snapshot placeholder: {path}")


def load_citofono_placeholder() -> bytes:
    """Backward-compatible alias — prefers wide day, then portrait day, then legacy."""
    for path in (
        PLACEHOLDER_DAY_WIDE_PATH,
        PLACEHOLDER_DAY_PATH,
        CITOFONO_PLACEHOLDER_PATH,
    ):
        if path.is_file():
            return path.read_bytes()
    raise FileNotFoundError("No snapshot placeholder assets found")
