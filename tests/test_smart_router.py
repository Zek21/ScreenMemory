"""Tests for tools/skynet_smart_router.py"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import skynet_smart_router as sr


class TestDetectDomains(unittest.TestCase):
    def test_frontend_keywords(self):
        domains = sr.detect_domains("build a dashboard panel with sidebar widgets")
        self.assertIn("frontend", domains)
        self.assertGreater(domains["frontend"], 0)

    def test_backend_keywords(self):
        domains = sr.detect_domains("add a new API endpoint for the server")
        self.assertIn("backend", domains)

    def test_testing_keywords(self):
        domains = sr.detect_domains("write pytest tests with assertions")
        self.assertIn("testing", domains)

    def test_multiple_domains(self):
        domains = sr.detect_domains("build a dashboard endpoint and write tests")
        self.assertIn("frontend", domains)
        self.assertIn("backend", domains)
        self.assertIn("testing", domains)

    def test_empty_text(self):
        domains = sr.detect_domains("")
        self.assertEqual(domains, {})

    def test_no_match(self):
        domains = sr.detect_domains("lorem ipsum dolor sit amet")
        # Should be empty or minimal
        self.assertTrue(len(domains) <= 1)

    def test_score_capping(self):
        # Many keyword hits should cap at 1.0
        domains = sr.detect_domains(
            "dashboard html css ui panel sidebar canvas chart widget layout card modal overlay button display"
        )
        self.assertIn("frontend", domains)
        self.assertLessEqual(domains["frontend"], 1.0)


class TestRankWorkers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, dir=str(ROOT / "data")
        )
        self.perf_data = {
            "version": 1,
            "updated": None,
            "workers": {
                "alpha": {
                    "tasks_completed": 10, "tasks_failed": 1,
                    "avg_duration_ms": 5000,
                    "specialties": {"frontend": 0.95, "dashboard": 0.9, "testing": 0.5},
                    "recent_tasks": [],
                },
                "beta": {
                    "tasks_completed": 8, "tasks_failed": 2,
                    "avg_duration_ms": 6000,
                    "specialties": {"backend": 0.95, "endpoints": 0.9, "testing": 0.7},
                    "recent_tasks": [],
                },
                "gamma": {
                    "tasks_completed": 5, "tasks_failed": 0,
                    "avg_duration_ms": 4000,
                    "specialties": {"testing": 0.9, "documentation": 0.95, "api": 0.85},
                    "recent_tasks": [],
                },
                "delta": {
                    "tasks_completed": 7, "tasks_failed": 3,
                    "avg_duration_ms": 7000,
                    "specialties": {"testing": 0.95, "validation": 0.95, "auditing": 0.9},
                    "recent_tasks": [],
                },
            },
        }
        json.dump(self.perf_data, self.tmp)
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    @patch.object(sr, "PERF_FILE")
    def test_frontend_routes_to_alpha(self, mock_file):
        mock_file.__class__ = Path
        mock_file.exists = lambda: True
        mock_file.read_text = lambda encoding="utf-8": json.dumps(self.perf_data)
        with patch.object(sr, "PERF_FILE", Path(self.tmp.name)):
            ranked = sr.rank_workers("build a dashboard panel")
            self.assertEqual(ranked[0]["worker"], "alpha")

    @patch.object(sr, "PERF_FILE")
    def test_backend_routes_to_beta(self, mock_file):
        with patch.object(sr, "PERF_FILE", Path(self.tmp.name)):
            ranked = sr.rank_workers("add server endpoint for API")
            self.assertEqual(ranked[0]["worker"], "beta")

    @patch.object(sr, "PERF_FILE")
    def test_testing_routes_to_delta(self, mock_file):
        with patch.object(sr, "PERF_FILE", Path(self.tmp.name)):
            ranked = sr.rank_workers("validate and audit test coverage")
            self.assertEqual(ranked[0]["worker"], "delta")

    @patch.object(sr, "PERF_FILE")
    def test_docs_routes_to_gamma(self, mock_file):
        with patch.object(sr, "PERF_FILE", Path(self.tmp.name)):
            ranked = sr.rank_workers("write documentation and markdown report")
            self.assertEqual(ranked[0]["worker"], "gamma")

    def test_returns_all_workers(self):
        ranked = sr.rank_workers("do something")
        workers = [r["worker"] for r in ranked]
        self.assertEqual(sorted(workers), sorted(sr.ALL_WORKERS))

    def test_subset_workers(self):
        ranked = sr.rank_workers("test something", workers=["alpha", "beta"])
        workers = [r["worker"] for r in ranked]
        self.assertEqual(sorted(workers), ["alpha", "beta"])

    def test_scores_are_bounded(self):
        ranked = sr.rank_workers("build dashboard api test audit validate fix")
        for r in ranked:
            self.assertGreaterEqual(r["score"], 0)
            self.assertLessEqual(r["score"], 1.5)  # generous bound


class TestRouteTask(unittest.TestCase):
    def test_returns_string(self):
        result = sr.route_task("do something")
        self.assertIn(result, sr.ALL_WORKERS)

    def test_empty_workers_fallback(self):
        result = sr.route_task("do something", workers=[])
        self.assertEqual(result, "alpha")


class TestRecordMetrics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, dir=str(ROOT / "data")
        )
        json.dump({"version": 1, "updated": None, "workers": {}}, self.tmp)
        self.tmp.close()
        self._orig = sr.PERF_FILE

    def tearDown(self):
        sr.PERF_FILE = self._orig
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass
        # Clean up .tmp file if it exists
        tmp_path = Path(self.tmp.name).with_suffix(".tmp")
        if tmp_path.exists():
            tmp_path.unlink()

    def test_record_success(self):
        sr.PERF_FILE = Path(self.tmp.name)
        result = sr.record_metrics("alpha", 5000, "success", "built dashboard")
        self.assertEqual(result["worker"], "alpha")
        self.assertEqual(result["tasks_completed"], 1)
        self.assertEqual(result["tasks_failed"], 0)

    def test_record_failure(self):
        sr.PERF_FILE = Path(self.tmp.name)
        result = sr.record_metrics("beta", 3000, "failure", "server crash")
        self.assertEqual(result["tasks_completed"], 0)
        self.assertEqual(result["tasks_failed"], 1)

    def test_multiple_records(self):
        sr.PERF_FILE = Path(self.tmp.name)
        sr.record_metrics("gamma", 2000, "success", "wrote tests")
        sr.record_metrics("gamma", 4000, "success", "wrote docs")
        result = sr.record_metrics("gamma", 6000, "failure", "audit failed")
        self.assertEqual(result["tasks_completed"], 2)
        self.assertEqual(result["tasks_failed"], 1)
        self.assertGreater(result["avg_duration_ms"], 0)

    def test_specialty_adjustment(self):
        sr.PERF_FILE = Path(self.tmp.name)
        sr.record_metrics("alpha", 5000, "success", "built a dashboard panel")
        perf = json.loads(Path(self.tmp.name).read_text())
        specialties = perf["workers"]["alpha"]["specialties"]
        # frontend domain should have been boosted
        self.assertIn("frontend", specialties)
        self.assertGreater(specialties["frontend"], 0.5)


class TestLeaderboard(unittest.TestCase):
    def test_returns_list(self):
        board = sr.get_leaderboard()
        self.assertIsInstance(board, list)

    def test_leaderboard_fields(self):
        board = sr.get_leaderboard()
        if board:
            entry = board[0]
            self.assertIn("worker", entry)
            self.assertIn("tasks_completed", entry)
            self.assertIn("success_rate", entry)
            self.assertIn("top_specialties", entry)


class TestGetWorkerPerformance(unittest.TestCase):
    def test_existing_worker(self):
        data = sr.get_worker_performance("alpha")
        # May be None if no perf data yet, or dict if seed exists
        if data:
            self.assertIn("worker", data)
            self.assertIn("specialties", data)

    def test_nonexistent_worker(self):
        data = sr.get_worker_performance("nonexistent_worker_xyz")
        self.assertIsNone(data)


if __name__ == "__main__":
    unittest.main()
