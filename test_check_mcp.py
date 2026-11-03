"""check_mcp.py's own logic, isolated from the real stdio MCP session it drives."""
import unittest

from check_mcp import save_preset_guard


class SavePresetGuardTests(unittest.TestCase):
    def test_no_established_preview_refuses_without_sending_anything(self):
        # main() only ever calls bandbox_save_user_preset when this returns
        # None -- a non-None result here is exactly zero save writes sent.
        refusal = save_preset_guard(None, 51)
        self.assertIsNotNone(refusal)
        self.assertFalse(refusal["verified"])
        self.assertEqual(refusal["setting_writes"], 0)

    def test_preview_for_a_different_preset_also_refuses(self):
        refusal = save_preset_guard(51, 52)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["setting_writes"], 0)

    def test_matching_same_process_preview_proceeds(self):
        self.assertIsNone(save_preset_guard(51, 51))


if __name__ == "__main__":
    unittest.main()
