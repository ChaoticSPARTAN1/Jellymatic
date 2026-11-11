# Adapter API and preset contract

The viewer is device neutral. Presets supply the name, colours, device drawing
and capability flags; device transports (adapters) own the wire protocol,
pairing and screen conversion. A third party can add a device without putting
its name, brand or art in the shared viewer files (`viewer.html`, `viewer.js`,
`viewer.css`, `themes.css`).

## Preset manifest

Each `presets/<id>/manifest.json` contains:

| Field | Use |
| --- | --- |
| `id`, `name`, `order`, `mark` | Stable selector ID (folder name), visible name, menu order, one-to-three letter badge. |
| `transport` | `python-v1` for a local adapter (`adapter.py` beside the manifest); any other key (for example `example-offline`) means no device transport. |
| `models` | Device drawings: `basic`, `dual-stick` (the generic dual-screen gamepad drawing) or a preset's own model id. |
| `capabilities` | Viewer modules: `screenshot`, `stream`, `audio`, `controller`, `console`, `input`, `report`, `log`, `perf`, `touch`, and `chromatic` for the Chromatic extension. |
| `theme` | Nine six-digit hex colours: `background`, `surface`, `surface2`, `line`, `text`, `muted`, `accent`, `accent2`, `glow`. |
| `styleSheet` | Optional, exactly `/presets/<id>/theme.css`. It must stay inside its preset folder and never load external URLs. |
| `panelScript` | Optional, exactly `/presets/<id>/panel.js` (the file must exist). It acts only while `body[data-preset]` names its preset. |
| `analogInput`, `inputButtons`, `controllerBasicFull`, `remoteReport`, `remoteLog`, `manualInputRequiresArm`, `maxLiveAudioBytes` | Optional control and request settings. |

The receiver validates IDs, paths and theme values before exposing manifests. A
preset with an invalid manifest is skipped with one warning instead of stopping
the receiver.

## Add a local transport (python-v1)

Set `transport` to `python-v1` and place an `adapter.py` beside the preset's
`manifest.json`. The receiver loads only that exact file from a validated
preset directory. **The adapter is executable Python: install only code you
trust.** It may import sibling helper modules with relative imports. Its factory
receives a private `local/<preset-id>/` folder and the validated manifest:

```python
def create_adapter(local_dir, manifest):
    return MyAdapter(local_dir, manifest)

class MyAdapter:
    def status(self):
        # Called by a one-second background poll, never by an HTTP worker.
        return {
            "connected": False, "session": False, "host": "",
            "discovery": "waiting for paired device", "meta": None, "perf": None,
        }

    def request(self, kind, payload):
        # A bounded worker calls this after the receiver checks the selected
        # preset, live session, link switch and capability flag.
        raise NotImplementedError
```

Optional methods: `start()`, `stop()`, `connect()` (called by **Connect to
device**; return `{"message": "..."}` promptly), `disconnect()`, `retry()`,
`configure(device_host, port)` (for `receiver.py setup-preset <id>`),
`set_network_host(host)` and `set_active(callback)`.

`status()` must report cached state promptly. `connected` and `session` are
booleans; `host` and `discovery` are short strings; `meta` and `perf` are
optional JSON objects (together at most 8 KB). `perf` uses finite nonnegative
`fps`, `frameMs`, `workMs` and an advancing `timestampMs`. An adapter may put a
short `meta.nextStep` string in `status()`; the connection badge then shows it.
**Keep pairing keys and other secrets out of every status field.** A failing
adapter is shown as disconnected and leaves the viewer running.

`request(kind, payload)` kinds: `console`, `input`, `gamepad`, `touch`, `shot`,
`stream`, `report`, `log`, `perf`.

- `console`: printable ASCII, up to 127 characters. Return text of at most 4 KB.
- `input`: `<hex button mask> <frames> <moveX> <moveY> <lookX> <lookY>`.
- `gamepad`: `buttons=0001,lx=250,ly=-500,rx=0,ry=0,lt=0,rt=700,frames=8`
  (Gamepad API bits 0 to 15, signed thousandths, triggers 0 to 1000). Newer
  snapshots replace older pending ones; the receiver never retries one.
