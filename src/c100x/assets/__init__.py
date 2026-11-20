"""Static assets shipped with the plugin."""

from __future__ import annotations

from pathlib import Path

_ASSETS = Path(__file__).resolve().parent

CITOFONO_PLACEHOLDER_PATH = _ASSETS / "citofono_placeholder.jpg"


def load_citofono_placeholder() -> bytes:
    """Return the bundled Casa-tile JPEG (no SIP / no monitoring call)."""
    return CITOFONO_PLACEHOLDER_PATH.read_bytes()
