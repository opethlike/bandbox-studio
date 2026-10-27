import struct
import unittest

from current_query import DETAIL_QUERY_PREFIX, RX, selection_query, current_result


SELECT_QUERY = {"event": "query", "purpose": "get_channel3_selection_3441", "hex": selection_query()}
DETAIL_QUERY = {"event": "query", "purpose": "get_preset_detail_3443", "hex": DETAIL_QUERY_PREFIX + "01"}
# Saved-detail-shaped fixture: preset1, genre2, Clean, AMP American Clean, Drive25.5.
DETAIL = bytes.fromhex("010205436C65616E01 0306000001 000000CC41")


def feature(feature_id, value):
    return struct.pack("<HH", feature_id, len(value)) + value


def packet(payload, command=3, count=1, index=0, identifier=0xDD00):
    raw = struct.pack("<HHBBH", identifier, command, count, index, len(payload)) + payload
    return {"event": "notification", "uuid": RX, "hex": raw.hex().upper(), "error": None}


def reply(feature_id, value, **kwargs):
    return packet(b"\x00\x00" + feature(feature_id, value), **kwargs)


SELECT = reply(0x3442, b"\x03\x01")
INFO = reply(0x3444, DETAIL)


class CurrentQueryTests(unittest.TestCase):
    def test_direct_get_and_notification_commands(self):
        for command in [1, 3]:
            result = current_result([SELECT_QUERY, reply(0x3442, b"\x03\x01", command=command),
                                     DETAIL_QUERY, reply(0x3444, DETAIL, command=command)])
            self.assertTrue(result["current_preset_read_verified"])
            self.assertEqual(result["selection"]["channel_label"], "CH3")
            self.assertEqual(result["preset_info"]["name"], "Clean")
            self.assertEqual(result["preset_info"]["settings_kind"], "preset_info_classified_as_saved_by_app")
            self.assertFalse(result["preset_info"]["live_preview_verified"])
            self.assertEqual(result["preset_info"]["saved_vs_preview_semantics"], "not_verified_on_hardware")

    def test_ack_alone_never_verifies(self):
        ack = packet(b"\x00\x00", command=2)
        self.assertFalse(current_result([SELECT_QUERY, ack])["current_preset_read_verified"])
        self.assertFalse(current_result([SELECT_QUERY, SELECT, DETAIL_QUERY, ack])["current_preset_read_verified"])

    def test_stale_and_prequery_values_ignored(self):
        for events in [[SELECT, SELECT_QUERY, DETAIL_QUERY, INFO],
                       [SELECT_QUERY, SELECT, INFO, DETAIL_QUERY],
                       [SELECT, INFO], [INFO, SELECT_QUERY, SELECT, DETAIL_QUERY]]:
            with self.subTest(events=events):
                self.assertFalse(current_result(events)["current_preset_read_verified"])

    def test_detail_in_selection_packet_does_not_skip_second_query(self):
        combined = packet(b"\x00\x00" + feature(0x3442, b"\x03\x01") + feature(0x3444, DETAIL))
        result = current_result([SELECT_QUERY, combined, DETAIL_QUERY])
        self.assertIsNotNone(result["selection"])
        self.assertFalse(result["current_preset_read_verified"])

    def test_wrong_channel_preset_and_forwarding_do_not_verify(self):
        for selected in [reply(0x3442, b"\x01\x01"), reply(0x3442, b"\x03\x01", identifier=0xDD01)]:
            self.assertFalse(current_result([SELECT_QUERY, selected, DETAIL_QUERY, INFO])["current_preset_read_verified"])
        for info in [reply(0x3444, b"\x02" + DETAIL[1:]), reply(0x3444, DETAIL, identifier=0xDD01)]:
            self.assertFalse(current_result([SELECT_QUERY, SELECT, DETAIL_QUERY, info])["current_preset_read_verified"])

    def test_unrelated_uuid_error_and_feature_not_responses(self):
        for info in [{**INFO, "uuid": "other"}, {**INFO, "error": "disconnected"}, reply(0x3445, DETAIL)]:
            self.assertFalse(current_result([SELECT_QUERY, SELECT, DETAIL_QUERY, info])["current_preset_read_verified"])

    def test_queries_are_exact_and_ordered(self):
        for first in [{**SELECT_QUERY, "hex": selection_query()[:-2] + "01"},
                      {**SELECT_QUERY, "purpose": "other"}, {**SELECT_QUERY, "hex": selection_query() + "00"}]:
            self.assertFalse(current_result([first, SELECT, DETAIL_QUERY, INFO])["current_preset_read_verified"])
        for second in [{**DETAIL_QUERY, "hex": DETAIL_QUERY_PREFIX + "02"},
                       {**DETAIL_QUERY, "hex": DETAIL_QUERY_PREFIX},
                       {**DETAIL_QUERY, "hex": DETAIL_QUERY_PREFIX + "0100"},
                       {**DETAIL_QUERY, "hex": DETAIL_QUERY_PREFIX + "zz"}]:
            result = current_result([SELECT_QUERY, SELECT, second, INFO])
            self.assertFalse(result["current_preset_read_verified"])
            self.assertTrue(result["current_read_errors"])

    def test_unsigned_preset_ids_in_exact_detail_query(self):
        query = {**DETAIL_QUERY, "hex": DETAIL_QUERY_PREFIX + "E1"}
        result = current_result([SELECT_QUERY, reply(0x3442, b"\x03\xe1"), query,
                                 reply(0x3444, b"\xe1" + DETAIL[1:])])
        self.assertTrue(result["current_preset_read_verified"])
        self.assertEqual(result["preset_info"]["preset_id"], 225)

    def test_fragment_payload_status_and_tlv_split_arbitrarily(self):
        selection = b"\x00\x00" + feature(0x3442, b"\x03\x01")
        details = b"\x00\x00" + feature(0x3444, DETAIL)
        for split in range(1, len(details)):
            events = [SELECT_QUERY, packet(selection[:1], count=2, index=0),
                      packet(selection[1:], count=2, index=1), DETAIL_QUERY,
                      packet(details[:split], count=2, index=0),
                      packet(details[split:], count=2, index=1)]
            with self.subTest(split=split):
                self.assertTrue(current_result(events)["current_preset_read_verified"])

    def test_malformed_fragment_sequences_fail(self):
        body = b"\x00\x00" + feature(0x3444, DETAIL)
        first = packet(body[:7], count=2, index=0)
        last = packet(body[7:], count=2, index=1)
        for tail in [[first], [last, first], [first, first, last],
                     [first, packet(body[7:], count=3, index=1)],
                     [first, packet(body[7:], command=1, count=2, index=1)],
                     [first, packet(body[7:], identifier=0xDD01, count=2, index=1)],
                     [first, INFO], [first, DETAIL_QUERY, last]]:
            with self.subTest(tail=tail):
                result = current_result([SELECT_QUERY, SELECT, DETAIL_QUERY] + tail)
                self.assertFalse(result["current_preset_read_verified"])
                self.assertTrue(result["current_read_errors"])

    def test_fragment_payload_does_not_have_per_fragment_status(self):
        body = b"\x00\x00" + feature(0x3444, DETAIL)
        result = current_result([SELECT_QUERY, SELECT, DETAIL_QUERY,
                                 packet(body[:7], count=2, index=0),
                                 packet(b"\x00\x00" + body[7:], count=2, index=1)])
        self.assertFalse(result["current_preset_read_verified"])
        self.assertTrue(result["current_read_errors"])

    def test_bad_status_duplicate_features_and_bad_detail(self):
        failures = [packet(b"\x01\x00" + feature(0x3444, DETAIL)),
                    packet(b"\x00\x00" + feature(0x3444, DETAIL) * 2),
                    reply(0x3444, DETAIL[:-1]), {**INFO, "hex": "not hex"}]
        for info in failures:
            with self.subTest(info=info):
                result = current_result([SELECT_QUERY, SELECT, DETAIL_QUERY, info])
                self.assertFalse(result["current_preset_read_verified"])
                self.assertIsNone(result["preset_info"])
                self.assertTrue(result["current_read_errors"])

    def test_short_selection_is_not_accepted(self):
        result = current_result([SELECT_QUERY, reply(0x3442, b"\x03"), DETAIL_QUERY, INFO])
        self.assertFalse(result["current_preset_read_verified"])
        self.assertTrue(result["current_read_errors"])

    def test_selection_changes_invalidate_current_match(self):
        changed = reply(0x3442, b"\x03\x02")
        for events in [[SELECT_QUERY, SELECT, DETAIL_QUERY, changed, INFO],
                       [SELECT_QUERY, SELECT, DETAIL_QUERY, INFO, changed]]:
            result = current_result(events)
            self.assertFalse(result["current_preset_read_verified"])
            self.assertIsNone(result["preset_info"])
            self.assertTrue(result["current_read_errors"])

    def test_repeated_query_cannot_reuse_previous_success(self):
        result = current_result([SELECT_QUERY, SELECT, DETAIL_QUERY, INFO, SELECT_QUERY])
        self.assertFalse(result["current_preset_read_verified"])
        self.assertTrue(result["current_read_errors"])

    def test_channel_1_solo_query_and_selection(self):
        solo_query = {**SELECT_QUERY, "hex": selection_query(1)}
        solo_select = reply(0x3442, b"\x01\x01")
        result = current_result([solo_query, solo_select, DETAIL_QUERY, INFO], channel=1)
        self.assertTrue(result["current_preset_read_verified"])
        self.assertEqual(result["selection"]["channel"], 1)
        # The default (channel=3) must reject a channel-1 selection, and vice versa.
        self.assertFalse(current_result([solo_query, solo_select, DETAIL_QUERY, INFO])
                          ["current_preset_read_verified"])
        self.assertFalse(current_result([SELECT_QUERY, SELECT, DETAIL_QUERY, INFO], channel=1)
                          ["current_preset_read_verified"])

    def test_selection_query_rejects_unsupported_channel(self):
        with self.assertRaises(ValueError):
            selection_query(2)


if __name__ == "__main__":
    unittest.main()
