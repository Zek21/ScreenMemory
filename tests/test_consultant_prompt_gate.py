import unittest
from unittest.mock import patch


class TestConsultantPromptGate(unittest.TestCase):
    def test_gate_blocks_real_prompt_when_test_is_not_delivered(self):
        import tools.skynet_consultant_prompt_gate as gate

        with patch.object(gate, "_route_snapshot", return_value={"consultant_id": "consultant", "hwnd_valid": False}), \
             patch.object(gate, "deliver_to_consultant", return_value={
                 "success": False,
                 "delivery_status": "queued",
                 "detail": "ghost_type=no_hwnd_or_failed",
             }) as deliver_mock:
            result = gate.run_gate(
                consultant_id="consultant",
                test_prompt="HWND TEST",
                real_prompt="REAL TASK",
            )

        self.assertTrue(result["blocked"])
        self.assertFalse(result["real_prompt_sent"])
        self.assertEqual(deliver_mock.call_count, 1)
        self.assertIn("aborted", result["block_reason"].lower())

    def test_gate_sends_real_prompt_after_successful_test(self):
        import tools.skynet_consultant_prompt_gate as gate

        deliver_results = [
            {"success": True, "delivery_status": "delivered", "detail": "ghost_type=delivered"},
            {"success": True, "delivery_status": "delivered", "detail": "ghost_type=delivered"},
        ]
        with patch.object(gate, "_route_snapshot", return_value={"consultant_id": "consultant", "hwnd_valid": True}), \
             patch.object(gate, "deliver_to_consultant", side_effect=deliver_results) as deliver_mock:
            result = gate.run_gate(
                consultant_id="consultant",
                test_prompt="HWND TEST",
                real_prompt="REAL TASK",
            )

        self.assertFalse(result["blocked"])
        self.assertTrue(result["real_prompt_sent"])
        self.assertEqual(deliver_mock.call_count, 2)
        self.assertEqual(result["real_prompt_result"]["delivery_status"], "delivered")


if __name__ == "__main__":
    unittest.main()
