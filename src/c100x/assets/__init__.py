"""Static assets shipped with the plugin."""

from __future__ import annotations

from pathlib import Path

_ASSETS = Path(__file__).resolve().parent

# Landscape / wide (≈16:9). Owner replaces with final photos.
PLACEHOLDER_DAY_PATH = _ASSETS / "placeholder_day.jpg"
PLACEHOLDER_NIGHT_PATH = _ASSETS / "placeholder_night.jpg"
# Legacy single placeholder kept as last-resort fallback.
CITOFONO_PLACEHOLDER_PATH = _ASSETS / "citofono_placeholder.jpg"

# Declared native size (stubs and real assets should match).
PLACEHOLDER_SIZE = (1280, 720)


def load_placeholder_jpeg(*, night: bool = False) -> bytes:
    """Return a day/night Casa-tile JPEG (landscape 16:9)."""
    path = PLACEHOLDER_NIGHT_PATH if night else PLACEHOLDER_DAY_PATH
    for candidate in (path, CITOFONO_PLACEHOLDER_PATH):
        if candidate.is_file():
            return candidate.read_bytes()
    raise FileNotFoundError(f"Missing snapshot placeholder: {path}")


def load_citofono_placeholder() -> bytes:
    """Backward-compatible alias — prefers day landscape, then legacy."""
    for path in (PLACEHOLDER_DAY_PATH, CITOFONO_PLACEHOLDER_PATH):
        if path.is_file():
            return path.read_bytes()
    raise FileNotFoundError("No snapshot placeholder assets found")
