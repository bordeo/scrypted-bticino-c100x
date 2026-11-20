"""Bundled Citofono snapshot placeholder."""

from __future__ import annotations

from c100x.assets import CITOFONO_PLACEHOLDER_PATH, load_citofono_placeholder


def test_citofono_placeholder_is_small_jpeg() -> None:
    assert CITOFONO_PLACEHOLDER_PATH.is_file()
    data = load_citofono_placeholder()
    assert data.startswith(b"\xff\xd8\xff")
    assert data.endswith(b"\xff\xd9")
    assert len(data) < 100_000
