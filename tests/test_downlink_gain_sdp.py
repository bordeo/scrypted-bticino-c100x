"""Gain path must advertise Opus PT that matches transcoder output."""

from __future__ import annotations

from c100x.downlink_gain import DoorAudioGainTranscoder


def test_gain_output_pt_matches_default_describe_opus() -> None:
    # Pre-negotiation DESCRIBE defaults to Opus/96; gained packets must use the
    # same PT or HomeKit drops them after locking the SDP.
    assert DoorAudioGainTranscoder.OUTPUT_PAYLOAD_TYPE == 96
    assert DoorAudioGainTranscoder.OUTPUT_RTPMAP == "opus/48000/2"
