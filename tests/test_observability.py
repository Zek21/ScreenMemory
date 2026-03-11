#!/usr/bin/env python3
"""Tests for skynet_observability.py -- system snapshots, comparison, trends."""

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

import skynet_observability as obs


class TestReadJson(unittest.TestCase):
    def test_read_valid_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"key": "value"}, f)
            f.flush()
            result = obs._read_json(Path(f.name))
            self.assertEqual(result, {"key": "value"})
        os.unlink(f.name)

    def test_read_nonexistent_returns_none(self):
        self.assertIsNone(obs._read_json(Path("/nonexistent/file.json")))

    def test_read_invalid_json_returns_none(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("not json {{{")
            f.flush()
            self.assertIsNone(obs._read_json(Path(f.name)))
        os.unlink(f.name)


class TestWorkerStates(unittest.TestCase):
    @patch("skynet_observability._read_json")
    def test_from_realtime(self, mock_read):
        mock_read.side_effect = [
            {"workers": {"alpha": "IDLE", "beta": "PROCESSING"}},
            None,
        ]
        result = obs._worker_states()
        self.assertEqual(result, {"alpha": "IDLE", "beta": "PROCESSING"})

    @patch("skynet_observability._read_json")
    def test_fallback_to_workers_json(self, mock_read):
        mock_read.side_effect = [
            None,  # realtime.json missing
            {"workers": [{"name": "gamma", "status": "IDLE"}]},
        ]
        result = obs._worker_states()
        self.assertEqual(result, {"gamma": "IDLE"})

    @patch("skynet_observability._read_json")
    def test_empty_when_no_files(self, mock_read):
        mock_read.return_value = None
        self.assertEqual(obs._worker_states(), {})


class TestDispatchStats(unittest.TestCase):
    @patch("skynet_observability._read_json")
    def test_stats_from_log(self, mock_read):
        mock_read.return_value = [
            {"success": True, "timestamp": "2026-01-01T00:00:00"},
            {"success": True, "timestamp": "2026-01-01T00:01:00"},
            {"success": False, "timestamp": "2026-01-01T00:02:00"},
        ]
        result = obs._dispatch_stats()
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["success"], 2)
        self.assertEqual(result["failure"], 1)
        self.assertAlmostEqual(result["success_rate"], 0.667, places=2)

    @patch("skynet_observability._read_json")
    def test_empty_log(self, mock_read):
        mock_read.return_value = []
        result = obs._dispatch_stats()
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["success_rate"], 0.0)

    @patch("skynet_observability._read_json")
    def test_no_log_file(self, mock_read):
        mock_read.return_value = None
        result = obs._dispatch_stats()
        self.assertEqual(result["total"], 0)


class TestEpisodeStats(unittest.TestCase):
    def test_no_episodes_dir(self):
        with patch.object(obs, "EPISODES_DIR", Path("/nonexistent/path")):
            result = obs._episode_stats()
            self.assertEqual(result["total"], 0)

    def test_with_episodes(self):
        tmpdir = tempfile.mkdtemp()
        ep_dir = Path(tmpdir) / "episodes"
        ep_dir.mkdir()
        (ep_dir / "ep1.json").write_text(json.dumps({"outcome": "success"}))
        (ep_dir / "ep2.json").write_text(json.dumps({"outcome": "failure"}))
        (ep_dir / "ep3.json").write_text(json.dumps({"outcome": "success"}))
        with patch.object(obs, "EPISODES_DIR", ep_dir):
            result = obs._episode_stats()
            self.assertEqual(result["total"], 3)
            self.assertEqual(result["by_outcome"]["success"], 2)
            self.assertEqual(result["by_outcome"]["failure"], 1)


class TestMissionStats(unittest.TestCase):
    @patch("skynet_observability._read_json")
    def test_mission_counts(self, mock_read):
        mock_read.return_value = {
            "missions": [
                {"status": "active"}, {"status": "active"},
                {"status": "completed"}, {"status": "planned"},
            ]
        }
        result = obs._mission_stats()
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["by_status"]["active"], 2)

    @patch("skynet_observability._read_json")
    def test_no_missions(self, mock_read):
        mock_read.return_value = None
        self.assertEqual(obs._mission_stats()["total"], 0)


class TestCollectSnapshot(unittest.TestCase):
    @patch("skynet_observability._process_count", return_value=5)
    @patch("skynet_observability._todo_stats", return_value={"total": 0, "by_status": {}})
    @patch("skynet_observability._mission_stats", return_value={"total": 0, "by_status": {}})
    @patch("skynet_observability._episode_stats", return_value={"total": 3, "by_outcome": {}})
    @patch("skynet_observability._dispatch_stats", return_value={"total": 10, "success": 8, "failure": 2, "success_rate": 0.8, "last_hour": 5, "throughput_per_min": 0.08})
    @patch("skynet_observability._bus_stats", return_value={"total": 50})
    @patch("skynet_observability._worker_states", return_value={"alpha": "IDLE"})
    def test_snapshot_structure(self, *_):
        snap = obs.collect_system_snapshot()
        self.assertIn("snapshot_id", snap)
        self.assertIn("timestamp", snap)
        self.assertIn("epoch", snap)
        self.assertEqual(snap["workers"], {"alpha": "IDLE"})
        self.assertEqual(snap["dispatch"]["total"], 10)
        self.assertEqual(snap["python_processes"], 5)


