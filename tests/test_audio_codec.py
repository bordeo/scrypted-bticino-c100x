"""Opus / Speex talkback format helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from c100x.audio_codec import OpusEncoder, talkback_format_for


def test_talkback_format_opus_speex_g711() -> None:
    assert talkback_format_for(SimpleNamespace(name="opus", clock_rate=48000)).sample_rate == 48000
    assert talkback_format_for(SimpleNamespace(name="speex", clock_rate=16000)).sample_rate == 16000
    assert talkback_format_for(SimpleNamespace(name="PCMA", clock_rate=8000)).sample_rate == 8000


def test_opus_encoder_silence_and_pcm() -> None:
    pytest.importorskip("opuslib")
    enc = OpusEncoder(sample_rate=48000, channels=1)
    silence = enc.encode_silence()
    assert isinstance(silence, bytes) and len(silence) > 0
    # ~50 B packets on the wire match official Door Entry observation.
    assert len(silence) < 120
    pcm = b"\x00\x01" * enc.frame_samples
    packet = enc.encode(pcm)
    assert isinstance(packet, bytes) and len(packet) > 0
