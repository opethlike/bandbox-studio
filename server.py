"""Local stdio MCP for BandBox presets, effects and onboard tools."""
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Literal

from protocol import summarize
from guitar_protocol import inventory_result
from current_query import current_result
from drive_control import PendingChanges, device_lease, validate_request, device_revision, mark_device_changed
from runtime_config import runtime_directory, pinned_identifier, guitar_channel

ROOT = Path(__file__).resolve().parent
CONFIG = {"name": "JBL BandBox Trio", "service_uuid": "65786365-6C70-6F69-6E74-2E636F6D0000"}
lock = asyncio.Lock()
PINNED_ID = pinned_identifier()
CHANNEL = guitar_channel()
runtime_id = PINNED_ID
RUNTIME = runtime_directory()
pending_changes = PendingChanges(revision=lambda: device_revision(RUNTIME))
controls = None


def active_identifier():
    global runtime_id
    if PINNED_ID:
        return PINNED_ID
    try:
        runtime_id = str(uuid.UUID((RUNTIME / "device-id.txt").read_text().strip())).upper()
    except (OSError, ValueError):
        pass
    return runtime_id


async def native(mode: str, identifier: str | None, seconds: int, channel: int = CHANNEL) -> tuple:
    args = [str(ROOT / "bin" / "bandbox-probe"), "--seconds", str(seconds)]
    if identifier:
        args += ["--id", identifier]
    if mode == "state":
        args.append("--read-state")
    elif mode == "presets":
        args.append("--read-presets")
    elif mode == "current":
        args += ["--read-current", "--channel", str(channel)]
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=seconds + 5)
    except (TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    events = [json.loads(line) for line in stdout.decode().splitlines() if line]
    return proc.returncode, events, stderr.decode()


async def run_probe(mode: str) -> dict:
    async with lock:
        with device_lease(RUNTIME):
            return await _run_probe(mode)


async def _run_probe(mode: str) -> dict:
    global runtime_id
    recovery = []
    identifier = active_identifier()
    code, events, stderr = (await native(mode, identifier, 12)
                            if identifier else (1, [], "No cached device; discovering."))
    if not summarize(events)["connected"] and not PINNED_ID:
        # An unconfigured or rotated identifier needs read-only discovery.
        # Explicitly pinned identifiers never fall back to a different amp.
        _, discovery, discovery_stderr = await native("inspect", None, 30)
        recovery = events + discovery
        if (summarize(discovery)["connected"] and any(
            e.get("event") == "service" and e.get("uuid") == CONFIG["service_uuid"] for e in discovery
        )):
            runtime_id = next(e["id"] for e in discovery if e.get("event") == "connected")
            RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
            (RUNTIME / "device-id.txt").write_text(runtime_id + "\n")
            if mode in ("state", "presets", "current"):
                code, events, stderr = await native(mode, runtime_id, 12)
            else:
                code, events, stderr = 0, discovery, discovery_stderr
        else:
            code, events, stderr = 1, discovery, discovery_stderr
    result = {
        "target": CONFIG, "active_corebluetooth_uuid": runtime_id, "mode": mode, "exit_code": code,
        "events": events, "recovery_events": recovery, "stderr": stderr, **summarize(events),
        "limitation": "Raw 0x000D maps to battery in JBL One 2.7.9 code; the phone's battery display was not compared. This operation changes no settings.",
    }
    if mode == "presets":
        result.update(inventory_result(events))
        result["limitation"] = "Preset IDs, genres and names follow JBL One 2.7.9's inventory decoder. Active selection and effect values are not part of this inventory read; this operation changes no settings."
    elif mode == "current":
        result.update(current_result(events, channel=CHANNEL))
        result["limitation"] = "The app classifies these details as saved preset information. They are not a verified snapshot of unsaved edits and must not be used alone as a rollback baseline. The two app-proven parameterized reads use command 2, but change no settings."
    if code != 0 or (mode == "state" and not result["query_response_verified"]):
        result["error"] = "Probe did not complete successfully; inspect events for the cause."
    if mode == "presets" and not result["preset_list_verified"]:
        result["error"] = "No complete, valid preset inventory GET reply was received."
    if mode == "current" and not result["current_preset_read_verified"]:
        result["error"] = "No complete, matched selection and preset-detail reply was received."
    return result


async def bandbox_inspect() -> dict:
    """Inspect the configured BandBox Trio's live GATT services and cached Classic records. Changes no amp settings."""
    return await run_probe("inspect")


async def bandbox_read_state() -> dict:
    """Read raw status 0x000D. JBL One code maps it to battery, but the phone battery display was not compared."""
    return await run_probe("state")


async def bandbox_list_presets() -> dict:
    """Read the BandBox's preset IDs, genre codes and names. Does not select, change, save or delete a preset."""
    return await run_probe("presets")


async def bandbox_read_current_preset() -> dict:
    """Read CH3's selected preset and app-classified saved details, including amp/Drive/EQ. Unsaved live edits are not verified. Changes no settings."""
    return await run_probe("current")


async def bandbox_prepare_drive_change(target_drive: int, current_drive: int,
                                       amp_model: str, amp_active: bool,
                                       baseline_source: Literal["user_display", "recent_verified_change"] = "user_display") -> dict:
    """Prepare the user's requested CH3 Drive change, without changing the amp.

    Use a current Drive/model/active state explicitly confirmed by the user
    from the amp or JBL One (user_display), or the preceding successful change
    in this server session (recent_verified_change, server-checked, 120s maximum).
    Saved preset readings are NOT the live baseline. Report known physical/app
    changes; they invalidate prior baseline evidence. Do not chain changes to
    bypass the five-point limit on a user's requested adjustment.
    Ask for missing display values, never invent them or reuse an old confirmation.
    Supports whole-number Drive 0..100, with at most a five-point change. Rechecks
    preset/model metadata. Returns a single-use token valid for two minutes; apply
    that exact token to perform the already requested change. No extra permission
    is needed when the user has already requested the concrete change.
    """
    validate_request(target_drive, current_drive, amp_model, amp_active)
    async with lock:
        with device_lease(RUNTIME):
            reading = await _run_probe("current")
            return pending_changes.prepare(reading, target_drive, current_drive, amp_model, amp_active,
                                           baseline_source)


async def bandbox_apply_drive_change(change_token: str) -> dict:
    """Apply a prepared user-requested Drive change ONCE and verify the amp reply.

    Only call with the exact token from prepare_drive_change. Rechecks selection
    and amp metadata before sending one parameter update. Requires protocol ACK
    and matching live value report. Never retries, selects/saves presets, resets,
    or changes firmware. If uncertain, report that and obtain the current display;
    do not assume success or issue a compensating change automatically.
    """
    async with lock:
        revision = device_revision(RUNTIME)
        plan, summary = pending_changes.consume(change_token)
        pending_changes.invalidate_recent()
        if controls is not None:
            controls.preview = None
        with device_lease(RUNTIME):
            if revision != device_revision(RUNTIME):
                raise ValueError("Another controller changed the amp. Prepare a fresh change.")
            mark_device_changed(RUNTIME)
            operation_id = uuid.uuid4().hex
            plan_path = RUNTIME / f"{operation_id}.plan.json"
            plan_path.write_text(json.dumps(plan, indent=2) + "\n")
            proc = await asyncio.create_subprocess_exec(
                str(ROOT / "bin" / "bandbox-probe"), "--id", plan["device_uuid"],
                "--set-drive-plan", str(plan_path), "--seconds", "45",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=50)
            except (TimeoutError, asyncio.CancelledError):
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
                (RUNTIME / f"{operation_id}.interrupted.json").write_text(json.dumps({
                    **summary, "outcome": "uncertain", "setting_writes": None,
                    "reason": "Interrupted; no retry or rollback attempted"}))
                raise
            (RUNTIME / f"{operation_id}.jsonl").write_bytes(stdout)
            (RUNTIME / f"{operation_id}.stderr").write_bytes(stderr)
            try:
                events = [json.loads(line) for line in stdout.decode().splitlines() if line]
                if not all(isinstance(event, dict) for event in events):
                    raise json.JSONDecodeError("Non-object native event", "", 0)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {**summary, "status": "uncertain", "setting_writes": None,
                        "error": "Incomplete native log; inspect the live display before any further change.",
                        "operation_id": operation_id}
            outcomes = [e for e in events if e.get("event") == "drive_test_result"]
            writes = [e for e in events if e.get("event") == "drive_test_send" and e.get("setting_write")]
            outcome = outcomes[-1] if len(outcomes) == 1 else {}
            verified = (proc.returncode == 0 and len(writes) == 1
                        and outcome.get("outcome") == "changedDeviceReported"
                        and outcome.get("setting_writes") == 1)
            if verified:
                pending_changes.record_verified_change(plan)
            stopped = (outcome.get("outcome") == "stoppedBeforeWrite"
                       and outcome.get("setting_writes") == 0 and not writes)
            write_count = (1 if verified else 0 if stopped else
                           len(writes) if writes and outcome.get("setting_writes") == len(writes) else None)
            result = {**summary, "status": "changed_device_reported" if verified else
                      ("stopped_before_write" if stopped else "uncertain"),
                      "change_verified": verified, "setting_writes": write_count,
                      "reason": outcome.get("reason", stderr.decode(errors="replace") or "No verified setting result"),
                      "operation_id": operation_id,
                      "live_drive": plan["requested_drive"] if verified else None,
                      "limitation": "Device-reported parameter state; no independent audio measurement. Saved preset values can differ."}
            (RUNTIME / f"{operation_id}.result.json").write_text(json.dumps(result, indent=2) + "\n")
            return result


def serve() -> None:
    global controls
    # Direct CLI reads need only the standard library and local decoders. Load
    # the larger MCP dependency tree only when starting the stdio server.
    from mcp.server.fastmcp import FastMCP
    from controls import Controls

    mcp = FastMCP("JBL BandBox Trio", instructions="Control CH3 presets and effects plus onboard tools. Read the effect/control catalogs for supported names and ranges. Saved preset reads do not capture unsaved edits. Select a preset through MCP before editing, or obtain explicit confirmation that live settings match saved settings. Create a separate preset for new tones, edit it, and save that user preset. Report physical/app changes so cached previews are not reused. Never retry an uncertain mutation. No firmware, reset, deletion, or loop discard tools are exposed.")
    for tool in (bandbox_inspect, bandbox_read_state, bandbox_list_presets, bandbox_read_current_preset,
                 bandbox_prepare_drive_change):
        mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False})(tool)
    mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                         "idempotentHint": False, "openWorldHint": False})(bandbox_apply_drive_change)
    controls = Controls(ROOT, RUNTIME, lock, _run_probe, active_identifier,
                        invalidate_drive=pending_changes.invalidate_all, channel=CHANNEL)
    for name, method, read_only in (
        ("bandbox_effect_catalog", controls.catalog, True),
        ("bandbox_list_controls", controls.list_controls, True),
        ("bandbox_read_tool", controls.read_tool, True),
        ("bandbox_read_preset", controls.read_preset, True),
        ("bandbox_set_tool", controls.set_tool, False),
        ("bandbox_control_looper", controls.control_looper, False),
        ("bandbox_set_mixer_volume", controls.set_mixer_volume, False),
        ("bandbox_select_preset", controls.select_preset, False),
        ("bandbox_create_preset", controls.create_preset, False),
        ("bandbox_save_as", controls.save_as, False),
        ("bandbox_save_user_preset", controls.save_user_preset, False),
        ("bandbox_rename_preset", controls.rename_preset, False),
        ("bandbox_set_effect_parameter", controls.set_effect_parameter, False),
        ("bandbox_set_effect_model", controls.set_effect_model, False),
        ("bandbox_set_effect_active", controls.set_effect_active, False),
    ):
        mcp.tool(name=name, annotations={"readOnlyHint": read_only,
            "destructiveHint": name == "bandbox_save_user_preset", "idempotentHint": False,
            "openWorldHint": False})(method)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("inspect", "state", "presets", "current"):
        print(json.dumps(asyncio.run(run_probe(sys.argv[1])), indent=2))
    elif len(sys.argv) == 1:
        serve()
    else:
        raise SystemExit("Usage: server.py [inspect|state|presets|current]")
