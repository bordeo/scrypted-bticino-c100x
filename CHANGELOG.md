# Changelog

## 0.1.20 — unreleased

- Snapshots: landscape-only day/night placeholders (`placeholder_{day,night}.jpg` at 1280×720). Cover-crop to any HAP size; no portrait/`_wide` dual assets.
- Talkback latency: inject low-probe / low-delay / declared `libopus` flags before HomeKit RTSP `-i`; log `after_ms` to first uplink frame and first non-silence frame.
- Speex uplink quality: libspeex `-abr 0 -cbr_quality 8 -frames_per_packet 1` (~18 kbps NB), no extra resample on the PCM→Speex path.
- Optional **Echo cancellation** setting (off by default): SpeexDSP AEC on downlink using uplink PCM as reference (`libspeexdsp`); gain still applies. Falls back if the library is missing.

## 0.1.19 — local / npm

- 332e740 gap-fill TX-stall fix + landscape-only placeholders (published outside this agent tree).

## 0.1.18 — local / npm

- Speex-aware gap-fill (9ecb49f lineage) with landscape placeholders.

## 0.1.17 — unreleased

- Talkback uplink handoff: never stop silence keepalive when Intercom arms. Gap-fill canned Speex/Opus/G.711 silence when no mic is ready (~18 ms TX gap and no recent PCM / in-flight Speex encode), so TX stays alive while HomeKit return-audio ffmpeg connects or stalls (field: tx_pkts froze after `startIntercom` on 0.1.15/0.1.16) without injecting silence into healthy Speex talkback.
- Talkback pipeline: drain ffmpeg stderr (avoid pipe-full deadlock), timed stdout reads (catch `asyncio.TimeoutError` on Py3.10), RTSP-only low-delay flags; bound Speex PCM backlog.
- Version bump past published npm 0.1.16 (PR’s 0.1.16 never shipped from this tree).

## 0.1.16 — unreleased

- Snapshots: portrait + landscape day/night assets (`placeholder_{day,night}.jpg` and `placeholder_{day,night}_wide.jpg`). Pick the closest aspect to the HAP request, then cover-crop (never stretch) to the exact size (long side ≤1280). No size → wide 16:9 asset. `getPictureOptions` advertises 1280×720 so HomeKit’s Cameras tile does not stretch a portrait.

## 0.1.15 — local only

- Published from branch + local placeholder image swap (not a distinct commit on this PR).

## 0.1.14 — unreleased

- Door audio gain: plugin setting **Door audio gain (dB)** (default +12, range 0–24). Downlink Speex/Opus/G.711 is still SRTP→RTP into local RTSP; HomeKit’s remux has no plugin `volume=` hook, so when gain > 0 the plugin transcodes in-path (ffmpeg decode → `volume=NdB,alimiter` → Opus) and advertises Opus on the RTSP audio track. Gain 0 keeps RTP passthrough.
- HomeKit door opener: opt-in provider setting **Expose door opener to HomeKit** (default on) publishes each official-topology visible Lock as a Scrypted `Lock` device. `lock()` is a no-op (stays Locked); `unlock()` sends the same SIP `MESSAGE` / `lock.setStatus` apriporta as ha-bticino-c100x, logs a UTC timestamp, then auto-returns to Locked after 3 s.
- Snapshots: day/night placeholders (`placeholder_day.jpg` / `placeholder_night.jpg`, 576×1024) chosen by local sunrise/sunset for Peraga/Vigonza (45.42, 11.96, Europe/Rome; optional lat/lon settings). `takePicture` honors HAP requested size with center-crop cover + resize (long side ≤ 1280, JPEG q≈80); without a size returns the portrait asset as-is.

## 0.1.13 — local only

- Placeholder JPEG swap + version bump (published locally; not on this branch).

## 0.1.12 — unreleased

- Field 0.1.11 (Speex accepted): door opens downlink only after upstream audio. Send continuous 20 ms Speex/8000 silence (canned libspeex frame, SRTP) from call start; log RTP audio rx/tx counters every 5 s for the first 30 s.
- RTSP: accept UDP `client_port` SETUP (not only TCP interleaved); support `GET_PARAMETER` keepalives; idle timeout raised to 300 s and refreshed by RTP activity (fixes ~35 s `TimeoutError` teardown).
- Loud `startIntercom` / talkback logs (entry, ffmpeg cmd, first uplink frame, errors). Monitoring start logs whether Intercom is advertised (talkback setting).
- Casa tile: `takePicture` / `getPictureOptions` return a bundled 1280×720 “Citofono” JPEG placeholder when no recent keyframe is available (no SIP for snapshots).

## 0.1.11 — unreleased

- Monitoring INVITE audio offer matches the official Door Entry / linphone-sdk shape: `RTP/SAVP 96 97 98 8 0 101 100` with Opus (`96 opus/48000/2`, `useinbandfec=1`), Speex 16/8 kHz, PCMA/PCMU, telephone-event, `a=ptime:20`, `a=sendrecv`, plus session `a=DEVADDR:` and SDES `a=crypto` (video remains H.264 `recvonly`).
- `parse_answer` accepts Opus/Speex/G.711 and logs one redacted line with audio port, chosen codec/PT, and direction.
- Media path: Opus downlink is RTP passthrough to RTSP (`opus/48000/2`); talkback/silence uplink encoded with `opuslib`. Speex downlink passthrough; uplink via ffmpeg `libspeex` when available (clear log otherwise). G.711 unchanged.
- Root cause for field 0.1.9 talk failure: PCMA/PCMU-only offer was rejected (`m=audio 0`); Opus-first offer should negotiate usable audio. No extra SIP step for talk (local unmute), same as the official app.
- Bugbot: abort `start()` before INVITE when `_discarded` after STUN/sockets; failed-start BYEs only the dialog that start created (including ACK-after-200 failures) so a replacement HomeKit call is not hung up.
- Speex uplink uses Scrypted `getFFmpegPath()` (not PATH-only), serializes encodes with epoch stop, caches silence for keepalive near ptime, and closes ffmpeg on warm-up failure.

## 0.1.10 — unreleased

- Talkback / door audio diagnosis and hardening for HomeKit remote views:
  - Print each monitoring SIP answer SDP with `a=crypto` inline keys redacted, plus a one-line audio summary (`port` / `direction` / codec, or why audio was omitted).
  - `startIntercom` reports a clear reason when audio was not negotiated (port 0 / missing section / direction / codec) instead of a bare `Talkback requires an active negotiated call` traceback.
  - Align closer with ha-bticino-c100x: do not block RTSP `OPTIONS` on previous-call BYE; best-effort wait before `DESCRIBE` for negotiated codec, but always advertise RTSP audio (default PCMA) so a slow BYE/486 window cannot leave HomeKit without an audio track.
  - Construct `SrtpMediaSession` only after previous BYE (do not steal `sip.on_call_end`); claim/release the callback in `start()`/`end()`.
  - Serialize media teardown; `start()` waits for in-flight `end()` instead of clobbering its done event; sticky `_discarded` so a finished teardown during INVITE cannot configure closed sockets (486 retries still reuse the object).
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
