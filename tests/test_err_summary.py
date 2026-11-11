"""_err_summary must include SIP status text without leaking auth errors."""

from __future__ import annotations

from c100x.auth import AuthenticationError
from c100x.sdp import SdpError
from c100x.sip import SipError
from c100x.srtp_media import MediaSessionError
from sdk_stub import install as install_sdk


def _raise(err: BaseException) -> BaseException:
    try:
        raise err
    except BaseException as caught:
        return caught


def test_err_summary_includes_sip_status() -> None:
    install_sdk()
    import main as plugin_main

    err = _raise(SipError("Monitoring call failed with SIP status 486"))
    summary = plugin_main._err_summary(err)
    assert "SipError Monitoring call failed with SIP status 486 at" in summary
    assert "test_err_summary.py" in summary


def test_err_summary_includes_media_and_sdp_errors() -> None:
    install_sdk()
    import main as plugin_main

    media = plugin_main._err_summary(
        _raise(MediaSessionError("STUN returned inconsistent public media addresses"))
    )
    assert "MediaSessionError STUN returned inconsistent public media addresses at" in media

    sdp = plugin_main._err_summary(
        _raise(SdpError("The external unit did not negotiate H.264 video"))
    )
    assert "SdpError The external unit did not negotiate H.264 video at" in sdp


def test_err_summary_omits_authentication_error_message() -> None:
    install_sdk()
    import main as plugin_main

    secret = "password=supersecret refresh_token=abcd"
    summary = plugin_main._err_summary(_raise(AuthenticationError(secret)))
    assert summary.startswith("AuthenticationError at")
    assert "supersecret" not in summary
    assert "refresh_token" not in summary
    assert "abcd" not in summary


def test_err_summary_still_includes_attribute_error() -> None:
    install_sdk()
    import main as plugin_main

    summary = plugin_main._err_summary(
        _raise(AttributeError("'NoneType' object has no attribute 'sdp'"))
    )
    assert "AttributeError 'NoneType' object has no attribute 'sdp' at" in summary