- `touch`: `x=123,y=90,down=1` (0..319 by 0..239); release with `down=0`.
- `shot`: an uncompressed 400×480 24-bit BMP (two stacked screens).
- `stream`: an uncompressed 200×240 24-bit BMP, or `(bmp, pcm)` with up to
  16 KB of mono signed 16-bit little-endian audio at 16,364 Hz when the
  manifest declares `audio`.
- `report`, `log`: UTF-8 text of at most 64 KB; the receiver saves it in
  `local/captures/`.

The adapter owns authentication, bounded lengths, timeouts and safe short input
holds. A timed-out gameplay action must never be retried silently, and remote
input must release itself after a short time even if the browser or network
goes away.

## Local HTTP API

The viewer is bound to `127.0.0.1` by default. Every state-changing call is a
`POST` with `Content-Type: application/json`, the header
`X-Remote-Pilot-Viewer: 1` and a body of at most 512 bytes. Requests whose
`Host` is not a numeric address with the viewer's port, or whose `Origin` is a
different site, are refused (see [docs/Security-model.md](docs/Security-model.md)).

| Endpoint | Request or result |
| --- | --- |
| `GET /api/state` | Selected preset, connection/session state, recent requests, capture bundles, optional device metadata and timing. |
| `GET /api/presets` | Ordered validated manifests and the selected ID. |
| `POST /api/preset` | `{"id": "preset-id"}`; remembers the selection in `local/`. |
| `POST /api/theme` | `{"id": "preset-id", "colors": {"accent": "#3388cc"}}`; an empty object resets. |
| `POST /api/link` | `{"connected": true or false}`; the PC-side command gate. |
| `POST /api/retry` | Ask the adapter for a fresh device search. |
| `POST /api/shot`, `/api/stream`, `/api/capture` | Saved capture, disposable live frame, or screen + log bundle. |
| `POST /api/console`, `/api/input`, `/api/gamepad`, `/api/touch` | Bounded control requests, when the preset declares them. |
| `POST /api/report`, `/api/log`, `/api/perf`, `/api/performance` | Diagnostics; `performance` turns timing capture on or off per preset. |
| `GET /captures/<file>` | A saved BMP, TXT or JSON from `local/captures/` (bare file names only). |
| `GET /latest.bmp`, `/live.bmp`, `/live-audio.pcm?since=<n>` | Latest capture, live frame, live audio. |

## The Chromatic extension (`chromatic`)

The `chromatic` preset (capability `chromatic`, model `chromatic`) refuses the
shared dual-screen actions above and adds its own routes, dispatched by
`chromatic/api.py` to the adapter's `chromatic_request(action, object,
local_client)`:

- `GET /api/chromatic/status`, `GET /api/chromatic/live.bmp?n=<k>`,
  `GET /api/chromatic/live.pcm?n=<k>`;
- `POST /api/chromatic/<action>` for `status`, `mode`, `retry`, `log`,
  `clear-log`, `shot`, `bundle`, `report`, `collect`, `console`, `arm`, `input`,
  `hold`, `pair-capture`, `pair-usb`, `ping`, `perf`, `connect`, `allow-usb`,
  `manager`, `manager-phrase`, `manager-run`, `manager-job`, `usb-release`,
  `usb-return`, `usb-hold`.

POSTs keep the shared header, origin, LAN-cookie and 512-byte checks. Device
actions require the Chromatic preset to be selected and the PC link to be on.
`allow-usb`, `mode`, `pair-capture`, `pair-usb`, `manager-run`, `manager-job`,
`collect` of `manager-files` or `manager-job`, `usb-release` and `usb-return`
additionally require a request from this PC (127.0.0.1); the LAN viewer can
never use them, and its `status` carries no Manager job output. `manager-run`
also needs `"managerRunAllowed": true` in `local/chromatic/config.json`. `shot`
answers with the bare file name (served from `/captures/<file>`). The wire
protocol is in [presets/chromatic/PROTOCOL.md](presets/chromatic/PROTOCOL.md).
