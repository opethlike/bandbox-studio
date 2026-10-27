"""Validation and single-use plans; no Bluetooth or MCP imports."""
from contextlib import contextmanager
import fcntl
import math
import os
from pathlib import Path
import secrets
import struct
import time
import uuid

MODELS = ("American Clean", "Jazz Sound", "Yellow Tweed", "UK Crunch", "British Lead",
          "Vintage Drive", "American Gain Stack", "US Metal", "Bass Amp")
PLAN_TTL = 120


def device_revision(directory: Path) -> str:
    """Shared mutation marker; callers hold the device lease while writing."""
    try:
        return (directory / "control-revision.txt").read_text().strip()
    except FileNotFoundError:
        return ""


def mark_device_changed(directory: Path) -> str:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = secrets.token_hex(16)
    temporary = directory / "control-revision.tmp"
    temporary.write_text(value + "\n")
    temporary.replace(directory / "control-revision.txt")
    return value


def validate_request(target_drive, current_drive, amp_model, amp_active):
    for value in (target_drive, current_drive):
        if type(value) is not int or not 0 <= value <= 100:
            raise ValueError("Drive must be a whole number from 0 to 100.")
    if abs(target_drive - current_drive) > 5:
        raise ValueError("This prototype allows at most five Drive points per requested change.")
    if amp_model not in MODELS or type(amp_active) is not bool:
        raise ValueError("Supply the exact displayed amp model and active/bypassed state.")


@contextmanager
def device_lease(directory: Path):
    """Prevent concurrent Bluetooth operations across local MCP processes."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / "device.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another BandBox operation is running. Wait for it to finish.") from None
        yield
    finally:
        os.close(fd)


class PendingChanges:
    def __init__(self, monotonic=time.monotonic, wall_time=time.time, revision=lambda: ""):
        self._pending = {}
        self._clock = monotonic
        self._wall = wall_time
        self._recent = None
        self._revision = revision

    def record_verified_change(self, plan):
        self._recent = (self._clock(), self._wall(), dict(plan), self._revision())

    def invalidate_recent(self):
        self._recent = None

    def invalidate_all(self):
        self._pending.clear()
        self.invalidate_recent()

    def prepare(self, reading, target_drive, current_drive, amp_model, amp_active,
                baseline_source="user_display"):
        validate_request(target_drive, current_drive, amp_model, amp_active)
        if baseline_source not in ("user_display", "recent_verified_change"):
            raise ValueError("Unsupported live baseline source.")
        if not reading.get("current_preset_read_verified") or reading.get("error"):
            raise ValueError("A fresh verified selection and saved-metadata read is required.")
        selection = reading["selection"]
        info = reading["preset_info"]
        if selection["channel"] not in (1, 3) or selection["preset_id"] != info["preset_id"]:
            raise ValueError("Selected channel or preset does not match its details.")
        channel = selection["channel"]
        identifier = str(uuid.UUID(reading["active_corebluetooth_uuid"])).upper()
        slots = [s for s in info["slots"] if s["slot_id"] == 3]
        if len(slots) != 1:
            raise ValueError("One supported AMP slot is required.")
        amp = slots[0]
        if (amp["algorithm_id"] != 6 or amp["config_id"] != MODELS.index(amp_model)
                or amp["bypass_raw"] != (0 if amp_active else 1)):
            raise ValueError("Preset amp metadata differs from the confirmed screen. No change prepared.")
        baseline_at, baseline_wall = self._clock(), self._wall()
        if baseline_source == "recent_verified_change":
            if self._recent is None:
                raise ValueError("No recent verified change exists in this server session.")
            at, wall_at, recent, revision = self._recent
            if not (0 <= self._clock() - at <= PLAN_TTL
                    and 0 <= self._wall() - wall_at <= PLAN_TTL
                    and recent["device_uuid"] == identifier
                    and recent["preset_id"] == selection["preset_id"]
                    and recent["requested_drive"] == current_drive
                    and recent["displayed_amp_model"] == amp_model
                    and recent["displayed_amp_active"] == amp_active
                    and revision == self._revision()):
                self.invalidate_recent()
                raise ValueError("Recent live report expired or conflicts with this request.")
            baseline_at, baseline_wall = at, wall_at
        self._pending.clear()
        summary = {"channel": f"CH{channel}", "preset_id": selection["preset_id"],
                   "preset_name": info["name"], "amp_model": amp_model, "amp_active": amp_active,
                   "from_drive": current_drive, "to_drive": target_drive,
                   "baseline_source": baseline_source,
                   "setting_writes": 0}
        if target_drive == current_drive:
            return {"status": "no_change_needed", **summary}
        token = secrets.token_urlsafe(24)
        plan = {"device_uuid": identifier, "created_at": baseline_wall, "channel": channel,
                "preset_id": selection["preset_id"], "slot_id": 3, "algorithm_id": 6,
                "config_id": amp["config_id"], "bypass_raw": amp["bypass_raw"],
                "original_float_hex": struct.pack("<f", float(current_drive)).hex().upper(),
                "displayed_drive": current_drive, "displayed_amp_model": amp_model,
                "displayed_amp_active": amp_active, "requested_drive": target_drive}
        self._pending[token] = (baseline_at, plan, summary, self._revision())
        return {"status": "prepared", "change_token": token,
                "expires_in_seconds": max(0, int(PLAN_TTL - (self._clock() - baseline_at))),
                "instruction": "Apply this token once for the user's requested change. Do not infer live baseline values from saved preset data.", **summary}

    def consume(self, token):
        item = self._pending.pop(token, None)
        if item is None:
            raise ValueError("Unknown or already used change token. Prepare a fresh change.")
        created, plan, summary, revision = item
        if revision != self._revision():
            self.invalidate_recent()
            raise ValueError("Another controller changed the amp. Prepare a fresh change.")
        age = self._clock() - created
        wall_age = self._wall() - plan["created_at"]
        if not (math.isfinite(age) and math.isfinite(wall_age)
                and 0 <= age <= PLAN_TTL and 0 <= wall_age <= PLAN_TTL):
            raise ValueError("The prepared change expired. Confirm the live display and prepare again.")
        return dict(plan), dict(summary)
