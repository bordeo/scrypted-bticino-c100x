"""Pure-Python G.711 A-law / μ-law encode and decode (ITU-T G.711).

No FFmpeg / PyAV dependency — used for silence keepalive and talkback frames.
Algorithm follows the classic CCITT / Sun reference implementation.
"""

from __future__ import annotations

import struct

_ULAW_BIAS = 0x84
_ULAW_CLIP = 32635
_ALAW_CLIP = 32635

# exp_lut[(sample + BIAS) >> 7] for μ-law
_ULAW_EXP_LUT = (
    [0] * 2
    + [1] * 2
    + [2] * 4
    + [3] * 8
    + [4] * 16
    + [5] * 32
    + [6] * 64
    + [7] * 128
)


def linear_to_ulaw(sample: int) -> int:
    """Encode one signed 16-bit PCM sample to μ-law."""
    sample = max(-32768, min(32767, int(sample)))
    sign = (sample >> 8) & 0x80
    if sign:
        sample = -sample
    if sample > _ULAW_CLIP:
        sample = _ULAW_CLIP
    sample += _ULAW_BIAS
    exponent = _ULAW_EXP_LUT[(sample >> 7) & 0xFF]
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def ulaw_to_linear(byte: int) -> int:
    """Decode one μ-law byte to signed 16-bit PCM."""
    byte = (~int(byte)) & 0xFF
    sign = byte & 0x80
    exponent = (byte >> 4) & 0x07
    mantissa = byte & 0x0F
    sample = ((mantissa << 3) + _ULAW_BIAS) << exponent
    sample -= _ULAW_BIAS
    return -sample if sign else sample


def linear_to_alaw(sample: int) -> int:
    """Encode one signed 16-bit PCM sample to A-law."""
    sample = max(-32768, min(32767, int(sample)))
    sign = 0x80 if sample >= 0 else 0x00
    if sample < 0:
        sample = -sample
    if sample > _ALAW_CLIP:
        sample = _ALAW_CLIP
    if sample >= 256:
        exponent = 7
        for exp in range(1, 8):
            if sample < (0x100 << exp):
                exponent = exp
                break
        mantissa = (sample >> (exponent + 3)) & 0x0F
        encoded = sign | (exponent << 4) | mantissa
    else:
        encoded = sign | (sample >> 4)
    return encoded ^ 0x55


def alaw_to_linear(byte: int) -> int:
    """Decode one A-law byte to signed 16-bit PCM."""
    byte = int(byte) ^ 0x55
    t = (byte & 0x0F) << 4
    seg = (byte & 0x70) >> 4
    if seg:
        t = (t + 0x108) << (seg - 1)
    else:
        t += 8
    return t if (byte & 0x80) else -t


def _pcm_samples(pcm_s16le: bytes) -> tuple[int, ...]:
    """Unpack little-endian mono s16 PCM samples."""
    count = len(pcm_s16le) // 2
    return struct.unpack(f"<{count}h", pcm_s16le[: count * 2])


def encode_pcmu(pcm_s16le: bytes) -> bytes:
    """Encode little-endian mono s16 PCM to PCMU (μ-law)."""
    return bytes(linear_to_ulaw(sample) for sample in _pcm_samples(pcm_s16le))


def encode_pcma(pcm_s16le: bytes) -> bytes:
    """Encode little-endian mono s16 PCM to PCMA (A-law)."""
    return bytes(linear_to_alaw(sample) for sample in _pcm_samples(pcm_s16le))


def encode_g711(pcm_s16le: bytes, codec: str) -> bytes:
    """Encode PCM to the negotiated G.711 codec name (PCMA / PCMU)."""
    name = codec.casefold()
    if name == "pcma":
        return encode_pcma(pcm_s16le)
    if name == "pcmu":
        return encode_pcmu(pcm_s16le)
    raise ValueError(f"Unsupported G.711 codec: {codec}")


def silence_frame(codec: str, *, samples: int = 160) -> bytes:
    """Return one 20 ms (160 sample @ 8 kHz) G.711 silence frame."""
    pcm = bytes(samples * 2)  # zero s16le
    return encode_g711(pcm, codec)


# Selected PCM amplitudes for encode/decode smoke tests.
REFERENCE_PCM_SAMPLES = (
    0,
    8,
    16,
    32,
    64,
    128,
    256,
    512,
    1024,
    2048,
    4096,
    8192,
    16384,
    32511,
    -8,
    -16,
    -32,
    -64,
    -128,
    -256,
    -512,
    -1024,
    -2048,
    -4096,
    -8192,
    -16384,
    -32511,
)
