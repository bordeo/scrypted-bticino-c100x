# scrypted-bticino-c100x

Scrypted **Python** plugin for the **BTicino Classe 100X** video door entry
(Door Entry / Legrand Eliot cloud). Exposes a HomeKit video doorbell with ring
notification, listen audio, and optional two-way talkback.

Protocol code is ported from
[`bordeo/ha-bticino-c100x`](https://github.com/bordeo/ha-bticino-c100x)
(`2cf3709`) with every Home Assistant import removed. Community continuation of
the original [`alpichlabs/ha-bticino-c100x`](https://github.com/alpichlabs/ha-bticino-c100x)
work (upstream GitHub repo no longer available). Maintainer: [@bordeo](https://github.com/bordeo).

> **Not affiliated** with BTicino, Legrand, or Netatmo. Door Entry is a trademark
> of their respective owners. Use at your own risk.

**Licensing:** The licence of the original alpichlabs repository is unknown. No
`LICENSE` file is published here so as not to invent one. Treat reuse accordingly.

## Install

```bash
# From a release zip (recommended)
npx scrypted install ./scrypted-bticino-c100x-plugin.zip

# Or from the built dist after npm run build
npx scrypted install ./dist/plugin.zip
```

You can also install from the GitHub release asset attached to each tag.

## Setup

1. Install the plugin in Scrypted.
2. Open **BTicino C100X** settings: enter the same **email / password** as the
   Door Entry app.
3. Pick the **installation** (plant) and **gateway**.
4. Leave **Enable talkback** off unless you need HomeKit two-way audio (mic).
5. Enable the Scrypted **HomeKit** plugin and pair; the device type is
   **Doorbell** (`BinarySensor` for rings, `VideoCamera` + optional `Intercom`).

### Important

- **Disable the prebuffer / rebroadcast mixin** on this device. Continuous
  streaming holds SIP monitoring calls open and will disrupt the outdoor unit.
  Snapshots use a cached keyframe and never place a call.
- **HomeKit in Docker** needs host networking (or bridge + host Avahi + fixed
  ports) so mDNS / RTP work.
- The microphone / HomeKit return audio is **off** until you enable
  **Enable talkback**. That toggle adds the `Intercom` interface.

## Behaviour

| Feature | Notes |
| --- | --- |
| Ring | Incoming INVITE from `c100x@{gateway}` → `binaryState=true` for 5 s; SIP replies 180 then 486 |
| View | RTSP on `127.0.0.1`; monitoring INVITE with SDES-SRTP H.264 + G.711 |
| Talkback | FFmpegInput → s16le 8 kHz → G.711 → SRTP on the call's audio socket |
| Snapshot | Last keyframe JPEG (or placeholder); never starts a SIP call |

Each Scrypted install creates its **own** Legrand SIP `clientId` (`clientName`
`Scrypted`) and never reuses a Home Assistant client's credentials.

## Development

```bash
npm install
npm run build          # writes dist/plugin.zip (or out/plugin.zip)
pip install -r tests/requirements-test.txt
pytest tests/ -q
```

Offline tests only — no live calls to Legrand.

## Disclaimer

Unofficial community software. Not affiliated with, endorsed by, or supported by
BTicino, Legrand, Netatmo, or Apple.
