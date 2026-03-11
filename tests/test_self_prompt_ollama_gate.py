#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import skynet_self_prompt as self_prompt
import skynet_ollama_prompt as ollama_prompt


class TestSelfPromptOllamaGate(unittest.TestCase):
    def setUp(self):
        self.daemon = self_prompt.SelfPromptDaemon()
        self.perception = {
            "workers": {
                "alpha": {"state": "IDLE", "alive": True},
                "beta": {"state": "IDLE", "alive": True},
                "gamma": {"state": "IDLE", "alive": True},
                "delta": {"state": "IDLE", "alive": True},
            },
            "bus_results": [],
            "bus_alerts": [],
            "pending_todos": 0,
            "pending_tasks": 0,
        }

    def test_ollama_gate_stays_closed_before_final_shot(self):
        self.daemon.max_consecutive = 2
        self.daemon.consecutive_prompts = 0
        self.assertFalse(self.daemon._should_use_ollama_for_prompt())
        self.daemon.consecutive_prompts = 0
        self.assertFalse(self.daemon._should_use_ollama_for_prompt())

    def test_ollama_gate_opens_on_final_shot(self):
        self.daemon.max_consecutive = 2
        self.daemon.consecutive_prompts = 1
        self.assertTrue(self.daemon._should_use_ollama_for_prompt())

    def test_synthesize_prompt_skips_ollama_before_final_shot(self):
        self.daemon.max_consecutive = 2
        self.daemon.consecutive_prompts = 0
        with patch.object(self.daemon, "_try_ollama_prompt") as ollama_mock, \
             patch.object(self.daemon, "_filter_undelivered", return_value=[]), \
             patch.object(self.daemon, "_collect_daemon_health", return_value="OK"), \
             patch.object(self.daemon, "_build_status_line", return_value="A=IDLE B=IDLE G=IDLE D=IDLE"), \
             patch.object(self.daemon, "_build_actions", return_value=[(90, "Act now")]), \
             patch.object(self.daemon, "_has_actionable", return_value=True):
            result = self.daemon._synthesize_prompt(
                self.perception,
                {"stall_pattern": False, "failure_rate_10m": 0, "dispatch_drought": False},
                [],
                0,
            )
        self.assertIn("[SELF-PROMPT ", result)
        self.assertIn("Act now", result)
        ollama_mock.assert_not_called()

    def test_synthesize_prompt_uses_ollama_on_final_shot(self):
        self.daemon.max_consecutive = 2
        self.daemon.consecutive_prompts = 1
        with patch.object(self.daemon, "_try_ollama_prompt", return_value="Escalate now") as ollama_mock:
            result = self.daemon._synthesize_prompt(
                self.perception,
                {"stall_pattern": False, "failure_rate_10m": 0, "dispatch_drought": False},
                [],
                0,
            )
        self.assertIn("[SELF-PROMPT ", result)
        self.assertIn("Escalate now", result)
        ollama_mock.assert_called_once()

    def test_final_shot_cooldown_blocks_until_gap_elapses(self):
        original_gap = self_prompt.MIN_PROMPT_GAP
        try:
            self_prompt.MIN_PROMPT_GAP = 600
            self.daemon.max_consecutive = 2
            self.daemon.consecutive_prompts = 2
            self.daemon.last_cycle_complete_time = 1000.0
            self.assertTrue(self.daemon._final_shot_cooldown_active(1500.0))
            self.assertEqual(self.daemon.consecutive_prompts, 2)
            self.assertFalse(self.daemon._final_shot_cooldown_active(1600.0))
            self.assertEqual(self.daemon.consecutive_prompts, 0)
            self.assertEqual(self.daemon.last_cycle_complete_time, 0.0)
        finally:
            self_prompt.MIN_PROMPT_GAP = original_gap

    def test_ollama_payload_includes_final_shot_context(self):
        original_gap = self_prompt.MIN_PROMPT_GAP
        self.daemon.max_consecutive = 2
        self.daemon.consecutive_prompts = 1
        self_prompt.MIN_PROMPT_GAP = 600
        with patch("tools.skynet_ollama_prompt.generate_technical_prompt", return_value="Escalate now") as gen_mock:
            result = self.daemon._try_ollama_prompt(
                {
                    "workers": self.perception["workers"],
                    "bus_results": [],
                    "bus_alerts": [],
                    "pending_todos": 5,
                },
                {"stall_pattern": False, "failure_rate_10m": 0, "dispatch_drought": False},
            )
        self.assertIn("Escalate now", result)
        payload = gen_mock.call_args.kwargs
        self.assertEqual(payload["model"], self_prompt.OLLAMA_MODEL)
        shot_context = gen_mock.call_args.args[0]["shot_context"]
        self.assertEqual(shot_context["mode"], "final_shot")
        self.assertEqual(shot_context["shot_number"], 2)
        self.assertEqual(shot_context["max_shots"], 2)
        self.assertEqual(shot_context["cooldown_after_s"], 600)
        self.assertIn("timestamp", gen_mock.call_args.args[0])
        self.assertIn("skynet_purpose", gen_mock.call_args.args[0])
        self_prompt.MIN_PROMPT_GAP = original_gap

    def test_ollama_user_prompt_mentions_final_shot_context(self):
        user_prompt = ollama_prompt._build_user_prompt({
            "timestamp": "2026-03-11T23:30:00+08:00",
            "skynet_purpose": "Serve GOD by prioritizing truthful, high-value worker coordination.",
            "shot_context": {"mode": "final_shot", "shot_number": 2, "max_shots": 2, "cooldown_after_s": 600},
            "workers": self.perception["workers"],
            "new_results": [],
            "new_alerts": [],
            "pending_todos": 7,
            "orch_todos": [{"priority": "high", "task": "Validate the most important worker result"}],
            "daemon_status": "monitor=stale",
            "patterns": {"stall_pattern": False, "failure_rate_10m": 0, "dispatch_drought": False},
        })
        self.assertIn("Timestamp: 2026-03-11T23:30:00+08:00", user_prompt)
        self.assertIn("Skynet Purpose: Serve GOD by prioritizing truthful, high-value worker coordination.", user_prompt)
        self.assertIn("mode=final_shot", user_prompt)
        self.assertIn("shot=2/2", user_prompt)
        self.assertIn("cooldown_after_s=600", user_prompt)
        self.assertIn("final self-prompt shot before cooldown", user_prompt)

    def test_format_prompt_adds_timestamp_prefix(self):
        result = self.daemon._format_prompt("2026-03-11T23:30:00+08:00", "Skynet Intel: Example")
        self.assertEqual(
            result,
            "[SELF-PROMPT 2026-03-11T23:30:00+08:00] Skynet Intel: Example",
        )

    def test_check_and_prompt_requires_continuous_all_idle_window(self):
        self.daemon.all_idle_since = 950.0
        with patch.object(self.daemon, "_report_health"), \
             patch.object(self.daemon, "_fetch_worker_status_snapshot", return_value={
                 "alpha": "IDLE", "beta": "IDLE", "gamma": "IDLE", "delta": "IDLE"
             }), \
             patch.object(self.daemon, "_scan_triggers") as scan_mock, \
             patch("skynet_self_prompt.time.time", return_value=1000.0):
            ok = self.daemon.check_and_prompt(deliver_queue_first=False)
        self.assertFalse(ok)
        scan_mock.assert_not_called()

    def test_check_and_prompt_aborts_when_worker_turns_busy_before_fire(self):
        self.daemon.all_idle_since = 400.0
        first_snapshot = {"alpha": "IDLE", "beta": "IDLE", "gamma": "IDLE", "delta": "IDLE"}
        second_snapshot = {"alpha": "IDLE", "beta": "IDLE", "gamma": "PROCESSING", "delta": "IDLE"}
        with patch.object(self.daemon, "_report_health"), \
             patch.object(self.daemon, "_orchestrator_took_action", return_value=False), \
             patch.object(self.daemon, "_final_shot_cooldown_active", return_value=False), \
             patch.object(self.daemon, "_scan_triggers", return_value=(True, ["alert"], "Skynet Intel: test", 75)), \
             patch("skynet_self_prompt._get_orch_hwnd", return_value=123), \
             patch.object(self.daemon, "_check_orch_typing", return_value=False), \
             patch.object(self.daemon, "_is_duplicate_prompt", return_value=False), \
             patch.object(self.daemon, "_fetch_worker_status_snapshot", side_effect=[first_snapshot, second_snapshot]), \
             patch("skynet_self_prompt._send_self_prompt") as send_mock, \
             patch("skynet_self_prompt.time.time", side_effect=[1000.0, 1600.0]):
            ok = self.daemon.check_and_prompt(deliver_queue_first=False)
        self.assertFalse(ok)
        send_mock.assert_not_called()
        self.assertEqual(self.daemon.all_idle_since, 0.0)

    def test_perceive_bus_ignores_self_prompt_alert_noise(self):
        perception = {"bus_results": [], "bus_alerts": []}
        with patch("skynet_self_prompt._fetch_json", return_value=[
            {
                "id": "alert-self",
                "sender": "self_prompt",
                "topic": "orchestrator",
                "type": "monitor_alert",
                "content": "SELF_PROMPT_ONLINE v1.2.0: daemon started",
            },
            {
                "id": "alert-real",
                "sender": "monitor",
                "topic": "orchestrator",
                "type": "alert",
                "content": "MONITOR_STALE: no fresh heartbeat",
            },
        ]):
            self.daemon._perceive_bus(perception)
        self.assertEqual(len(perception["bus_alerts"]), 1)
        self.assertEqual(perception["bus_alerts"][0]["id"], "alert-real")

    def test_health_payload_includes_version_fields(self):
        with patch("skynet_self_prompt._get_orch_hwnd", return_value=67568), \
             patch("skynet_self_prompt._get_skynet_version", return_value="3.0"), \
             patch("skynet_self_prompt.os.getpid", return_value=12345):
            health = self.daemon._build_health_payload(now=1000.0)
        self.assertEqual(health["daemon"], "self_prompt")
        self.assertEqual(health["daemon_version"], self_prompt.DAEMON_VERSION)
        self.assertEqual(health["skynet_version"], "3.0")
        self.assertEqual(health["orchestrator_hwnd"], 67568)
        self.assertEqual(health["pid"], 12345)

    def test_worker_status_snapshot_prefers_live_uia_over_backend_idle_claims(self):
        fake_workers = [
            {"name": "alpha", "hwnd": 101},
            {"name": "beta", "hwnd": 102},
            {"name": "gamma", "hwnd": 103},
            {"name": "delta", "hwnd": 104},
        ]
        states = {101: "IDLE", 102: "PROCESSING", 103: "IDLE", 104: "IDLE"}
        with patch("skynet_self_prompt._load_workers", return_value=fake_workers), \
             patch("skynet_self_prompt._is_window_alive", return_value=True), \
             patch("skynet_self_prompt._get_worker_state", side_effect=lambda hwnd: states[hwnd]):
            snapshot = self.daemon._fetch_worker_status_snapshot()
        self.assertEqual(snapshot["beta"], "PROCESSING")
        self.assertFalse(self.daemon._snapshot_all_workers_idle(snapshot))


if __name__ == "__main__":
    unittest.main()
