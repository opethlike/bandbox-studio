"""Offline Drive plan/MCP boundary tests. Every native transport is mocked."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from drive_control import PendingChanges, device_lease, validate_request
import server


DEVICE = "00000000-0000-4000-8000-000000000001"


def reading(saved_drive=41):
    return {
        "current_preset_read_verified": True,
        "active_corebluetooth_uuid": DEVICE,
        "selection": {"channel": 3, "preset_id": 1},
        "preset_info": {"preset_id": 1, "name": "Clean Comping", "slots": [{
            "slot_id": 3, "algorithm_id": 6, "config_id": 0, "bypass_raw": 0,
            "parameters": [{"parameter_id": 0, "value": saved_drive}],
        }]},
    }


class Clock:
    monotonic = 1000.0
    wall = 2000.0

    def store(self):
        return PendingChanges(lambda: self.monotonic, lambda: self.wall)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = self.clock.store()

    def prepare(self, **kwargs):
        args = {"target_drive": 43, "current_drive": 40,
                "amp_model": "American Clean", "amp_active": True}
        args.update(kwargs)
        return self.store.prepare(reading(), **args)

    def test_invalid_drive_types_ranges_and_large_steps_rejected(self):
        for value in [True, False, None, "40", 40.0, float("nan"), -1, 101, 10**100]:
            for position in ["target_drive", "current_drive"]:
                with self.subTest(position=position, value=value), self.assertRaises(ValueError):
                    self.prepare(**{position: value})
        for current, target in [(40, 34), (40, 46)]:
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.prepare(current_drive=current, target_drive=target)

    def test_exact_models_and_boolean_active_state_required(self):
        for value in ["american clean", "Unknown", None, 0, [], {}]:
            with self.subTest(model=value), self.assertRaises(ValueError):
                self.prepare(amp_model=value)
        for value in [0, 1, "true", None, [], {}]:
            with self.subTest(active=value), self.assertRaises(ValueError):
                self.prepare(amp_active=value)

    def test_zero_maximum_and_five_point_bounds_allowed(self):
        for current, target in [(0, 5), (5, 0), (95, 100), (100, 95)]:
            with self.subTest(current=current, target=target):
                validate_request(target, current, "American Clean", True)

    def test_saved_drive_is_not_used_as_live_baseline(self):
        result = self.prepare()
        plan, summary = self.store.consume(result["change_token"])
        self.assertEqual(plan["original_float_hex"], struct.pack("<f", 40).hex().upper())
        self.assertEqual(plan["displayed_drive"], 40)
        self.assertEqual(plan["requested_drive"], 43)
        self.assertEqual(summary["from_drive"], 40)
        self.assertNotIn("confirmed_recovery_drive", plan)

    def test_channel_1_solo_selection_is_accepted_and_recorded(self):
        data = reading()
        data["selection"].update(channel=1)
        result = self.store.prepare(data, 43, 40, "American Clean", True)
        plan, summary = self.store.consume(result["change_token"])
        self.assertEqual(plan["channel"], 1)
        self.assertEqual(summary["channel"], "CH1")

    def test_unverified_or_error_reading_cannot_prepare(self):
        for change in [{"current_preset_read_verified": False}, {"error": "transport failed"}]:
            data = reading()
            data.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.prepare(data, 43, 40, "American Clean", True)

    def test_mismatched_selection_or_amp_metadata_cannot_prepare(self):
        mutations = [
            lambda r: r["selection"].update(channel=2),
            lambda r: r["selection"].update(preset_id=2),
            lambda r: r["preset_info"]["slots"][0].update(algorithm_id=7),
            lambda r: r["preset_info"]["slots"][0].update(config_id=1),
            lambda r: r["preset_info"]["slots"][0].update(bypass_raw=1),
            lambda r: r["preset_info"]["slots"][0].update(bypass_raw=255),
            lambda r: r["preset_info"]["slots"].clear(),
            lambda r: r["preset_info"]["slots"].append(deepcopy(r["preset_info"]["slots"][0])),
        ]
        for mutate in mutations:
            data = reading()
            mutate(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.store.prepare(data, 43, 40, "American Clean", True)

    def test_noop_has_no_token_and_invalidates_earlier_plan(self):
        old = self.prepare()["change_token"]
        result = self.prepare(target_drive=40)
        self.assertEqual(result["status"], "no_change_needed")
        self.assertEqual(result["setting_writes"], 0)
        self.assertNotIn("change_token", result)
        with self.assertRaises(ValueError):
            self.store.consume(old)

    def test_token_is_single_use_and_new_preparation_invalidates_old(self):
        first = self.prepare()["change_token"]
        second = self.prepare(target_drive=44)["change_token"]
        with self.assertRaises(ValueError):
            self.store.consume(first)
        self.store.consume(second)
        with self.assertRaises(ValueError):
            self.store.consume(second)

    def seed_recent(self):
        token = self.prepare()["change_token"]
        plan, _ = self.store.consume(token)
        self.store.record_verified_change(plan)

    def test_recent_source_requires_a_preceding_verified_change(self):
        with self.assertRaises(ValueError):
            self.prepare(current_drive=43, target_drive=42, baseline_source="recent_verified_change")
        with self.assertRaises(ValueError):
            self.prepare(baseline_source="saved_preset")

    def test_recent_source_uses_reported_target_not_previous_or_saved_baseline(self):
        self.seed_recent()
        prepared = self.prepare(current_drive=43, target_drive=42, baseline_source="recent_verified_change")
        plan, summary = self.store.consume(prepared["change_token"])
        self.assertEqual(plan["displayed_drive"], 43)
        self.assertEqual(plan["original_float_hex"], struct.pack("<f", 43).hex().upper())
        self.assertEqual(plan["requested_drive"], 42)
        self.assertEqual(summary["baseline_source"], "recent_verified_change")
        for value in [40, 41]:
            with self.subTest(current=value), self.assertRaises(ValueError):
                self.prepare(current_drive=value, target_drive=42, baseline_source="recent_verified_change")

    def test_recent_source_identity_preset_model_and_active_state_must_match(self):
        cases = [
            ({"active_corebluetooth_uuid": "00000000-0000-4000-8000-000000000002"}, None, "American Clean", True),
            ({}, "preset", "American Clean", True),
            ({}, "model", "Jazz Sound", True),
            ({}, "active", "American Clean", False),
        ]
        for updates, field, model, active in cases:
            with self.subTest(field=field, updates=updates):
                self.seed_recent()
                data = reading()
                data.update(updates)
                if field == "preset":
                    data["selection"]["preset_id"] = data["preset_info"]["preset_id"] = 2
                elif field == "model":
                    data["preset_info"]["slots"][0]["config_id"] = 1
                elif field == "active":
                    data["preset_info"]["slots"][0]["bypass_raw"] = 1
                with self.assertRaises(ValueError):
                    self.store.prepare(data, 42, 43, model, active, "recent_verified_change")

    def test_recent_source_expiry_checks_elapsed_and_wall_time(self):
        for clock_name, value in [("monotonic", 1121), ("monotonic", 999),
                                  ("wall", 2121), ("wall", 1999),
                                  ("monotonic", float("nan")), ("wall", float("inf"))]:
            self.clock = Clock()
            self.store = self.clock.store()
            self.seed_recent()
            setattr(self.clock, clock_name, value)
            with self.subTest(clock=clock_name, value=value), self.assertRaises(ValueError):
                self.prepare(current_drive=43, target_drive=42, baseline_source="recent_verified_change")

    def test_preparing_new_token_cannot_refresh_recent_baseline_age(self):
        self.seed_recent()
        self.clock.monotonic += 119
        self.clock.wall += 119
        prepared = self.prepare(current_drive=43, target_drive=42, baseline_source="recent_verified_change")
        self.clock.monotonic += 2
        self.clock.wall += 2
        with self.assertRaises(ValueError):
            self.store.consume(prepared["change_token"])

    def test_stale_reversed_and_nonfinite_clocks_consume_without_replay(self):
        for clock_name, value in [("monotonic", 1121), ("wall", 2121),
                                  ("monotonic", 999), ("wall", 1999),
                                  ("monotonic", float("nan")), ("wall", float("inf"))]:
            self.clock = Clock()
            self.store = self.clock.store()
            token = self.prepare()["change_token"]
            setattr(self.clock, clock_name, value)
            with self.subTest(clock=clock_name, value=value), self.assertRaises(ValueError):
                self.store.consume(token)
            with self.assertRaises(ValueError):
                self.store.consume(token)

    def test_lease_contention_and_release(self):
        with tempfile.TemporaryDirectory(prefix="bandbox-drive-tests-") as directory:
            root = Path(directory)
            with device_lease(root):
                with self.assertRaisesRegex(ValueError, "Another BandBox operation"):
                    with device_lease(root):
                        self.fail("Competing lease acquired")
            with device_lease(root):
                pass


def native_events(outcome="changedDeviceReported", writes=1):
    return ([{"event": "drive_test_send", "setting_write": True}] * writes + [{
        "event": "drive_test_result", "outcome": outcome,
        "setting_writes": writes, "reason": "Synthetic offline result",
    }])


class ServerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="bandbox-drive-tests-")
        self.addCleanup(self.directory.cleanup)
        self.clock = Clock()
        self.patches = [
            patch.object(server, "RUNTIME", Path(self.directory.name)),
            patch.object(server, "lock", asyncio.Lock()),
            patch.object(server, "pending_changes", self.clock.store()),
            patch.object(server, "_run_probe", AsyncMock(return_value=reading())),
            patch.object(server.asyncio, "create_subprocess_exec", AsyncMock(
                side_effect=AssertionError("Unexpected native call in offline test"))),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def prepare(self, target=43, current=40):
        return await server.bandbox_prepare_drive_change(target, current, "American Clean", True)

    def process(self, events=None, *, code=0, raw=None, failure=None):
        if raw is None:
            raw = ("\n".join(json.dumps(e) for e in events) + "\n").encode()
        process = Mock()
        process.returncode = code
        process.communicate = AsyncMock(return_value=(raw, b""), side_effect=failure)
        process.wait = AsyncMock(return_value=code)
        process.kill = Mock()
        server.asyncio.create_subprocess_exec.side_effect = None
        server.asyncio.create_subprocess_exec.return_value = process
        return process

    async def test_invalid_request_rejected_before_any_read_or_native_call(self):
        with self.assertRaises(ValueError):
            await self.prepare(target=True)
        server._run_probe.assert_not_awaited()
        server.asyncio.create_subprocess_exec.assert_not_awaited()

    async def test_noop_only_performs_read_and_returns_no_apply_token(self):
        result = await self.prepare(target=40)
        self.assertEqual(result["status"], "no_change_needed")
        self.assertEqual(result["setting_writes"], 0)
        self.assertNotIn("change_token", result)
        server._run_probe.assert_awaited_once_with("current")
        server.asyncio.create_subprocess_exec.assert_not_awaited()

    async def test_saved_metadata_mismatch_stops_before_native_mutation(self):
        data = reading()
        data["preset_info"]["slots"][0]["config_id"] = 1
        server._run_probe.return_value = data
        with self.assertRaises(ValueError):
            await self.prepare()
        server.asyncio.create_subprocess_exec.assert_not_awaited()

    async def test_stale_token_rejected_before_process_launch(self):
        prepared = await self.prepare()
        self.clock.monotonic += 121
        with self.assertRaises(ValueError):
            await server.bandbox_apply_drive_change(prepared["change_token"])
        server.asyncio.create_subprocess_exec.assert_not_awaited()

    async def test_lease_contention_blocks_prepare_and_apply_without_retry(self):
        prepared = await self.prepare()
        server._run_probe.reset_mock()
        with device_lease(server.RUNTIME):
            with self.assertRaisesRegex(ValueError, "Another BandBox operation"):
                await self.prepare()
            with self.assertRaisesRegex(ValueError, "Another BandBox operation"):
                await server.bandbox_apply_drive_change(prepared["change_token"])
        server._run_probe.assert_not_awaited()
        server.asyncio.create_subprocess_exec.assert_not_awaited()
        with self.assertRaises(ValueError):
            await server.bandbox_apply_drive_change(prepared["change_token"])

    async def test_success_is_one_target_process_and_token_cannot_be_replayed(self):
        prepared = await self.prepare()
        self.process(native_events())
        result = await server.bandbox_apply_drive_change(prepared["change_token"])
        self.assertEqual(result["status"], "changed_device_reported")
        self.assertTrue(result["change_verified"])
        self.assertEqual((result["setting_writes"], result["live_drive"]), (1, 43))
        args = server.asyncio.create_subprocess_exec.call_args.args
        self.assertIn("--set-drive-plan", args)
        self.assertNotIn("--drive-test-plan", args)
        plan = json.loads(Path(args[args.index("--set-drive-plan") + 1]).read_text())
        self.assertEqual(plan["displayed_drive"], 40)
        self.assertEqual(plan["requested_drive"], 43)
        self.assertEqual(plan["original_float_hex"], struct.pack("<f", 40).hex().upper())
        with self.assertRaises(ValueError):
            await server.bandbox_apply_drive_change(prepared["change_token"])
        server.asyncio.create_subprocess_exec.assert_awaited_once()

    async def test_native_failure_or_inconsistent_success_never_succeeds_or_retries(self):
        cases = [
            (native_events("uncertain"), 1),
            (native_events("restoredDeviceReported"), 0),
            (native_events(), 1),
            (native_events(writes=2), 0),
            (native_events() + [{"event": "drive_test_result", "outcome": "changedDeviceReported", "setting_writes": 1}], 0),
            ([{"event": "drive_test_send", "setting_write": True}], 0),
        ]
        for events, code in cases:
            with self.subTest(events=events, code=code):
                server.asyncio.create_subprocess_exec.reset_mock()
                prepared = await self.prepare()
                self.process(events, code=code)
                result = await server.bandbox_apply_drive_change(prepared["change_token"])
                self.assertFalse(result["change_verified"])
                self.assertEqual(result["status"], "uncertain")
                self.assertIsNone(result["live_drive"])
                with self.assertRaises(ValueError):
                    await server.bandbox_apply_drive_change(prepared["change_token"])
                server.asyncio.create_subprocess_exec.assert_awaited_once()

    async def test_success_can_supply_recent_baseline_only_from_requested_target(self):
        prepared = await self.prepare()
        self.process(native_events())
        await server.bandbox_apply_drive_change(prepared["change_token"])
        recent = await server.bandbox_prepare_drive_change(42, 43, "American Clean", True,
                                                            "recent_verified_change")
        plan, _ = server.pending_changes.consume(recent["change_token"])
        self.assertEqual(plan["displayed_drive"], 43)
        self.assertEqual(plan["requested_drive"], 42)
        with self.assertRaises(ValueError):
            await server.bandbox_prepare_drive_change(42, 40, "American Clean", True,
                                                       "recent_verified_change")
        server.asyncio.create_subprocess_exec.assert_awaited_once()

    async def test_uncertain_apply_invalidates_previous_verified_baseline(self):
        prepared = await self.prepare()
        self.process(native_events())
        await server.bandbox_apply_drive_change(prepared["change_token"])
        recent = await server.bandbox_prepare_drive_change(42, 43, "American Clean", True,
                                                            "recent_verified_change")
        self.process(native_events("uncertain"), code=1)
        result = await server.bandbox_apply_drive_change(recent["change_token"])
        self.assertEqual(result["status"], "uncertain")
        with self.assertRaises(ValueError):
            await server.bandbox_prepare_drive_change(42, 43, "American Clean", True,
                                                       "recent_verified_change")
        self.assertEqual(server.asyncio.create_subprocess_exec.await_count, 2)

    async def test_explicit_native_stop_with_zero_writes_is_stopped(self):
        prepared = await self.prepare()
        self.process(native_events("stoppedBeforeWrite", writes=0), code=1)
        result = await server.bandbox_apply_drive_change(prepared["change_token"])
        self.assertEqual(result["status"], "stopped_before_write")
        self.assertFalse(result["change_verified"])
        self.assertEqual(result["setting_writes"], 0)

    async def test_missing_send_record_cannot_hide_native_uncertainty(self):
        for events in [[], [{"event": "drive_test_result", "outcome": "uncertain", "setting_writes": 1}]]:
            with self.subTest(events=events):
                prepared = await self.prepare()
                self.process(events, code=1)
                result = await server.bandbox_apply_drive_change(prepared["change_token"])
                self.assertEqual(result["status"], "uncertain")
                self.assertFalse(result["change_verified"])
                self.assertNotEqual(result["setting_writes"], 0)

    async def test_malformed_native_log_is_uncertain_without_retry(self):
        for raw in [b"{partial", b"\xff"]:
            with self.subTest(raw=raw):
                server.asyncio.create_subprocess_exec.reset_mock()
                prepared = await self.prepare()
                self.process(raw=raw, code=1)
                result = await server.bandbox_apply_drive_change(prepared["change_token"])
                self.assertEqual(result["status"], "uncertain")
                self.assertIsNone(result["setting_writes"])
                server.asyncio.create_subprocess_exec.assert_awaited_once()

    async def test_interruption_never_records_zero_writes_or_retries(self):
        prepared = await self.prepare()
        process = self.process(raw=b"", code=None, failure=TimeoutError())
        with self.assertRaises(TimeoutError):
            await server.bandbox_apply_drive_change(prepared["change_token"])
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
        records = list(server.RUNTIME.glob("*.interrupted.json"))
        self.assertEqual(len(records), 1)
        result = json.loads(records[0].read_text())
        self.assertEqual(result["outcome"], "uncertain")
        self.assertIsNone(result["setting_writes"])
        with self.assertRaises(ValueError):
            await server.bandbox_apply_drive_change(prepared["change_token"])
        server.asyncio.create_subprocess_exec.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
