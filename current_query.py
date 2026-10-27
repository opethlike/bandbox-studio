"""Correlate the read-only CH3 selection/detail query sequence.

JBL semantic query features use command 2 on the wire. A command-2 transport
acknowledgement is not a state response. Only complete direct command-1/3
responses following each exact query are accepted here. There is no transaction
ID in this observed framing: matching is by event order, feature, channel and
preset ID, not proof of freshness of unsaved/live-preview parameters.
"""

import re
import struct

from current_preset import parse_preset_info, parse_selection
from protocol import decode_frame


RX = "65786365-6C70-6F69-6E74-2E636F6D0001"
SELECTION_QUERY_PREFIX = "00DD02000100050041340100"
DETAIL_QUERY_PREFIX = "00DD02000100050043340100"


def selection_query(channel: int = 3) -> str:
    if channel not in (1, 3):
        raise ValueError("channel must be 1 (Solo) or 3 (Trio)")
    return SELECTION_QUERY_PREFIX + f"{channel:02X}"


def current_result(events: list[dict], channel: int = 3) -> dict:
    """Return a matched selection and app-classified saved preset details.

    Unrelated features/channels/preset IDs cannot complete the sequence. A
    malformed or ambiguous response invalidates verification for this event
    batch. Raw source events remain the caller's responsibility to retain.
    """
    query = selection_query(channel)
    result = {
        "current_preset_read_verified": False,
        "selection": None,
        "preset_info": None,
        "current_read_errors": [],
    }
    phase = "before_query"
    pending = None
    detail_id = None
    info = None

    def error(message: str, event: dict | None = None) -> None:
        item = {"error": message}
        if event is not None:
            item["time"] = event.get("time")
        result["current_read_errors"].append(item)

    for event in events:
        if event.get("event") == "query":
            purpose = event.get("purpose")
            query_hex = event.get("hex")
            if purpose not in ("get_channel3_selection_3441", "get_preset_detail_3443"):
                if phase != "before_query":
                    error("unexpected query interrupted current-preset read", event)
                    pending = None
                    phase = "invalid"
                continue
            if pending is not None:
                error("query interrupted an incomplete fragmented response", event)
                pending = None
            if purpose == "get_channel3_selection_3441":
                if phase != "before_query" or query_hex != query:
                    error("invalid or repeated selection query", event)
                    phase = "invalid"
                else:
                    phase = "selection"
                continue
            if (phase != "selected" or not isinstance(query_hex, str)
                    or re.fullmatch(DETAIL_QUERY_PREFIX + "[0-9A-F]{2}", query_hex) is None):
                error("preset detail query lacks an exact preceding CH3 selection", event)
                phase = "invalid"
                continue
            queried_id = int(query_hex[-2:], 16)
            if queried_id != result["selection"]["preset_id"]:
                error("detail query preset ID differs from selected CH3 preset", event)
                phase = "invalid"
                continue
            detail_id = queried_id
            phase = "detail"
            continue

        if (phase in ("before_query", "invalid") or event.get("event") != "notification"
                or event.get("uuid") != RX or event.get("error") is not None):
            continue
        try:
            raw = bytes.fromhex(event["hex"])
            frame = decode_frame(raw)
            if frame["command"] not in (1, 3):
                # In particular, command 2 cannot verify selection or details.
                continue
            count, index = frame["packet_count"], frame["packet_index"]
            identity = (frame["forwarded"], frame["command"], count)
            if pending is not None:
                if identity != pending["identity"] or index != len(pending["parts"]):
                    raise ValueError("inconsistent, missing, duplicate or reordered response fragment")
                pending["parts"].append(raw[8:])
                if index != count - 1:
                    continue
                payload = b"".join(pending["parts"])
                if len(payload) > 65535:
                    raise ValueError("preset response exceeds bounded decoder size")
                # Each original fragment contains a slice of one payload. The
                # two-byte status is parsed once, only after concatenation.
                frame = decode_frame(struct.pack("<HHBBH", 0xDD00, frame["command"], 1, 0, len(payload)) + payload)
                pending = None
            else:
                if frame["forwarded"]:
                    continue
                if count > 1:
                    if index != 0:
                        raise ValueError("fragmented response started without index zero")
                    pending = {"identity": identity, "parts": [raw[8:]]}
                    continue
            if frame.get("status") != 0:
                raise ValueError("device returned nonzero status for preset response")
            selection_values = [f for f in frame["features"] if f["id"] == "0x3442"]
            detail_values = [f for f in frame["features"] if f["id"] == "0x3444"]
            if len(selection_values) > 1 or len(detail_values) > 1:
                raise ValueError("duplicate current-preset feature in one response")
            if selection_values:
                selected = parse_selection(bytes.fromhex(selection_values[0]["value_hex"]))
                if selected["channel"] == channel:
                    if phase == "selection":
                        result["selection"] = selected
                        phase = "selected"
                    elif selected["preset_id"] != result["selection"]["preset_id"]:
                        raise ValueError("CH3 selection changed during the preset read")
            if phase == "detail" and detail_values:
                decoded = parse_preset_info(bytes.fromhex(detail_values[0]["value_hex"]))
                if decoded["preset_id"] == detail_id:
                    info = {
                        **decoded,
                        "settings_kind": "preset_info_classified_as_saved_by_app",
                        "live_preview_verified": False,
                        "saved_vs_preview_semantics": "not_verified_on_hardware",
                    }
                    phase = "complete"
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            error(str(exc), event)
            pending = None
            phase = "invalid"

    if pending is not None:
        error("incomplete fragmented preset response")
    if info is not None and not result["current_read_errors"]:
        result["preset_info"] = info
        result["current_preset_read_verified"] = True
    return result
