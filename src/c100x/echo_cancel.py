"""Optional SpeexDSP acoustic echo canceller (ctypes → libspeexdsp).

Alex hears his own voice on the phone because the outdoor unit speaker couples
into the door mic (no hardware AEC). When enabled we cancel downlink (near-end
door mic) using uplink PCM as the far-end reference.

Requires ``libspeexdsp.so.1`` on the host. If the library is missing, callers
see ``available=False`` and should leave the audio path unchanged.
"""

from __future__ import annotations

import ctypes
import logging
from collections import deque
from ctypes import POINTER, c_int, c_int16, c_void_p
from typing import Deque

_LOGGER = logging.getLogger(__name__)

SPEEX_ECHO_SET_SAMPLING_RATE = 24


def _load_lib():
    for name in ("libspeexdsp.so.1", "libspeexdsp.so", "speexdsp"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


_LIB = _load_lib()
if _LIB is not None:
    _LIB.speex_echo_state_init.argtypes = [c_int, c_int]
    _LIB.speex_echo_state_init.restype = c_void_p
    _LIB.speex_echo_state_destroy.argtypes = [c_void_p]
    _LIB.speex_echo_state_destroy.restype = None
    _LIB.speex_echo_ctl.argtypes = [c_void_p, c_int, c_void_p]
    _LIB.speex_echo_ctl.restype = c_int
    _LIB.speex_echo_cancellation.argtypes = [
        c_void_p,
        POINTER(c_int16),
        POINTER(c_int16),
        POINTER(c_int16),
    ]
    _LIB.speex_echo_cancellation.restype = None
    _LIB.speex_echo_playback.argtypes = [c_void_p, POINTER(c_int16)]
    _LIB.speex_echo_playback.restype = None
    _LIB.speex_echo_capture.argtypes = [c_void_p, POINTER(c_int16), POINTER(c_int16)]
    _LIB.speex_echo_capture.restype = None


def speexdsp_available() -> bool:
    return _LIB is not None


class EchoCanceller:
    """Frame-based Speex AEC with a short far-end (uplink) PCM queue."""

    def __init__(
        self,
        *,
        sample_rate: int = 8000,
        frame_ms: int = 20,
        filter_ms: int = 200,
        max_queued_frames: int = 50,
    ) -> None:
        if _LIB is None:
            raise RuntimeError("libspeexdsp is not available")
        self.sample_rate = int(sample_rate)
        self.frame_samples = max(1, self.sample_rate * int(frame_ms) // 1000)
        self.frame_bytes = self.frame_samples * 2
        filter_length = max(self.frame_samples, self.sample_rate * int(filter_ms) // 1000)
        self._state = _LIB.speex_echo_state_init(self.frame_samples, filter_length)
        if not self._state:
            raise RuntimeError("speex_echo_state_init failed")
        rate = c_int(self.sample_rate)
        _LIB.speex_echo_ctl(self._state, SPEEX_ECHO_SET_SAMPLING_RATE, ctypes.byref(rate))
        self._far_queue: Deque[bytes] = deque(maxlen=max_queued_frames)
        self._near_buf = bytearray()

    def close(self) -> None:
        state, self._state = self._state, None
        if state and _LIB is not None:
            _LIB.speex_echo_state_destroy(state)
        self._far_queue.clear()
        self._near_buf.clear()

    def playback(self, far_pcm: bytes) -> None:
        """Queue uplink (loudspeaker / far-end) PCM as the AEC reference."""
        if not far_pcm or self._state is None:
            return
        # Accept any multiple of frame size; pad short tails with zeros.
        data = far_pcm
        if len(data) % 2:
            data = data[:-1]
        offset = 0
        while offset + self.frame_bytes <= len(data):
            frame = data[offset : offset + self.frame_bytes]
            self._far_queue.append(frame)
            # Also notify Speex of playback timing.
            arr = (c_int16 * self.frame_samples).from_buffer_copy(frame)
            _LIB.speex_echo_playback(self._state, arr)
            offset += self.frame_bytes

    def process(self, near_pcm: bytes) -> bytes:
        """Cancel echo from downlink (near-end mic) PCM; returns same-length PCM."""
        if not near_pcm or self._state is None:
            return near_pcm
        if len(near_pcm) % 2:
            near_pcm = near_pcm[:-1]
        self._near_buf.extend(near_pcm)
        out = bytearray()
        silence = b"\x00" * self.frame_bytes
        while len(self._near_buf) >= self.frame_bytes:
            near = bytes(self._near_buf[: self.frame_bytes])
            del self._near_buf[: self.frame_bytes]
            far = self._far_queue.popleft() if self._far_queue else silence
            near_arr = (c_int16 * self.frame_samples).from_buffer_copy(near)
            far_arr = (c_int16 * self.frame_samples).from_buffer_copy(far)
            out_arr = (c_int16 * self.frame_samples)()
            _LIB.speex_echo_cancellation(self._state, near_arr, far_arr, out_arr)
            out.extend(ctypes.string_at(out_arr, self.frame_bytes))
        return bytes(out)
