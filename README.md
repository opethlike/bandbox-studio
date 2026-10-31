# BandBox Studio

An unofficial local control panel and MCP server for the **JBL BandBox Trio**, running on macOS. Shape guitar sounds in your browser, or let a local MCP client control presets, effects, mixer levels, and practice tools over Bluetooth.

**Experimental: tested with one BandBox Trio.** This project is independent of JBL and Harman. Some controls have been verified on hardware; others are implemented from protocol research and covered by offline tests. See [validation status](#validation-status).

## What it does

| Area | Controls |
| --- | --- |
| Guitar presets | List, load, create a copy, Save As, rename, and save user presets |
| Amp and effects | 44 models across GAT, COM, WAH, AMP, CAB, MOD, DLY, and RVB; model selection, parameters, and bypass |
| Mixer | Master and channel levels in the amp's raw `0..32` steps |
| Practice | Drums, metronome, tuner settings, and guarded looper controls |
| MCP | 21 named tools over local stdio, using the same controller as the GUI |

The browser interface uses local HTML, CSS, and JavaScript. The Mac talks directly to the amp through a Swift CoreBluetooth helper. JBL One, an Android phone, and a cloud account are not required at runtime.

```text
Local browser ── GUI server ─┐
                            ├── Shared controller ── Bluetooth ── BandBox Trio
Local MCP client ────────────┘
```

## Setup

You need **macOS**, **Python 3.11 or newer**, **Xcode Command Line Tools**, and a BandBox Trio. Other operating systems and BandBox models are unsupported. If the command line tools are missing, install them with `xcode-select --install` first.

```sh
git clone https://github.com/opethlike/bandbox-studio.git
cd bandbox-studio
python3 install.py
open "BandBox Studio.app"
```

The installer creates a Python environment, installs dependencies, compiles the native helper, and creates a local app launcher. It does not connect to the amp, change its settings, or register a login service. Keep the repository in a stable local folder: the launcher and generated MCP configuration reference its location.

This is a **source distribution**. The app is built locally and is not a signed or notarized downloadable release.

Allow Bluetooth access when macOS asks. Turn on your amp, then press **Read the amp** in the panel. If the amp cannot be found, press its **Bluetooth** button once, wait for the blue light to pulse, and read again.

The panel opens at **http://127.0.0.1:8765**. You can also launch it with:

```sh
.venv/bin/python gui_launch.py
```

See the [GUI guide](GUI-Guide.md) for editing, saving, and practice controls.

## Connect an MCP client

Setup generates **`.local/mcp-config.json`** with the correct absolute paths for this checkout. Copy its `jbl-bandbox` entry into your client's `mcpServers` configuration, preserving any other servers already there, then reload that client. Clients with another configuration format need the same executable, arguments, and environment values from that file.

This server uses **local stdio**. It works with a client that can start a process on the Mac. Publishing this repository does not make the amp accessible to cloud ChatGPT; no remote bridge or public tunnel is included.

Try requests such as:

- “List my BandBox presets and show the current saved amp settings.”
- “Create a new blues preset from this saved sound, load it, then show me the available amp models.”
- “Set the selected user preset's Drive to 40 and save it.”
- “Read the master volume and metronome tempo.”

The tools provide parameter ranges and control choices through `bandbox_effect_catalog` and `bandbox_list_controls`. Clients must use internal preset IDs returned by the tools, which can differ from the preset numbers on the amp's display.

## Editing behavior

Reading the amp or opening the page changes no settings. To edit a tone in the GUI, explicitly **Load saved sound** first. Parameter sliders require **Apply**; the Active/Bypassed switch takes effect immediately. **Save changes** commits applied edits to a user preset. Factory presets cannot be overwritten here.

The amp's detail query returns **saved settings**, which may differ from unsaved knob or JBL One changes. After editing elsewhere, preserve any sound you want to keep on the amp, then load a saved preset again to establish a known starting point. The GUI and MCP coordinate their own writes through shared state, but cannot detect every physical-knob change.

Uncertain writes are reported and never automatically retried. Firmware flashing, factory reset, preset deletion, loop discard, and arbitrary raw-write tools are not exposed. Existing-loop overdubbing is also unavailable. Saving a user preset intentionally overwrites that preset's previous saved sound.

## Validation status

Hardware validation has been performed on **one Trio, one Solo, and one Mac**, not a range of devices or firmware versions.

| Area | Evidence |
| --- | --- |
| BLE service discovery and reads | Device services, preset inventory, current selection, and saved details read successfully |
| BandBox Solo — every implemented operation confirmed over BLE | Preset select (channel `1`), the full looper set, effects (parameter, bypass, model change), channel-1 (guitar) mixer volume, and preset duplicate/rename/save all confirmed directly against hardware. See [docs/protocol.md#bandbox-solo](docs/protocol.md#bandbox-solo). Master volume gets no reply at all on the Solo. The real `server.py`/`Controls`/`DriveRoundTrip.swift`/Swift-probe path (`BANDBOX_GUITAR_CHANNEL=1`) is itself fully confirmed live too, via `check_mcp.py`'s `--live-*` flags: reading, selecting/duplicating/renaming/saving a preset, setting an effect parameter, setting a channel's mixer volume, the Drive round-trip, and (via `--live-looper`) a start/stop recording round trip with a verified non-zero recorded length. Nothing in this repo's Solo support remains purely inferred. |
| Drive | `41 → 40 → 41`, confirmed by device reports and the amp's display |
| User-preset workflow | New presets created, selected, programmed, saved, and read back; source factory preset preserved |
| Effects | Model changes, parameter changes, and active/bypass changes exercised while programming those presets; not every model tested |
| Master mixer | `15 → 14 → 15` raw steps, with matching reports and final readback |
| Drum tempo | `90 → 89 → 90` BPM, with matching reports and final readback |
| GUI | Live reads verified; editing, saving, creation, and practice-control flows browser-tested with a simulated device |
| Other paths | Save As, rename, remaining catalog models, tuner writes, channel mixer writes, and looper bar changes are not individually hardware-verified |

The test suite covers decoding, request validation, state guards, shared-state invalidation, and GUI HTTP boundaries. Offline tests and a matching device report do not establish audio quality or universal firmware compatibility. USB control is not implemented.

## Local files and discovery

Runtime state is stored in `~/Library/Application Support/BandBoxStudio/state`. It includes the discovered CoreBluetooth identifier and local diagnostics. Generated files, personal device configuration, logs, and build artifacts are ignored by Git.

| Variable | Purpose |
| --- | --- |
| `BANDBOX_RUNTIME_DIR` | Override the shared state folder. Use the same value for GUI and MCP. |
| `BANDBOX_DEVICE_UUID` | Optionally pin a Mac CoreBluetooth peripheral UUID. This is not a Bluetooth MAC address. |

Without a pin, the helper discovers the BandBox by its expected name and checks its control service. Ambiguous multiple-device discovery is refused. A pinned device is not silently replaced by another device if its connection fails.

For a read-only command-line check:

```sh
.venv/bin/python server.py current
```

Other read modes are `inspect`, `state`, and `presets`. Their output can include local device identifiers; redact it before posting an issue. The raw `state` value maps to battery in JBL One's code but has not been independently compared to the amp's battery display.

## Development

```sh
.venv/bin/python -m unittest discover -v
.venv/bin/python check_mcp.py
```

Both commands are offline; the MCP check performs a handshake and catalog checks without contacting the amp. See [CONTRIBUTING.md](CONTRIBUTING.md) for native tests, protocol changes, and useful bug reports. For connection help, see [the GUI guide](GUI-Guide.md#connection-problems).

Protocol details and source provenance are in [docs/protocol.md](docs/protocol.md) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Transport research was informed by [OpenJBL's protocol notes](https://github.com/NiceDayZc/openjbl/blob/e6111667eaab8ee5816345605ac212f8d7d14725/docs/PROTOCOL.md); BandBox-specific mappings were identified through JBL One 2.7.9 inspection and device observations.

Licensed under [MIT](LICENSE). See [SECURITY.md](SECURITY.md) for private vulnerability reporting.