class TestSaveLoadSnapshot(unittest.TestCase):
    def test_save_and_load(self):
        tmpdir = tempfile.mkdtemp()
        with patch.object(obs, "OBS_DIR", Path(tmpdir)):
            snap = {"snapshot_id": "snap-test123", "timestamp": "2026-01-01", "epoch": 100}
            path = obs.save_snapshot(snap)
            self.assertTrue(path.exists())
            loaded = obs.load_snapshot("snap-test123")
            self.assertEqual(loaded["snapshot_id"], "snap-test123")

    def test_load_nonexistent(self):
        with patch.object(obs, "OBS_DIR", Path("/nonexistent")):
            self.assertIsNone(obs.load_snapshot("bad-id"))


class TestListSnapshots(unittest.TestCase):
    def test_list_empty(self):
        with patch.object(obs, "OBS_DIR", Path("/nonexistent")):
            self.assertEqual(obs.list_snapshots(), [])

    def test_list_with_snapshots(self):
        tmpdir = tempfile.mkdtemp()
        obs_dir = Path(tmpdir)
        for i in range(3):
            (obs_dir / f"snap-2026010{i}_000000.json").write_text(
                json.dumps({"snapshot_id": f"snap-2026010{i}_000000", "timestamp": f"2026-01-0{i}", "dispatch": {"total": i*10}})
            )
        with patch.object(obs, "OBS_DIR", obs_dir):
            result = obs.list_snapshots(limit=2)
            self.assertEqual(len(result), 2)


class TestCompareSnapshots(unittest.TestCase):
    def test_basic_comparison(self):
        a = {"epoch": 100, "dispatch": {"total": 10, "success": 8, "failure": 2}, "episodes": {"total": 5}, "workers": {"alpha": "IDLE"}}
        b = {"epoch": 200, "dispatch": {"total": 15, "success": 12, "failure": 3}, "episodes": {"total": 8}, "workers": {"alpha": "PROCESSING"}}
        result = obs.compare_snapshots(a, b)
        self.assertEqual(result["time_delta_s"], 100.0)
        self.assertEqual(result["dispatch_delta"]["total"], 5)
        self.assertEqual(result["episode_delta"]["total"], 3)
        self.assertEqual(result["worker_changes"]["alpha"]["from"], "IDLE")
        self.assertEqual(result["worker_changes"]["alpha"]["to"], "PROCESSING")

    def test_no_changes(self):
        a = {"epoch": 100, "dispatch": {"total": 10, "success": 10, "failure": 0}, "episodes": {"total": 5}, "workers": {"alpha": "IDLE"}}
        result = obs.compare_snapshots(a, a)
        self.assertEqual(result["time_delta_s"], 0.0)
        self.assertEqual(result["dispatch_delta"]["total"], 0)
        self.assertEqual(result["worker_changes"], {})


class TestTrendAnalysis(unittest.TestCase):
    def test_no_snapshots(self):
        with patch.object(obs, "OBS_DIR", Path("/nonexistent")):
            result = obs.trend_analysis(hours=1)
            self.assertEqual(result["snapshots"], 0)

    def test_with_snapshots(self):
        tmpdir = tempfile.mkdtemp()
        obs_dir = Path(tmpdir)
        now = time.time()
        for i in range(3):
            snap = {
                "snapshot_id": f"snap-t{i}",
                "epoch": now - (300 * (2 - i)),
                "dispatch": {"total": 10 + i * 5, "success_rate": 0.8 + i * 0.05},
                "episodes": {"total": 3 + i},
            }
            (obs_dir / f"snap-t{i}.json").write_text(json.dumps(snap))
        with patch.object(obs, "OBS_DIR", obs_dir):
            result = obs.trend_analysis(hours=1)
            self.assertEqual(result["snapshots"], 3)
            self.assertEqual(result["dispatch"]["growth"], 10)
            self.assertEqual(result["episodes"]["growth"], 2)


class TestSystemHealth(unittest.TestCase):
    @patch("skynet_observability._bus_stats", return_value={"total": 50})
    @patch("skynet_observability._episode_stats", return_value={"total": 10, "by_outcome": {}})
    @patch("skynet_observability._dispatch_stats", return_value={"total": 20, "success": 18, "failure": 2, "success_rate": 0.9, "last_hour": 5, "throughput_per_min": 0.08})
    @patch("skynet_observability._worker_states", return_value={"alpha": "IDLE", "beta": "PROCESSING"})
    def test_healthy_system(self, *_):
        result = obs.system_health()
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["workers"]["total"], 2)
        self.assertTrue(result["bus_ok"])
        self.assertEqual(result["issues"], [])

    @patch("skynet_observability._bus_stats", return_value={"error": "unreachable", "total": 0})
    @patch("skynet_observability._episode_stats", return_value={"total": 0, "by_outcome": {}})
    @patch("skynet_observability._dispatch_stats", return_value={"total": 10, "success": 3, "failure": 7, "success_rate": 0.3, "last_hour": 0, "throughput_per_min": 0})
    @patch("skynet_observability._worker_states", return_value={})
    def test_unhealthy_system(self, *_):
        result = obs.system_health()
        self.assertEqual(result["status"], "unhealthy")
        self.assertGreater(len(result["issues"]), 0)


class TestThroughputMetrics(unittest.TestCase):
    @patch("skynet_observability._dispatch_stats", return_value={"total": 100, "success": 95, "failure": 5, "success_rate": 0.95, "last_hour": 30, "throughput_per_min": 0.5})
    def test_throughput(self, _):
        result = obs.throughput_metrics()
        self.assertEqual(result["total_dispatches"], 100)
        self.assertEqual(result["successful"], 95)
        self.assertEqual(result["success_rate"], 0.95)
        self.assertEqual(result["throughput_per_min"], 0.5)


if __name__ == "__main__":
    unittest.main()
