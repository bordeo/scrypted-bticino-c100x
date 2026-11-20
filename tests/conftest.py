"""Shared pytest fixtures.

Offline tests must not spawn the door-audio-gain ffmpeg transcoder. The
production default is +12 dB; patch it to 0 unless a test sets the storage key.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_door_audio_gain_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("c100x.const.DEFAULT_DOOR_AUDIO_GAIN_DB", 0)
