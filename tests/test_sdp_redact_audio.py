"""SDP redaction and audio-rejection diagnostics (0.1.10+)."""

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
from c100x.sip import SipDialog
from c100x.srtp_media import MediaSessionError, SrtpMediaSession


def _keys() -> tuple[str, str]:
    audio = base64.b64encode(b"\x11" * SRTP_KEY_BYTES).decode()
    video = base64.b64encode(b"\x22" * SRTP_KEY_BYTES).decode()
    return audio, video


def _answer_sdp() -> bytes:
    audio_key, video_key = _keys()
    return (
        "v=0\r\n"
        "o=- 1 1 IN IP4 198.51.100.9\r\n"
        "c=IN IP4 198.51.100.9\r\n"
        "t=0 0\r\n"
        "m=audio 6000 RTP/SAVP 96\r\n"
        "a=rtpmap:96 opus/48000/2\r\n"
        "a=sendrecv\r\n"
        f"a=crypto:1 {SRTP_SUITE} inline:{audio_key}\r\n"
        "m=video 6002 RTP/SAVP 96\r\n"
        "a=rtpmap:96 H264/90000\r\n"
        "a=sendonly\r\n"
        f"a=crypto:1 {SRTP_SUITE} inline:{video_key}\r\n"
    ).encode()


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
async def test_start_aborts_when_discarded_during_invite(monkeypatch) -> None:
    """end() that finishes during INVITE must not configure closed sockets."""
    import c100x.srtp_media as srtp_media

    real_reserve = srtp_media.reserve_even_pair

    class FakeSip:
        on_call_end = None
        _dialog = None

        async def start_monitoring(self, offer_sdp: str) -> bytes:
            # Simulate HomeKit teardown completing while INVITE is in flight.
            await media.end()
            # Late 200 installs the dialog after end() already finished.
            self._dialog = object()
            return _answer_sdp()

        async def end_monitoring(self, dialog=None) -> None:
            self.bye_dialogs = getattr(self, "bye_dialogs", [])
            self.bye_dialogs.append(dialog if dialog is not None else self._dialog)

    sip = FakeSip()
    media = SrtpMediaSession(sip)  # type: ignore[arg-type]
    monkeypatch.setattr(
        srtp_media, "reserve_even_pair", lambda bind_host="0.0.0.0": real_reserve("127.0.0.1")
    )
    monkeypatch.setattr(
        srtp_media,
        "discover_mappings",
        lambda sockets: tuple(("203.0.113.10", 50000 + i) for i in range(len(sockets))),
    )

    with pytest.raises(MediaSessionError, match="was ended"):
        await media.start("eu-1")
    assert media.negotiated is None
    assert media._discarded is True
    # end() mid-INVITE had no dialog; failed-start BYEs the late dialog only.
    assert sip.bye_dialogs == [None, sip._dialog]


@pytest.mark.asyncio
async def test_start_aborts_before_invite_when_discarded_during_stun(monkeypatch) -> None:
    """Finished end() during STUN must not send INVITE (door single-slot)."""
    import c100x.srtp_media as srtp_media

    real_reserve = srtp_media.reserve_even_pair
    invites: list[str] = []

    class FakeSip:
        on_call_end = None
        _dialog = None

        async def start_monitoring(self, offer_sdp: str) -> bytes:
            invites.append(offer_sdp)
            return _answer_sdp()

        async def end_monitoring(self, dialog=None) -> None:
            return None

    media = SrtpMediaSession(FakeSip())  # type: ignore[arg-type]

    def discover_and_discard(sockets):
        # Simulate finished end() while STUN ran (sticky _discarded).
        media._discarded = True
        return tuple(("203.0.113.10", 50000 + i) for i in range(len(sockets)))

    monkeypatch.setattr(
        srtp_media, "reserve_even_pair", lambda bind_host="0.0.0.0": real_reserve("127.0.0.1")
    )
    monkeypatch.setattr(srtp_media, "discover_mappings", discover_and_discard)

    with pytest.raises(MediaSessionError, match="was ended"):
        await media.start("eu-1")
    assert invites == []
    assert media._discarded is True


