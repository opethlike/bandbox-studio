"""Small MCP-facing controller over named APK-backed commands."""
import asyncio
import copy
from functools import wraps
import json
from pathlib import Path
import time
import uuid
from typing import Literal

from current_preset import parse_preset_info, _parse_slot
from drive_control import device_lease, device_revision, mark_device_changed
import effect_controls as effects
from exchange_protocol import parse_exchange
import onboard_controls as onboard
import preset_controls as presets
import looper_controls as looper
import mixer_controls as mixer


def serialized(method):
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self.lock:
            with device_lease(self.state):
                return await method(self, *args, **kwargs)
    return wrapped


class Controls:
    def __init__(self, root, state, lock, read, identifier, invalidate_drive=None,
                 preview_max_age=120, channel=3):
        if channel not in (1, 3):
            raise ValueError("channel must be 1 (Solo) or 3 (Trio)")
        self.root, self.state, self.lock = Path(root), Path(state), lock
        self.read, self.identifier = read, identifier
        self.invalidate_drive = invalidate_drive
        self.preview_max_age = preview_max_age
        self.channel = channel
        self.preview = None

    def preview_valid(self):
        cache = self.preview
        age = time.monotonic() - cache["at"] if cache else -1
        return bool(cache and cache["id"] == self.identifier()
                    and age >= 0 and (self.preview_max_age is None or age <= self.preview_max_age)
                    and cache.get("revision") == device_revision(self.state))

    def mark_mutation(self):
        valid = self.preview_valid()
        revision = mark_device_changed(self.state)
        if valid:
            self.preview["revision"] = revision
        else:
            self.preview = None

    def record_result(self, result):
        if result.get("operation_id"):
            path = self.state / f'{result["operation_id"]}.result.json'
            path.write_text(json.dumps(result, indent=2) + "\n")
        return result

    def readback_failed(self, result, reason):
        self.preview = None
        result.update(verified=False, status="operation_reported_readback_unverified")
        result["errors"].append({"error": str(reason)})
        return self.record_result(result)

    async def exchange(self, request, matcher):
        request = {"value_hex": "", **request}
        if request["setting_write"]:
            self.mark_mutation()
        if request["setting_write"] and self.invalidate_drive is not None:
            self.invalidate_drive()
        operation = uuid.uuid4().hex
        plan = dict(request, device_uuid=self.identifier(), created_at=time.time())
        path = self.state / f"{operation}.plan.json"
        path.write_text(json.dumps(plan, indent=2) + "\n")
        process = await asyncio.create_subprocess_exec(
            str(self.root / "bin/bandbox-probe"), "--id", plan["device_uuid"],
            "--control-plan", str(path), "--seconds", "20",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, error = await asyncio.wait_for(process.communicate(), timeout=25)
        except (TimeoutError, asyncio.CancelledError):
            self.preview = None
            if process.returncode is None: process.kill()
            await process.wait()
            raise RuntimeError("Control interrupted; result uncertain. No retry attempted.")
        (self.state / f"{operation}.jsonl").write_bytes(output)
        (self.state / f"{operation}.stderr").write_bytes(error)
        try:
            events = [json.loads(line) for line in output.decode().splitlines() if line]
            parsed = parse_exchange(events, request)
            reports = [value for value in parsed["feature_values"]
                       if value["feature"] == request["response_feature"]]
            matches = [value for value in reports if matcher(value["value_hex"])]
            verified = bool(matches) and not parsed["errors"] and (
                parsed["acknowledged"] or request["command"] == 1)
        except (ValueError, KeyError, TypeError) as exc:
            parsed = {"errors": [str(exc)], "setting_writes": None}
            reports, matches, verified = [], [], False
        if request["setting_write"] and not verified:
            self.preview = None
        result = {"action": request["action"], "verified": verified,
                  "status": ("changed_device_reported" if request["setting_write"] else "read_verified")
                  if verified else "unverified", "operation_id": operation,
                  "setting_writes": parsed["setting_writes"],
                  "values": matches, "reports": reports, "errors": parsed["errors"],
                  "native_error": error.decode(errors="replace")}
        return self.record_result(result)

    async def current(self):
        result = await self.read("current")
        if not result.get("current_preset_read_verified"):
            raise ValueError("Could not verify the selected preset.")
        return result

    async def inventory(self):
        result = await self.read("presets")
        if not result.get("preset_list_verified"):
            raise ValueError("Could not verify preset inventory.")
        return result["presets"]

    async def saved_detail(self, preset_id):
        request = {"action": "read_preset_details", "command": 2, "feature": 0x3443,
                   "value_hex": bytes([preset_id]).hex().upper(), "setting_write": False,
                   "response_feature": 0x3444}
        def matches(raw):
            return parse_preset_info(bytes.fromhex(raw))["preset_id"] == preset_id
        result = await self.exchange(request, matches)
        if not result["verified"]:
            raise ValueError("New preset details could not be verified")
        return parse_preset_info(bytes.fromhex(result["values"][-1]["value_hex"]))

    def remember(self, info):
        self.preview = {"id": self.identifier(), "at": time.monotonic(), "info": copy.deepcopy(info),
                        "revision": device_revision(self.state)}

    async def snapshot(self, expected_preset_id, confirm_saved_settings):
        current = await self.current()
        if current["selection"]["preset_id"] != expected_preset_id:
            self.preview = None
            raise ValueError("The selected preset changed; no setting sent.")
        if confirm_saved_settings:
            self.remember(current["preset_info"])
        cache = self.preview
        if not self.preview_valid() or cache["info"]["preset_id"] != expected_preset_id:
            raise ValueError("Select the preset through MCP first, or explicitly confirm its live settings match the saved preset. Saved reads alone cannot prove unsaved settings.")
        return cache["info"]

    async def effect(self, action):
        result = await self.exchange(action, lambda raw: effects.match_reply(action, raw))
        if result["verified"] and self.preview:
            expected = action["expected"]
            model = effects.MODELS[expected["model_id"]]
            identity = (expected["channel"], expected["preset_id"], expected["slot_id"])
            updates, matched = [], False
            try:
                for report in result.get("reports", result["values"]):
                    raw = bytes.fromhex(report["value_hex"])
                    if len(raw) < 4 or (raw[0], raw[1], raw[3]) != identity:
                        continue
                    new, end = _parse_slot(raw, 3)
                    change = raw[2]
                    if end != len(raw) or change > 4:
                        raise ValueError("Malformed same-slot report after setting write")
                    is_match = effects.match_reply(action, report["value_hex"])
                    if not matched and not is_match:
                        continue
                    matched = True
                    for field in ("algorithm_id", "config_id"):
                        if new[field] != model[field] and (new[field] != 255 or change == 0):
                            raise ValueError("Later report changed the edited slot's model")
                    if ("bypass" in expected and new["bypass_raw"] != 255
                            and new["bypass_raw"] != expected["bypass"]):
                        raise ValueError("Later report changed the edited slot's active state")
                    if "parameter_id" in expected:
                        values = {p["parameter_id"]: p["value_hex"] for p in new["parameters"]}
                        number = expected["parameter_id"]
                        if ((number in values and values[number] != expected["value_hex"])
                                or (change == 0 and number not in values)):
                            raise ValueError("Later report contradicts the requested parameter value")
                    updates.append((change, new))
                old = next(s for s in self.preview["info"]["slots"] if s["slot_id"] == expected["slot_id"])
                for change, new in updates:
                    model_changed = any(new[field] != 255 and new[field] != old[field]
                                        for field in ("algorithm_id", "config_id"))
                    for field in ("algorithm_id", "config_id", "bypass_raw"):
                        if new[field] != 255: old[field] = new[field]
                    if change == 0 or model_changed:
                        old["parameters"] = new["parameters"]
                    else:
                        values = {p["parameter_id"]: p for p in new["parameters"]}
                        old["parameters"] = [values.get(p["parameter_id"], p) for p in old["parameters"]]
                old["parameter_count"] = len(old["parameters"])
                old["bypass"] = {0: False, 1: True}.get(old["bypass_raw"])
                old["algorithm_label"] = model["name"]
                old.pop("raw_hex", None)
                self.preview["info"].pop("raw_hex", None)
                self.preview["at"] = time.monotonic()
                result["slot"] = copy.deepcopy(old)
            except (ValueError, KeyError, StopIteration) as exc:
                return self.readback_failed(result, exc)
        return self.record_result(result)

    async def catalog(self, slot_id: int | None = None) -> dict:
        """List APK-backed amp/effect models, slot IDs and parameter ranges. UI integer values use the listed wire multiplier; Compressor Ratio is divided by ten."""
        models = effects.CATALOG["models"]
        if slot_id is not None:
            if type(slot_id) is not int or not 0 <= slot_id <= 7: raise ValueError("Slot ID must be 0..7")
            models = [model for model in models if model["slot_id"] == slot_id]
        return {"slots": effects.CATALOG["slots"], "models": models,
                "source": "JBL One 2.7.9; firmware support is verified by each command reply."}

    @serialized
    async def read_preset(self, preset_id: int) -> dict:
        """Read any existing preset's saved settings without selecting it or changing the active sound. Does not include unsaved preview edits."""
        if type(preset_id) is not int or preset_id not in {p["preset_id"] for p in await self.inventory()}:
            raise ValueError("That preset ID is not in the amp inventory")
        return {"verified": True, "setting_writes": 0, "preset": await self.saved_detail(preset_id)}

    @serialized
    async def select_preset(self, preset_id: int) -> dict:
        """Load the user's requested saved preset on CH3. This replaces the active preview. Does not save, overwrite or delete presets."""
        if preset_id not in {p["preset_id"] for p in await self.inventory()}:
            raise ValueError("That preset ID is not in the amp inventory.")
        action = presets.select(self.channel, preset_id)
        result = await self.exchange(action, lambda raw: raw.upper() == bytes([self.channel, preset_id]).hex().upper())
        if result["verified"]:
            try:
                current = await self.current()
            except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
                return self.readback_failed(result, exc)
            if current["selection"]["preset_id"] != preset_id:
                self.preview = None
                result.update(verified=False, status="selection_changed_after_reply")
            else:
                self.remember(current["preset_info"])
                result["preset"] = current["preset_info"]
        return self.record_result(result)

    async def preset_operation(self, action):
        source_id = bytes.fromhex(action["value_hex"])[2]
        current = await self.current()
        if current["selection"]["preset_id"] != source_id:
            raise ValueError("Select the source preset first; no preset operation sent.")
        inventory = await self.inventory()
        old_ids = {item["preset_id"] for item in inventory}
        if source_id not in old_ids:
            raise ValueError("Source preset does not exist.")
        def matches(raw):
            try: presets.parse_operation_response(raw, action, old_ids); return True
            except (ValueError, TypeError): return False
        result = await self.exchange(action, matches)
        if result["verified"]:
            parsed = presets.parse_operation_response(result["values"][-1]["value_hex"], action, old_ids)
            result["operation"] = parsed
            try:
                result["inventory"] = await self.inventory()
            except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
                return self.readback_failed(result, exc)
            new_id = parsed.get("new_preset_id")
            check_id = new_id if action["action"] in ("duplicate", "save_as") else parsed["source_id"]
            found = next((item for item in result["inventory"] if item["preset_id"] == check_id), None)
            result["inventory_verified"] = found is not None and (not parsed.get("name") or found["name"] == parsed["name"])
            if action["action"] in ("duplicate", "save_as") and found is not None:
                result["inventory_verified"] &= found["genre"] == parsed["genre"]
            if not result["inventory_verified"]:
                result.update(verified=False, status="operation_reported_inventory_unverified")
            elif action["action"] in ("duplicate", "save_as"):
                try:
                    details = await self.saved_detail(new_id)
                except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
                    return self.readback_failed(result, exc)
                result["preset"] = details
                result["details_verified"] = details["name"] == parsed["name"] and details["genre"] == parsed["genre"]
                if not result["details_verified"]:
                    result.update(verified=False, status="operation_reported_details_unverified")
        return self.record_result(result)

    @serialized
    async def create_preset(self, source_preset_id: int, name: str, genre: int = 0) -> dict:
        """Create a new preset by duplicating an existing one, preserving the source. Device assigns its ID. Does not activate it; select the returned new ID separately, edit effects, then save that user preset."""
        return await self.preset_operation(presets.duplicate(self.channel, source_preset_id, name, genre))

    @serialized
    async def save_as(self, expected_preset_id: int, name: str, genre: int = 0) -> dict:
        """Save the current CH3 preview into a new named preset. Preserves the source preset; device assigns the new ID. Requires the expected preset still selected."""
        current = await self.current()
        if current["selection"]["preset_id"] != expected_preset_id: raise ValueError("Selected preset changed")
        return await self.preset_operation(presets.save_as(self.channel, expected_preset_id, name, genre))

    @serialized
    async def save_user_preset(self, expected_preset_id: int) -> dict:
        """Persist the user's requested edits to the selected USER preset (ID51+). Overwrites that user's saved settings. Factory presets are protected; use save_as to preserve a source."""
        action = presets.save_user_preset(self.channel, expected_preset_id)
        current = await self.current()
        if current["selection"]["preset_id"] != expected_preset_id: raise ValueError("Selected preset changed")
        expected = copy.deepcopy(self.preview["info"]) if (self.preview_valid() and
            self.preview["info"]["preset_id"] == expected_preset_id) else None
        result = await self.preset_operation(action)
        if result["verified"]:
            try:
                current = await self.current()
            except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
                return self.readback_failed(result, exc)
            def values(info):
                return {s["slot_id"]: (s["algorithm_id"], s["config_id"], s["bypass_raw"],
                    {p["parameter_id"]: p["value_hex"] for p in s["parameters"]}) for s in info["slots"]}
            result["saved_content_verified"] = (expected is not None
                and current["selection"]["preset_id"] == expected_preset_id
                and current["preset_info"]["preset_id"] == expected_preset_id
                and values(expected) == values(current["preset_info"]))
            if not result["saved_content_verified"]:
                result.update(verified=False, status="save_reported_content_unverified")
                self.preview = None
            else:
                self.remember(current["preset_info"])
            result["preset"] = current["preset_info"]
        return self.record_result(result)

    @serialized
    async def rename_preset(self, preset_id: int, name: str) -> dict:
        """Rename a USER preset (ID51+) without changing its sound. Factory names are protected."""
        if type(preset_id) is not int or preset_id < 51: raise ValueError("Only user presets can be renamed")
        result = await self.preset_operation(presets.rename(self.channel, preset_id, name))
        if result["verified"] and self.preview and self.preview["info"]["preset_id"] == preset_id:
            self.preview["info"]["name"] = name
        return result

    @serialized
    async def set_effect_parameter(self, expected_preset_id: int, slot_id: int, parameter_id: int,
                                   ui_value: int, confirm_saved_settings: bool = False) -> dict:
        """Set one amp/effect parameter using effect_catalog UI units. Select a preset through MCP first. Only set confirm_saved_settings when the user explicitly confirms no unsaved edits. Do not use stale snapshots after touching amp/JBL One controls. Does not save the preset."""
        info = await self.snapshot(expected_preset_id, confirm_saved_settings)
        slot = next((s for s in info["slots"] if s["slot_id"] == slot_id), None)
        if slot is None: raise ValueError("Slot does not exist")
        model = f'{slot["algorithm_id"]}:{slot["config_id"]}'
        action = effects.set_parameter(self.channel, expected_preset_id, slot_id, model, parameter_id,
                                       ui_value, slot["bypass_raw"] == 0, slot)
        return await self.effect(action)

    @serialized
    async def set_effect_model(self, expected_preset_id: int, slot_id: int, model_id: str,
                               confirm_saved_settings: bool = False) -> dict:
        """Choose an amp, cab or effect model from effect_catalog. Replaces that slot's model and its parameters with device defaults. Requires a recently selected/confirmed preset. Does not save it."""
        info = await self.snapshot(expected_preset_id, confirm_saved_settings)
        slot = next((s for s in info["slots"] if s["slot_id"] == slot_id), None)
        if slot is None: raise ValueError("Slot does not exist")
        old = f'{slot["algorithm_id"]}:{slot["config_id"]}'
        return await self.effect(effects.set_model(self.channel, expected_preset_id, slot_id, old, model_id))

    @serialized
    async def set_effect_active(self, expected_preset_id: int, slot_id: int, active: bool,
                                confirm_saved_settings: bool = False) -> dict:
        """Enable or bypass an effect while preserving its parameters. Requires a recent MCP-selected preset or explicit confirmation that live settings match saved values. Does not save it."""
        info = await self.snapshot(expected_preset_id, confirm_saved_settings)
        slot = next((s for s in info["slots"] if s["slot_id"] == slot_id), None)
        if slot is None: raise ValueError("Slot does not exist")
        model = f'{slot["algorithm_id"]}:{slot["config_id"]}'
        return await self.effect(effects.set_bypass(self.channel, expected_preset_id, slot_id, model, active, slot))

    @serialized
    async def read_tool(self, control: str, channel: int | None = None,
                        input_type: str | None = None, page: str | None = None) -> dict:
        """Read one named drum, metronome, tuner, looper or mixer control. Use list_controls for names. Does not enter app pages or clear recordings."""
        return await self._read_tool(control, channel, input_type, page)

    async def _read_tool(self, control, channel=None, input_type=None, page=None):
        request = onboard.build_read(control, channel=channel, input_type=input_type, page=page)
        result = await self.exchange(request, lambda raw: onboard.response_matches(request, request["response_feature"], bytes.fromhex(raw)))
        if result["verified"]:
            raw = bytes.fromhex(result["values"][-1]["value_hex"])
            result["state"] = (mixer.decode_volume(raw) if request["response_feature"] == 0x3406
                               else onboard.decode_response(request["response_feature"], raw))
        return result

    @serialized
    async def control_looper(self, action: Literal["start_recording", "stop_recording", "set_bars"],
                             bars: int | None = None,
                             mode: Literal["drum", "metronome"] | None = None) -> dict:
        """Start a new freehand recording only on an empty stopped looper, stop an active recording, or set bars in an empty drum/metronome preview. Reads and checks fresh state first. Never overdubs, clears, discards or undoes a loop. Recording requires an explicit user request; it continues until separately stopped. set_bars requires bars and mode, with its backing preview already enabled."""
        if action not in ("start_recording", "stop_recording", "set_bars"):
            raise ValueError("Unsupported looper action")
        if action != "set_bars" and (bars is not None or mode is not None):
            raise ValueError("Only set_bars accepts bars and mode")
        controls = ["looper_recording_state"]
        if action != "stop_recording":
            controls += ["looper_recorded_length", "looper_layer_count", "looper_playback",
                         "drum_enabled", "metronome_enabled"]
        if action == "set_bars":
            controls += ["looper_bars", "looper_estimated_duration_ms"]
        started, identifier = time.monotonic(), self.identifier()
        state = {}
        for control in controls:
            reading = await self._read_tool(control)
            if not reading["verified"]:
                return {"verified": False, "status": "stopped_before_write", "setting_writes": 0,
                        "error": f"Cannot verify {control}", "read_result": reading}
            state[control] = reading["state"]["value"]
        state.update(verified=self.identifier() == identifier,
                     age_seconds=time.monotonic() - started, mode=mode)
        request = (looper.build_recording_start(state) if action == "start_recording" else
                   looper.build_recording_stop(state) if action == "stop_recording" else
                   looper.build_bars(bars, state))
        result = await self.exchange(request, lambda raw:
            looper.response_matches(request, request["response_feature"], bytes.fromhex(raw)))
        if result["verified"] and action == "stop_recording":
            try:
                length = await self._read_tool("looper_recorded_length")
                result["recorded_audio_reported"] = length["verified"] and length["state"]["value"] > 0
                result["length_readback"] = length
            except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
                result.update(recorded_audio_reported=False, length_readback_error=str(exc))
        return self.record_result(result)

    @serialized
    async def set_mixer_volume(self, scope: Literal["master", "channel"], level: int,
                               channel: int | None = None) -> dict:
        """Set master or a plugged channel's mixer level in raw steps 0..32, not percent. Reads the current input and volume before writing. Refuses unknown input pairings or firmware values outside the source-backed scale. This is separate from AMP block Volume (0..100)."""
        if scope not in ("master", "channel") or type(level) is not int or not 0 <= level <= 32:
            raise ValueError("Choose master/channel and an integer level from 0 to 32")
        if scope == "master" and channel is not None:
            raise ValueError("Master volume has no channel argument")
        started, identifier = time.monotonic(), self.identifier()
        input_raw, input_type = None, None
        if scope == "channel":
            reading = await self._read_tool("channel_input", channel=channel)
            if not reading["verified"]:
                return {"verified": False, "status": "stopped_before_write", "setting_writes": 0,
                        "error": "Channel input could not be verified", "read_result": reading}
            input_raw = bytes.fromhex(reading["state"]["raw_hex"])
            input_type = reading["state"]["input_type_name"]
        control = f"{scope}_volume"
        reading = await self._read_tool(control, channel=channel, input_type=input_type)
        if not reading["verified"]:
            return {"verified": False, "status": "stopped_before_write", "setting_writes": 0,
                    "error": "Mixer level could not be verified", "read_result": reading}
        if self.identifier() != identifier or time.monotonic() - started > 30:
            raise ValueError("Fresh same-device mixer state is required; no setting sent")
        request = mixer.build_set(control, level, channel=channel, input_status=input_raw,
                                  current_volume=bytes.fromhex(reading["state"]["raw_hex"]))
        result = await self.exchange(request, lambda raw:
            mixer.response_matches(request, request["response_feature"], bytes.fromhex(raw)))
        if result["verified"]:
            result["state"] = mixer.decode_volume(bytes.fromhex(result["values"][-1]["value_hex"]))
        return self.record_result(result)

    @serialized
    async def set_tool(self, control: str, value: bool | int | float | str) -> dict:
        """Set one named onboard control: drum/metronome, tuner or looper playback. Only source-verified names/ranges are accepted. Never clears/discards loops, resets or changes firmware."""
        request = onboard.build_set(control, value)
        result = await self.exchange(request, lambda raw: onboard.response_matches(request, request["response_feature"], bytes.fromhex(raw)))
        if result["verified"]:
            result["state"] = onboard.decode_response(request["response_feature"], bytes.fromhex(result["values"][-1]["value_hex"]))
        return result

    async def list_controls(self) -> dict:
        """List the exact named onboard controls and their supported read/write values."""
        catalog = onboard.describe_controls()
        catalog["guarded_looper_actions"] = {
            "tool": "bandbox_control_looper", "actions": ["start_recording", "stop_recording", "set_bars"],
            "recording": "New freehand recording only when empty and stopped; stop preserves audio.",
            "bars": "Empty backing preview only; drum or metronome mode, maximum 60 seconds."}
        catalog["mixer_controls"] = {"tool": "bandbox_set_mixer_volume", **mixer.describe_controls()}
        for name, description in catalog["mixer_controls"]["settable_controls"].items():
            catalog["readable_controls"][name]["value_description"] = description["value"]
        return catalog
