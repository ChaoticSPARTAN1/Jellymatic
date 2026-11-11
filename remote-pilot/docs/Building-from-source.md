# Building from source

[Home](Home.md) › Building from source

**On this page:** [Nothing to compile](#nothing-to-compile) · [Running the tests](#running-the-tests) ·
[Project layout](#project-layout) · [Adding a device](#adding-a-device)

## Nothing to compile

Remote Pilot is plain Python (standard library) and plain HTML, CSS and JavaScript. The source is
what runs: there is no build step and no bundled JavaScript library.

## Running the tests

From the Remote Pilot folder:

```text
py -3 -m unittest                 # Python tests: receiver, Chromatic protocol, USB/Wi-Fi, simulators
node test_chromatic_panel.js      # Chromatic panel behaviour
node test_privacy.js              # the privacy eye and masking
node test_tooltips.js             # every control explains itself
node test_pointer_controls.js     # mouse, touch and controller input
```

The JavaScript tests need [Node.js](https://nodejs.org/) 18 or newer and no packages.
The tests use only simulators and temporary folders; they never open a real serial port or reach
your network.

Optional: `test_chromatic_usb.py` can also build the Chromatic launcher's own PC harness with
`gcc` and test against it. It looks for a [Jellymatic](https://github.com/ChaoticSPARTAN1/Jellymatic)
checkout next to this folder, or in the folder named by the `CHROMATIC_LAUNCHER_SOURCE` environment
variable (its `chromatic/launcher` folder), and skips that part if there is none.

## Project layout

| Path | What it is |
| --- | --- |
| `receiver.py` | The local web server: viewer files, HTTP API, request queue, captures |
| `viewer.html`, `viewer.js`, `viewer.css`, `themes.css` | The device-neutral viewer page |
| `presets.py`, `plugin_runtime.py` | Preset manifests and the python-v1 adapter loader |
| `capture_api.py`, `touch_api.py`, `performance_capture.py` | Shared capture bundles, touch and timing settings |
| `chromatic/` | Everything Chromatic: CHR1 protocol, USB and Wi-Fi transports, simulators, CLI |
| `presets/chromatic/` | The Chromatic preset: manifest, adapter entry, panel, pixel-art theme, protocol docs |
| `presets/example/` | The local Example preset |
| `fonts/` | The two OFL pixel fonts and their licences |

## Adding a device

Make a new `presets/<id>/` folder with a `manifest.json` and, for a real link, an `adapter.py`.
[ADAPTER_API.md](../ADAPTER_API.md) describes the contract. Keep device names and art out of the
shared viewer files.
