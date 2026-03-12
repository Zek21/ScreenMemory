#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import skynet_self_prompt as self_prompt


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

    def test_ollama_gate_stays_closed_on_final_shot(self):
        self.daemon.max_consecutive = 2
        self.daemon.consecutive_prompts = 1
        self.assertFalse(self.daemon._should_use_ollama_for_prompt())

    def test_synthesize_prompt_skips_ollama_before_final_shot(self):
        self.daemon.max_consecutive = 2
        self.daemon.consecutive_prompts = 0
        with patch.object(self.daemon, "_try_ollama_prompt") as ollama_mock, \
             patch.object(self.daemon, "_filter_undelivered", return_value=[]), \
             patch.object(self.daemon, "_collect_daemon_health", return_value="verified_ok"), \
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

    def test_synthesize_prompt_stays_template_only_on_final_shot(self):
        self.daemon.max_consecutive = 2
        self.daemon.consecutive_prompts = 1
        with patch.object(self.daemon, "_try_ollama_prompt") as ollama_mock, \
             patch.object(self.daemon, "_filter_undelivered", return_value=[]), \
             patch.object(self.daemon, "_collect_daemon_health", return_value="verified_ok"), \
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

    def test_ollama_helper_is_disabled(self):
        result = self.daemon._try_ollama_prompt(
            {
                "workers": self.perception["workers"],
                "bus_results": [],
                "bus_alerts": [],
                "pending_todos": 5,
            },
            {"stall_pattern": False, "failure_rate_10m": 0, "dispatch_drought": False},
        )
        self.assertIsNone(result)

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
        self.daemon.all_idle_since = 50.0
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

    def test_perceive_bus_ignores_idle_unproductive_monitor_noise(self):
        perception = {"bus_results": [], "bus_alerts": []}
        with patch("skynet_self_prompt._fetch_json", return_value=[
            {
                "id": "alert-idle",
                "sender": "monitor",
                "topic": "orchestrator",
                "type": "alert",
                "content": "IDLE_UNPRODUCTIVE: ALPHA idle 313s with 7 pending tasks. Dispatch work!",
            },
            {
                "id": "alert-real",
                "sender": "monitor",
                "topic": "orchestrator",
                "type": "alert",
                "content": "REALTIME DAEMON DOWN -- data/realtime.json stale or missing",
            },
        ]):
            self.daemon._perceive_bus(perception)
        self.assertEqual(len(perception["bus_alerts"]), 1)
        self.assertEqual(perception["bus_alerts"][0]["id"], "alert-real")

    def test_has_actionable_rejects_idle_backlog_noise(self):
        actionable = self.daemon._has_actionable(
            [],
            [],
            ["alpha", "beta"],
            2,
            [],
            {"stall_pattern": False, "failure_rate_10m": 0},
            "verified_ok",
            self.perception,
        )
        self.assertFalse(actionable)
        # signed: consultant

    def test_has_actionable_rejects_idle_unproductive_alert_noise(self):
        actionable = self.daemon._has_actionable(
            [],
            [{
                "sender": "monitor",
                "topic": "orchestrator",
                "type": "alert",
                "content": "IDLE_UNPRODUCTIVE: ALPHA idle 313s with 7 pending tasks. Dispatch work!",
            }],
            ["alpha", "beta", "gamma", "delta"],
            7,
            [],
            {"stall_pattern": False, "failure_rate_10m": 0},
            "verified_ok",
            self.perception,
        )
        self.assertFalse(actionable)

    def test_has_actionable_accepts_orchestrator_todos(self):
        actionable = self.daemon._has_actionable(
            [],
            [],
            ["alpha"],
            0,
            [{"id": "orch-1", "task": "Review latest worker result"}],
            {"stall_pattern": False, "failure_rate_10m": 0},
            "verified_ok",
            self.perception,
        )
        self.assertTrue(actionable)

    def test_prompt_state_hash_normalizes_alert_timers(self):
        base_patterns = {"stall_pattern": False, "failure_rate_10m": 0}
        alert_a = [{
            "sender": "idle_monitor",
            "topic": "orchestrator",
            "type": "alert",
            "content": "IDLE_UNPRODUCTIVE: ALPHA idle 355s with 2 TODOs",
        }]
        alert_b = [{
            "sender": "idle_monitor",
            "topic": "orchestrator",
            "type": "alert",
            "content": "IDLE_UNPRODUCTIVE: ALPHA idle 320s with 2 TODOs",
        }]
        hash_a = self.daemon._build_prompt_state_hash(
            self.perception,
            base_patterns,
            [],
            alert_a,
            2,
            [],
            "verified_ok",
        )
        hash_b = self.daemon._build_prompt_state_hash(
            self.perception,
            base_patterns,
            [],
            alert_b,
            2,
            [],
            "verified_ok",
        )
        self.assertEqual(hash_a, hash_b)

    def test_duplicate_prompt_suppresses_repeated_state_for_fifteen_minutes(self):
        self.daemon.pending_prompt_state_hash = "state-1"
        self.daemon.last_prompt_state_hash = "state-1"
        self.daemon.last_prompt_state_hash_time = 1000.0
        self.assertTrue(self.daemon._is_duplicate_prompt("prompt-a", 1500.0))
        self.assertFalse(self.daemon._is_duplicate_prompt("prompt-b", 1901.0))

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
        self.assertEqual(health["min_prompt_gap_s"], self_prompt.MIN_PROMPT_GAP)
        self.assertEqual(health["all_idle_interval_s"], self_prompt.ALL_IDLE_INTERVAL)
        self.assertEqual(
            health["repeated_state_suppression_s"],
            self_prompt.REPEATED_STATE_SUPPRESSION_S,
        )
        self.assertEqual(health["boot_prompt_enabled"], self_prompt.BOOT_PROMPT_ENABLED)

    def test_boot_prompt_skips_when_nothing_actionable(self):
        with patch("skynet_self_prompt._get_orch_hwnd", return_value=123), \
             patch("skynet_self_prompt._is_window_alive", return_value=True), \
             patch("skynet_self_prompt._get_worker_state", return_value="IDLE"), \
             patch.object(self.daemon, "_gather_intelligence", return_value=[""]), \
             patch("skynet_self_prompt._send_self_prompt") as send_mock:
            self.daemon._boot_prompt()
        send_mock.assert_not_called()

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
