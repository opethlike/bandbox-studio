#!/usr/bin/env python3
"""Send confirmed control commands directly to the JBL BandBox (Trio or
Solo) over BLE, bypassing JBL One. Useful for validating protocol commands
directly against hardware, independent of the Swift/CoreBluetooth helper.
Uses bleak (already installed), CoreBluetooth backend on macOS.

Service/characteristic UUIDs (see docs/protocol.md for the full feature-ID
table and byte layouts; vendor base UUID leaks "excelpoint.com", the BLE
module maker):
  service:  65786365-6c70-6f69-6e74-2e636f6d0000
  write:    65786365-6c70-6f69-6e74-2e636f6d0002   (ATT handle 0x03ea)
  notify:   65786365-6c70-6f69-6e74-2e636f6d0001   (ATT handle 0x03ec)

Usage:
    python3 tools/bandbox_ctl.py scan
    python3 tools/bandbox_ctl.py preset <1-254> [--channel 1|3]    # default 3 (Trio); Solo uses 1; 1-27 factory, 51+ user
    python3 tools/bandbox_ctl.py looper start|stop|undo|delete|play|pause
    python3 tools/bandbox_ctl.py raw <hex ID LE> <hex payload>      # e.g. raw 4234 02000301
    python3 tools/bandbox_ctl.py get <hex ID LE>                    # plain command=1 GET, e.g. get 4034 (inventory)

Any command accepts --wait <seconds> to change how long it listens for
notifications after the write (default 4.0) -- useful to rule out a slow
reply before concluding a feature just doesn't answer.

If the amp advertises with no name (observed on some Solo units), pass
--allow-anonymous to probe nearby unnamed devices for the control service.
The control service UUID is public (it's documented above), so this trusts
whichever nearby device answers it; only use it when you're sure no one
else's BLE device is nearby, and prefer pinning the confirmed address
afterwards over repeating anonymous probing on every run.

Requires the amp already powered on and advertising (not connected to a
phone's JBL One at the same time — BLE peripherals here only take one
central connection).
"""
import asyncio
import sys

from bleak import BleakClient, BleakScanner

SERVICE_UUID = "65786365-6c70-6f69-6e74-2e636f6d0000"
WRITE_CHAR = "65786365-6c70-6f69-6e74-2e636f6d0002"
NOTIFY_CHAR = "65786365-6c70-6f69-6e74-2e636f6d0001"
NAME_FILTER = "bandbox"  # case-insensitive substring; override with --name
ALLOW_ANONYMOUS = False  # opt-in via --allow-anonymous; see find_amp_client()

LOOPER_ID = bytes.fromhex("4031")  # 0x3140 LE
LOOPER_PLAYBACK_ID = bytes.fromhex("4531")  # 0x3145 LE
PRESET_ID = bytes.fromhex("4234")  # 0x3442 LE

LOOPER_CMDS = {
    "start": (LOOPER_ID, bytes.fromhex("010001")),
    "stop": (LOOPER_ID, bytes.fromhex("010000")),
    "undo": (LOOPER_ID, bytes.fromhex("010004")),
    "delete": (LOOPER_ID, bytes.fromhex("010005")),
    "play": (LOOPER_PLAYBACK_ID, bytes.fromhex("010003")),
    "pause": (LOOPER_PLAYBACK_ID, bytes.fromhex("010002")),
}


def build_frame(char_id: bytes, payload: bytes) -> bytes:
    # 8-byte LE header: id 0xDD00 | command 2 | packet_count 1 | packet_index 0 | len
    body = char_id + payload
    return bytes.fromhex("00dd02000100") + len(body).to_bytes(2, "little") + body


def build_get_frame(char_id: bytes) -> bytes:
    # command=1 plain GET: no value, len is always 2 (just the feature id).
    return bytes.fromhex("00dd01000100") + len(char_id).to_bytes(2, "little") + char_id


def notify_handler(_, data: bytearray):
    print(f"  <- notify: {data.hex()}")


async def find_amp_client() -> BleakClient:
    """Scan for an advertised name containing NAME_FILTER (matches Trio and
    Solo), connect, and verify our control service. Press the amp's Bluetooth
    button once first if it doesn't show up (it may not advertise otherwise)."""
    print(f"Scanning 10s for a device named *{NAME_FILTER}*...")
    found = await BleakScanner.discover(timeout=10.0, return_adv=True)
    matches = [
        (device, adv) for device, adv in found.values()
        if NAME_FILTER in (device.name or adv.local_name or "").lower()
    ]
    for device, adv in matches:
        print(f"  found {device.name or adv.local_name!r} {device.address} rssi={adv.rssi}")
    if not matches:
        # The control service UUID is public (it's in this file's own header),
        # so a nearby device advertising it is not proof it's the user's amp.
        # Only probe anonymous candidates (no name, no manufacturer data) when
        # explicitly asked to with --allow-anonymous.
        if not ALLOW_ANONYMOUS:
            sys.exit("no named BandBox found — the amp can advertise anonymously; "
                      "pass --allow-anonymous to probe nearby unnamed devices for "
                      "the control service (only if you're sure no one else's "
                      "device is nearby)")
        # Observed: the amp can advertise with no name and no manufacturer data.
        # Probe such devices (strongest first) for the control service.
        anon = sorted(
            [(d, a) for d, a in found.values()
             if not (d.name or a.local_name) and not a.manufacturer_data],
            key=lambda pair: -(pair[1].rssi or -999))
        print(f"  no named match; probing {len(anon)} anonymous device(s)")
        for device, adv in anon:
            print(f"  probing {device.address} rssi={adv.rssi} ...")
            client = BleakClient(device, timeout=15.0)
            try:
                await client.connect()
            except Exception as e:
                print(f"    connect failed: {type(e).__name__}: {e}")
                continue
            if SERVICE_UUID in {s.uuid.lower() for s in client.services}:
                print("    MATCH")
                return client
            await client.disconnect()
        sys.exit("no BandBox found — press the amp's Bluetooth button once, wait "
                 "for the blue light to pulse, close JBL One, retry.")
    if len(matches) > 1:
        sys.exit("multiple BandBox devices found — pass --name with a more "
                 "specific substring (e.g. --name solo)")
    device = matches[0][0]
    client = BleakClient(device, timeout=20.0)
    await client.connect()
    uuids = {s.uuid.lower() for s in client.services}
    if SERVICE_UUID not in uuids:
        await client.disconnect()
        sys.exit(f"connected but control service missing; services: {sorted(uuids)}")
    return client


