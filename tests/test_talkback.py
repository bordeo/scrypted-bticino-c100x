"""Talkback pipeline and Intercom gating tests (offline)."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from types import SimpleNamespace

import pytest

from c100x.g711 import encode_g711
from c100x.srtp_media import SrtpMediaSession, serialize_rtp
from c100x.talkback import TalkbackError, TalkbackPipeline


class FakeAudio:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.talkback = False
        self.timestamps: list[float] = []

    def set_talkback(self, enabled: bool) -> None:
        self.talkback = enabled

    def send_pcm(self, pcm: bytes) -> None:
        self.frames.append(pcm)
        self.timestamps.append(time.monotonic())


class FakeMedia:
    def __init__(self) -> None:
        self._active = True
        self.audio = FakeAudio()
        self.talkback_enabled = False

    @property
    def active(self) -> bool:
        return self._active

    def talkback_format(self):
        from c100x.audio_codec import TalkbackFormat

        return TalkbackFormat(8000)

    async def set_talkback(self, enabled: bool) -> None:
        self.talkback_enabled = enabled
        self.audio.set_talkback(enabled)

    def send_talkback_pcm(self, pcm: bytes) -> None:
        self.audio.send_pcm(pcm)


class GapFillAudio:
    """Minimal audio stub for keepalive gap-fill handoff tests."""

    def __init__(self) -> None:
        self.silence_force: list[bool] = []
        self._last_tx_mono = 0.0
        self._mic_queued_mono = 0.0
        self._speex_chain: asyncio.Task | None = None
        self._talkback = True
        self.tx_packets = 0

    @property
    def last_tx_mono(self) -> float:
        return self._last_tx_mono

    def tx_gap_seconds(self, now: float | None = None) -> float:
        if self._last_tx_mono <= 0.0:
            return 999.0
        return (time.monotonic() if now is None else now) - self._last_tx_mono

    def needs_uplink_gap_fill(
        self,
        *,
        gap_fill_after: float = 0.018,
        encode_budget: float = 0.1,
        now: float | None = None,
    ) -> bool:
        # Mirror _MediaRtpProtocol.needs_uplink_gap_fill for the keepalive test.
        now = time.monotonic() if now is None else now
        tx_gap = self.tx_gap_seconds(now)
        if tx_gap < gap_fill_after:
            return False
        if not self._talkback:
            return True
        mic_age = (
            now - self._mic_queued_mono if self._mic_queued_mono > 0.0 else 999.0
        )
        inflight = self._speex_chain is not None and not self._speex_chain.done()
        if tx_gap < encode_budget and (inflight or mic_age < gap_fill_after):
            return False
        return True

    def note_mic_queued(self) -> None:
        self._mic_queued_mono = time.monotonic()

    def note_mic_tx(self) -> None:
        self._mic_queued_mono = time.monotonic()
        self._last_tx_mono = time.monotonic()
        self.tx_packets += 1

    async def send_silence(self, *, force: bool = False) -> None:
        self.silence_force.append(force)
        self._last_tx_mono = time.monotonic()
        self.tx_packets += 1


@pytest.mark.asyncio
async def test_talkback_pipeline_ffmpeg_sine_produces_20ms_frames() -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not installed")
    media = FakeMedia()
    pipeline = TalkbackPipeline(media)  # type: ignore[arg-type]
    # 0.2 s of sine → ~10 frames of 20 ms
    ffmpeg_input = {
        "inputArguments": [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=8000:duration=0.2",
        ]
    }
    await pipeline.start(ffmpeg, ffmpeg_input)
    # Wait for pump to consume
    for _ in range(50):
        if len(media.audio.frames) >= 5:
            break
        await asyncio.sleep(0.05)
    await pipeline.stop()
    assert len(media.audio.frames) >= 5
    assert all(len(frame) == 320 for frame in media.audio.frames)
    # Timing: consecutive 20 ms frames should be roughly spaced (allow slack).
    if len(media.audio.timestamps) >= 3:
        deltas = [
            media.audio.timestamps[i + 1] - media.audio.timestamps[i]
            for i in range(len(media.audio.timestamps) - 1)
        ]
        assert min(deltas) >= 0.0
        assert sum(deltas) / len(deltas) < 0.2
    # Encoded payload size for PCMA of one frame is 160
    encoded = encode_g711(media.audio.frames[0], "pcma")
    assert len(encoded) == 160
    packet = serialize_rtp(
        payload_type=8,
        sequence=1,
        timestamp=0,
        ssrc=1,
        payload=encoded,
    )
    assert packet[1] & 0x7F == 8


@pytest.mark.asyncio
async def test_uplink_handoff_gap_fills_silence_until_mic_flows() -> None:
    """Talkback armed without mic frames must keep ~50 pps via forced silence.

    Regression for field 0.1.16: set_talkback(True) stopped silence before the
    HomeKit return-audio ffmpeg produced PCM, freezing tx_pkts for seconds.
    """
    session = SrtpMediaSession.__new__(SrtpMediaSession)
    session._ending = False
    session.talkback_enabled = True
    audio = GapFillAudio()
    session.audio = audio  # type: ignore[assignment]
    session.negotiated = SimpleNamespace(
        audio=SimpleNamespace(direction="sendrecv"),
    )

    task = asyncio.create_task(session._audio_keepalive())
    try:
        # Phase 1: talkback on, no mic — forced silence gap-fill.
        await asyncio.sleep(0.12)
        forced = [f for f in audio.silence_force if f]
        assert len(forced) >= 3, f"expected gap-fill silence, got {audio.silence_force!r}"
        before_mic = audio.tx_packets

        # Phase 2: mic frames arrive every 20 ms — silence should pause.
        audio.silence_force.clear()
        for _ in range(6):
            audio.note_mic_tx()
            await asyncio.sleep(0.02)
        # At most one race tick may sneak in; must not keep 50 pps silence.
        assert len(audio.silence_force) <= 2, (
            f"mic flowing but silence still fired: {audio.silence_force!r}"
        )
        assert audio.tx_packets > before_mic

        # Phase 3: Speex-like encode in flight with short TX lag — no inject.
        audio.silence_force.clear()

        async def _slow_encode() -> None:
            await asyncio.sleep(0.05)

        audio._speex_chain = asyncio.create_task(_slow_encode())
        audio.note_mic_queued()
        # Age TX enough for naive 18 ms check, but under encode_budget (100 ms).
        audio._last_tx_mono = time.monotonic() - 0.03
        await asyncio.sleep(0.04)
        assert not audio.silence_force, (
            f"in-flight Speex encode must not get silence injected: {audio.silence_force!r}"
        )
        await audio._speex_chain
        audio._speex_chain = None
        audio.note_mic_tx()

        # Phase 4: stuck Speex (TX > encode_budget) while PCM keeps arriving —
        # must force silence so tx_pkts cannot freeze again.
        audio.silence_force.clear()

        async def _stuck_encode() -> None:
            await asyncio.sleep(0.3)

        audio._speex_chain = asyncio.create_task(_stuck_encode())
        audio._last_tx_mono = time.monotonic() - 0.15
        for _ in range(4):
            audio.note_mic_queued()  # refreshing mic_age must not block fill
            await asyncio.sleep(0.02)
        assert any(audio.silence_force), (
            f"stuck Speex with fresh PCM must still gap-fill: {audio.silence_force!r}"
        )
        audio._speex_chain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await audio._speex_chain
        audio._speex_chain = None
        audio.note_mic_tx()

        # Phase 5: mic stalls — silence resumes (force=True).
        audio.silence_force.clear()
        await asyncio.sleep(0.12)
        assert any(audio.silence_force), "silence must resume when mic stalls"
        assert all(audio.silence_force), "gap-fill during talkback must force=True"
    finally:
        session._ending = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_needs_uplink_gap_fill_speex_inflight() -> None:
    """Unit: short TX lag with Speex in flight must not gap-fill; long stall must."""
    from c100x.srtp_media import _MediaRtpProtocol

    class _Inflight:
        def done(self) -> bool:
            return False

    class _Done:
        def done(self) -> bool:
            return True

    proto = _MediaRtpProtocol("audio", lambda *_: None)
    proto._talkback = True
    # Healthy Speex: TX lag 50 ms, encode still running, mic just queued.
    proto._last_tx_mono = time.monotonic() - 0.05
    proto._mic_queued_mono = time.monotonic()
    proto._speex_chain = _Inflight()  # type: ignore[assignment]
    assert proto.needs_uplink_gap_fill() is False
    # Stuck Speex: PCM keeps refreshing mic_age, but TX stalled > encode_budget.
    proto._last_tx_mono = time.monotonic() - 0.15
    proto._mic_queued_mono = time.monotonic()  # fresh accept must NOT block fill
    assert proto.needs_uplink_gap_fill() is True
    proto._speex_chain = _Done()  # type: ignore[assignment]
    proto._mic_queued_mono = time.monotonic() - 0.05
    assert proto.needs_uplink_gap_fill() is True
    proto._speex_chain = None
    assert proto.needs_uplink_gap_fill() is True


@pytest.mark.asyncio
async def test_talkback_refuses_without_active_call() -> None:
    media = FakeMedia()
    media._active = False
    pipeline = TalkbackPipeline(media)  # type: ignore[arg-type]
    with pytest.raises(TalkbackError):
        await pipeline.start("ffmpeg", {"url": "x"})


@pytest.mark.asyncio
async def test_device_talkback_disabled_raises_and_no_intercom(monkeypatch) -> None:
    """Device without Enable talkback must not expose Intercom; startIntercom raises."""
    from sdk_stub import install as install_sdk

    sdk, types_mod = install_sdk()
    import main as plugin_main

    storage = {}

    class FakeStorage:
        def getItem(self, key):
            return storage.get(key)

        def setItem(self, key, value):
            storage[key] = value

    plugin = plugin_main.C100XPlugin.__new__(plugin_main.C100XPlugin)
    plugin.storage = FakeStorage()
    plugin.sip = None
    plugin.gateway_id = "gw"
    plugin.device = None
    device = plugin_main.C100XDoorbell("doorbell", plugin)
    with pytest.raises(Exception) as exc:
        await device.startIntercom(object())
    assert "talkback" in str(exc.value).casefold()

    events = []

    async def capture(manifest):
        events.append(manifest)

    sdk.deviceManager.onDevicesChanged = capture
    plugin.storage.setItem("gateway_id", '"gw"')
    plugin.storage.setItem("enable_talkback", "false")
    await plugin.refreshDevices()
    assert events
    interfaces = events[0]["devices"][0]["interfaces"]
    assert types_mod.ScryptedInterface.Intercom.value not in interfaces
