#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ "$(uname -s)" != Darwin ]; then
  echo "The Bluetooth helper requires macOS and Xcode Command Line Tools." >&2
  exit 1
fi
APP=bin/bandbox-probe.app
mkdir -p "$APP/Contents/MacOS" .build
cat DriveRoundTrip.swift ControlRequest.swift BandBoxProbe.swift > .build/CombinedProbe.swift
xcrun swiftc .build/CombinedProbe.swift -o .build/bandbox-probe \
  -framework CoreBluetooth -framework IOBluetooth
# A real app bundle, not a bare signed binary: macOS's Bluetooth privacy
# prompt requires a genuine Contents/Info.plist to find
# NSBluetoothAlwaysUsageDescription -- a bare executable with the plist
# linked into a Mach-O section (the previous approach) now hard-crashes
# instead of prompting, on macOS 26+.
cp Info.plist "$APP/Contents/Info.plist"
mv .build/bandbox-probe "$APP/Contents/MacOS/bandbox-probe"
codesign --force --sign - "$APP"
