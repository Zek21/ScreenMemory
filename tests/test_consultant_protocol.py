import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestConsultantProtocol(unittest.TestCase):
    def setUp(self):
        import tools.skynet_consultant_protocol as protocol

        self.protocol = protocol
        self.tmpdir = tempfile.TemporaryDirectory(dir=str(self.protocol.ROOT / "data"))
        self.plan_file = Path(self.tmpdir.name) / "plan.md"
        self.plan_file.write_text("# Test Plan\n\nShip the consultant protocol.\n", encoding="utf-8")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_select_reviewers_prefers_available_workers(self):
        config = {
            "review_worker_pool": ["alpha", "beta", "gamma", "delta"],
            "min_worker_reviewers": 3,
            "prefer_available_workers": True,
        }
        with patch.object(self.protocol, "_load_worker_snapshot", return_value={
            "available_workers": ["gamma", "alpha"],
            "workers": {},
        }):
            reviewers = self.protocol.select_reviewers(config)
        self.assertEqual(reviewers, ["gamma", "alpha", "beta"])

    def test_activate_protocol_queues_publishes_and_dispatches(self):
        fake_run_path = self.protocol.ROOT / "data" / "consultant_protocol_runs" / "test-run.json"
        with patch.object(self.protocol, "queue_plan_to_consultant", return_value={"success": True, "detail": "queued"}), \
             patch.object(self.protocol, "publish_plan_packet", return_value=True), \
             patch.object(self.protocol, "dispatch_cross_validation", return_value=[
                 {"worker": "alpha", "success": True},
                 {"worker": "beta", "success": True},
                 {"worker": "gamma", "success": True},
             ]), \
             patch.object(self.protocol, "persist_run", return_value=fake_run_path):
            result = self.protocol.activate_protocol(
                consultant_id="gemini_consultant",
                title="Consultant Protocol Test",
                plan_file=self.plan_file,
                requested_reviewers=["alpha", "beta", "gamma"],
            )
        self.assertEqual(result["packet"]["consultant_id"], "gemini_consultant")
        self.assertTrue(result["plan_published"])
        self.assertEqual(result["reviewers"], ["alpha", "beta", "gamma"])
        self.assertEqual(result["run_file"], str(fake_run_path.relative_to(self.protocol.ROOT)))

    def test_publish_plan_packet_emits_bus_message(self):
        packet = self.protocol.build_plan_packet(
            title="Consultant Protocol",
            consultant_id="gemini_consultant",
            plan_file=self.plan_file,
            protocol=self.protocol.load_protocol_config(),
        )
        with patch("tools.shared.bus.bus_post", return_value=True) as bus_post:
            ok = self.protocol.publish_plan_packet(packet, self.protocol.load_protocol_config())
        self.assertTrue(ok)
        payload = bus_post.call_args.args[0]
        self.assertEqual(payload["topic"], "planning")
        self.assertEqual(payload["type"], "consultant_plan")
        self.assertIn(packet["id"], payload["content"])

    def test_queue_plan_to_consultant_retries_http_when_delivery_layer_fails(self):
        packet = self.protocol.build_plan_packet(
            title="Consultant Protocol",
            consultant_id="gemini_consultant",
            plan_file=self.plan_file,
            protocol=self.protocol.load_protocol_config(),
        )
        with patch("tools.skynet_delivery.deliver_to_consultant", return_value={"success": False, "detail": "stale"}), \
             patch.object(self.protocol, "_queue_plan_via_bridge_http", return_value={"success": True, "method": "bridge_http_retry"}) as retry:
            result = self.protocol.queue_plan_to_consultant(packet, self.protocol.load_protocol_config())
        self.assertTrue(result["success"])
        self.assertEqual(result["method"], "bridge_http_retry")
        self.assertTrue(retry.called)


if __name__ == "__main__":
    unittest.main()
