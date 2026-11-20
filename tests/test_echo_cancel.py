"""SpeexDSP AEC ctypes wrapper and talkback low-latency helpers."""

from __future__ import annotations

import array
import math

import pytest

from c100x.door_audio_aec import apply_pcm_gain, upsample_s16le
from c100x.echo_cancel import EchoCanceller, speexdsp_available
from c100x.talkback import inject_low_latency_input_args, pcm_peak_abs


def _sine_frame(hz: float = 440.0, rate: int = 8000, ms: int = 20, amp: int = 8000) -> bytes:
    n = rate * ms // 1000
    samples = array.array("h")
    for i in range(n):
        samples.append(int(amp * math.sin(2 * math.pi * hz * i / rate)))
    return samples.tobytes()


def test_inject_low_latency_before_rtsp_i() -> None:
    args = ["-rtsp_transport", "tcp", "-i", "rtsp://127.0.0.1:9/x"]
    out = inject_low_latency_input_args(args)
    assert out.index("-probesize") < out.index("-i")
    assert out.index("-acodec") < out.index("-i")
    assert "libopus" in out
    assert "-fflags" in out


def test_inject_low_latency_skips_lavfi() -> None:
    args = ["-f", "lavfi", "-i", "sine=frequency=440:duration=0.2"]
    assert inject_low_latency_input_args(args) == args


def test_pcm_peak_abs() -> None:
    quiet = b"\x00\x00" * 160
    loud = _sine_frame()
    assert pcm_peak_abs(quiet) == 0
    assert pcm_peak_abs(loud) > 1000


def test_apply_pcm_gain_and_upsample() -> None:
    frame = _sine_frame(amp=1000)
    gained = apply_pcm_gain(frame, 12)
    assert pcm_peak_abs(gained) > pcm_peak_abs(frame)
    up = upsample_s16le(frame, factor=6)
    assert len(up) == len(frame) * 6


@pytest.mark.skipif(not speexdsp_available(), reason="libspeexdsp not installed")
def test_echo_canceller_reduces_coupled_sine() -> None:
    ec = EchoCanceller(sample_rate=8000, frame_ms=20, filter_ms=200)
    try:
        far = _sine_frame(hz=700, amp=12000)
        # Near = attenuated copy of far (simulated acoustic coupling) + noise.
        near_samples = array.array("h")
        far_samples = array.array("h")
        far_samples.frombytes(far)
        for s in far_samples:
            near_samples.append(int(s * 0.35))
        near = near_samples.tobytes()
        # Adapt on several frames.
        for _ in range(30):
            ec.playback(far)
            cleaned = ec.process(near)
        assert len(cleaned) == len(near)
        # After adaptation, residual peak should drop vs raw near.
        assert pcm_peak_abs(cleaned) < pcm_peak_abs(near)
    finally:
        ec.close()