@pytest.mark.asyncio
async def test_failed_start_bye_does_not_drop_replacement_dialog(monkeypatch) -> None:
    """Discarded start must BYE its own dialog, not a newer session's."""
    import c100x.srtp_media as srtp_media

    real_reserve = srtp_media.reserve_even_pair
    our_dialog = SipDialog(
        call_id="ours",
        local_uri="sip:a@x",
        remote_uri="sip:c100x@x",
        local_tag="t1",
        remote_to="sip:c100x@x;tag=r1",
        remote_target="sip:c100x@x",
        route_set=(),
        local_sequence=1,
    )
    replacement = SipDialog(
        call_id="theirs",
        local_uri="sip:a@x",
        remote_uri="sip:c100x@x",
        local_tag="t2",
        remote_to="sip:c100x@x;tag=r2",
        remote_target="sip:c100x@x",
        route_set=(),
        local_sequence=1,
    )

    class FakeSip:
        on_call_end = None
        _dialog = None
        bye_calls: list = []

        async def start_monitoring(self, offer_sdp: str) -> bytes:
            await media.end()
            # Dialog this start created — start() captures it as owned_dialog.
            self._dialog = our_dialog
            return _answer_sdp()

        async def end_monitoring(self, dialog=None) -> None:
            # Newer session already owns sip._dialog when orphan BYE runs.
            if self._dialog is our_dialog:
                self._dialog = replacement
            target = dialog if dialog is not None else self._dialog
            self.bye_calls.append(target)
            if dialog is None or self._dialog is dialog:
                self._dialog = None
            # Explicit orphan BYE leaves replacement in place.

    sip = FakeSip()
    media = SrtpMediaSession(sip)  # type: ignore[arg-type]
    monkeypatch.setattr(
        srtp_media, "reserve_even_pair", lambda bind_host="0.0.0.0": real_reserve("127.0.0.1")
    )
    monkeypatch.setattr(
        srtp_media,
        "discover_mappings",
        lambda sockets: tuple(("203.0.113.10", 50000 + i) for i in range(len(sockets))),
    )

    with pytest.raises(MediaSessionError, match="was ended"):
        await media.start("eu-1")
    # end() mid-INVITE: no dialog yet (None). Failed-start BYEs our_dialog only.
    assert sip.bye_calls == [None, our_dialog]
    assert sip._dialog is replacement


@pytest.mark.asyncio
async def test_ack_failure_after_200_still_byes_dialog(monkeypatch) -> None:
    """If ACK fails after dialog install, failed-start must still BYE it."""
    import c100x.srtp_media as srtp_media
    from c100x.sip import SipError

    real_reserve = srtp_media.reserve_even_pair
    dialog = SipDialog(
        call_id="ack-fail",
        local_uri="sip:a@x",
        remote_uri="sip:c100x@x",
        local_tag="t1",
        remote_to="sip:c100x@x;tag=r1",
        remote_target="sip:c100x@x",
        route_set=(),
        local_sequence=1,
    )

    class FakeSip:
        on_call_end = None
        _dialog = None
        bye_calls: list = []

        async def start_monitoring(self, offer_sdp: str) -> bytes:
            self._dialog = dialog
            raise SipError("ACK send failed")

        async def end_monitoring(self, dlg=None) -> None:
            self.bye_calls.append(dlg if dlg is not None else self._dialog)
            if dlg is None or self._dialog is dlg:
                self._dialog = None

    sip = FakeSip()
    media = SrtpMediaSession(sip)  # type: ignore[arg-type]
    monkeypatch.setattr(
        srtp_media, "reserve_even_pair", lambda bind_host="0.0.0.0": real_reserve("127.0.0.1")
    )
    monkeypatch.setattr(
        srtp_media,
        "discover_mappings",
        lambda sockets: tuple(("203.0.113.10", 50000 + i) for i in range(len(sockets))),
    )

    with pytest.raises(SipError, match="ACK"):
        await media.start("eu-1")
    assert sip.bye_calls == [dialog]
    assert sip._dialog is None


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
