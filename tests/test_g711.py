"""G.711 reference vector tests."""

from __future__ import annotations

from c100x.g711 import (
    REFERENCE_PCM_SAMPLES,
    alaw_to_linear,
    encode_pcma,
    encode_pcmu,
    linear_to_alaw,
    linear_to_ulaw,
    silence_frame,
    ulaw_to_linear,
)


def test_silence_frame_size() -> None:
    assert len(silence_frame("pcma")) == 160
    assert len(silence_frame("pcmu")) == 160


def test_roundtrip_ulaw_reference_samples() -> None:
    for sample in REFERENCE_PCM_SAMPLES:
        encoded = linear_to_ulaw(sample)
        decoded = ulaw_to_linear(encoded)
        # G.711 is lossy; quantization error grows with magnitude (~1.5%).
        tol = max(8, abs(sample) // 32)
        assert abs(decoded - sample) <= tol


def test_roundtrip_alaw_reference_samples() -> None:
    for sample in REFERENCE_PCM_SAMPLES:
        encoded = linear_to_alaw(sample)
        decoded = alaw_to_linear(encoded)
        tol = max(8, abs(sample) // 32)
        assert abs(decoded - sample) <= tol


def test_encode_buffers() -> None:
    pcm = (0).to_bytes(2, "little", signed=True) * 160
    assert len(encode_pcmu(pcm)) == 160
    assert len(encode_pcma(pcm)) == 160
    # Known: zero → μ-law 0xFF, A-law 0xD5
    assert encode_pcmu(pcm)[0] == 0xFF
    assert encode_pcma(pcm)[0] == 0xD5
