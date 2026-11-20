"""Scrypted BTicino Classe 100X plugin entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
import scrypted_sdk
from scrypted_sdk.types import (
    BinarySensor,
    Camera,
    DeviceProvider,
    Intercom,
    Lock,
    LockState,
    MediaObject,
    RequestMediaStreamOptions,
    ResponseMediaStreamOptions,
    ScryptedDeviceType,
    ScryptedInterface,
    ScryptedMimeTypes,
    ScryptedSystemDevice,
    Setting,
    Settings,
    VideoCamera,
)

from c100x.api import ApiError, LegrandApi, log_api_failure
from c100x.auth import AuthenticationError, C100XAuth, log_auth_failure
from c100x.certificate import certificate_expiry, provision_certificate
from c100x.const import (
    CERTIFICATE_RENEWAL_DAYS,
    CONF_DOOR_AUDIO_GAIN_DB,
    CONF_EMAIL,
    CONF_ENABLE_TALKBACK,
    CONF_EXPOSE_DOOR_OPENER,
    CONF_GATEWAY_ID,
    CONF_HOME_ID,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_PASSWORD,
    DEFAULT_DOOR_AUDIO_GAIN_DB,
    DOOR_AUDIO_GAIN_MAX_DB,
    DOOR_AUDIO_GAIN_MIN_DB,
    DOOR_RELOCK_SECONDS,
    MEDIA_IDLE_SECONDS,
    MONITOR_BUSY_RETRY_INTERVAL,
    MONITOR_BUSY_RETRY_SECONDS,
    RING_ACTIVE_SECONDS,
)
from c100x.downlink_gain import (
    DoorAudioGainTranscoder,
    clamp_door_audio_gain_db,
)
from c100x.models import SipAccount
from c100x.rtsp_server import RtspServer, RtspSession
from c100x.sdp import NegotiatedSession, SdpError, format_negotiated_audio, select_audio_codec
from c100x.sip import SipClient, SipError
from c100x.snapshot import (
    choose_placeholder_jpeg,
    parse_picture_size,
    resize_jpeg_cover,
)
from c100x.srtp_media import MediaSessionError, SrtpMediaSession, new_client_id, next_reconnect_delay
from c100x.sun import (
    DEFAULT_LATITUDE,
    DEFAULT_LONGITUDE,
    DEFAULT_TIMEZONE,
    clamp_latitude,
    clamp_longitude,
)
from c100x.talkback import TalkbackError, TalkbackPipeline
from c100x.topology import visible_external_units, visible_lock_modules

_LOGGER = logging.getLogger("scrypted-bticino-c100x")

# Never log these storage keys / setting values.
_SECRET_KEYS = frozenset(
    {
        CONF_PASSWORD,
        "access_token",
        "refresh_token",
        "certificate_pem",
        "private_key_pem",
        "ca_pem",
        "sip_password",
        "client_id",
    }
)

def _json_get(storage, key: str, default=None):
    try:
        raw = storage.getItem(key)
        if raw is None or raw == "":
            return default
        return json.loads(raw)
    except Exception:
        return default


def _json_set(storage, key: str, value: Any) -> None:
    storage.setItem(key, json.dumps(value))


class C100XDoorbell(
    scrypted_sdk.ScryptedDeviceBase,
    VideoCamera,
    Camera,
    BinarySensor,
    Settings,
    Intercom,
):
    """One Classe 100X outdoor unit exposed as a HomeKit video doorbell."""

    def __init__(self, nativeId: str, plugin: "C100XPlugin") -> None:
        super().__init__(nativeId=nativeId)
        self.plugin = plugin
        self.binaryState = False
        self._rtsp: RtspServer | None = None
        self._media: SrtpMediaSession | None = None
        self._talkback: TalkbackPipeline | None = None
        self._last_jpeg: bytes | None = None
        self._session_lock = asyncio.Lock()
        # Serialize talkback/media teardown so overlapping on_connect / on_teardown
        # cannot clear a newer session (Bugbot race on non-reentrant _end_media).
        self._media_lock = asyncio.Lock()
        self._ring_reset: asyncio.TimerHandle | None = None
        self._seen_call_ids: set[str] = set()
        self._prebuffer_warned = False
        self._logged_first_video = False
        self._logged_first_audio = False
        self._audio_gain: DoorAudioGainTranscoder | None = None

    # --- Settings ---------------------------------------------------------

    def _talkback_enabled(self) -> bool:
        try:
            return bool(_json_get(self.plugin.storage, CONF_ENABLE_TALKBACK, False))
        except Exception:
            return False

    def _door_audio_gain_db(self) -> int:
        # Read default from the const module at call time so tests can patch it.
        from c100x import const as _const

        return clamp_door_audio_gain_db(
            _json_get(
                self.plugin.storage,
                CONF_DOOR_AUDIO_GAIN_DB,
                _const.DEFAULT_DOOR_AUDIO_GAIN_DB,
            )
        )

    async def getSettings(self) -> list[Setting]:
        from c100x import const as _const

        settings: list[Setting] = [
            {
                "key": "info",
                "title": "Status",
                "readonly": True,
                "value": self._status_text(),
                "description": "SIP / media status for this doorbell.",
            },
            {
                "key": CONF_ENABLE_TALKBACK,
                "title": "Enable talkback",
                "type": "boolean",
                "value": self._talkback_enabled(),
                "description": (
                    "Off by default. When enabled, this device advertises Intercom so "
                    "HomeKit can send return audio. Requires an active viewing call."
                ),
            },
            {
                "key": CONF_DOOR_AUDIO_GAIN_DB,
                "title": "Door audio gain (dB)",
                "type": "number",
                "value": self._door_audio_gain_db(),
                "description": (
                    f"Boost door→HomeKit audio ({DOOR_AUDIO_GAIN_MIN_DB}–"
                    f"{DOOR_AUDIO_GAIN_MAX_DB} dB, default +{_const.DEFAULT_DOOR_AUDIO_GAIN_DB}). "
                    "Applied in-plugin (decode → volume+alimiter → Opus) because RTSP "
                    "passthrough cannot set ffmpeg volume= for HomeKit remux. "
                    "0 keeps Speex/Opus RTP passthrough. Takes effect on the next call."
                ),
            },
        ]
        if self._prebuffer_active():
            settings.append(
                {
                    "key": "prebuffer_warning",
                    "title": "Prebuffer / rebroadcast warning",
                    "readonly": True,
                    "value": "ENABLED — disable it",
                    "description": (
                        "The prebuffer/rebroadcast mixin keeps a continuous stream and "
                        "will place monitoring SIP calls constantly. Disable it on this "
                        "device. Snapshots use a cached keyframe, never a new call."
                    ),
                }
            )
        return settings

    async def putSetting(self, key: str, value: Any) -> None:
        if key == CONF_ENABLE_TALKBACK:
            enabled = value in (True, "true", "True", 1, "1")
            _json_set(self.plugin.storage, CONF_ENABLE_TALKBACK, enabled)
            await self.plugin.refreshDevices()
            await scrypted_sdk.deviceManager.onDeviceEvent(
                self.nativeId, ScryptedInterface.Settings.value, None
            )
            return
        if key == CONF_DOOR_AUDIO_GAIN_DB:
            gain = clamp_door_audio_gain_db(value)
            _json_set(self.plugin.storage, CONF_DOOR_AUDIO_GAIN_DB, gain)
            await scrypted_sdk.deviceManager.onDeviceEvent(
                self.nativeId, ScryptedInterface.Settings.value, None
            )
            return
        # Device settings are otherwise read-only / informational.

    def _status_text(self) -> str:
        parts = []
        if self.plugin.sip and self.plugin.sip.registered:
            parts.append("SIP registered")
        else:
            parts.append("SIP not registered")
        if self._media and self._media.active:
            parts.append("call active")
        if self._talkback_enabled():
            parts.append("talkback on")
        else:
            parts.append("talkback off")
        return "; ".join(parts)

    def _prebuffer_active(self) -> bool:
        try:
            interfaces = getattr(self, "interfaces", None) or []
            return any("prebuffer" in str(item).casefold() for item in interfaces)
        except Exception:
            return False

    def _warn_prebuffer(self) -> None:
        if self._prebuffer_active() and not self._prebuffer_warned:
            self._prebuffer_warned = True
            self.print(
                "WARNING: prebuffer/rebroadcast mixin is enabled on this device. "
                "Disable it — continuous streaming will hold SIP monitoring calls open."
            )

    # --- Doorbell binary sensor -------------------------------------------

    async def on_ring(self, event: dict[str, Any]) -> None:
        call_id = str(event.get("call_id", ""))
        if call_id and call_id in self._seen_call_ids:
            return
        if call_id:
            self._seen_call_ids.add(call_id)
            if len(self._seen_call_ids) > 100:
                self._seen_call_ids.pop()
        if self.binaryState:
            return  # debounce inside RING_ACTIVE_SECONDS window
        self.binaryState = True
        await self.onDeviceEvent(ScryptedInterface.BinarySensor.value, True)
        loop = asyncio.get_running_loop()
        if self._ring_reset:
            self._ring_reset.cancel()
        self._ring_reset = loop.call_later(
            RING_ACTIVE_SECONDS, lambda: asyncio.create_task(self._clear_ring())
        )

    async def _clear_ring(self) -> None:
        self._ring_reset = None
        self.binaryState = False
        with contextlib.suppress(Exception):
            await self.onDeviceEvent(ScryptedInterface.BinarySensor.value, False)

    # --- Camera / snapshots -----------------------------------------------

    async def getPictureOptions(self):
        # Native asset is 576×1024 portrait; HomeKit may request other sizes (canResize).
        return [
            {
                "id": "default",
                "name": "Citofono",
                "picture": {"width": 576, "height": 1024},
                "canResize": True,
            }
        ]

    def _snapshot_coords(self) -> tuple[float, float]:
        lat = _json_get(self.plugin.storage, CONF_LATITUDE, DEFAULT_LATITUDE)
        lon = _json_get(self.plugin.storage, CONF_LONGITUDE, DEFAULT_LONGITUDE)
        try:
            return clamp_latitude(float(lat)), clamp_longitude(float(lon))
        except (TypeError, ValueError):
            return DEFAULT_LATITUDE, DEFAULT_LONGITUDE

    async def takePicture(self, options=None) -> MediaObject:
        """Return a recent keyframe JPEG, else day/night placeholder.

        Never places a monitoring SIP call for snapshots. When HomeKit passes
        a requested size, center-crop (cover) and resize (long side ≤ 1280).
        """
        self._warn_prebuffer()
        if self._last_jpeg:
            jpeg = self._last_jpeg
        else:
            lat, lon = self._snapshot_coords()
            jpeg = choose_placeholder_jpeg(
                latitude=lat, longitude=lon, tz_name=DEFAULT_TIMEZONE
            )
        size = parse_picture_size(options)
        if size is not None:
            jpeg = resize_jpeg_cover(jpeg, size[0], size[1])
        return await scrypted_sdk.mediaManager.createMediaObject(jpeg, "image/jpeg")

    async def _cache_keyframe_jpeg(self, nal_payload: bytes) -> None:
        """Best-effort: decode one H.264 keyframe NAL to JPEG via Scrypted ffmpeg."""
        try:
            ffmpeg = await scrypted_sdk.mediaManager.getFFmpegPath()
        except Exception:
            return
        # Wrap single NAL with annex-B start code for ffmpeg.
        annexb = b"\x00\x00\x00\x01" + nal_payload
        try:
            process = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "h264",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert process.stdin and process.stdout
            process.stdin.write(annexb)
            await process.stdin.drain()
            process.stdin.close()
            out, _ = await asyncio.wait_for(process.communicate(), timeout=5)
            if out.startswith(b"\xff\xd8"):
                self._last_jpeg = out
        except Exception:
            return

    # --- Video stream -----------------------------------------------------

    async def getVideoStreamOptions(self) -> list[ResponseMediaStreamOptions]:
        # Prefer Opus (official Door Entry offer); Scrypted/HomeKit remux as needed.
        # When door-audio gain is active we re-encode Opus ourselves for RTSP.
        audio: dict[str, Any] = {"codec": "opus", "sampleRate": 48000}
        if self._audio_gain and self._audio_gain.active:
            audio = {"codec": "opus", "sampleRate": 48000}
        elif self._media and self._media.active:
            codec = self._media.selected_audio_codec()
            if codec is not None:
                name = codec.name.casefold()
                if name == "opus":
                    audio = {"codec": "opus", "sampleRate": codec.clock_rate}
                elif name == "speex":
                    audio = {"codec": "speex", "sampleRate": codec.clock_rate}
                elif name == "pcmu":
                    audio = {"codec": "pcm_mulaw", "sampleRate": 8000}
                else:
                    audio = {"codec": "pcm_alaw", "sampleRate": 8000}
        return [
            {
                "id": "default",
                "name": "Door Entry",
                "video": {"codec": "h264"},
                "audio": audio,
            }
        ]

    async def getVideoStream(
        self, options: RequestMediaStreamOptions = None
    ) -> MediaObject:
        self._warn_prebuffer()
        if not self.plugin.sip or not self.plugin.sip.registered:
            raise RuntimeError("SIP is not registered yet")

        async with self._session_lock:
            # One session at a time: reuse the listening RTSP URL when present.
            if self._rtsp and self._rtsp.port:
                url = self._rtsp.url
            else:
                url = await self._start_rtsp_session()

        media_stream_url = {
            "url": url,
            "mediaStreamOptions": (await self.getVideoStreamOptions())[0],
        }
        # JSON bytes, not a dict: a Python dict arrives in Node as an RPC
        # object, and WebRTC's MediaStreamUrl→FFmpegInput converter does
        # JSON.parse(data.toString()), which becomes "[object Object]".
        return await scrypted_sdk.mediaManager.createMediaObject(
            json.dumps(media_stream_url).encode("utf-8"),
            ScryptedMimeTypes.MediaStreamUrl.value,
            {"sourceId": self.id},
        )

    async def _start_rtsp_session(self) -> str:
        if self._rtsp:
            with contextlib.suppress(Exception):
                await self._rtsp.stop()
            self._rtsp = None
        await self._end_media()

        # One SrtpMediaSession per RTSP client, bound to the SipClient that is
        # current *at connect time*. The server can outlive a SIP reconnect
        # (settings save, supervisor), so it must never capture a SipClient.
        session_media: dict[int, SrtpMediaSession | None] = {}
        session_ready: dict[int, asyncio.Event] = {}

        async def on_connect(session: RtspSession) -> None:
            # Do not block OPTIONS on SIP BYE or INVITE. Scrypted's RtspClient
            # times out OPTIONS after 10s; awaiting talkback stop + BYE here
            # (Bugbot) exceeded that on HomeKit reconnect during a live view.
            # DEVADDR must be the visible external-unit module id (HA 0.3.3),
            # not the gateway id — wrong DEVADDR yields SIP 486 Busy Here.
            # Construct SrtpMediaSession only after previous BYE so we do not
            # steal sip.on_call_end from the session that still owns _dialog.

            sip = self.plugin.sip
            if sip is None or not sip.registered:
                self.print("Monitoring call failed: SIP is not registered yet")
                await session.close()
                return
            ready = asyncio.Event()
            session_media[id(session)] = None
            session_ready[id(session)] = ready
            self._logged_first_video = False
            self._logged_first_audio = False

            async def _start_monitoring() -> None:
                media: SrtpMediaSession | None = None
                try:
                    # Previous call teardown (BYE) happens here — not before OPTIONS.
                    async with self._media_lock:
                        await self._end_media_unlocked()
                        if session.closed:
                            return
                        ffmpeg_path: str | None = None
                        with contextlib.suppress(Exception):
                            ffmpeg_path = await scrypted_sdk.mediaManager.getFFmpegPath()
                        media = SrtpMediaSession(
                            sip,
                            on_plain_rtp=self._on_plain_rtp,
                            on_keyframe=lambda nal: asyncio.create_task(
                                self._cache_keyframe_jpeg(nal)
                            ),
                            ffmpeg_path=ffmpeg_path,
                            on_log=self.print,
                        )
                        session_media[id(session)] = media
                        self._media = media
                    talkback_on = self._talkback_enabled()
                    self.print(
                        "Starting monitoring call "
                        f"(Intercom {'advertised' if talkback_on else 'OFF — enable talkback in settings'})"
                    )
                    device_address = await self.plugin.monitoring_device_address()
                    deadline = asyncio.get_running_loop().time() + MONITOR_BUSY_RETRY_SECONDS
                    while True:
                        if session.closed or media is None:
                            return
                        try:
                            session_neg = await media.start(device_address)
                            break
                        except MediaSessionError as err:
                            # Ended underneath us (RTSP teardown during 486 retry).
                            if "was ended" in str(err).casefold() or session.closed:
                                return
                            raise
                        except SipError as err:
                            # 486: the door already has a monitoring call (for example
                            # Home Assistant's, which ends ~10 s after its last viewer).
                            busy = "SIP status 486" in str(err)
                            now = asyncio.get_running_loop().time()
                            if not busy or session.closed or now >= deadline:
                                raise
                            self.print("Door busy (486); retrying monitoring call")
                            await asyncio.sleep(MONITOR_BUSY_RETRY_INTERVAL)
                            if session.closed:
                                return
                    if session.closed:
                        await media.end()
                        return
                    redacted = getattr(media, "last_answer_redacted", None)
                    if redacted:
                        self.print("Monitoring SDP answer (redacted):\n" + redacted)
                    if session_neg.audio is None:
                        reason = (
                            getattr(media, "audio_reject_reason", None)
                            or "answer omitted usable audio"
                        )
                        self.print(f"Monitoring call started (video only; {reason})")
                    else:
                        self.print(
                            "Monitoring call started "
                            f"(audio {format_negotiated_audio(session_neg)})"
                        )
                        await self._start_audio_gain(session_neg, ffmpeg_path)
                except Exception as err:
                    self.print(f"Monitoring call failed: {_err_summary(err)}")
                    await session.close()
                finally:
                    ready.set()

            asyncio.create_task(_start_monitoring())

        async def on_describe_ready(session: RtspSession) -> None:
            ready = session_ready.get(id(session))
            if ready is None:
                return
            # Best-effort wait for INVITE so DESCRIBE can use the negotiated
            # codec. Audio is always advertised (default Opus) even on timeout
            # so a slow BYE/486 window cannot leave HomeKit without an audio track.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ready.wait(), timeout=12.0)

        async def on_teardown(session: RtspSession) -> None:
            session_ready.pop(id(session), None)
            media = session_media.pop(id(session), None)
            if media is None:
                return
            async with self._media_lock:
                if self._media is media:
                    await self._end_media_unlocked()
                else:
                    with contextlib.suppress(Exception):
                        await media.end()

        def sdp_builder(session: RtspSession) -> str:
            media = session_media.get(id(session))
            negotiated: NegotiatedSession | None = (
                media.negotiated if media is not None else None
            )
            # Default to Opus (official offer preference) before negotiation.
            audio_pt = 96
            audio_rtpmap = "opus/48000/2"
            audio_fmtp = "a=fmtp:96 useinbandfec=1"
            if self._audio_gain and self._audio_gain.active:
                # In-plugin gain path always emits Opus on the RTSP track.
                audio_pt = DoorAudioGainTranscoder.OUTPUT_PAYLOAD_TYPE
                audio_rtpmap = DoorAudioGainTranscoder.OUTPUT_RTPMAP
                audio_fmtp = f"a=fmtp:{audio_pt} useinbandfec=1"
            elif negotiated and negotiated.audio:
                codec = select_audio_codec(negotiated.audio)
                if codec is not None:
                    audio_pt = codec.payload_type
                    channels = f"/{codec.channels}" if codec.channels != 1 else ""
                    audio_rtpmap = f"{codec.name}/{codec.clock_rate}{channels}"
                    audio_fmtp = (
                        f"a=fmtp:{codec.payload_type} {codec.fmtp}"
                        if codec.fmtp
                        else ""
                    )
            lines = [
                "v=0",
                "o=- 0 0 IN IP4 127.0.0.1",
                "s=BTicino C100X",
                "c=IN IP4 127.0.0.1",
                "t=0 0",
                "m=video 0 RTP/AVP 96",
                "a=rtpmap:96 H264/90000",
                "a=fmtp:96 packetization-mode=1;profile-level-id=42e01f",
                "a=control:trackID=0",
                "a=sendonly",
                f"m=audio 0 RTP/AVP {audio_pt}",
                f"a=rtpmap:{audio_pt} {audio_rtpmap}",
            ]
            if audio_fmtp:
                lines.append(audio_fmtp)
            lines.extend(["a=control:trackID=1", "a=sendonly", ""])
            return "\r\n".join(lines)

        server = RtspServer(
            on_connect=on_connect,
            on_teardown=on_teardown,
            on_describe_ready=on_describe_ready,
            sdp_builder=sdp_builder,
            idle_seconds=MEDIA_IDLE_SECONDS,
            on_log=self.print,
        )
        self._rtsp = server
        url = await server.start()
        return url

    async def _end_media(self) -> None:
        """Stop talkback and end the current monitoring call, if any."""
        async with self._media_lock:
            await self._end_media_unlocked()

    async def _end_media_unlocked(self) -> None:
        """Caller must hold ``_media_lock``."""
        talkback, self._talkback = self._talkback, None
        if talkback:
            with contextlib.suppress(Exception):
                await talkback.stop()
        gain, self._audio_gain = self._audio_gain, None
        if gain:
            with contextlib.suppress(Exception):
                await gain.close()
        media, self._media = self._media, None
        if media:
            with contextlib.suppress(Exception):
                await media.end()

    async def _start_audio_gain(
        self, session_neg: NegotiatedSession, ffmpeg_path: str | None
    ) -> None:
        """Start Speex/Opus/G.711 → gain → Opus when the setting is > 0 dB."""
        gain_db = self._door_audio_gain_db()
        if gain_db <= 0 or session_neg.audio is None:
            return
        codec = select_audio_codec(session_neg.audio)
        if codec is None:
            return
        previous, self._audio_gain = self._audio_gain, None
        if previous:
            with contextlib.suppress(Exception):
                await previous.close()

        async def _forward_opus(packet: bytes) -> None:
            if self._rtsp:
                await self._rtsp.forward_rtp(packet, "audio")

        transcoder = DoorAudioGainTranscoder(
            gain_db=gain_db,
            on_opus_rtp=_forward_opus,
            ffmpeg_path=ffmpeg_path,
            on_log=self.print,
        )
        try:
            await transcoder.start(codec)
        except Exception as err:
            self.print(f"Door audio gain failed; falling back to passthrough: {type(err).__name__}")
            with contextlib.suppress(Exception):
                await transcoder.close()
            return
        if not transcoder.active:
            return
        # Teardown may have run while ffmpeg was starting — do not orphan it.
        if self._media is None or self._rtsp is None:
            with contextlib.suppress(Exception):
                await transcoder.close()
            return
        self._audio_gain = transcoder

    async def reset_media(self) -> None:
        """Drop every media object tied to the current SipClient.

        Called before the provider closes SIP. Stopping the RTSP server closes
        any connected client so the next view asks for a fresh stream URL.
        """
        rtsp, self._rtsp = self._rtsp, None
        if rtsp:
            with contextlib.suppress(Exception):
                await rtsp.stop()
        await self._end_media()

    async def _on_plain_rtp(self, packet: bytes, kind: str) -> None:
        if kind == "video" and not self._logged_first_video:
            self._logged_first_video = True
            self.print("First RTP packet: video")
        elif kind == "audio" and not self._logged_first_audio:
            self._logged_first_audio = True
            self.print("First RTP packet: audio")
        if kind == "audio" and self._audio_gain and self._audio_gain.active:
            await self._audio_gain.feed_rtp(packet)
            return
        if self._rtsp:
            await self._rtsp.forward_rtp(packet, kind)

    # --- Intercom / talkback ----------------------------------------------

    async def startIntercom(self, media: MediaObject) -> None:
        self.print("startIntercom: entry")
        if not self._talkback_enabled():
            self.print("startIntercom: refused — talkback disabled in settings")
            raise TalkbackError("Talkback is disabled; enable it in device settings")
        if not self._media or not self._media.active:
            self.print("startIntercom: refused — no active viewing call")
            raise TalkbackError("Talkback requires an active viewing call")
        block = self._media.talkback_block_reason()
        if block is not None:
            message = f"Talkback unavailable: {block}"
            self.print(f"startIntercom: refused — {message}")
            raise TalkbackError(message)
        codec = self._media.selected_audio_codec()
        self.print(
            "startIntercom: active call "
            f"(codec={codec.name if codec else '?'}/"
            f"{codec.clock_rate if codec else '?'})"
        )
        try:
            ffmpeg_input = await scrypted_sdk.mediaManager.convertMediaObjectToJSON(
                media, ScryptedMimeTypes.FFmpegInput.value
            )
        except Exception as err:
            self.print(f"startIntercom: FFmpegInput convert failed: {type(err).__name__}: {err}")
            raise
        if not isinstance(ffmpeg_input, dict):
            self.print("startIntercom: refused — expected FFmpegInput JSON")
            raise TalkbackError("Expected FFmpegInput JSON for talkback")
        ffmpeg = await scrypted_sdk.mediaManager.getFFmpegPath()
        pipeline = TalkbackPipeline(self._media, on_log=self.print)
        try:
            await pipeline.start(ffmpeg, ffmpeg_input)
        except MediaSessionError as err:
            self.print(f"startIntercom: MediaSessionError: {err}")
            raise TalkbackError(str(err)) from err
        except TalkbackError as err:
            self.print(f"startIntercom: TalkbackError: {err}")
            raise
        self._talkback = pipeline
        self.print("startIntercom: pipeline started")

    async def stopIntercom(self) -> None:
        self.print("stopIntercom: entry")
        if self._talkback:
            await self._talkback.stop()
            self._talkback = None
        else:
            self.print("stopIntercom: no active pipeline")


class C100XDoorLock(scrypted_sdk.ScryptedDeviceBase, Lock):
    """Momentary apriporta exposed as a HomeKit Lock (always returns to Locked)."""

    def __init__(self, nativeId: str, plugin: "C100XPlugin", lock_id: str, name: str) -> None:
        super().__init__(nativeId=nativeId)
        self.plugin = plugin
        self.lock_id = lock_id
        self.display_name = name
        self.lockState = LockState.Locked.value
        self._relock_task: asyncio.Task | None = None

    async def lock(self) -> None:
        """HomeKit lock — no-op; strike is momentary and state stays Locked."""
        self.lockState = LockState.Locked.value
        await scrypted_sdk.deviceManager.onDeviceEvent(
            self.nativeId, ScryptedInterface.Lock.value, self.lockState
        )

    async def unlock(self) -> None:
        """Release the door (SIP lock.setStatus) then auto-relock after a few seconds."""
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.print(f"Door unlock requested at {stamp} (lock_id={self.lock_id})")
        sip = self.plugin.sip
        gateway_id = self.plugin.gateway_id or _json_get(self.plugin.storage, CONF_GATEWAY_ID)
        if sip is None or not sip.registered or not gateway_id:
            self.print("Door unlock failed: SIP is not registered")
            raise RuntimeError("Door release is unavailable while SIP is disconnected")
        await sip.release_door(str(self.lock_id), str(gateway_id))
        self.lockState = LockState.Unlocked.value
        await scrypted_sdk.deviceManager.onDeviceEvent(
            self.nativeId, ScryptedInterface.Lock.value, self.lockState
        )
        if self._relock_task and not self._relock_task.done():
            self._relock_task.cancel()
        self._relock_task = asyncio.create_task(self._auto_relock())

    async def _auto_relock(self) -> None:
        try:
            await asyncio.sleep(DOOR_RELOCK_SECONDS)
            self.lockState = LockState.Locked.value
            await scrypted_sdk.deviceManager.onDeviceEvent(
                self.nativeId, ScryptedInterface.Lock.value, self.lockState
            )
            self.print(
                f"Door auto-relocked after {DOOR_RELOCK_SECONDS:.0f}s "
                f"(lock_id={self.lock_id})"
            )
        except asyncio.CancelledError:
            raise


class C100XPlugin(
    scrypted_sdk.ScryptedDeviceBase, DeviceProvider, Settings, ScryptedSystemDevice
):
    """Legrand cloud login + SIP registration + doorbell DeviceProvider."""

    def __init__(self, nativeId: str | None = None) -> None:
        super().__init__(nativeId=nativeId)
        self.systemDevice = {
            "settings": "BTicino C100X",
        }
        self.device: C100XDoorbell | None = None
        self.locks: dict[str, C100XDoorLock] = {}
        self.sip: SipClient | None = None
        self.gateway_id: str | None = None
        self.home_id: str | None = None
        # Official-topology visible EU module ids (buttonId order), like HA camera_ids.
        self.camera_ids: list[str] = []
        # Official-topology visible Lock module ids (buttonId order), like HA lock_ids.
        self.lock_ids: list[str] = []
        self._lock_names: dict[str, str] = {}
        self._session: aiohttp.ClientSession | None = None
        self._auth: C100XAuth | None = None
        self._api: LegrandApi | None = None
        self._supervise_task: asyncio.Task | None = None
        self._material_dir: Path | None = None
        self._plants_cache: list[dict] = []
        self._gateways_cache: list[dict] = []
        asyncio.ensure_future(self._bootstrap())

    async def _bootstrap(self) -> None:
        try:
            await self._ensure_session()
            email = _json_get(self.storage, CONF_EMAIL)
            password = _json_get(self.storage, CONF_PASSWORD)
            home_id = _json_get(self.storage, CONF_HOME_ID)
            gateway_id = _json_get(self.storage, CONF_GATEWAY_ID)
            if email and password and home_id and gateway_id:
                await self._connect_stack(str(email), str(password), str(home_id), str(gateway_id))
                await self.refreshDevices()
        except Exception as err:
            self.print(f"Bootstrap failed: {_err_summary(err)}")

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        self._session = aiohttp.ClientSession()
        return self._session

    def _files_dir(self) -> Path:
        if self._material_dir:
            return self._material_dir
        root = os.environ.get("SCRYPTED_PLUGIN_VOLUME") or tempfile.gettempdir()
        path = Path(root) / "files" / "c100x"
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._material_dir = path
        return path

    async def getDevice(self, nativeId: str) -> Any:
        if nativeId == "doorbell":
            if self.device is None:
                self.device = C100XDoorbell(nativeId, self)
            return self.device
        if nativeId.startswith("lock-"):
            lock_id = nativeId[len("lock-") :]
            existing = self.locks.get(nativeId)
            if existing is not None:
                return existing
            name = self._lock_names.get(lock_id) or "Apriporta"
            lock = C100XDoorLock(nativeId, self, lock_id, name)
            self.locks[nativeId] = lock
            return lock
        return None

    async def releaseDevice(self, id: str, nativeId: str) -> None:
        if nativeId == "doorbell":
            self.device = None
        elif nativeId.startswith("lock-"):
            self.locks.pop(nativeId, None)

    def _expose_door_opener(self) -> bool:
        return bool(_json_get(self.storage, CONF_EXPOSE_DOOR_OPENER, True))

    def _door_audio_gain_db(self) -> int:
        from c100x import const as _const

        return clamp_door_audio_gain_db(
            _json_get(
                self.storage,
                CONF_DOOR_AUDIO_GAIN_DB,
                _const.DEFAULT_DOOR_AUDIO_GAIN_DB,
            )
        )

    # --- Settings ---------------------------------------------------------

    async def getSettings(self) -> list[Setting]:
        from c100x import const as _const

        await self._maybe_refresh_pickers()
        plants = self._plants_cache
        gateways = self._gateways_cache
        plant_choices = [
            f"{item.get('name', 'Home')} ({item.get('id', '')})" for item in plants
        ]
        gateway_choices = [
            f"{item.get('name') or item.get('id')} ({item.get('id', '')})"
            for item in gateways
        ]
        home_id = _json_get(self.storage, CONF_HOME_ID) or ""
        gateway_id = _json_get(self.storage, CONF_GATEWAY_ID) or ""
        home_value = next(
            (c for c in plant_choices if f"({home_id})" in c), home_id or None
        )
        gateway_value = next(
            (c for c in gateway_choices if f"({gateway_id})" in c), gateway_id or None
        )
        return [
            {
                "key": CONF_EMAIL,
                "title": "Email",
                "description": "Door Entry / Legrand account email.",
                "value": _json_get(self.storage, CONF_EMAIL) or "",
            },
            {
                "key": CONF_PASSWORD,
                "title": "Password",
                "type": "password",
                "description": "Door Entry account password (never logged).",
                "value": _json_get(self.storage, CONF_PASSWORD) or "",
            },
            {
                "key": CONF_HOME_ID,
                "title": "Installation",
                "description": "Legrand plant / home. Save email+password first to load choices.",
                "choices": plant_choices or None,
                "value": home_value,
            },
            {
                "key": CONF_GATEWAY_ID,
                "title": "Gateway",
                "description": "Classe 100X gateway module.",
                "choices": gateway_choices or None,
                "value": gateway_value,
            },
            {
                "key": CONF_ENABLE_TALKBACK,
                "title": "Enable talkback",
                "type": "boolean",
                "value": bool(_json_get(self.storage, CONF_ENABLE_TALKBACK, False)),
                "description": "Advertise Intercom on the doorbell (HomeKit two-way audio).",
            },
            {
                "key": CONF_DOOR_AUDIO_GAIN_DB,
                "title": "Door audio gain (dB)",
                "type": "number",
                "value": self._door_audio_gain_db(),
                "description": (
                    f"Boost quiet door→phone audio ({DOOR_AUDIO_GAIN_MIN_DB}–"
                    f"{DOOR_AUDIO_GAIN_MAX_DB} dB, default +{_const.DEFAULT_DOOR_AUDIO_GAIN_DB}). "
                    "Transcodes Speex/Opus/G.711 through ffmpeg volume+alimiter to Opus "
                    "on the RTSP track when > 0. Applies on the next monitoring call."
                ),
            },
            {
                "key": CONF_EXPOSE_DOOR_OPENER,
                "title": "Expose door opener to HomeKit",
                "type": "boolean",
                "value": self._expose_door_opener(),
                "description": (
                    "Publish each official-topology door release as a Lock device "
                    "(unlock = apriporta). Off removes the Lock(s) from Scrypted/HomeKit."
                ),
            },
            {
                "key": CONF_LATITUDE,
                "title": "Snapshot latitude",
                "value": _json_get(self.storage, CONF_LATITUDE, DEFAULT_LATITUDE),
                "description": (
                    f"Used for day/night placeholder (local sunrise/sunset, no network). "
                    f"Default {DEFAULT_LATITUDE} (Peraga, Vigonza)."
                ),
            },
            {
                "key": CONF_LONGITUDE,
                "title": "Snapshot longitude",
                "value": _json_get(self.storage, CONF_LONGITUDE, DEFAULT_LONGITUDE),
                "description": (
                    f"Used with latitude for day/night placeholder. "
                    f"Default {DEFAULT_LONGITUDE} (Peraga, Vigonza, Europe/Rome)."
                ),
            },
        ]

    async def putSetting(self, key: str, value: Any) -> None:
        if key in _SECRET_KEYS or key == CONF_PASSWORD:
            # Store but never print the value.
            pass
        else:
            self.print(f"Setting updated: {key}")

        if key in (CONF_HOME_ID, CONF_GATEWAY_ID) and isinstance(value, str) and "(" in value:
            # Choices are "Name (id)" — extract id.
            value = value.rsplit("(", 1)[-1].rstrip(")").strip()

        if key == CONF_ENABLE_TALKBACK:
            # Only toggles the Intercom interface; never tear down SIP for it.
            _json_set(self.storage, key, value in (True, "true", "True", 1, "1"))
            await self.refreshDevices()
            await scrypted_sdk.deviceManager.onDeviceEvent(
                None, ScryptedInterface.Settings.value, None
            )
            return

        if key == CONF_EXPOSE_DOOR_OPENER:
            _json_set(self.storage, key, value in (True, "true", "True", 1, "1"))
            await self.refreshDevices()
            await scrypted_sdk.deviceManager.onDeviceEvent(
                None, ScryptedInterface.Settings.value, None
            )
            return

        if key == CONF_DOOR_AUDIO_GAIN_DB:
            _json_set(self.storage, key, clamp_door_audio_gain_db(value))
            await scrypted_sdk.deviceManager.onDeviceEvent(
                None, ScryptedInterface.Settings.value, None
            )
            return

        if key == CONF_LATITUDE:
            try:
                _json_set(self.storage, key, clamp_latitude(float(value)))
            except (TypeError, ValueError):
                _json_set(self.storage, key, DEFAULT_LATITUDE)
            await scrypted_sdk.deviceManager.onDeviceEvent(
                None, ScryptedInterface.Settings.value, None
            )
            return

        if key == CONF_LONGITUDE:
            try:
                _json_set(self.storage, key, clamp_longitude(float(value)))
            except (TypeError, ValueError):
                _json_set(self.storage, key, DEFAULT_LONGITUDE)
            await scrypted_sdk.deviceManager.onDeviceEvent(
                None, ScryptedInterface.Settings.value, None
            )
            return

        _json_set(self.storage, key, value)

        email = _json_get(self.storage, CONF_EMAIL)
        password = _json_get(self.storage, CONF_PASSWORD)
        if key in (CONF_EMAIL, CONF_PASSWORD) and email and password:
            try:
                await self._load_plants(str(email), str(password))
            except Exception as err:
                self._print_picker_failure(err, where="load_plants")

        if key == CONF_HOME_ID:
            self._gateways_cache = []
            home_id = _json_get(self.storage, CONF_HOME_ID)
            if home_id:
                try:
                    if self._api is not None:
                        await self._load_gateways(self._api, str(home_id))
                    elif email and password:
                        await self._load_plants(str(email), str(password))
                except Exception as err:
                    self._print_picker_failure(err, where="load_gateways")

        home_id = _json_get(self.storage, CONF_HOME_ID)
        gateway_id = _json_get(self.storage, CONF_GATEWAY_ID)
        if email and password and home_id and gateway_id:
            try:
                await self._connect_stack(
                    str(email), str(password), str(home_id), str(gateway_id)
                )
            except Exception as err:
                self.print(f"Connect failed: {_err_summary(err)}")

        await self.refreshDevices()
        await scrypted_sdk.deviceManager.onDeviceEvent(
            None, ScryptedInterface.Settings.value, None
        )

    def _print_picker_failure(self, err: Exception, *, where: str) -> None:
        # Type and HTTP status only — never tokens, passwords, or email.
        if isinstance(err, ApiError):
            self.print(f"{where} failed: {type(err).__name__} HTTP {err.status}")
        else:
            self.print(f"{where} failed: {type(err).__name__}")

    async def _maybe_refresh_pickers(self) -> None:
        email = _json_get(self.storage, CONF_EMAIL)
        password = _json_get(self.storage, CONF_PASSWORD)
        home_id = _json_get(self.storage, CONF_HOME_ID)
        try:
            if email and password and not self._plants_cache:
                await self._load_plants(str(email), str(password))
            elif home_id and not self._gateways_cache:
                if self._api is not None:
                    await self._load_gateways(self._api, str(home_id))
                elif email and password:
                    await self._load_plants(str(email), str(password))
        except Exception as err:
            self._print_picker_failure(err, where="refresh_pickers")

    async def _load_plants(self, email: str, password: str) -> None:
        session = await self._ensure_session()

        async def save_tokens(data: dict[str, Any]) -> None:
            tokens = _json_get(self.storage, "tokens", {}) or {}
            tokens.update(data)
            _json_set(self.storage, "tokens", tokens)

        auth = C100XAuth(session, email, password, save_tokens=save_tokens)
        tokens = _json_get(self.storage, "tokens") or {}
        if tokens:
            auth.restore(tokens)
        api = LegrandApi(session, auth)
        try:
            plants = await api.plants()
        except AuthenticationError as err:
            log_auth_failure(err, where="load_plants")
            raise
        except ApiError as err:
            log_api_failure(err, where="load_plants")
            raise
        self._plants_cache = plants
        home_id = _json_get(self.storage, CONF_HOME_ID)
        if home_id:
            await self._load_gateways(api, str(home_id))
        self._auth = auth
        self._api = api

    async def _load_gateways(self, api: LegrandApi, home_id: str) -> None:
        try:
            modules = await api.modules(home_id)
        except AuthenticationError as err:
            log_auth_failure(err, where="load_gateways")
            raise
        except ApiError as err:
            log_api_failure(err, where="load_gateways")
            raise
        if not modules:
            self.print("No modules for this installation; pick another one")
            self._gateways_cache = []
            return
        # HA config_flow selects the module whose device field is exactly "gateway".
        gateways = [item for item in modules if item.get("device") == "gateway"]
        if not gateways:
            # Loose fallback so a differently tagged unit is still offered.
            gateways = [
                item
                for item in modules
                if "gateway" in str(item.get("device", "")).casefold()
                or "c100" in str(item.get("device", "")).casefold()
                or "vde" in str(item.get("type", "")).casefold()
                or "gateway" in str(item.get("name", "")).casefold()
            ]
        self._gateways_cache = gateways or modules

    async def _refresh_camera_ids(self) -> None:
        """Load visible external-unit and lock ids from official topology."""
        if not self._api or not self.home_id:
            self.camera_ids = []
            self.lock_ids = []
            self._lock_names = {}
            return
        modules = await self._api.modules(self.home_id)
        self.camera_ids = [
            str(module["id"]) for module in visible_external_units(modules)
        ]
        locks = visible_lock_modules(modules)
        self.lock_ids = [str(module["id"]) for module in locks]
        self._lock_names = {
            str(module["id"]): str(module.get("name") or "Apriporta") for module in locks
        }

    async def monitoring_device_address(self) -> str:
        """Return the EU module id for SDP ``a=DEVADDR:`` on a monitoring INVITE.

        Matches ha-bticino-c100x 0.3.3: MediaSession.start(camera_id) where
        camera_ids come from ``visible_external_units`` (buttonId order).
        This plugin supports one unit — the first visible EU. Never use the
        gateway id here; that yields SIP 486 Busy Here on live door units.
        """
        if not self.camera_ids:
            await self._refresh_camera_ids()
        if not self.camera_ids:
            raise RuntimeError(
                "No official-app-visible external unit was found in the topology"
            )
        return self.camera_ids[0]

    async def _connect_stack(
        self, email: str, password: str, home_id: str, gateway_id: str
    ) -> None:
        await self._shutdown_sip()
        session = await self._ensure_session()

        async def save_tokens(data: dict[str, Any]) -> None:
            tokens = _json_get(self.storage, "tokens", {}) or {}
            tokens.update(data)
            _json_set(self.storage, "tokens", tokens)

        auth = C100XAuth(session, email, password, save_tokens=save_tokens)
        tokens = _json_get(self.storage, "tokens") or {}
        if tokens:
            auth.restore(tokens)
        api = LegrandApi(session, auth)
        self._auth = auth
        self._api = api
        self.home_id = home_id
        self.gateway_id = gateway_id
        await self._load_gateways(api, home_id)
        await self._refresh_camera_ids()

        account = await self._prepare_account_and_certificate(api, auth, home_id, gateway_id)
        material = self._files_dir()
        sip = SipClient(
            account,
            str(material / "client.crt"),
            str(material / "client.key"),
            str(material / "ca.crt"),
            self._on_ring,
        )
        await sip.connect()
        self.sip = sip
        self._supervise_task = asyncio.create_task(self._supervise(account))
        self.print("SIP registered")

    async def _prepare_account_and_certificate(
        self,
        api: LegrandApi,
        auth: C100XAuth,
        home_id: str,
        gateway_id: str,
    ) -> SipAccount:
        client_id = _json_get(self.storage, "client_id")
        if not client_id:
            client_id = new_client_id()
            _json_set(self.storage, "client_id", client_id)
        accounts = await api.sip_accounts(gateway_id)
        account = next((c for c in accounts if c.client_id == client_id), None)
        if account is None:
            account = await api.register_sip_account(gateway_id, str(client_id))

        material = self._files_dir()
        cert_path = material / "client.crt"
        key_path = material / "client.key"
        ca_path = material / "ca.crt"
        needs_cert = True
        if cert_path.is_file() and key_path.is_file() and ca_path.is_file():
            try:
                expires = certificate_expiry(cert_path.read_text())
                needs_cert = expires <= datetime.now(timezone.utc) + timedelta(
                    days=CERTIFICATE_RENEWAL_DAYS
                )
            except Exception:
                needs_cert = True
        # Also allow restoring PEMs from plugin storage.
        stored_cert = _json_get(self.storage, "certificate_pem")
        stored_key = _json_get(self.storage, "private_key_pem")
        stored_ca = _json_get(self.storage, "ca_pem")
        if needs_cert and stored_cert and stored_key and stored_ca:
            try:
                expires = certificate_expiry(str(stored_cert))
                if expires > datetime.now(timezone.utc) + timedelta(
                    days=CERTIFICATE_RENEWAL_DAYS
                ):
                    cert_path.write_text(str(stored_cert))
                    key_path.write_text(str(stored_key))
                    ca_path.write_text(str(stored_ca))
                    for path in (cert_path, key_path, ca_path):
                        path.chmod(0o600)
                    needs_cert = False
            except Exception:
                needs_cert = True

        if needs_cert:
            plant = await api.plant(home_id)
            bundle = await provision_certificate(
                api,
                plant=plant,
                gateway_id=gateway_id,
                client_id=str(client_id),
                user_oid=account.user_oid or await auth.user_oid(),
            )
            cert_path.write_text(bundle.certificate_pem)
            key_path.write_text(bundle.private_key_pem)
            ca_path.write_text(bundle.ca_pem)
            for path in (cert_path, key_path, ca_path):
                path.chmod(0o600)
            _json_set(self.storage, "certificate_pem", bundle.certificate_pem)
            _json_set(self.storage, "private_key_pem", bundle.private_key_pem)
            _json_set(self.storage, "ca_pem", bundle.ca_pem)
        return account

    async def _on_ring(self, event: dict[str, Any]) -> None:
        device = self.device or await self.getDevice("doorbell")
        if device:
            await device.on_ring(event)

    async def _supervise(self, account: SipAccount) -> None:
        while True:
            try:
                assert self.sip is not None
                await self.sip.wait_closed()
                raise SipError("SIP connection exited")
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.print(f"SIP connection lost: {type(err).__name__}")
            await self._shutdown_sip(keep_task=True)
            attempt = 0
            while True:
                delay = next_reconnect_delay(attempt)
                await asyncio.sleep(delay)
                try:
                    material = self._files_dir()
                    sip = SipClient(
                        account,
                        str(material / "client.crt"),
                        str(material / "client.key"),
                        str(material / "ca.crt"),
                        self._on_ring,
                    )
                    await sip.connect()
                    self.sip = sip
                    self.print("SIP re-registered")
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    self.print(
                        f"SIP reconnect failed: {type(err).__name__} "
                        f"(next backoff {next_reconnect_delay(attempt + 1):.0f}s)"
                    )
                    attempt += 1

    async def _shutdown_sip(self, *, keep_task: bool = False) -> None:
        if not keep_task and self._supervise_task:
            self._supervise_task.cancel()
            await asyncio.gather(self._supervise_task, return_exceptions=True)
            self._supervise_task = None
        if self.device:
            await self.device.reset_media()
        if self.sip:
            with contextlib.suppress(Exception):
                await self.sip.close()
            self.sip = None

    async def refreshDevices(self) -> None:
        gateway_id = _json_get(self.storage, CONF_GATEWAY_ID)
        if not gateway_id:
            await scrypted_sdk.deviceManager.onDevicesChanged({"devices": []})
            return
        talkback = bool(_json_get(self.storage, CONF_ENABLE_TALKBACK, False))
        interfaces = [
            ScryptedInterface.VideoCamera.value,
            ScryptedInterface.Camera.value,
            ScryptedInterface.BinarySensor.value,
            ScryptedInterface.Settings.value,
        ]
        if talkback:
            interfaces.append(ScryptedInterface.Intercom.value)
        devices: list[dict[str, Any]] = [
            {
                "nativeId": "doorbell",
                "name": "BTicino C100X",
                "type": ScryptedDeviceType.Doorbell.value,
                "interfaces": interfaces,
                "info": {
                    "manufacturer": "BTicino",
                    "model": "Classe 100X",
                },
            }
        ]
        lock_ids = getattr(self, "lock_ids", None) or []
        lock_names = getattr(self, "_lock_names", None) or {}
        locks = getattr(self, "locks", None)
        if locks is None:
            self.locks = {}
            locks = self.locks
        if self._expose_door_opener():
            if not lock_ids and getattr(self, "_api", None) and getattr(self, "home_id", None):
                with contextlib.suppress(Exception):
                    await self._refresh_camera_ids()
                lock_ids = getattr(self, "lock_ids", None) or []
                lock_names = getattr(self, "_lock_names", None) or {}
            for lock_id in lock_ids:
                name = lock_names.get(lock_id) or "Apriporta"
                devices.append(
                    {
                        "nativeId": f"lock-{lock_id}",
                        "name": name,
                        "type": ScryptedDeviceType.Lock.value,
                        "interfaces": [ScryptedInterface.Lock.value],
                        "info": {
                            "manufacturer": "BTicino",
                            "model": "Classe 100X Door",
                        },
                    }
                )
        await scrypted_sdk.deviceManager.onDevicesChanged({"devices": devices})
        # Ensure instances exist for advertised devices.
        await self.getDevice("doorbell")
        if self._expose_door_opener():
            for lock_id in lock_ids:
                await self.getDevice(f"lock-{lock_id}")
        else:
            locks.clear()



def _err_summary(err: BaseException) -> str:
    """Type, safe protocol message and frame locations; never args of HTTP/auth errors."""
    import traceback as _tb
    frames = _tb.extract_tb(err.__traceback__)[-4:]
    where = " <- ".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in reversed(frames))
    msg = ""
    if isinstance(
        err,
        (
            AttributeError,
            TypeError,
            NameError,
            KeyError,
            IndexError,
            SipError,
            MediaSessionError,
            SdpError,
        ),
    ):
        msg = f" {str(err)[:160]}"
    return f"{type(err).__name__}{msg} at {where}"


def create_scrypted_plugin():
    return C100XPlugin()
