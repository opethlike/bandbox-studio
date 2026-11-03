"""Exercise stdio MCP; optional live flags call read-only device tools.

Live writes (deliberate opt-in, each changes something on the amp):
  --live-select-preset <preset_id>
  --live-set-effect-parameter <preset_id> <slot_id> <parameter_id> <ui_value>
  --live-set-mixer-channel-volume <mixer_channel> <level>
  --live-drive-change <target_drive> <current_drive>
    Chains prepare_drive_change + apply_drive_change (the AMP-slot
    decrement/restore round trip). Assumes the AMP block is currently
    "American Clean" and active -- it is whatever preset is selected
    right now; adjust the hardcoded amp_model below if that's not it.
  --live-create-preset <source_preset_id>
    Duplicates the currently-selected source_preset_id as a new user
    preset named "Solo CRUD Test". Prints the new preset_id.
  --live-rename-preset <preset_id>
    Renames a USER preset (ID 51+) to "Solo CRUD Renamed".
  --live-save-user-preset <preset_id>
    Saves current live edits into a USER preset (ID 51+). Requires
    --live-select-preset or --live-set-effect-parameter for the *same*
    preset_id earlier in this same command, establishing a same-process
    preview this script can actually verify -- refused before sending
    anything otherwise (selecting here itself would discard pending
    edits, and a preset only selected on the amp can't be confirmed from
    this process).
  --live-looper [record_seconds]
    Starts a freehand looper recording through the full MCP path, waits
    record_seconds (default 4.0) for the amp to actually capture audio,
    then stops it and checks recorded_audio_reported. The looper must be
    empty and stopped beforehand (play/clear it on the amp first if not).
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent


async def live_write(session, output, name, args, verified_key="verified", output_key=None):
    output_key = output_key or name
    result = await session.call_tool(name, args)
    assert not result.isError, result
    data = json.loads(next(c.text for c in result.content if c.type == "text"))
    output["results"][output_key] = data
    if not data.get(verified_key):
        output.setdefault("failed_tools", []).append(output_key)
    return data


def save_preset_guard(established_preview_preset_id, preset_id):
    """None means proceed with the save; otherwise a refusal result for it,
    sent without calling bandbox_save_user_preset at all. Selecting here
    would discard pending edits, and a preset only selected on the amp
    can't be confirmed from this process -- so without a same-process
    preview already verified for this exact preset_id, the save is
    refused up front rather than sent and reported as a failure only
    after it may have already overwritten the preset."""
    if established_preview_preset_id == preset_id:
        return None
    return {
        "verified": False, "setting_writes": 0,
        "error": (f"No same-process preview for preset_id {preset_id}; combine with "
                 f"--live-select-preset {preset_id} or --live-set-effect-parameter "
                 f"{preset_id} ... earlier in this command."),
    }


async def main():
    # The mcp SDK's StdioServerParameters doesn't inherit the caller's
    # environment by default (only a fixed safe-list) -- forward it
    # explicitly so BANDBOX_GUITAR_CHANNEL/BANDBOX_DEVICE_UUID/etc. reach
    # server.py the same way they would through any real MCP client config.
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")],
                                   env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            reads = {"bandbox_inspect", "bandbox_read_state", "bandbox_list_presets", "bandbox_read_current_preset"}
            readonly = reads | {"bandbox_prepare_drive_change", "bandbox_effect_catalog",
                                "bandbox_list_controls", "bandbox_read_tool", "bandbox_read_preset"}
            writes = {"bandbox_apply_drive_change", "bandbox_set_tool", "bandbox_control_looper", "bandbox_set_mixer_volume", "bandbox_select_preset",
                      "bandbox_create_preset", "bandbox_save_as", "bandbox_save_user_preset",
                      "bandbox_rename_preset", "bandbox_set_effect_parameter",
                      "bandbox_set_effect_model", "bandbox_set_effect_active"}
            assert names == readonly | writes, names
            assert all(t.annotations.readOnlyHint == (t.name in readonly) for t in tools.tools)
            output = {"server": init.serverInfo.model_dump(), "tools": sorted(names), "results": {}}
            # These catalogs are local files/code. The default check never
            # launches the native Bluetooth helper or reads device state.
            for name, required in (("bandbox_effect_catalog", {"models", "slots"}),
                                   ("bandbox_list_controls", {"readable_controls", "mixer_controls"})):
                result = await session.call_tool(name, {})
                assert not result.isError, result
                data = json.loads(next(c.text for c in result.content if c.type == "text"))
                assert required <= data.keys(), name
                output["results"][name] = {"validated": True}
            if any(a in sys.argv for a in ("--live", "--live-presets", "--live-current")):
                failures = []
                selected = {"bandbox_list_presets"} if "--live-presets" in sys.argv else reads
                if "--live-current" in sys.argv:
                    selected = {"bandbox_read_current_preset"}
                for name in sorted(selected):
                    result = await session.call_tool(name, {})
                    assert not result.isError, result
                    data = json.loads(next(c.text for c in result.content if c.type == "text"))
                    output["results"][name] = data
                    if (data["exit_code"] != 0 or not data["connected"]
                            or (name == "bandbox_read_state" and not data["query_response_verified"])
                            or (name == "bandbox_list_presets" and not data["preset_list_verified"])
                            or (name == "bandbox_read_current_preset" and not data["current_preset_read_verified"])):
                        failures.append(name)
                output["failed_tools"] = failures
            established_preview_preset_id = None
            if "--live-select-preset" in sys.argv:
                i = sys.argv.index("--live-select-preset")
                preset_id = int(sys.argv[i + 1])
                data = await live_write(session, output, "bandbox_select_preset", {"preset_id": preset_id})
                if data.get("verified"):
                    established_preview_preset_id = preset_id
            if "--live-set-effect-parameter" in sys.argv:
                i = sys.argv.index("--live-set-effect-parameter")
                preset_id, slot_id, parameter_id, ui_value = (int(a) for a in sys.argv[i + 1:i + 5])
                data = await live_write(session, output, "bandbox_set_effect_parameter",
                                        {"expected_preset_id": preset_id, "slot_id": slot_id,
                                         "parameter_id": parameter_id, "ui_value": ui_value,
                                         "confirm_saved_settings": True})
                if data.get("verified"):
                    established_preview_preset_id = preset_id
            if "--live-set-mixer-channel-volume" in sys.argv:
                i = sys.argv.index("--live-set-mixer-channel-volume")
                mixer_channel, level = (int(a) for a in sys.argv[i + 1:i + 3])
                await live_write(session, output, "bandbox_set_mixer_volume",
                                 {"scope": "channel", "level": level, "channel": mixer_channel})
            if "--live-drive-change" in sys.argv:
                i = sys.argv.index("--live-drive-change")
                target_drive, current_drive = (int(a) for a in sys.argv[i + 1:i + 3])
                prepared = await session.call_tool("bandbox_prepare_drive_change", {
                    "target_drive": target_drive, "current_drive": current_drive,
                    "amp_model": "American Clean", "amp_active": True})
                assert not prepared.isError, prepared
                prep_data = json.loads(next(c.text for c in prepared.content if c.type == "text"))
                output["results"]["bandbox_prepare_drive_change"] = prep_data
                if prep_data.get("status") == "prepared":
                    await live_write(session, output, "bandbox_apply_drive_change",
                                     {"change_token": prep_data["change_token"]},
                                     verified_key="change_verified")
                else:
                    output.setdefault("failed_tools", []).append("bandbox_prepare_drive_change")
            if "--live-create-preset" in sys.argv:
                i = sys.argv.index("--live-create-preset")
                source_preset_id = int(sys.argv[i + 1])
                await live_write(session, output, "bandbox_create_preset",
                                 {"source_preset_id": source_preset_id, "name": "Solo CRUD Test", "genre": 0})
            if "--live-rename-preset" in sys.argv:
                i = sys.argv.index("--live-rename-preset")
                preset_id = int(sys.argv[i + 1])
                await live_write(session, output, "bandbox_rename_preset",
                                 {"preset_id": preset_id, "name": "Solo CRUD Renamed"})
            if "--live-save-user-preset" in sys.argv:
                i = sys.argv.index("--live-save-user-preset")
                preset_id = int(sys.argv[i + 1])
                refusal = save_preset_guard(established_preview_preset_id, preset_id)
                if refusal is not None:
                    output.setdefault("failed_tools", []).append("bandbox_save_user_preset")
                    output["results"]["bandbox_save_user_preset"] = refusal
                else:
                    await live_write(session, output, "bandbox_save_user_preset",
                                     {"expected_preset_id": preset_id})
            if "--live-looper" in sys.argv:
                i = sys.argv.index("--live-looper")
                record_seconds = (float(sys.argv[i + 1])
                                  if len(sys.argv) > i + 1 and not sys.argv[i + 1].startswith("--")
                                  else 4.0)
                start = await live_write(session, output, "bandbox_control_looper",
                                         {"action": "start_recording"},
                                         output_key="bandbox_control_looper_start")
                if start.get("verified"):
                    print(f"recording for {record_seconds}s -- play something now", file=sys.stderr)
                    try:
                        await asyncio.sleep(record_seconds)
                    finally:
                        # Stop even if the delay is interrupted or raises --
                        # otherwise a failed test can leave the amp recording.
                        await live_write(session, output, "bandbox_control_looper",
                                         {"action": "stop_recording"},
                                         output_key="bandbox_control_looper_stop")
                else:
                    output.setdefault("failed_tools", []).append("bandbox_control_looper_stop_skipped")
            print(json.dumps(output, indent=2))
            if output.get("failed_tools"):
                raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
