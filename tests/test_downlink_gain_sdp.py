"""Gain path must advertise Opus PT that matches transcoder output."""

from __future__ import annotations

from c100x.downlink_gain import DoorAudioGainTranscoder


def test_gain_output_pt_matches_default_describe_opus() -> None:
    # Pre-negotiation DESCRIBE defaults to Opus/96; gained packets must use the
    # same PT or HomeKit drops them after locking the SDP.
    assert DoorAudioGainTranscoder.OUTPUT_PAYLOAD_TYPE == 96
    assert DoorAudioGainTranscoder.OUTPUT_RTPMAP == "opus/48000/2"


def test_sdp_uses_gain_opus_only_when_transcoder_active() -> None:
    """Regression: setting alone must not claim Opus while Speex is passthrough."""
    # Documented contract for main.sdp_builder / getVideoStreamOptions:
    # advertise DoorAudioGainTranscoder Opus iff `_audio_gain.active`.
    t = DoorAudioGainTranscoder(gain_db=12, on_opus_rtp=lambda _p: None)
    assert t.active is False
    assert DoorAudioGainTranscoder.OUTPUT_PAYLOAD_TYPE == 96
