# Changelog

## 0.1.1 — 2026-11-10

- Load the Gateway picker when Installation is saved, not only when email or password changes.
- Refresh gateways if plants are already cached but the Gateway list is empty.
- Prefer modules with `device == "gateway"` (same rule as ha-bticino-c100x). Display choices as `Name (id)`.
- Log picker failures with error type and HTTP status only; print a console message when an installation has no modules.

## 0.1.0 — 2026-11-10

Initial Scrypted Python plugin for **BTicino Classe 100X** via the Legrand / Door Entry cloud.

### Features

- Azure B2C login with refresh tokens; Legrand plants/modules pickers
- Own SIP `clientId` (`clientName` **Scrypted**) + mTLS client certificate
- Persistent SIP TLS registration (v0.3.3 behaviour from ha-bticino-c100x): Expires 600, REGISTER every 180 s, CRLFCRLF keepalive, reconnect backoff 1/2/5/30 s
- Doorbell `BinarySensor` pulse (5 s debounce); INVITE → 180 then 486
- One-client RTSP server on `127.0.0.1` for HomeKit video; monitoring INVITE on connect; SDES-SRTP AES_CM_128_HMAC_SHA1_80; STUN priming; G.711 silence while viewing
- Optional talkback (`Intercom`) via FFmpegInput → G.711 → SRTP
- Cached keyframe JPEG for `takePicture` (never places a call for snapshots)
- Warns when the prebuffer/rebroadcast mixin is enabled

### Protocol

Ported from [`bordeo/ha-bticino-c100x`](https://github.com/bordeo/ha-bticino-c100x) @ `2cf3709` with all Home Assistant imports removed. No `aiortc` / PyAV dependency.
