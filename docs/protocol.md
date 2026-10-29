# Protocol notes

These notes describe what this implementation understands about the JBL BandBox Trio. They are an interoperability reference, not an official specification. Hardware observations come from one Trio; other models and firmware may differ. See [validation status](../README.md#validation-status) and [sources](../THIRD_PARTY_NOTICES.md).

## BandBox Solo

A **JBL BandBox Solo** was independently found to answer on the same GATT
service (`65786365-…-0000`), and preset selection (feature `0x3442`) was
confirmed on real hardware using guitar channel `1` instead of the Trio's
channel `3` (the Solo has a single instrument input, no mic/vocal channel).
`preset_controls.py`'s `_channel` already accepts `1` or `3`, and
`Controls`, `server.py`, and `DriveRoundTrip.swift` now take this channel
as configuration (`BANDBOX_GUITAR_CHANNEL`, default `3`) instead of
hardcoding it, so existing Trio setups are unaffected unless that variable
is set.

### Discovery: the Solo can advertise with no name at all

`BandBoxProbe.swift` recognizes both `"JBL BandBox Trio"` and
`"JBL BandBox Solo"` by name. But the Solo has also been observed
advertising with **no name and no manufacturer data** -- its Bluetooth
button may need a fresh press to make it discoverable at all, and even
then the name doesn't always come through on the first advertisement.
A name-only filter never finds it in that state.

To handle this, discovery now runs in two passes:

1. **Named pass** (unchanged): scan for `~5s` (`min(5.0, seconds/2)`),
   collect peripherals whose advertised name matches either model. If
   exactly one is found, connect to it as before.
2. **Anonymous fallback** (new): if the named pass finds nothing, probe
   every peripheral seen advertising with no name *and* no manufacturer
   data, strongest RSSI first -- connect, ask only for the one control
   service (`65786365-…-0000`, nothing broader), and either keep that
   connection if the service is present or disconnect and try the next
   candidate. This mirrors `tools/bandbox_ctl.py`'s `find_amp_client()`,
   which needed the same fallback for the same reason.

This means an anonymous-fallback run **does briefly connect to other
nearby BLE peripherals** that happen to also advertise without a name
(for example some headphones or fitness trackers) before finding or
ruling each one out -- it only ever requests that single service UUID
from them, never enumerates their other services or characteristics, and
disconnects immediately on a non-match. The whole probe is still bounded
by its existing `--seconds` deadline regardless of how many candidates it
tries. New state backing this
(`anonymousCandidates`, `probingAnonymous`, `ignoredDisconnects`) is kept
separate from the single-target fields the rest of the file already used,
so a rejected candidate's later disconnect callback can't be mistaken for
the real target dropping.

One asymmetry worth knowing: this fallback only runs during a full scan
(no `--id` given). The `--id`-based fast path still requires
`peripheral.name` to match, so it depends on CoreBluetooth having already
cached the Solo's name from an earlier connection (which it does, in
practice, once the anonymous fallback has connected to it once). If that
cache is ever absent, `server.py`'s existing discovery-recovery path
(re-running a full, nameless `inspect` scan when a cached identifier
fails) already falls back to a full scan, which goes through the
anonymous path again -- no special-casing needed there.

