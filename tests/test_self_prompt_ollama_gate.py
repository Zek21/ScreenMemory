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
        self.assertIn("Loop: poll bus -> check worker states -> dispatch the highest-priority real work -> collect results -> repeat until TODOs and queued tasks are zero.", result)
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
             patch.object(self.daemon, "_check_orch_typing", return_value=(False, "orch_not_typing")), \
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
        self.assertIn("next_eligible_timestamp", health)
        self.assertIn("missed_fire_pending", health)
        self.assertIn("missed_fire_consultant_notified", health)

    def test_health_snapshot_freshness_uses_timestamp_age(self):
        fresh = {"timestamp": "2026-03-15T20:17:52+08:00"}
        stale = {"timestamp": "2026-03-15T20:15:00+08:00"}
        with patch("skynet_self_prompt.datetime") as dt_mock:
            real_datetime = __import__("datetime").datetime
            dt_mock.fromisoformat.side_effect = real_datetime.fromisoformat
            dt_mock.now.return_value = real_datetime.fromisoformat("2026-03-15T20:18:10+08:00")
            self.assertEqual(self_prompt._health_snapshot_age_s(fresh), 18)
            self.assertTrue(self_prompt._health_snapshot_is_fresh(fresh))
            self.assertFalse(self_prompt._health_snapshot_is_fresh(stale))

    def test_match_prompt_visibility_text_accepts_header_plus_content_proof(self):
        prompt = (
            "[SELF-PROMPT 2026-03-15T20:22:41+08:00] "
            "Skynet Intel: A=IDLE || Dispatch idle workers on 2 pending TODO(s). (1/3) "
            "|| Loop: poll bus -> check worker states -> dispatch the highest-priority real work -> collect results -> repeat until TODOs and queued tasks are zero."
        )
        ocr_text = (
            "[SELF-PROM] 2026-03-15 20:22 "
            "Skynet Intel Dispatch idle workers on 2 pending TODO(s) "
            "poll bus check worker states collect results"
        )
        match = self_prompt._match_prompt_visibility_text(ocr_text, prompt)
        self.assertTrue(match["screenshot_verified"])
        self.assertIn("self prom", match["matched_signals"])

    def test_match_prompt_visibility_text_rejects_same_day_without_current_content(self):
        prompt = (
            "[SELF-PROMPT 2026-03-15T20:22:41+08:00] "
            "Skynet Intel: A=IDLE || Dispatch idle workers on 2 pending TODO(s). (1/3) "
            "|| Loop: poll bus -> check worker states -> dispatch the highest-priority real work -> collect results -> repeat until TODOs and queued tasks are zero."
        )
        ocr_text = "[SELF-PROM] 2026-03-15 older prompt unrelated advisory task"
        match = self_prompt._match_prompt_visibility_text(ocr_text, prompt)
        self.assertFalse(match["screenshot_verified"])

    def test_prefer_delivery_proof_picks_verified_match(self):
        weak = {
            "screenshot_verified": False,
            "matched_signals": ["self prom", "2026 03 15"],
        }
        strong = {
            "screenshot_verified": True,
            "matched_signals": ["self prom", "20:22", "poll bus", "collect results"],
            "screenshot_path": "strong.png",
        }
        selected = self_prompt._prefer_delivery_proof(weak, strong)
        self.assertTrue(selected["screenshot_verified"])
        self.assertEqual(selected["screenshot_path"], "strong.png")

    def test_prefer_delivery_proof_uses_stronger_unverified_signal_set(self):
        weaker = {
            "screenshot_verified": False,
            "matched_signals": ["self prom", "2026 03 15"],
        }
        stronger = {
            "screenshot_verified": False,
            "matched_signals": ["self prom", "2026 03 15", "poll bus", "collect results"],
            "crop_label": "shared_bottom_half",
        }
        selected = self_prompt._prefer_delivery_proof(weaker, stronger)
        self.assertEqual(selected["crop_label"], "shared_bottom_half")

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

    def test_check_and_prompt_arms_missed_fire_watch_when_typing_blocks_due_prompt(self):
        self.daemon.all_idle_since = 600.0
        self.daemon.last_prompt_time = 600.0
        with patch.object(self.daemon, "_report_health"), \
             patch.object(self.daemon, "_orchestrator_took_action", return_value=False), \
             patch.object(self.daemon, "_final_shot_cooldown_active", return_value=False), \
             patch.object(self.daemon, "_fetch_worker_status_snapshot", return_value={
                 "alpha": "IDLE", "beta": "IDLE", "gamma": "IDLE", "delta": "IDLE"
             }), \
             patch.object(self.daemon, "_scan_triggers", return_value=(True, ["dispatch_opportunity"], "Skynet Intel: test", 85)), \
             patch("skynet_self_prompt._get_orch_hwnd", return_value=123), \
             patch.object(self.daemon, "_check_orch_typing", return_value=(True, "orch_typing")), \
             patch("skynet_self_prompt.time.time", return_value=901.0):
            ok = self.daemon.check_and_prompt(deliver_queue_first=False)
        self.assertFalse(ok)
        self.assertEqual(self.daemon.missed_fire_reason, "orch_typing")
        self.assertEqual(self.daemon.missed_fire_eligible_at, 900.0)

    def test_missed_fire_alert_notifies_consultant_once(self):
        self.daemon.last_prompt_time = 100.0
        self.daemon.all_idle_since = 200.0
        self.daemon._arm_missed_fire_watch(560.0, "orch_typing", 85, ["dispatch_opportunity"])
        with patch("skynet_self_prompt._post_bus", return_value=True) as bus_mock, \
             patch.object(self.daemon, "_notify_consultant_watch", return_value={"success": True}) as notify_mock:
            alerted = self.daemon._maybe_alert_missed_fire(561.0)
            alerted_again = self.daemon._maybe_alert_missed_fire(620.0)
        self.assertTrue(alerted)
        self.assertFalse(alerted_again)
        bus_mock.assert_called_once()
        notify_mock.assert_called_once()
        self.assertTrue(self.daemon.missed_fire_consultant_notified)
        self.assertEqual(self.daemon.missed_fire_alerted_at, 561.0)

    def test_build_actions_labels_pending_todos_truthfully(self):
        perception = dict(self.perception)
        perception["pending_tasks"] = 0
        actions = self.daemon._build_actions(
            perception,
            {"stall_pattern": False, "failure_rate_10m": 0, "dispatch_drought": False},
            [],
            [],
            2,
            [],
            ["alpha", "beta", "gamma", "delta"],
        )
        self.assertIn((40, "Dispatch idle workers on 2 pending TODO(s)."), actions)

    def test_build_actions_separates_todos_from_queued_tasks(self):
        perception = dict(self.perception)
        perception["pending_tasks"] = 3
        actions = self.daemon._build_actions(
            perception,
            {"stall_pattern": False, "failure_rate_10m": 0, "dispatch_drought": False},
            [],
            [],
            2,
            [],
            ["alpha", "beta"],
        )
        self.assertIn((40, "Dispatch idle workers: 2 pending TODO(s), 3 queued task(s)."), actions)


if __name__ == "__main__":
    unittest.main()