async def read_name(client) -> str:
    try:
        return (await client.read_gatt_char("00002a00-0000-1000-8000-00805f9b34fb")).decode()
    except Exception:
        return "?"


async def send(frame: bytes, wait: float = 4.0):
    # Retrying is only safe before the write: a drop during connect/subscribe
    # means nothing reached the amp.
    client = None
    for attempt in (1, 2, 3):
        try:
            client = await find_amp_client()
            print(f"  connected to: {await read_name(client)}")
            await client.start_notify(NOTIFY_CHAR, notify_handler)
            break
        except Exception as e:
            print(f"  link dropped before write ({type(e).__name__}: {e}), retry {attempt}/3")
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            client = None
            await asyncio.sleep(2.0)
    if client is None:
        sys.exit("could not establish a stable link; nothing was sent")
    try:
        print(f"-> write:  {frame.hex()}")
        await client.write_gatt_char(WRITE_CHAR, frame, response=True)
        await asyncio.sleep(wait)  # looper position ticks ~1/s
        await client.stop_notify(NOTIFY_CHAR)
    finally:
        await client.disconnect()


async def scan():
    client = await find_amp_client()
    print(f"found: {client.address}")
    await client.disconnect()


async def scanall():
    print("Scanning 10s for every nearby BLE device (checks macOS Bluetooth "
          "permission + shows the amp's real advertised name if it differs)...")
    found = await BleakScanner.discover(timeout=10.0, return_adv=True)
    if not found:
        print("no BLE devices seen at all — likely a macOS Bluetooth permission "
              "issue: System Settings > Privacy & Security > Bluetooth, allow "
              "your terminal app, then retry.")
        return
    rows = sorted(found.values(), key=lambda pair: -(pair[1].rssi or -999))
    for device, adv in rows:
        name = device.name or adv.local_name
        print(f"  rssi={adv.rssi:>4d}  {device.address}  name={name!r}  "
              f"service_uuids={adv.service_uuids}  mfg_data={dict(adv.manufacturer_data)}")


def main():
    global NAME_FILTER, ALLOW_ANONYMOUS
    args = sys.argv[1:]
    if "--name" in args:
        i = args.index("--name")
        NAME_FILTER = args[i + 1].lower()
        del args[i:i + 2]
    if "--allow-anonymous" in args:
        ALLOW_ANONYMOUS = True
        args.remove("--allow-anonymous")
    wait = 4.0
    if "--wait" in args:
        i = args.index("--wait")
        wait = float(args[i + 1])
        del args[i:i + 2]
    if not args:
        sys.exit(__doc__)

    if args[0] == "scan":
        asyncio.run(scan())
        return

    if args[0] == "scanall":
        asyncio.run(scanall())
        return

    if args[0] == "preset":
        channel = 3
        if "--channel" in args:
            i = args.index("--channel")
            channel = int(args[i + 1])
            del args[i:i + 2]
        if channel not in (1, 3):
            sys.exit("--channel must be 1 (Solo) or 3 (Trio)")
        idx = int(args[1])
        if not (1 <= idx <= 254):
            sys.exit("preset index must be 1-254 (1-27 factory, 51+ user)")
        frame = build_frame(PRESET_ID, bytes([0x02, 0x00, channel, idx]))
        asyncio.run(send(frame, wait))
        return

    if args[0] == "looper":
        action = args[1]
        if action not in LOOPER_CMDS:
            sys.exit(f"unknown looper action '{action}', choose from {list(LOOPER_CMDS)}")
        char_id, payload = LOOPER_CMDS[action]
        frame = build_frame(char_id, payload)
        asyncio.run(send(frame, wait))
        return

    if args[0] == "raw":
        char_id = bytes.fromhex(args[1])
        payload = bytes.fromhex(args[2])
        frame = build_frame(char_id, payload)
        asyncio.run(send(frame, wait))
        return

    if args[0] == "get":
        char_id = bytes.fromhex(args[1])
        frame = build_get_frame(char_id)
        asyncio.run(send(frame, wait))
        return

    sys.exit(__doc__)


if __name__ == "__main__":
    main()
