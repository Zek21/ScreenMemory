"""Tests for tools/skynet_self_heal.py"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import skynet_self_heal as sh


class TestDetectStuckTasks(unittest.TestCase):
    @patch.object(sh, "_get_worker_states")
    def test_no_stuck_workers(self, mock_states):
        mock_states.return_value = {
            "alpha": {"state": "IDLE", "since": time.time(), "task": ""},
            "beta": {"state": "IDLE", "since": time.time(), "task": ""},
        }
        stuck = sh.detect_stuck_tasks()
        self.assertEqual(stuck, [])

    @patch.object(sh, "_get_worker_states")
    def test_stuck_worker_detected(self, mock_states):
        mock_states.return_value = {
            "alpha": {"state": "PROCESSING", "since": time.time() - 300, "task": "big task"},
        }
        stuck = sh.detect_stuck_tasks(threshold_s=180)
        self.assertEqual(len(stuck), 1)
        self.assertEqual(stuck[0]["worker"], "alpha")
        self.assertGreater(stuck[0]["stuck_seconds"], 100)

    @patch.object(sh, "_get_worker_states")
    def test_not_stuck_within_threshold(self, mock_states):
        mock_states.return_value = {
            "alpha": {"state": "PROCESSING", "since": time.time() - 60, "task": "quick task"},
        }
        stuck = sh.detect_stuck_tasks(threshold_s=180)
        self.assertEqual(stuck, [])

    @patch.object(sh, "_get_worker_states")
    def test_severity_levels(self, mock_states):
        mock_states.return_value = {
            "alpha": {"state": "PROCESSING", "since": time.time() - 200, "task": "task"},
            "beta": {"state": "PROCESSING", "since": time.time() - 500, "task": "old task"},
        }
        stuck = sh.detect_stuck_tasks(threshold_s=180)
        severities = {s["worker"]: s["severity"] for s in stuck}
        self.assertEqual(severities["alpha"], "warning")
        self.assertEqual(severities["beta"], "critical")

    @patch.object(sh, "_get_worker_states")
    def test_empty_worker_states(self, mock_states):
        mock_states.return_value = {}
        stuck = sh.detect_stuck_tasks()
        self.assertEqual(stuck, [])


class TestAutoHeal(unittest.TestCase):
    @patch.object(sh, "_get_worker_states")
    @patch.object(sh, "_save_json")
    @patch.object(sh, "_load_json")
    def test_dry_run(self, mock_load, mock_save, mock_states):
        mock_states.return_value = {
            "alpha": {"state": "PROCESSING", "since": time.time() - 300, "task": "stuck"},
        }
        mock_load.return_value = []
        actions = sh.auto_heal(dry_run=True)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "would_cancel")
        mock_save.assert_not_called()

    @patch.object(sh, "_get_worker_states")
    def test_no_stuck_no_actions(self, mock_states):
        mock_states.return_value = {
            "alpha": {"state": "IDLE", "since": time.time(), "task": ""},
        }
        actions = sh.auto_heal(dry_run=True)
        self.assertEqual(actions, [])


class TestHealthReport(unittest.TestCase):
    @patch.object(sh, "_get_worker_states")
    @patch.object(sh, "_load_json")
    def test_report_structure(self, mock_load, mock_states):
        mock_states.return_value = {
            "alpha": {"state": "IDLE", "since": time.time(), "task": ""},
            "beta": {"state": "PROCESSING", "since": time.time() - 10, "task": "working"},
        }
        mock_load.return_value = {"version": 1, "workers": {}}
        report = sh.health_report()
        self.assertIn("timestamp", report)
        self.assertIn("worker_states", report)
        self.assertIn("state_summary", report)
        self.assertIn("stuck_tasks", report)
        self.assertIn("dispatch_stats", report)
        self.assertIn("recommendations", report)

    @patch.object(sh, "_get_worker_states")
    @patch.object(sh, "_load_json")
    def test_healthy_recommendation(self, mock_load, mock_states):
        mock_states.return_value = {
            "alpha": {"state": "PROCESSING", "since": time.time(), "task": "working"},
        }
        mock_load.return_value = {"version": 1, "workers": {}}
        report = sh.health_report()
        self.assertTrue(any("healthy" in r.lower() for r in report["recommendations"]))

    @patch.object(sh, "_get_worker_states")
    @patch.object(sh, "_load_json")
    def test_underutilized_recommendation(self, mock_load, mock_states):
        mock_states.return_value = {
            "alpha": {"state": "IDLE", "since": time.time(), "task": ""},
            "beta": {"state": "IDLE", "since": time.time(), "task": ""},
            "gamma": {"state": "IDLE", "since": time.time(), "task": ""},
        }
        mock_load.return_value = {"version": 1, "workers": {}}
        report = sh.health_report()
        self.assertTrue(any("underutilized" in r.lower() for r in report["recommendations"]))


class TestRunContinuous(unittest.TestCase):
    @patch.object(sh, "detect_stuck_tasks")
    @patch("time.sleep")
    def test_limited_iterations(self, mock_sleep, mock_detect):
        mock_detect.return_value = []
        sh.run_continuous(interval_s=0.01, max_iterations=3)
        self.assertEqual(mock_detect.call_count, 3)


class TestHelperFunctions(unittest.TestCase):
    def test_load_json_missing_file(self):
        result = sh._load_json(Path("nonexistent_xyz.json"))
        self.assertEqual(result, {})

    def test_save_and_load_json(self):
        tmp = Path(tempfile.mktemp(suffix=".json"))
        try:
            sh._save_json(tmp, {"test": True})
            loaded = sh._load_json(tmp)
            self.assertEqual(loaded["test"], True)
        finally:
            if tmp.exists():
                tmp.unlink()
            tmp_file = tmp.with_suffix(".tmp")
            if tmp_file.exists():
                tmp_file.unlink()


if __name__ == "__main__":
    unittest.main()
