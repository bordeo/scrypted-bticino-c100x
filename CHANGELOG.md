# Changelog

## 0.1.10 — unreleased

- Talkback / door audio diagnosis and hardening for HomeKit remote views:
  - Print each monitoring SIP answer SDP with `a=crypto` inline keys redacted, plus a one-line audio summary (`port` / `direction` / codec, or why audio was omitted).
  - `startIntercom` reports a clear reason when audio was not negotiated (port 0 / missing section / direction / codec) instead of a bare `Talkback requires an active negotiated call` traceback.
  - Align closer with ha-bticino-c100x: do not block RTSP `OPTIONS` on previous-call BYE; best-effort wait before `DESCRIBE` for negotiated codec, but always advertise RTSP audio (default PCMA) so a slow BYE/486 window cannot leave HomeKit without an audio track.
  - Construct `SrtpMediaSession` only after previous BYE (do not steal `sip.on_call_end`); claim/release the callback in `start()`/`end()`.
  - Serialize media teardown; `start()` waits for in-flight `end()` instead of clobbering its done event; abort 486 retries if the session was ended underneath.
- RTSP `461 Unsupported Transport` on UDP SETUP is intentional: only TCP interleaved is implemented; accepting UDP with a TCP transport reply breaks Scrypted's fallback. The two 461s before interleaved SETUP are expected.

## 0.1.9 — unreleased

- Retry a monitoring INVITE for up to 15 s (every 2 s) when the door answers `486 Busy Here`, instead of failing the HomeKit view at once. The door allows one monitoring call; Home Assistant keeps its call about 10 s after its last viewer leaves.

## 0.1.8 — unreleased

- Fix Casa/HomeKit video stopping after a provider settings save (for example ticking "Enable talkback"): the save reconnected SIP, but the reused RTSP server kept a media session bound to the closed SipClient, so every view failed with `SIP connection is not open`. Media sessions are now created per RTSP client from the SipClient current at connect time, and the RTSP server, talkback and media are dropped whenever SIP is torn down (settings save or supervisor reconnect).
- Toggling "Enable talkback" on the provider no longer tears down and re-registers SIP; it only refreshes the doorbell's Intercom interface.

## 0.1.7 — 2026-11-11

- Advertise the official-topology visible external unit (`deviceType`/`device` `eu`) module id as SDP `a=DEVADDR:` on monitoring INVITEs, matching ha-bticino-c100x 0.3.3 (`visible_external_units` / `buttonId` order). Passing the gateway id instead caused SIP `486 Busy Here` and a black player with no First RTP on live door units.
- Fail clearly when no official-app-visible external unit is present in the plant topology.

## 0.1.6 — 2026-11-11

- Include `SipError`, `MediaSessionError`, and `SdpError` messages in console summaries so a failed monitoring INVITE prints its SIP status (for example `486` or `488`) instead of only the exception type and stack frames.

## 0.1.5 — 2026-11-11

- Reject non-TCP RTSP SETUP with `461 Unsupported Transport` so Scrypted's client falls back from UDP to TCP interleaved. A 200 reply to UDP SETUP made Play start, then receive no RTP (`stream ended: readLength done`).
- Advertise `a=sendonly` on the RTSP SDP (the server sends media to the player).
- Buffer RTP until RTSP PLAY so the first keyframe is not dropped during the handshake.
- Do not block the RTSP handshake on the SIP monitoring INVITE, and print monitoring/RTSP events to the plugin console.



## 0.1.4 — 2026-11-11

- Serialize `getVideoStream`'s MediaStreamUrl as JSON bytes so Scrypted WebRTC can convert it to FFmpegInput. A Python dict was arriving as an RPC object, which made Play fail with `SyntaxError: "[object Object]" is not valid JSON`.


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
