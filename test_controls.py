"""Focused controller tests with mocked reads/transport; never touch hardware."""
import asyncio
import copy
import json
from pathlib import Path
import struct
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from controls import Controls
from drive_control import mark_device_changed
from current_preset import _parse_slot
from exchange_protocol import packet_for, RX
import effect_controls as effects
import preset_controls as presets


IDENTIFIER = "00000000-0000-0000-0000-000000000001"


def slot_report(change=4, bypass=255, values=((0, 40),), preset=1):
    return (bytes([3, preset, change, 3, 6, 0, bypass, len(values)])
            + b"".join(bytes([key]) + struct.pack("<f", value) for key, value in values)).hex().upper()


def info(preset=1, drive=41):
    raw = bytes.fromhex(slot_report(change=0, bypass=0,
        values=((0, drive), (1, 50), (2, 59), (3, 60), (4, 70)), preset=preset))
    slot, _ = _parse_slot(raw, 3)
    return {"preset_id": preset, "name": "Clean", "genre": 0,
            "slot_count": 1, "slots": [slot]}


def current(preset=1, drive=41):
    return {"current_preset_read_verified": True,
            "selection": {"channel": 3, "preset_id": preset},
            "preset_info": info(preset, drive)}


def verified(raw, feature=0x3445):
    return {"verified": True, "status": "changed_device_reported", "setting_writes": 1,
            "values": [{"feature": feature, "value_hex": raw}], "errors": []}


def frame(payload, command=3):
    raw = struct.pack("<HHBBH", 0xDD00, command, 1, 0, len(payload)) + payload
    return {"event": "notification", "uuid": RX, "hex": raw.hex(), "error": None}


class ControlsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        work = Path(__file__).resolve().parents[2] / "work"
        work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="controls-test-", dir=work)
        self.directory = Path(self.temp.name)
        self.reader = AsyncMock(return_value=current())
        self.control = Controls(self.directory, self.directory, asyncio.Lock(),
                                self.reader, lambda: IDENTIFIER)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_saved_read_alone_does_not_establish_live_cache(self):
        self.control.exchange = AsyncMock()
        with self.assertRaises(ValueError):
            await self.control.set_effect_parameter(1, 3, 0, 40)
        self.assertIsNone(self.control.preview)
        self.control.exchange.assert_not_awaited()

    async def test_explicit_saved_confirmation_initializes_independent_copy(self):
        read = current()
        self.reader.return_value = read
        snapshot = await self.control.snapshot(1, True)
        self.assertEqual(snapshot["slots"][0]["parameters"][0]["value"], 41)
        read["preset_info"]["slots"][0]["parameters"][0]["value"] = 99
        self.assertEqual(snapshot["slots"][0]["parameters"][0]["value"], 41)

    async def test_changed_selection_and_expired_cache_block_setting(self):
        self.control.remember(info())
        self.control.preview["at"] = time.monotonic() - 121
        with self.assertRaises(ValueError):
            await self.control.snapshot(1, False)
        self.control.remember(info())
        self.reader.return_value = current(2)
        with self.assertRaises(ValueError):
            await self.control.snapshot(1, False)
        self.assertIsNone(self.control.preview)

    async def test_session_preview_keeps_known_edits_without_renewing_timestamp(self):
        control = Controls(self.directory, self.directory, asyncio.Lock(), self.reader,
                           lambda: IDENTIFIER, preview_max_age=None)
        control.remember(info(drive=40))
        old_time = time.monotonic() - 3600
        control.preview["at"] = old_time
        self.assertTrue(control.preview_valid())
        snapshot = await control.snapshot(1, False)
        self.assertEqual(snapshot["slots"][0]["parameters"][0]["value"], 40)
        self.assertEqual(control.preview["at"], old_time)

    async def test_session_preview_still_rejects_external_revision_and_device_change(self):
        control = Controls(self.directory, self.directory, asyncio.Lock(), self.reader,
                           lambda: IDENTIFIER, preview_max_age=None)
        control.remember(info())
        control.preview["at"] = time.monotonic() - 3600
        mark_device_changed(self.directory)
        self.assertFalse(control.preview_valid())
        with self.assertRaises(ValueError):
            await control.snapshot(1, False)
        control.remember(info())
        control.identifier = lambda: "different-device"
        self.assertFalse(control.preview_valid())

    async def test_partial_parameter_reply_preserves_other_values_and_bypass(self):
        saved = info()
        self.control.remember(saved)
        action = effects.set_parameter(3, 1, 3, "6:0", 0, 40, True, saved["slots"][0])
        self.control.exchange = AsyncMock(return_value=verified(slot_report()))
        result = await self.control.effect(action)
        slot = result["slot"]
        self.assertEqual(slot["bypass_raw"], 0)
        self.assertEqual([p["value"] for p in slot["parameters"]], [40, 50, 59, 60, 70])
        self.assertEqual(saved["slots"][0]["parameters"][0]["value"], 41)
        self.control.exchange.assert_awaited_once()

    async def test_bypass_report_merges_explicit_parameter_values(self):
        saved = info()
        self.control.remember(saved)
        action = effects.set_bypass(3, 1, 3, "6:0", False, saved["slots"][0])
        self.control.exchange = AsyncMock(return_value=verified(
            slot_report(change=1, bypass=1, values=((0, 42),))))
        result = await self.control.effect(action)
        self.assertEqual(result["slot"]["bypass_raw"], 1)
        self.assertEqual([p["value"] for p in result["slot"]["parameters"]], [42, 50, 59, 60, 70])

    async def test_unselected_duplicate_source_is_rejected_before_write(self):
        self.control.inventory = AsyncMock(return_value=[{"preset_id": 1, "name": "Clean", "genre": 0},
                                                          {"preset_id": 2, "name": "Other", "genre": 0}])
        self.control.current = AsyncMock(return_value=current(2))
        self.control.exchange = AsyncMock()
        with self.assertRaises(ValueError):
            await self.control.create_preset(1, "Copy", 0)
        self.control.exchange.assert_not_awaited()

    async def test_verified_rename_updates_only_matching_preview_name(self):
        for verified_result, selected_id in ((True, 51), (False, 51), (True, 52)):
            with self.subTest(verified=verified_result, selected_id=selected_id):
                self.control.remember(info(selected_id, drive=40))
                expected = copy.deepcopy(self.control.preview)
                self.control.preset_operation = AsyncMock(return_value={"verified": verified_result})
                result = await self.control.rename_preset(51, "New name")
                if verified_result and selected_id == 51:
                    expected["info"]["name"] = "New name"
                self.assertEqual(self.control.preview, expected)
                self.assertEqual(result["verified"], verified_result)
                self.control.preset_operation.assert_awaited_once()

    async def test_creation_requires_new_id_to_appear_in_inventory(self):
        before = [{"preset_id": 1, "genre": 0, "name": "Clean"}]
        self.control.inventory = AsyncMock(side_effect=[before, before])
        self.control.current = AsyncMock(return_value=current())
        self.control.exchange = AsyncMock(return_value=verified("030101330004436F7079", 0x3446))
        result = await self.control.create_preset(1, "Copy", 0)
        self.assertFalse(result["verified"])
        self.assertEqual(result["setting_writes"], 1)
        self.assertIsNone(self.control.preview)
        self.control.exchange.assert_awaited_once()

    async def test_post_creation_inventory_failure_keeps_write_outcome(self):
        before = [{"preset_id": 1, "genre": 0, "name": "Clean"}]
        self.control.inventory = AsyncMock(side_effect=[before, ValueError("Inventory read failed")])
        self.control.current = AsyncMock(return_value=current())
        self.control.exchange = AsyncMock(return_value=verified("030101330004436F7079", 0x3446))
        result = await self.control.create_preset(1, "Copy", 0)
        self.assertFalse(result["verified"])
        self.assertEqual(result["setting_writes"], 1)
        self.assertTrue(result["errors"])
        self.control.exchange.assert_awaited_once()

    async def test_saved_readback_requires_same_selected_preset(self):
        self.control.remember(info(51, drive=40))
        self.control.current = AsyncMock(side_effect=[current(51, drive=40),
            current(51, drive=40), current(52, drive=40)])
        self.control.inventory = AsyncMock(return_value=[{"preset_id": 51, "genre": 0, "name": "Clean"}])
        self.control.exchange = AsyncMock(return_value=verified("030433FFFF00", 0x3446))
        result = await self.control.save_user_preset(51)
        self.assertFalse(result["verified"])
        self.assertEqual(result["setting_writes"], 1)
        self.assertIsNone(self.control.preview)

    async def test_later_conflicting_effect_report_invalidates_live_cache(self):
        saved = info()
        self.control.remember(saved)
        action = effects.set_parameter(3, 1, 3, "6:0", 0, 40, True, saved["slots"][0])
        target = bytes.fromhex(slot_report(values=((0, 40),)))
        conflict = bytes.fromhex(slot_report(values=((0, 42),)))
        events = [{"event": "connected", "id": IDENTIFIER},
                  {"event": "control_send", "action": action["action"],
                   "hex": packet_for(action).hex(), "setting_write": True},
                  frame(b"\x00\x00", command=2),
                  frame(b"\x00\x00" + struct.pack("<HH", 0x3445, len(target)) + target),
                  frame(b"\x00\x00" + struct.pack("<HH", 0x3445, len(conflict)) + conflict),
                  {"event": "complete", "setting_writes": 1}]
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b"\n".join(json.dumps(e).encode() for e in events), b"")
        with patch("controls.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            result = await self.control.effect(action)
        self.assertFalse(result["verified"])
        self.assertEqual(result["setting_writes"], 1)
        self.assertIsNone(self.control.preview)
        spawn.assert_awaited_once()

    async def test_creation_requires_the_requested_name_in_inventory(self):
        before = [{"preset_id": 1, "genre": 0, "name": "Clean"}]
        after = before + [{"preset_id": 51, "genre": 0, "name": "Wrong name"}]
        self.control.inventory = AsyncMock(side_effect=[before, after])
        self.control.current = AsyncMock(return_value=current())
        self.control.exchange = AsyncMock(return_value=verified("030101330004436F7079", 0x3446))
        result = await self.control.create_preset(1, "Copy", 0)
        self.assertFalse(result["verified"])
        self.control.exchange.assert_awaited_once()

    async def test_creation_requires_the_requested_genre_in_inventory(self):
        before = [{"preset_id": 1, "genre": 0, "name": "Clean"}]
        after = before + [{"preset_id": 51, "genre": 1, "name": "Copy"}]
        self.control.inventory = AsyncMock(side_effect=[before, after])
        self.control.current = AsyncMock(return_value=current())
        self.control.exchange = AsyncMock(return_value=verified("030101330004436F7079", 0x3446))
        result = await self.control.create_preset(1, "Copy", 0)
        self.assertFalse(result["verified"])

    async def test_creation_omitted_source_requires_inventory_and_new_details(self):
        before = [{"preset_id": 1, "genre": 0, "name": "Clean"}]
        after = before + [{"preset_id": 51, "genre": 0, "name": "Copy"}]
        self.control.inventory = AsyncMock(side_effect=[before, after])
        self.control.current = AsyncMock(return_value=current())
        self.control.exchange = AsyncMock(return_value=verified("0301FF330004436F7079", 0x3446))
        self.control.saved_detail = AsyncMock(return_value={**info(51), "name": "Copy"})
        result = await self.control.create_preset(1, "Copy", 0)
        self.assertTrue(result["verified"])
        self.assertTrue(result["inventory_verified"])
        self.assertTrue(result["details_verified"])
        self.assertIsNone(result["operation"]["source_id"])
        self.assertEqual(result["operation"]["requested_source_id"], 1)
        self.control.saved_detail.assert_awaited_once_with(51)
        self.control.exchange.assert_awaited_once()
        self.assertIsNone(self.control.preview)

    async def test_creation_detail_failure_is_unverified_without_retry(self):
        before = [{"preset_id": 1, "genre": 0, "name": "Clean"}]
        after = before + [{"preset_id": 51, "genre": 0, "name": "Copy"}]
        for detail in (ValueError("Detail read failed"), {**info(51), "name": "Wrong"}):
            with self.subTest(detail=detail):
                self.control.inventory = AsyncMock(side_effect=[before, after])
                self.control.current = AsyncMock(return_value=current())
                self.control.exchange = AsyncMock(return_value=verified("0301FF330004436F7079", 0x3446))
                self.control.saved_detail = AsyncMock(side_effect=[detail])
                result = await self.control.create_preset(1, "Copy", 0)
                self.assertFalse(result["verified"])
                self.assertEqual(result["setting_writes"], 1)
                self.control.exchange.assert_awaited_once()

    async def test_model_merge_refreshes_label_and_removes_stale_aggregate_bytes(self):
        self.control.remember(info())
        self.control.preview["info"]["raw_hex"] = "old-preset"
        action = effects.set_model(3, 1, 3, "6:0", "6:1")
        report = bytearray.fromhex(slot_report(change=3, bypass=0,
            values=((0, 20), (1, 50), (2, 50), (3, 50), (4, 50))))
        report[5] = 1
        self.control.exchange = AsyncMock(return_value=verified(report.hex()))
        result = await self.control.effect(action)
        self.assertTrue(result["verified"])
        self.assertEqual(result["slot"]["algorithm_label"], "Jazz Sound")
        self.assertEqual(result["slot"]["config_id"], 1)
        self.assertNotIn("raw_hex", result["slot"])
        self.assertNotIn("raw_hex", self.control.preview["info"])

    async def test_redundant_model_echo_keeps_preceding_full_parameter_report(self):
        self.control.remember(info())
        action = effects.set_model(3, 1, 3, "6:0", "6:1")
        full = bytearray.fromhex(slot_report(change=0, bypass=0,
            values=((0, 20), (1, 50), (2, 51), (3, 52), (4, 53))))
        full[5] = 1
        echo = bytearray.fromhex(slot_report(change=3, bypass=255, values=()))
        echo[5] = 1
        result = verified(echo.hex())
        result["reports"] = [{"feature": 0x3445, "value_hex": raw.hex()} for raw in (full, echo)]
        self.control.exchange = AsyncMock(return_value=result)
        result = await self.control.effect(action)
        self.assertTrue(result["verified"])
        self.assertEqual(result["slot"]["config_id"], 1)
        self.assertEqual(result["slot"]["bypass_raw"], 0)
        self.assertEqual([p["value"] for p in result["slot"]["parameters"]], [20, 50, 51, 52, 53])

    async def test_saved_detail_mismatch_cannot_verify_known_preview_persistence(self):
        self.control.remember(info(51, drive=40))
        self.control.current = AsyncMock(return_value=current(51, drive=41))
        self.control.inventory = AsyncMock(return_value=[{"preset_id": 51, "genre": 0, "name": "Clean"}])
        self.control.exchange = AsyncMock(return_value=verified("030433FFFF00", 0x3446))
        result = await self.control.save_user_preset(51)
        self.assertFalse(result["verified"])
        self.assertEqual(result["setting_writes"], 1)
        self.assertIsNone(self.control.preview)
        self.control.exchange.assert_awaited_once()

    async def test_transport_failure_is_one_attempt_and_invalidates_preview(self):
        action = presets.select(3, 1)
        events = [{"event": "connected", "id": IDENTIFIER},
                  {"event": "control_send", "action": action["action"],
                   "hex": packet_for(action).hex(), "setting_write": True},
                  {"event": "query_transport_ack", "error": "failed"},
                  {"event": "complete", "setting_writes": 1}]
        process = AsyncMock()
        process.returncode = 1
        process.communicate.return_value = (b"\n".join(json.dumps(e).encode() for e in events), b"")
        self.control.remember(info())
        with patch("controls.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            result = await self.control.exchange(action, lambda raw: raw == "0301")
        self.assertFalse(result["verified"])
        self.assertEqual(result["setting_writes"], 1)
        self.assertIsNone(self.control.preview)
        spawn.assert_awaited_once()
        process.communicate.assert_awaited_once()

    async def test_drive_invalidation_callback_runs_before_setting_dispatch_only(self):
        callback = Mock()
        self.control.invalidate_drive = callback
        action = presets.select(3, 1)
        events = [{"event": "connected", "id": IDENTIFIER},
                  {"event": "control_send", "action": action["action"],
                   "hex": packet_for(action).hex(), "setting_write": True},
                  frame(b"\x00\x00", command=2),
                  frame(bytes.fromhex("0000423402000301")),
                  {"event": "complete", "setting_writes": 1}]
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b"\n".join(json.dumps(e).encode() for e in events), b"")

        async def spawn(*args, **kwargs):
            callback.assert_called_once_with()
            return process

        with patch("controls.asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn)):
            result = await self.control.exchange(action, lambda raw: raw == "0301")
        self.assertTrue(result["verified"])
        callback.reset_mock()
        read = {"action": "read_drum_bpm", "command": 1, "feature": 0x3001,
                "response_feature": 0x3001, "setting_write": False}
        events = [{"event": "connected", "id": IDENTIFIER},
                  {"event": "control_send", "action": read["action"],
                   "hex": packet_for({"value_hex": "", **read}).hex(), "setting_write": False},
                  frame(bytes.fromhex("00000130010078"), command=1),
                  {"event": "complete", "setting_writes": 0}]
        process.communicate.return_value = (b"\n".join(json.dumps(e).encode() for e in events), b"")
        with patch("controls.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            result = await self.control.exchange(read, lambda raw: raw == "78")
        self.assertTrue(result["verified"])
        callback.assert_not_called()

    def mock_looper_state(self, **changes):
        state = {"looper_recording_state": 0, "looper_playback": 0,
                 "looper_recorded_length": 0, "looper_layer_count": 0,
                 "drum_enabled": False, "metronome_enabled": False,
                 "looper_bars": 4, "looper_estimated_duration_ms": 8000, **changes}

        async def read(name):
            return {"verified": True, "state": {"value": state[name]}, "setting_writes": 0}

        self.control._read_tool = AsyncMock(side_effect=read)
        self.control.exchange = AsyncMock(return_value=verified("01", 0x3140))
        return state

    async def test_looper_unverified_or_failed_state_never_writes(self):
        self.mock_looper_state()
        self.control._read_tool = AsyncMock(return_value={"verified": False, "setting_writes": 0})
        result = await self.control.control_looper("start_recording")
        self.assertFalse(result["verified"])
        self.assertEqual(result["setting_writes"], 0)
        self.control.exchange.assert_not_awaited()
        self.control._read_tool = AsyncMock(side_effect=RuntimeError("Read failed"))
        with self.assertRaises(RuntimeError):
            await self.control.control_looper("start_recording")
        self.control.exchange.assert_not_awaited()

    async def test_looper_start_rejects_existing_audio_or_unknown_state(self):
        for change in ({"looper_recorded_length": 1}, {"looper_layer_count": 1},
                       {"looper_recording_state": 255}):
            with self.subTest(change=change):
                self.mock_looper_state(**change)
                with self.assertRaises(ValueError):
                    await self.control.control_looper("start_recording")
                self.control.exchange.assert_not_awaited()

    async def test_looper_start_sends_one_start_without_companion_changes(self):
        self.mock_looper_state()
        result = await self.control.control_looper("start_recording")
        self.assertTrue(result["verified"])
        self.control.exchange.assert_awaited_once()
        request, matcher = self.control.exchange.await_args.args
        self.assertEqual((request["feature"], request["value_hex"]), (0x3140, "01"))
        self.assertTrue(matcher("01"))
        self.assertFalse(matcher("05"))
        self.assertEqual(self.control._read_tool.await_count, 6)

    async def test_looper_stop_requires_active_state_and_reports_audio_readback(self):
        self.mock_looper_state(looper_recording_state=1, looper_recorded_length=500)
        self.control.exchange.return_value = verified("00", 0x3140)
        result = await self.control.control_looper("stop_recording")
        self.assertTrue(result["verified"])
        self.assertTrue(result["recorded_audio_reported"])
        request, matcher = self.control.exchange.await_args.args
        self.assertEqual((request["feature"], request["value_hex"]), (0x3140, "00"))
        self.assertTrue(matcher("00"))
        self.assertEqual([call.args[0] for call in self.control._read_tool.await_args_list],
                         ["looper_recording_state", "looper_recorded_length"])
        self.mock_looper_state(looper_recording_state=0)
        with self.assertRaises(ValueError):
            await self.control.control_looper("stop_recording")
        self.control.exchange.assert_not_awaited()

    async def test_looper_bars_change_only_with_empty_matching_preview(self):
        self.mock_looper_state(drum_enabled=True)
        self.control.exchange.return_value = verified("08", 0x3147)
        result = await self.control.control_looper("set_bars", bars=8, mode="drum")
        self.assertTrue(result["verified"])
        self.control.exchange.assert_awaited_once()
        request, matcher = self.control.exchange.await_args.args
        self.assertEqual((request["feature"], request["value_hex"]), (0x3147, "08"))
        self.assertTrue(matcher("08"))
        self.assertEqual(self.control._read_tool.await_count, 8)

    async def test_looper_slow_reads_or_device_change_never_write(self):
        self.mock_looper_state()
        with patch("controls.time", SimpleNamespace(monotonic=Mock(side_effect=[100, 131]))):
            with self.assertRaises(ValueError):
                await self.control.control_looper("start_recording")
        self.control.exchange.assert_not_awaited()
        self.mock_looper_state()
        self.control.identifier = Mock(side_effect=[IDENTIFIER, "changed-device"])
        with self.assertRaises(ValueError):
            await self.control.control_looper("start_recording")
        self.control.exchange.assert_not_awaited()

    async def test_mixer_master_endpoints_use_fixed_scope_and_one_write(self):
        for level in (0, 32):
            with self.subTest(level=level):
                self.control._read_tool = AsyncMock(return_value={"verified": True,
                    "state": {"raw_hex": "000010"}})
                raw = bytes([0, 0, level]).hex().upper()
                self.control.exchange = AsyncMock(return_value=verified(raw, 0x3406))
                result = await self.control.set_mixer_volume("master", level)
                self.assertTrue(result["verified"])
                self.assertEqual(result["state"]["value"], level)
                self.assertEqual(result["state"]["unit"], "raw_mixer_steps")
                self.control._read_tool.assert_awaited_once_with("master_volume", channel=None, input_type=None)
                self.control.exchange.assert_awaited_once()
                request, matcher = self.control.exchange.await_args.args
                self.assertEqual((request["feature"], request["value_hex"]), (0x3406, raw))
                self.assertTrue(matcher(raw))
                self.assertFalse(matcher(bytes([3, 2, level]).hex()))
        for level in (-1, 33, True, 15.5):
            self.control._read_tool.reset_mock()
            self.control.exchange.reset_mock()
            with self.subTest(level=level), self.assertRaises(ValueError):
                await self.control.set_mixer_volume("master", level)
            self.control._read_tool.assert_not_awaited()
            self.control.exchange.assert_not_awaited()

    async def test_mixer_channel_pairing_mismatch_refuses_without_write(self):
        for input_raw, input_name, volume_raw in (("030102", "guitar", "030110"),
                                                  ("030101", "mic", "030110"),
                                                  ("030002", "guitar", "030210")):
            with self.subTest(input_raw=input_raw, volume_raw=volume_raw):
                self.control._read_tool = AsyncMock(side_effect=[
                    {"verified": True, "state": {"raw_hex": input_raw, "input_type_name": input_name}},
                    {"verified": True, "state": {"raw_hex": volume_raw}}])
                self.control.exchange = AsyncMock()
                with self.assertRaises(ValueError):
                    await self.control.set_mixer_volume("channel", 15, channel=3)
                self.control.exchange.assert_not_awaited()

    async def test_mixer_baseline_above_source_scale_blocks_even_lower_target(self):
        self.control._read_tool = AsyncMock(return_value={"verified": True,
            "state": {"raw_hex": "00002F"}})
        self.control.exchange = AsyncMock()
        with self.assertRaisesRegex(ValueError, "firmware scale"):
            await self.control.set_mixer_volume("master", 15)
        self.control.exchange.assert_not_awaited()
        self.control._read_tool = AsyncMock(side_effect=[
            {"verified": True, "state": {"raw_hex": "030102", "input_type_name": "guitar"}},
            {"verified": True, "state": {"raw_hex": "030221"}}])
        with self.assertRaisesRegex(ValueError, "firmware scale"):
            await self.control.set_mixer_volume("channel", 15, channel=3)
        self.control.exchange.assert_not_awaited()

    async def test_mixer_requires_fresh_same_device_and_master_scope(self):
        for reason in ("slow", "different_device", "wrong_scope", "unverified"):
            self.control._read_tool = AsyncMock(return_value={"verified": reason != "unverified",
                "state": {"raw_hex": "030210" if reason == "wrong_scope" else "000010"}})
            self.control.exchange = AsyncMock()
            self.control.identifier = Mock(side_effect=[IDENTIFIER, "changed"] if reason == "different_device"
                                           else lambda: IDENTIFIER)
            with self.subTest(reason=reason):
                if reason == "slow":
                    with patch("controls.time", SimpleNamespace(monotonic=Mock(side_effect=[100, 131]))):
                        with self.assertRaises(ValueError):
                            await self.control.set_mixer_volume("master", 15)
                elif reason == "unverified":
                    result = await self.control.set_mixer_volume("master", 15)
                    self.assertFalse(result["verified"])
                    self.assertEqual(result["setting_writes"], 0)
                else:
                    with self.assertRaises(ValueError):
                        await self.control.set_mixer_volume("master", 15)
                self.control.exchange.assert_not_awaited()

    async def test_rejects_unsupported_channel(self):
        for bad in (0, 2, 4, "3"):
            with self.subTest(channel=bad), self.assertRaises(ValueError):
                Controls(self.directory, self.directory, asyncio.Lock(), self.reader,
                         lambda: IDENTIFIER, channel=bad)

    async def test_channel_1_is_threaded_into_preset_select_and_effect_writes(self):
        solo_current = {"current_preset_read_verified": True,
                        "selection": {"channel": 1, "preset_id": 1}, "preset_info": info(1, 41)}
        reader = AsyncMock(return_value=solo_current)
        solo = Controls(self.directory, self.directory, asyncio.Lock(), reader,
                        lambda: IDENTIFIER, channel=1)
        solo.inventory = AsyncMock(return_value=[{"preset_id": 1, "name": "Clean", "genre": 0}])
        solo.exchange = AsyncMock(return_value=verified("0101", 0x3442))
        await solo.select_preset(1)
        action = solo.exchange.call_args[0][0]
        self.assertEqual(bytes.fromhex(action["value_hex"]), bytes([1, 1]))

        solo.exchange = AsyncMock(return_value=verified(slot_report()))
        await solo.set_effect_parameter(1, 3, 0, 50, confirm_saved_settings=True)
        action = solo.exchange.call_args[0][0]
        self.assertEqual(bytes.fromhex(action["value_hex"])[0], 1)


if __name__ == "__main__":
    unittest.main()
