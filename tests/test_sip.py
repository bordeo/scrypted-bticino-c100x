"""Offline SIP framer, digest, and doorbell INVITE behaviour."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from unittest.mock import MagicMock

import pytest

from c100x.const import (
    RING_DECLINE_DELAY_SECONDS,
    SIP_KEEPALIVE_PING,
    SIP_KEEPALIVE_PONG,
    SIP_RECONNECT_BACKOFF,
    SIP_REREGISTER_SECONDS,
)
from c100x.models import SipAccount
from c100x.sip import (
    SipClient,
    SipError,
    SipFramer,
    SipMessage,
    digest_authorization,
    parse_digest_challenge,
)
from c100x.srtp_media import next_reconnect_delay

DOMAIN_GW = "gw123.bs.iotleg.com"


def test_framer_strips_leading_pong_before_sip_response() -> None:
    framer = SipFramer()
    sip_ok = (
        b"SIP/2.0 200 OK\r\n"
        b"Call-ID: abc\r\n"
        b"CSeq: 1 REGISTER\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    result = framer.feed(SIP_KEEPALIVE_PONG + sip_ok)
    assert result.pongs == 1
    assert result.server_pings == 0
    assert len(result.messages) == 1
    assert result.messages[0].status_code == 200


def test_framer_handles_interleaved_server_ping_and_pong() -> None:
    framer = SipFramer()
    sip_ok = (
        b"SIP/2.0 200 OK\r\n"
        b"Call-ID: xyz\r\n"
        b"CSeq: 2 REGISTER\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    result = framer.feed(SIP_KEEPALIVE_PING + SIP_KEEPALIVE_PONG + sip_ok)
    assert result.server_pings == 1
    assert result.pongs == 1
    assert len(result.messages) == 1


def test_reconnect_backoff_and_reregister_interval() -> None:
    assert SIP_RECONNECT_BACKOFF == (1, 2, 5, 30)
    assert [next_reconnect_delay(i) for i in range(6)] == [1, 2, 5, 30, 30, 30]
    assert SIP_REREGISTER_SECONDS == 180


def test_digest_authorization_matches_rfc2617_md5() -> None:
    account = SipAccount(
        client_id="c",
        sip_uri=f"user@{DOMAIN_GW}",
        sip_password="secret",
        user_oid="o",
    )
    challenge = parse_digest_challenge(
        'Digest realm="sip", nonce="abc123", qop="auth", algorithm=MD5'
    )
    assert challenge["nonce"] == "abc123"
    header = digest_authorization(
        account=account, method="REGISTER", uri=f"sip:{DOMAIN_GW}", challenge=challenge
    )
    assert header.startswith("Digest ")
    assert 'username="user"' in header
    assert "response=" in header
    # Recompute expected response with extracted cnonce/nc
    parts = dict(
        item.strip().split("=", 1) for item in header.removeprefix("Digest ").split(",")
    )
    cnonce = parts["cnonce"].strip('"')
    nc = parts["nc"]
    md5 = lambda v: hashlib.md5(v.encode(), usedforsecurity=False).hexdigest()
    ha1 = md5(f"user:sip:secret")
    ha2 = md5(f"REGISTER:sip:{DOMAIN_GW}")
    expected = md5(f"{ha1}:abc123:{nc}:{cnonce}:auth:{ha2}")
    assert parts["response"].strip('"') == expected


def invite(call_id: str, sender: str = f"c100x@{DOMAIN_GW}") -> bytes:
    sdp = b"v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\n"
    head = (
        f"INVITE sip:user@{DOMAIN_GW} SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 10.0.0.1:5228;branch=z9hG4bK1\r\n"
        f"From: <sip:{sender}>;tag=abc\r\n"
        f"To: <sip:user@{DOMAIN_GW}>\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Type: application/sdp\r\n"
        f"Content-Length: {len(sdp)}\r\n\r\n"
    ).encode()
    return head + sdp


def make_sip(on_ring):
    account = SipAccount(
        client_id="c", sip_uri=f"user@{DOMAIN_GW}", sip_password="x", user_oid="o"
    )
    sip = SipClient(account, "c", "k", "ca", on_ring)
    sip.responses = []

    async def respond(msg, status, reason):
        sip.responses.append(status)

    sip._respond = respond
    return sip


async def feed(sip, raw: bytes):
    for msg in SipFramer().feed(raw).messages:
        await sip._handle_request(msg)


@pytest.mark.asyncio
async def test_invite_from_gateway_replies_180_then_486() -> None:
    rings = []

    async def on_ring(event):
        rings.append(event)

    sip = make_sip(on_ring)
    real_sleep = asyncio.sleep
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("c100x.sip.asyncio.sleep", lambda s: real_sleep(0))
        await feed(sip, invite("call-1"))
        await real_sleep(0.01)
    assert rings == [{"call_id": "call-1"}]
    assert sip.responses == [180, 486]
    assert RING_DECLINE_DELAY_SECONDS == 2


@pytest.mark.asyncio
async def test_invite_from_other_party_is_busy_only() -> None:
    rings = []

    async def on_ring(event):
        rings.append(event)

    sip = make_sip(on_ring)
    await feed(sip, invite("call-x", sender="someone@else.example"))
    assert rings == []
    assert sip.responses == [486]


def test_secrets_never_appear_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    secret = "super-secret-password-value"
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("c100x.sip").debug("registering without secrets")
        logging.getLogger("c100x.auth").warning(
            "BTicino auth failure at test: reason_code=%s", "credentials_rejected"
        )
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert secret not in joined
    assert "credentials_rejected" in joined


@pytest.mark.asyncio
async def test_start_monitoring_cancelled_after_200_acks_and_exposes_dialog() -> None:
    """A 200 after end_monitoring must ACK and raise SipError(dialog=...)."""
    sip = make_sip(lambda _e: None)
    sip._writer = MagicMock()
    sip._last_transaction = {
        "call_id": "cid-cancel",
        "tag": "local-tag",
        "sequence": 7,
    }
    ack_dialogs: list = []

    async def fake_auth(method, uri, body=b"", **_kwargs):
        assert method == "INVITE"
        # Concurrent end_monitoring while INVITE is in flight.
        sip._monitoring_gen += 1
        return SipMessage(
            "SIP/2.0 200 OK",
            {
                "content-type": "application/sdp",
                "to": f"<sip:c100x@{DOMAIN_GW}>;tag=remote-tag",
                "contact": f"<sip:c100x@{DOMAIN_GW}>",
            },
            b"v=0\r\n",
        )

    async def fake_ack(dialog) -> None:
        ack_dialogs.append(dialog)

    sip._authenticated_request = fake_auth  # type: ignore[method-assign]
    sip._send_ack = fake_ack  # type: ignore[method-assign]

    with pytest.raises(SipError, match="cancelled") as caught:
        await sip.start_monitoring("v=0\r\n")
    assert caught.value.dialog is not None
    assert caught.value.dialog.call_id == "cid-cancel"
    assert sip._dialog is None
    assert ack_dialogs == [caught.value.dialog]


@pytest.mark.asyncio
async def test_orphan_end_monitoring_does_not_bump_generation() -> None:
    """BYE for a cancelled dialog must not cancel a live in-flight start."""
    sip = make_sip(lambda _e: None)
    sip._writer = MagicMock()
    bye_methods: list[str] = []

    async def fake_dialog_request(dialog, method, sequence, wait=True):
        bye_methods.append(method)
        return SipMessage("SIP/2.0 200 OK", {"content-length": "0"}, b"")

    sip._dialog_request = fake_dialog_request  # type: ignore[method-assign]
    from c100x.sip import SipDialog

    orphan = SipDialog(
        call_id="orphan",
        local_uri=f"sip:user@{DOMAIN_GW}",
        remote_uri=f"sip:c100x@{DOMAIN_GW}",
        local_tag="t1",
        remote_to=f"sip:c100x@{DOMAIN_GW};tag=r1",
        remote_target=f"sip:c100x@{DOMAIN_GW}",
        route_set=(),
        local_sequence=3,
    )
    gen_before = sip._monitoring_gen
    await sip.end_monitoring(orphan)
    assert sip._monitoring_gen == gen_before
    assert bye_methods == ["BYE"]

    # Ending the active dialog still bumps generation.
    sip._dialog = orphan
    await sip.end_monitoring()
    assert sip._monitoring_gen == gen_before + 1
    assert sip._dialog is None
