"""Scrypted BTicino Classe 100X plugin entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import scrypted_sdk
from scrypted_sdk.types import (
    BinarySensor,
    Camera,
    DeviceProvider,
    Intercom,
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
    CONF_EMAIL,
    CONF_ENABLE_TALKBACK,
    CONF_GATEWAY_ID,
    CONF_HOME_ID,
    CONF_PASSWORD,
    MEDIA_IDLE_SECONDS,
    RING_ACTIVE_SECONDS,
)
from c100x.models import SipAccount
from c100x.rtsp_server import RtspServer, RtspSession
from c100x.sdp import NegotiatedSession
from c100x.sip import SipClient, SipError
from c100x.srtp_media import SrtpMediaSession, new_client_id, next_reconnect_delay
from c100x.talkback import TalkbackError, TalkbackPipeline

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

PLACEHOLDER_JPEG = bytes(
    [
        0xFF,
        0xD8,
        0xFF,
        0xE0,
        0x00,
        0x10,
        0x4A,
        0x46,
        0x49,
        0x46,
        0x00,
        0x01,
        0x01,
        0x00,
        0x00,
        0x01,
        0x00,
        0x01,
        0x00,
        0x00,
        0xFF,
        0xDB,
        0x00,
        0x43,
        0x00,
        0x08,
        0x06,
        0x06,
        0x07,
        0x06,
        0x05,
        0x08,
        0x07,
        0x07,
        0x07,
        0x09,
        0x09,
        0x08,
        0x0A,
        0x0C,
        0x14,
        0x0D,
        0x0C,
        0x0B,
        0x0B,
        0x0C,
        0x19,
        0x12,
        0x13,
        0x0F,
        0x14,
        0x1D,
        0x1A,
        0x1F,
        0x1E,
        0x1D,
        0x1A,
        0x1C,
        0x1C,
        0x20,
        0x24,
        0x2E,
        0x27,
        0x20,
        0x22,
        0x2C,
        0x23,
        0x1C,
        0x1C,
        0x28,
        0x37,
        0x29,
        0x2C,
        0x30,
        0x31,
        0x34,
        0x34,
        0x34,
        0x1F,
        0x27,
        0x39,
        0x3D,
        0x38,
        0x32,
        0x3C,
        0x2E,
        0x33,
        0x34,
        0x32,
        0xFF,
        0xC0,
        0x00,
        0x0B,
        0x08,
        0x00,
        0x01,
        0x00,
        0x01,
        0x01,
        0x01,
        0x11,
        0x00,
        0xFF,
        0xC4,
        0x00,
        0x14,
        0x00,
        0x01,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x03,
        0xFF,
        0xC4,
        0x00,
        0x14,
        0x10,
        0x01,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0xFF,
        0xDA,
        0x00,
        0x08,
        0x01,
        0x01,
        0x00,
        0x00,
        0x3F,
        0x00,
        0x7F,
        0xFF,
        0xD9,
    ]
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
        self._ring_reset: asyncio.TimerHandle | None = None
        self._seen_call_ids: set[str] = set()
        self._prebuffer_warned = False

    # --- Settings ---------------------------------------------------------

    def _talkback_enabled(self) -> bool:
        return bool(_json_get(self.plugin.storage, CONF_ENABLE_TALKBACK, False))

    async def getSettings(self) -> list[Setting]:
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
        return []

    async def takePicture(self, options=None) -> MediaObject:
        """Return a cached keyframe JPEG. Never places a monitoring call."""
        self._warn_prebuffer()
        jpeg = self._last_jpeg or PLACEHOLDER_JPEG
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
        return [
            {
                "id": "default",
                "name": "Door Entry",
                "video": {"codec": "h264"},
                "audio": {"codec": "pcm_alaw", "sampleRate": 8000},
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
        return await scrypted_sdk.mediaManager.createMediaObject(
            media_stream_url,
            ScryptedMimeTypes.MediaStreamUrl.value,
            {"sourceId": self.id},
        )

    async def _start_rtsp_session(self) -> str:
        if self._rtsp:
            with contextlib.suppress(Exception):
                await self._rtsp.stop()
            self._rtsp = None
        if self._media:
            with contextlib.suppress(Exception):
                await self._media.end()
            self._media = None

        media = SrtpMediaSession(
            self.plugin.sip,
            on_plain_rtp=self._on_plain_rtp,
            on_keyframe=lambda nal: asyncio.create_task(self._cache_keyframe_jpeg(nal)),
        )
        self._media = media

        async def on_connect(session: RtspSession) -> None:
            # Place the monitoring INVITE as soon as the RTSP TCP client appears
            # so DESCRIBE/PLAY already have negotiated codecs / media flowing.
            device_address = self.plugin.gateway_id or ""
            await media.start(device_address)

        async def on_teardown(session: RtspSession) -> None:
            if self._talkback:
                with contextlib.suppress(Exception):
                    await self._talkback.stop()
            if self._media is media:
                with contextlib.suppress(Exception):
                    await media.end()
                self._media = None

        def sdp_builder(session: RtspSession) -> str:
            negotiated: NegotiatedSession | None = media.negotiated
            audio_pt = 8
            audio_name = "PCMA"
            if negotiated and negotiated.audio:
                codec = negotiated.audio.codec({"pcma", "pcmu"})
                if codec:
                    audio_pt = codec.payload_type
                    audio_name = codec.name.upper()
            return "\r\n".join(
                [
                    "v=0",
                    "o=- 0 0 IN IP4 127.0.0.1",
                    "s=BTicino C100X",
                    "c=IN IP4 127.0.0.1",
                    "t=0 0",
                    "m=video 0 RTP/AVP 96",
                    "a=rtpmap:96 H264/90000",
                    "a=fmtp:96 packetization-mode=1;profile-level-id=42e01f",
                    "a=control:trackID=0",
                    "a=recvonly",
                    f"m=audio 0 RTP/AVP {audio_pt}",
                    f"a=rtpmap:{audio_pt} {audio_name}/8000",
                    "a=control:trackID=1",
                    "a=recvonly",
                    "",
                ]
            )

        server = RtspServer(
            on_connect=on_connect,
            on_teardown=on_teardown,
            sdp_builder=sdp_builder,
            idle_seconds=MEDIA_IDLE_SECONDS,
        )
        self._rtsp = server
        url = await server.start()
        return url

    async def _on_plain_rtp(self, packet: bytes, kind: str) -> None:
        if self._rtsp:
            await self._rtsp.forward_rtp(packet, kind)

    # --- Intercom / talkback ----------------------------------------------

    async def startIntercom(self, media: MediaObject) -> None:
        if not self._talkback_enabled():
            raise TalkbackError("Talkback is disabled; enable it in device settings")
        if not self._media or not self._media.active:
            raise TalkbackError("Talkback requires an active viewing call")
        ffmpeg_input = await scrypted_sdk.mediaManager.convertMediaObjectToJSON(
            media, ScryptedMimeTypes.FFmpegInput.value
        )
        if not isinstance(ffmpeg_input, dict):
            raise TalkbackError("Expected FFmpegInput JSON for talkback")
        ffmpeg = await scrypted_sdk.mediaManager.getFFmpegPath()
        pipeline = TalkbackPipeline(self._media)
        await pipeline.start(ffmpeg, ffmpeg_input)
        self._talkback = pipeline

    async def stopIntercom(self) -> None:
        if self._talkback:
            await self._talkback.stop()
            self._talkback = None


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
        self.sip: SipClient | None = None
        self.gateway_id: str | None = None
        self.home_id: str | None = None
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
            self.print(f"Bootstrap failed: {type(err).__name__}")

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
        if nativeId != "doorbell":
            return None
        if self.device is None:
            self.device = C100XDoorbell(nativeId, self)
        return self.device

    async def releaseDevice(self, id: str, nativeId: str) -> None:
        if nativeId == "doorbell":
            self.device = None

    # --- Settings ---------------------------------------------------------

    async def getSettings(self) -> list[Setting]:
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
            value = value in (True, "true", "True", 1, "1")

        _json_set(self.storage, key, value)

        email = _json_get(self.storage, CONF_EMAIL)
        password = _json_get(self.storage, CONF_PASSWORD)
        if key in (CONF_EMAIL, CONF_PASSWORD) and email and password:
            with contextlib.suppress(Exception):
                await self._load_plants(str(email), str(password))

        home_id = _json_get(self.storage, CONF_HOME_ID)
        gateway_id = _json_get(self.storage, CONF_GATEWAY_ID)
        if email and password and home_id and gateway_id:
            try:
                await self._connect_stack(
                    str(email), str(password), str(home_id), str(gateway_id)
                )
            except Exception as err:
                self.print(f"Connect failed: {type(err).__name__}")

        await self.refreshDevices()
        await scrypted_sdk.deviceManager.onDeviceEvent(
            None, ScryptedInterface.Settings.value, None
        )

    async def _maybe_refresh_pickers(self) -> None:
        email = _json_get(self.storage, CONF_EMAIL)
        password = _json_get(self.storage, CONF_PASSWORD)
        if email and password and not self._plants_cache:
            with contextlib.suppress(Exception):
                await self._load_plants(str(email), str(password))

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
        modules = await api.modules(home_id)
        gateways = [
            item
            for item in modules
            if str(item.get("device", "")).upper().startswith("C100")
            or "gateway" in str(item.get("device", "")).casefold()
            or item.get("id")
        ]
        # Prefer modules that look like the VDE gateway; fall back to all modules.
        vde = [
            item
            for item in modules
            if "c100" in str(item.get("device", "")).casefold()
            or "vde" in str(item.get("type", "")).casefold()
            or "gateway" in str(item.get("name", "")).casefold()
        ]
        self._gateways_cache = vde or gateways or modules

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
                needs_cert = expires <= datetime.now(UTC) + timedelta(
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
                if expires > datetime.now(UTC) + timedelta(days=CERTIFICATE_RENEWAL_DAYS):
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
        if self.device and self.device._media:
            with contextlib.suppress(Exception):
                await self.device._media.end()
            self.device._media = None
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
        device = {
            "nativeId": "doorbell",
            "name": "BTicino C100X",
            "type": ScryptedDeviceType.Doorbell.value,
            "interfaces": interfaces,
            "info": {
                "manufacturer": "BTicino",
                "model": "Classe 100X",
            },
        }
        await scrypted_sdk.deviceManager.onDevicesChanged({"devices": [device]})
        # Ensure instance exists.
        await self.getDevice("doorbell")


def create_scrypted_plugin():
    return C100XPlugin()
