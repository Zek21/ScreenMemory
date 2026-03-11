#!/usr/bin/env python3
"""Tests for skynet_ci.py -- CI runner."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import skynet_ci as ci


class TestDiscoverTestFiles(unittest.TestCase):
    def test_discovers_files(self):
        files = ci.discover_test_files()
        self.assertIsInstance(files, list)
        # We know test_ci.py exists since we're running it
        names = [f.name for f in files]
        self.assertIn("test_ci.py", names)

    def test_pattern_filter(self):
        files = ci.discover_test_files(pattern="test_ci")
        self.assertTrue(all("test_ci" in f.stem for f in files))

    def test_pattern_no_match(self):
        files = ci.discover_test_files(pattern="nonexistent_xyz_999")
        self.assertEqual(len(files), 0)

    def test_returns_sorted(self):
        files = ci.discover_test_files()
        names = [f.name for f in files]
        self.assertEqual(names, sorted(names))


class TestParseSummary(unittest.TestCase):
    def test_standard_pytest_output(self):
        output = "====== 42 passed, 3 failed, 1 error in 2.5s ======"
        p, f, e, s = ci._parse_summary(output)
        self.assertEqual(p, 42)
        self.assertEqual(f, 3)
        self.assertEqual(e, 1)
        self.assertEqual(s, 0)

    def test_all_passed(self):
        output = "====== 100 passed in 1.0s ======"
        p, f, e, s = ci._parse_summary(output)
        self.assertEqual(p, 100)
        self.assertEqual(f, 0)

    def test_with_skipped(self):
        output = "10 passed, 2 skipped"
        p, f, e, s = ci._parse_summary(output)
        self.assertEqual(p, 10)
        self.assertEqual(s, 2)

    def test_fallback_counts(self):
        output = "test_a PASSED\ntest_b PASSED\ntest_c FAILED\n"
        p, f, e, s = ci._parse_summary(output)
        self.assertEqual(p, 2)
        self.assertEqual(f, 1)

    def test_empty_output(self):
        p, f, e, s = ci._parse_summary("")
        self.assertEqual(p, 0)
        self.assertEqual(f, 0)


class TestRunPytest(unittest.TestCase):
    def test_no_files_returns_zero(self):
        result = ci.run_pytest([])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["total"], 0)

    def test_result_structure(self):
        result = ci.run_pytest([])
        for key in ("exit_code", "passed", "failed", "errors", "skipped",
                     "total", "duration_s", "output", "files"):
            self.assertIn(key, result)


class TestRunCi(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_ci_dir = ci.CI_DIR
        ci.CI_DIR = Path(self.tmpdir)

    def tearDown(self):
        ci.CI_DIR = self._orig_ci_dir

    def test_run_ci_returns_report(self):
        fake = {
            "exit_code": 0,
            "passed": 3,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "total": 3,
            "duration_s": 0.12,
            "output": "ok",
            "files": ["test_ci.py"],
        }
        with patch.object(ci, "run_pytest", return_value=fake):
            report = ci.run_ci(pattern="test_ci", save=True)
        self.assertIn("run_id", report)
        self.assertIn("status", report)
        self.assertIn("results", report)
        self.assertIn(report["status"], ("PASS", "FAIL"))

    def test_run_ci_saves_file(self):
        fake = {
            "exit_code": 0,
            "passed": 1,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "total": 1,
            "duration_s": 0.01,
            "output": "ok",
            "files": ["test_ci.py"],
        }
        with patch.object(ci, "run_pytest", return_value=fake):
            report = ci.run_ci(pattern="test_ci", save=True)
        saved = ci.CI_DIR / f"{report['run_id']}.json"
        self.assertTrue(saved.exists())

    def test_run_ci_no_save(self):
        report = ci.run_ci(pattern="nonexistent_xyz", save=False)
        files = list(Path(self.tmpdir).glob("ci-*.json"))
        self.assertEqual(len(files), 0)


class TestLoadAndList(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_ci_dir = ci.CI_DIR
        ci.CI_DIR = Path(self.tmpdir)

    def tearDown(self):
        ci.CI_DIR = self._orig_ci_dir

    def test_load_nonexistent(self):
        self.assertIsNone(ci.load_run("ci-nonexistent"))

    def test_latest_run_none(self):
        self.assertIsNone(ci.latest_run())

    def test_list_runs_empty(self):
        self.assertEqual(ci.list_runs(), [])

    def test_save_load_roundtrip(self):
        report = {"run_id": "ci-test", "status": "PASS", "results": {"passed": 5}, "summary": "5 passed"}
        ci._save_run("ci-test", report)
        loaded = ci.load_run("ci-test")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["status"], "PASS")

    def test_list_after_save(self):
        report = {"run_id": "ci-test-1", "timestamp": "2026-01-01", "status": "PASS", "results": {}, "summary": "ok"}
        ci._save_run("ci-test-1", report)
        runs = ci.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["run_id"], "ci-test-1")


class TestRotateRuns(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_ci_dir = ci.CI_DIR
        self._orig_max = ci.MAX_RUNS
        ci.CI_DIR = Path(self.tmpdir)
        ci.MAX_RUNS = 3

    def tearDown(self):
        ci.CI_DIR = self._orig_ci_dir
        ci.MAX_RUNS = self._orig_max

    def test_rotation_removes_oldest(self):
        for i in range(5):
            p = ci.CI_DIR / f"ci-2026-{i:04d}.json"
            p.write_text("{}", encoding="utf-8")
        ci._rotate_runs()
        remaining = list(ci.CI_DIR.glob("ci-*.json"))
        self.assertEqual(len(remaining), 3)


class TestGenerateReport(unittest.TestCase):
    def test_report_from_none(self):
        report = ci.generate_report(None)
        self.assertIn("No CI runs", report)

    def test_report_from_data(self):
        data = {
            "run_id": "ci-test",
            "timestamp": "2026-01-01T00:00:00",
            "status": "PASS",
            "pattern": None,
            "test_count": 3,
            "results": {"passed": 10, "failed": 0, "errors": 0, "skipped": 0,
                         "duration_s": 1.5, "files": ["test_a.py"]},
        }
        report = ci.generate_report(data)
        self.assertIn("PASS", report)
        self.assertIn("ci-test", report)
        self.assertIn("test_a.py", report)


class TestCiStatus(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_ci_dir = ci.CI_DIR
        ci.CI_DIR = Path(self.tmpdir)

    def tearDown(self):
        ci.CI_DIR = self._orig_ci_dir

    def test_no_runs(self):
        s = ci.ci_status()
        self.assertEqual(s["latest"], "NO_RUNS")
        self.assertEqual(s["pass_streak"], 0)

    def test_with_runs(self):
        for i in range(3):
            data = {"run_id": f"ci-{i}", "timestamp": f"2026-01-0{i+1}", "status": "PASS", "summary": "ok", "results": {}}
            (ci.CI_DIR / f"ci-{i}.json").write_text(json.dumps(data), encoding="utf-8")
        s = ci.ci_status()
        self.assertEqual(s["latest"], "PASS")
        self.assertEqual(s["pass_streak"], 3)


class TestRunPytestGuard(unittest.TestCase):
    def test_blocks_nested_ci_invocation(self):
        test_file = ROOT / "tests" / "test_ci.py"
        with patch.dict(os.environ, {ci.CI_DEPTH_ENV: "1"}):
            result = ci.run_pytest([test_file])
        self.assertEqual(result["exit_code"], -3)
        self.assertEqual(result["errors"], 1)
        self.assertIn("Nested skynet_ci pytest invocation blocked", result["output"])


if __name__ == "__main__":
    unittest.main()