**The control service UUID is public** (it's printed in this file and in
`tools/bandbox_ctl.py`'s header), so a nearby device advertising it is not
proof it's the user's own amp -- a spoofed peripheral exposing the same
UUID would be accepted just as readily. For that reason the anonymous
fallback only runs when the caller opts in: `bandbox-probe --allow-anonymous`
(CLI), `--allow-anonymous` to `tools/bandbox_ctl.py`, or, for the MCP
server and GUI, setting `BANDBOX_GUITAR_CHANNEL=1` -- configuring a Solo is
itself treated as consent to probe anonymous candidates during discovery,
since that's the only reason to need the fallback. Trio setups
(`BANDBOX_GUITAR_CHANNEL=3`, the default) never probe anonymous candidates.
Once a device has been confirmed once (named or anonymous), pin its
CoreBluetooth UUID with `BANDBOX_DEVICE_UUID` / `--id` rather than relying
on fresh anonymous probing on every run.

The looper (features `0x3140`/`0x3145`, no channel byte at all) was also
confirmed directly against a Solo via `tools/bandbox_ctl.py`: start, stop
(ends recording and starts playback immediately, same as the Trio), pause,
play/resume, undo (removes the last overdub and resumes the base loop),
and delete/clear all behave as expected.

Effects (feature `0x3445`) were also confirmed directly against a Solo via
`tools/bandbox_ctl.py`, using `effect_controls.py`'s builders with
`channel=1`: parameter changes, bypass on/off, and model changes all
behave as expected. One finding worth noting: **changing a slot's model
implicitly activates it**, even if it was bypassed and even though the
request's bypass byte is `0xFF` (omitted) -- there is no need for a
separate bypass-off write after a model change to hear the new model.
Also reconfirmed on the Solo: a saved-detail read (`0x3443`/`0x3444`)
reflects the *saved* preset, not live edits made via `0x3445` -- matching
"Saved values and live edits" above, now verified on this model too.

The mixer (feature `0x3405`/`0x3406`) was confirmed directly against a
Solo too, via `mixer_controls.py`'s builders: reading all four
`channel_input` slots (`0x3400`/`0x3401`) shows only channel `1` plugged,
input type `guitar` (2, 3, 4 report unplugged); reading and writing that
channel's volume works exactly as on the Trio. **`master_volume` (scope
`[0,0]`) gets no reply at all on the Solo** — confirmed with a 10s listen
window (`tools/bandbox_ctl.py ... --wait 10`), ruling out a slow reply;
the amp just never answers that specific query, unlike channel volume on
the same feature pair. Whether this means the Solo has no separate
"master" mix stage, or JBL One simply never asks it that question, is not
yet known -- but per the Solo's manual, it only exposes three volumes:
guitar, mic (sharing the same instrument jack as guitar -- plugging in a
mic disables the guitar input -- or the internal mic when using
headphones), and Bluetooth music. Guitar and mic are the same `channel 1`
this repo already reads/writes, just with a different `input_type`
(`mixer_controls.py`'s `_CHANNEL_INPUTS[1] = {mic, guitar}`); Bluetooth
music is presumably a separate volume outside this `0x3400`-`0x3406`
feature family entirely. If so, the Solo may simply have no "master" sum
of simultaneous channels to report, unlike the Trio's four inputs -- this
still isn't confirmed over BLE, just a plausible explanation.

Preset `duplicate`, `rename`, and `save_user_preset` (feature `0x3446`)
were also confirmed directly against a Solo via `tools/bandbox_ctl.py`,
using `preset_controls.py`'s builders with `channel=1`: duplicating
preset 1 created a new user preset (ID `51`, the first user slot, exactly
as on the Trio), confirmed present in a follow-up `0x3440` inventory read;
renaming it worked; and `save_user_preset` persisted a live parameter
edit, confirmed by reading the preset detail back afterward and seeing
the saved value rather than the prior one. `save_as` wasn't separately
exercised but shares the same `0x3446` builder and wire layout as
`duplicate`, just a different operation code.

**Every BandBox Solo operation this repo implements has now been
confirmed over BLE**, using channel `1` throughout.

`Controls` (the GUI/MCP's actual controller) no longer hardcodes channel
`3`: it takes a `channel` constructor parameter, used everywhere it talks
to `preset_controls.py`/`effect_controls.py`. The server reads this from
a new `BANDBOX_GUITAR_CHANNEL` environment variable (default `3`, same
pattern as `BANDBOX_DEVICE_UUID`) -- set it to `1` to drive a Solo
instead of a Trio. The Drive round-trip feature (`DriveRoundTrip.swift`,
driven by `drive_control.py`) is adapted the same way: its `Plan` now
carries the selection's actual channel instead of assuming `3`, both in
the packet it writes and in the notification guards that watch for a
reply.

**This path (the real `server.py` -> `Controls` -> compiled Swift probe
chain, not `tools/bandbox_ctl.py`) was itself confirmed against a Solo**,
via `check_mcp.py --live-current` with `BANDBOX_GUITAR_CHANNEL=1`:
`bandbox_read_current_preset` returned a fully verified read (channel 1,
preset 51, all 8 slots decoded). Two bugs surfaced only by this, since
nothing in the unit tests could catch either:

- `current_query.py`'s `SELECTION_QUERY_PREFIX` was missing a byte (the
  value-length field's high byte), so every `selection_query()` result
  was two hex characters short -- the real device's correctly-built
  query/reply never matched it. Unit tests passed anyway because
  `current_result()`'s self-check compared against this same, equally
  wrong constant.
- `BandBoxProbe.swift`'s `--read-current` mode had a second, separate
  hardcoded channel `3`: the exact-byte match that recognizes a selection
  reply (to then send the follow-up preset-detail query) ended in a
  literal `3` instead of `readCurrentChannel`, missed in the first pass.
  With channel 1 this match never fired, so the detail query was never
  sent and the read never completed.

Also fixed in the process: discovery's anonymous-advertising fallback
(see above) didn't exist yet at this point in testing, so this run only
succeeded once that was added.

**A write through this path was also confirmed live**: `check_mcp.py
--live-select-preset 1` (a new, deliberate opt-in flag, since the rest of
the script only drives read-only tools) called `bandbox_select_preset`
on the Solo via `BANDBOX_GUITAR_CHANNEL=1` -- `verified: true`,
`status: changed_device_reported`, and the post-write readback matched
preset 1's actual saved values. No further bugs surfaced; the two fixed
above were sufficient.

**Effects and mixer through this same path were confirmed live too**,
via two more deliberate opt-in flags: `check_mcp.py
--live-set-effect-parameter 1 3 0 55` set the AMP's Drive to 55 on the
Solo (`verified: true`); `--live-set-mixer-channel-volume 1 8` set
channel 1's (guitar) mixer level to 8 (`verified: true`, decoded state
confirms `input_type: guitar, value: 8`). The mixer call failed twice
first with `read_channel_input` reporting channel 1 as unplugged
(`00DD030001000900000001340300010000` -- plugged=false, input_type=
unknown) -- not a bug, the guitar cable actually was unplugged at that
moment; `set_mixer_volume` correctly refused to proceed rather than
query an invalid channel/input-type pairing. Reconnecting it fixed it.

**The Drive round-trip feature was confirmed live too**, via
`check_mcp.py --live-drive-change` (chains `bandbox_prepare_drive_change`
+ `bandbox_apply_drive_change`): nudging Drive 55 -> 58 on the Solo
returned `change_verified: true`, `live_drive: 58`. This caught a
**third** hardcoded channel `3`, missed in both earlier passes:
`DriveRoundTrip.swift`'s `querySelection()` built its baseline-check
query (`0x3441`) with a literal `[3]` instead of `[plan.channel]`. Live
evidence: the sent query was `...4134010003` (channel 3) and the Solo
dutifully replied with channel 3 back -- the round trip's own selection
check then compared `plan.presetID` against the wrong channel's state
and the whole sequence stalled at "Timed out; no unsafe retry or
unverified restoration attempted" before ever reaching the actual write.
Fixed the same way as the other two: use `plan.channel`.

**Preset duplicate, rename, and save_user_preset through this same path
were confirmed live too**, via three more `check_mcp.py` flags:
`--live-create-preset 1` duplicated preset 1 into a new user preset
(`inventory_verified: true`, `details_verified: true`);
`--live-rename-preset` renamed it (`inventory_verified: true`);
`--live-save-user-preset` persisted a live edit into it
(`saved_content_verified: true`). The only snag was environmental, not a
bug: `preset_operation()` requires the target already selected (same
precondition the Python docstrings already state), and
`save_user_preset`'s content verification needs a `Controls.preview`
established in the *same* process -- each `check_mcp.py` invocation is
its own process, so a `--live-select-preset` run earlier doesn't carry
over; `--live-save-user-preset` now selects first in the same session to
match how a real MCP/GUI session would actually do it.

**Every feature this repo implements has now been confirmed live
through the real `server.py` -> `Controls`/`DriveRoundTrip.swift` ->
Swift-probe path against a Solo**, not just via `tools/bandbox_ctl.py`:
preset read/select/duplicate/rename/save, looper (direct only, see
above), effects, mixer, and Drive. Nothing in this repo's Solo support
remains purely inferred.

## Transport

The native Swift helper uses macOS CoreBluetooth to discover and connect to the control service:

| Role | UUID |
| --- | --- |
| Service | `65786365-6C70-6F69-6E74-2E636F6D0000` |
| RX notifications | `65786365-6C70-6F69-6E74-2E636F6D0001` |
| TX writes | `65786365-6C70-6F69-6E74-2E636F6D0002` |

These are protocol constants, not a particular user's device identifiers. CoreBluetooth peripheral identifiers are machine-local and may change. The helper verifies the service during discovery. A failed read can rediscover a device; setting writes are never automatically repeated.

No USB transport is implemented. Bluetooth audio pairing and the BLE control connection are separate concerns. An Android phone is not required.

## Frames and responses

Protocol 4 uses an eight-byte little-endian header:

```text
identifier:u16 | command:u16 | packet_count:u8 | packet_index:u8 | payload_length:u16
```

The ordinary identifier is `0xDD00`; `0xDD01` indicates forwarding. Replies are validated for framing, status, fragmentation, and feature content. Feature records use a two-byte ID, two-byte length, and value bytes. The decoders reject incomplete or contradictory data instead of guessing.

Command `1` carries GET responses, command `2` carries requests that include both writes and parameterized semantic reads, and command `3` carries notifications. A command number by itself does not establish that a setting changed. An acknowledgement or unsolicited notification alone is not a verified value readback.

## Implemented feature families

| Features | Meaning |
| --- | --- |
| `0x3440` | Preset inventory |
| `0x3441` / `0x3442` | Current selection query / selection report and selection change |
| `0x3443` / `0x3444` | Saved preset detail query / response |
| `0x3445` | Effect model, parameter, and active/bypass changes |
| `0x3446` | User-preset operations |
| `0x3400` / `0x3401` | Channel input query / response |
| `0x3405` / `0x3406` | Mixer level query / response and change |
| `0x300x`, `0x304x`, `0x310x`, `0x314x` | Drum, metronome, tuner, and looper families; only named controls are exposed |
| `0x000D` | Raw status mapped to battery in JBL One; not independently compared to a battery display |

`onboard_controls.py` defines the actual supported subsets, ranges, and reply decoders. `effect_catalog.json` defines 44 models and their UI parameter ranges. `ControlRequest.swift` provides a separate native request allowlist.

## Values and identity

- The guitar path implemented here is channel 3, with eight blocks: GAT, COM, WAH, AMP, CAB, MOD, DLY, RVB.
- The GUI's display number is its one-based position in the returned inventory. It is not necessarily the device's internal `preset_id`. MCP calls use internal IDs returned by reads; callers must not infer an ID from the screen number.
- Model identity combines algorithm and configuration IDs. Effect parameters use the amp's UI scale. Compressor Ratio has a wire multiplier of `0.1`; a UI value of 10 is encoded as 1.
- Mixer levels are raw integers `0..32`. The master tuple is channel/input `0,0`. Channel writes require a fresh matching input report and a plugged-in input. Mixer levels are distinct from the AMP block's `0..100` Volume parameter and other JBL products' volume features.

## Saved values and live edits

A saved-detail query does **not** provide a complete snapshot of unsaved live edits. The controller establishes an editable baseline by explicitly loading a saved preset, or through a supported confirmation path in MCP. It retains verified edits to that baseline.

GUI and MCP operations use a process lock and a shared device lease. A shared revision marker invalidates another local controller's old preview after a write. Physical knob changes or changes from JBL One do not update that marker, so the user must re-establish the baseline after editing elsewhere.

Mutations need matching device reports. An uncertain outcome is exposed to the caller, invalidates the preview, and is not followed by an automatic retry or compensating write. Saving checks the stored preset content. Factory presets cannot be overwritten through the exposed save operation.

The looper adds state guards before recording or bar changes. Existing-loop overdubbing and discard are not offered. Firmware, reset, deletion, and arbitrary raw-write APIs are absent.
