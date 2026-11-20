"""SDP redaction and audio-rejection diagnostics (0.1.10)."""

from __future__ import annotations

import base64

import pytest

from c100x.sdp import (
    SRTP_KEY_BYTES,
    SRTP_SUITE,
    build_monitoring_offer,
    explain_answer_audio,
    format_negotiated_audio,
    parse_answer,
    redact_sdp,
)
from c100x.srtp_media import MediaSessionError, SrtpMediaSession


def _keys() -> tuple[str, str]:
    audio = base64.b64encode(b"\x11" * SRTP_KEY_BYTES).decode()
    video = base64.b64encode(b"\x22" * SRTP_KEY_BYTES).decode()
    return audio, video


def test_redact_sdp_strips_inline_keys() -> None:
    audio_key, video_key = _keys()
    raw = (
        "v=0\r\n"
        f"a=crypto:1 {SRTP_SUITE} inline:{audio_key}\r\n"
        f"a=crypto:1 {SRTP_SUITE} inline:{video_key}|2^20|1:32\r\n"
    )
    redacted = redact_sdp(raw)
    assert audio_key not in redacted
    assert video_key not in redacted
    assert "inline:<redacted>" in redacted
    assert SRTP_SUITE in redacted


def test_explain_answer_audio_port_zero() -> None:
    raw = (
        "v=0\r\n"
        "m=audio 0 RTP/SAVP 8\r\n"
        "a=sendrecv\r\n"
        "m=video 6002 RTP/SAVP 96\r\n"
        "a=sendonly\r\n"
    )
    assert "port 0" in explain_answer_audio(raw)
    assert "rejected" in explain_answer_audio(raw)


def test_parse_answer_port_zero_yields_none_audio() -> None:
    offer = build_monitoring_offer(
        address="203.0.113.10",
        audio_port=40000,
        video_port=40002,
        device_address="dev",
    )
    audio_key, video_key = _keys()
    answer = (
        "v=0\r\n"
        "o=- 1 1 IN IP4 198.51.100.9\r\n"
        "c=IN IP4 198.51.100.9\r\n"
        "t=0 0\r\n"
        "m=audio 0 RTP/SAVP 8\r\n"
        "a=rtpmap:8 PCMA/8000\r\n"
        "a=inactive\r\n"
        f"a=crypto:1 {SRTP_SUITE} inline:{audio_key}\r\n"
        "m=video 6002 RTP/SAVP 96\r\n"
        "a=rtpmap:96 H264/90000\r\n"
        "a=sendonly\r\n"
        f"a=crypto:1 {SRTP_SUITE} inline:{video_key}\r\n"
    )
    session = parse_answer(answer, offer)
    assert session.audio is None
    assert session.video is not None
    assert format_negotiated_audio(session) == "none"
    assert "port 0" in explain_answer_audio(answer)


@pytest.mark.asyncio
async def test_set_talkback_reports_missing_audio_reason() -> None:
    class FakeSip:
        on_call_end = None

    media = SrtpMediaSession(FakeSip())  # type: ignore[arg-type]
    offer = build_monitoring_offer(
        address="203.0.113.10",
        audio_port=40000,
        video_port=40002,
        device_address="dev",
    )
    _audio_key, video_key = _keys()
    answer = (
        "v=0\r\n"
        "o=- 1 1 IN IP4 198.51.100.9\r\n"
        "c=IN IP4 198.51.100.9\r\n"
        "t=0 0\r\n"
        "m=audio 0 RTP/SAVP 8\r\n"
        "a=inactive\r\n"
        "m=video 6002 RTP/SAVP 96\r\n"
        "a=rtpmap:96 H264/90000\r\n"
        "a=sendonly\r\n"
        f"a=crypto:1 {SRTP_SUITE} inline:{video_key}\r\n"
    )
    session = parse_answer(answer, offer)
    media.negotiated = session
    media.audio_reject_reason = explain_answer_audio(answer)
    # audio protocol object present but unconfigured — block reason is missing audio
    media.audio = object()  # type: ignore[assignment]
    reason = media.talkback_block_reason()
    assert reason is not None
    assert "did not negotiate audio" in reason
    assert "port 0" in reason
    with pytest.raises(MediaSessionError, match="Talkback unavailable"):
        await media.set_talkback(True)
