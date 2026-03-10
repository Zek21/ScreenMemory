#!/usr/bin/env python3
"""
Tests for tools/skynet_realtime.py — hash fingerprinting, response extraction,
conversation hashing, and worker scoring.

Uses mocking to avoid COM/UIA dependencies so tests run in any environment.

Run:
    python -m pytest tests/test_skynet_realtime.py -v
    python tests/test_skynet_realtime.py
"""

import hashlib
import json
import sys
import time
import tempfile
from pathlib import Path
from unittest import TestCase, main as unittest_main
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ─── Mock helpers ─────────────────────────────────────────────────────────────

def _make_listitems(texts, y_start=100, y_step=50):
    """Create mock ListItem tuples: [(y, text), ...]"""
    return [(y_start + i * y_step, t) for i, t in enumerate(texts)]


def _mock_snapshot(items):
    """Return a patcher that replaces _get_listitem_snapshot with fixed items."""
    return patch("tools.skynet_realtime._get_listitem_snapshot", return_value=items)


# ─── Hash Fingerprinting ─────────────────────────────────────────────────────

class TestConversationHash(TestCase):
    """Tests for get_conversation_hash() — fast content fingerprinting."""

    def test_empty_returns_empty_string(self):
        with _mock_snapshot([]):
            from tools.skynet_realtime import get_conversation_hash
            h = get_conversation_hash(12345)
            self.assertEqual(h, "")

    def test_returns_12char_hex(self):
        items = _make_listitems(["Hello world, this is a test response"])
        with _mock_snapshot(items):
            from tools.skynet_realtime import get_conversation_hash
            h = get_conversation_hash(12345)
            self.assertEqual(len(h), 12)
            # Should be valid hex
            int(h, 16)

    def test_same_content_same_hash(self):
        items = _make_listitems(["Consistent response text here"])
        with _mock_snapshot(items):
            from tools.skynet_realtime import get_conversation_hash
            h1 = get_conversation_hash(12345)
            h2 = get_conversation_hash(12345)
            self.assertEqual(h1, h2)

    def test_different_content_different_hash(self):
        items1 = _make_listitems(["Response version A"])
        items2 = _make_listitems(["Response version B"])
        from tools.skynet_realtime import get_conversation_hash
        with _mock_snapshot(items1):
            h1 = get_conversation_hash(12345)
        with _mock_snapshot(items2):
            h2 = get_conversation_hash(12345)
        self.assertNotEqual(h1, h2)

    def test_uses_last_item_sorted_by_y(self):
        # Items out of order — hash should use the one with highest Y
        items = [(300, "Last item"), (100, "First item"), (200, "Middle item")]
        from tools.skynet_realtime import get_conversation_hash
        with _mock_snapshot(items):
            h = get_conversation_hash(12345)
        expected = hashlib.md5("Last item".encode(errors="replace")).hexdigest()[:12]
        self.assertEqual(h, expected)


# ─── extract_new_response ────────────────────────────────────────────────────

class TestExtractNewResponse(TestCase):
    """Tests for extract_new_response() — delta extraction via hash comparison."""

    def test_no_items_returns_none(self):
        with _mock_snapshot([]):
            from tools.skynet_realtime import extract_new_response
            text, h = extract_new_response(12345, "abc123")
            self.assertIsNone(text)
            self.assertEqual(h, "")

    def test_same_hash_returns_none(self):
        items = _make_listitems(["Same old response"])
        last_text = "Same old response"
        baseline = hashlib.md5(last_text.encode(errors="replace")).hexdigest()[:12]
        with _mock_snapshot(items):
            from tools.skynet_realtime import extract_new_response
            text, h = extract_new_response(12345, baseline)
            self.assertIsNone(text)
            self.assertEqual(h, baseline)

    def test_changed_content_returns_text(self):
        items = _make_listitems(["New response text"])
        with _mock_snapshot(items):
            from tools.skynet_realtime import extract_new_response
            text, h = extract_new_response(12345, "old_hash_000")
            self.assertIsNotNone(text)
            self.assertIn("New response text", text)
            self.assertNotEqual(h, "old_hash_000")

    def test_skips_command_previews(self):
        items = _make_listitems([
            "User asked a question",
            "Ran terminal command: ls -la",
            "Here is the actual response",
            "More response content",
        ])
        with _mock_snapshot(items):
            from tools.skynet_realtime import extract_new_response
            text, h = extract_new_response(12345, "different_hash")
            self.assertIsNotNone(text)
            # Should include post-command content
            self.assertIn("Here is the actual response", text)
            self.assertIn("More response content", text)
            # Should NOT include the command preview
            self.assertNotIn("Ran terminal command", text)

    def test_max_chars_truncation(self):
        long_text = "A" * 3000
        items = _make_listitems([long_text])
        with _mock_snapshot(items):
            from tools.skynet_realtime import extract_new_response
            text, h = extract_new_response(12345, "old_hash", max_chars=500)
            self.assertIsNotNone(text)
            self.assertLessEqual(len(text), 500)

    def test_returns_current_hash(self):
        items = _make_listitems(["Specific content for hash"])
        expected_hash = hashlib.md5("Specific content for hash".encode(errors="replace")).hexdigest()[:12]
        with _mock_snapshot(items):
            from tools.skynet_realtime import extract_new_response
            _, h = extract_new_response(12345, "old_hash")
            self.assertEqual(h, expected_hash)


# ─── extract_last_response ───────────────────────────────────────────────────

class TestExtractLastResponse(TestCase):
    """Tests for extract_last_response() — last response block extraction."""

    def test_no_items_returns_none(self):
        with _mock_snapshot([]):
            from tools.skynet_realtime import extract_last_response
            result = extract_last_response(12345)
            self.assertIsNone(result)

    def test_single_item(self):
        items = _make_listitems(["Only response here"])
        with _mock_snapshot(items):
            from tools.skynet_realtime import extract_last_response
            result = extract_last_response(12345)
            self.assertEqual(result, "Only response here")

    def test_multiple_items_returns_recent(self):
        items = _make_listitems([
            "Old message from earlier",
            "Another old message",
            "The latest response",
        ])
        with _mock_snapshot(items):
            from tools.skynet_realtime import extract_last_response
            result = extract_last_response(12345)
            self.assertIn("The latest response", result)

    def test_stops_at_command_boundary(self):
        items = _make_listitems([
            "Pre-command context",
            "Ran terminal command: git status",
            "Post-command response line 1",
            "Post-command response line 2",
        ])
        with _mock_snapshot(items):
            from tools.skynet_realtime import extract_last_response
            result = extract_last_response(12345)
            self.assertIn("Post-command response line 1", result)
            self.assertIn("Post-command response line 2", result)
            self.assertNotIn("Ran terminal command", result)
            self.assertNotIn("Pre-command context", result)

    def test_ran_command_variant(self):
        items = _make_listitems([
            "Ran command: npm test",
            "All 42 tests passed",
        ])
        with _mock_snapshot(items):
            from tools.skynet_realtime import extract_last_response
            result = extract_last_response(12345)
            self.assertIn("All 42 tests passed", result)
            self.assertNotIn("Ran command", result)

    def test_max_chars_limit(self):
        items = _make_listitems(["X" * 5000])
        with _mock_snapshot(items):
            from tools.skynet_realtime import extract_last_response
            result = extract_last_response(12345, max_chars=100)
            self.assertLessEqual(len(result), 100)

    def test_items_sorted_by_y(self):
        # Out-of-order Y positions — should still get bottom-most items
        items = [(300, "Bottom item"), (100, "Top item"), (200, "Middle item")]
        with _mock_snapshot(items):
            from tools.skynet_realtime import extract_last_response
            result = extract_last_response(12345)
            self.assertIn("Bottom item", result)


# ─── Worker Scoring ──────────────────────────────────────────────────────────

class TestWorkerScoring(TestCase):
    """Tests for record_outcome() and get_best_workers() — reliability scoring."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.score_file = Path(self.tmpdir) / "worker_scores.json"

    def tearDown(self):
        if self.score_file.exists():
            self.score_file.unlink()

    def test_record_outcome_creates_entry(self):
        with patch("tools.skynet_realtime.SCORE_FILE", self.score_file):
            from tools.skynet_realtime import record_outcome
            result = record_outcome("alpha", True, 5.0, "test")
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["success"], 1)
            self.assertEqual(result["fail"], 0)
            self.assertGreater(result["success_rate"], 0)

    def test_record_outcome_accumulates(self):
        with patch("tools.skynet_realtime.SCORE_FILE", self.score_file):
            from tools.skynet_realtime import record_outcome
            record_outcome("beta", True, 3.0)
            record_outcome("beta", True, 4.0)
            result = record_outcome("beta", False, 10.0)
            self.assertEqual(result["total"], 3)
            self.assertEqual(result["success"], 2)
            self.assertEqual(result["fail"], 1)
            self.assertAlmostEqual(result["success_rate"], 66.7, places=1)

    def test_record_outcome_tracks_history(self):
        with patch("tools.skynet_realtime.SCORE_FILE", self.score_file):
            from tools.skynet_realtime import record_outcome
            for i in range(5):
                record_outcome("gamma", True, float(i + 1), "code")
            data = json.loads(self.score_file.read_text())
            self.assertEqual(len(data["gamma"]["history"]), 5)
            self.assertTrue(all(h["type"] == "code" for h in data["gamma"]["history"]))

    def test_history_rolling_window_20(self):
        with patch("tools.skynet_realtime.SCORE_FILE", self.score_file):
            from tools.skynet_realtime import record_outcome
            for i in range(25):
                record_outcome("delta", True, 1.0)
            data = json.loads(self.score_file.read_text())
            self.assertLessEqual(len(data["delta"]["history"]), 20)

    def test_avg_time_only_successful(self):
        with patch("tools.skynet_realtime.SCORE_FILE", self.score_file):
            from tools.skynet_realtime import record_outcome
            record_outcome("alpha", True, 5.0)
            record_outcome("alpha", False, 100.0)  # failure — should NOT affect avg
            result = record_outcome("alpha", True, 10.0)
            # avg should be (5+10)/2 = 7.5, NOT (5+100+10)/3
            self.assertAlmostEqual(result["avg_time"], 7.5, places=1)

    def test_record_outcome_persists_to_file(self):
        with patch("tools.skynet_realtime.SCORE_FILE", self.score_file):
            from tools.skynet_realtime import record_outcome
            record_outcome("alpha", True, 3.0)
            self.assertTrue(self.score_file.exists())
            data = json.loads(self.score_file.read_text())
            self.assertIn("alpha", data)

    def test_get_best_workers_ranking(self):
        # Pre-populate scores with known values
        scores = {
            "alpha": {"total": 10, "success": 9, "fail": 1, "avg_time": 5.0,
                      "success_rate": 90.0, "history": []},
            "beta": {"total": 10, "success": 5, "fail": 5, "avg_time": 20.0,
                     "success_rate": 50.0, "history": []},
            "gamma": {"total": 10, "success": 10, "fail": 0, "avg_time": 3.0,
                      "success_rate": 100.0, "history": []},
            "delta": {"total": 10, "success": 7, "fail": 3, "avg_time": 10.0,
                      "success_rate": 70.0, "history": []},
        }
        self.score_file.write_text(json.dumps(scores))

        workers_data = [{"name": n, "hwnd": 0} for n in ["alpha", "beta", "gamma", "delta"]]
        with patch("tools.skynet_realtime.SCORE_FILE", self.score_file), \
             patch("tools.skynet_realtime._load_workers", return_value=(workers_data, None)):
            from tools.skynet_realtime import get_best_workers
            ranked = get_best_workers(4)
            # gamma (100% success, fastest) should be first
            self.assertEqual(ranked[0], "gamma")
            # beta (50% success, slowest) should be last
            self.assertEqual(ranked[-1], "beta")

    def test_get_best_workers_count_limit(self):
        scores = {n: {"total": 5, "success": 5, "fail": 0, "avg_time": 5.0,
                       "success_rate": 100.0, "history": []} for n in WORKER_NAMES}
        self.score_file.write_text(json.dumps(scores))

        workers_data = [{"name": n, "hwnd": 0} for n in WORKER_NAMES]
        with patch("tools.skynet_realtime.SCORE_FILE", self.score_file), \
             patch("tools.skynet_realtime._load_workers", return_value=(workers_data, None)):
            from tools.skynet_realtime import get_best_workers
            ranked = get_best_workers(2)
            self.assertEqual(len(ranked), 2)

    def test_get_best_workers_no_scores_defaults(self):
        # No score file exists — should still return workers with default scores
        workers_data = [{"name": n, "hwnd": 0} for n in WORKER_NAMES]
        with patch("tools.skynet_realtime.SCORE_FILE", self.score_file), \
             patch("tools.skynet_realtime._load_workers", return_value=(workers_data, None)):
            from tools.skynet_realtime import get_best_workers
            ranked = get_best_workers(4)
            self.assertEqual(len(ranked), 4)
            self.assertEqual(set(ranked), set(WORKER_NAMES))


# ─── Hash Fingerprint Integration ────────────────────────────────────────────

class TestHashFingerprinting(TestCase):
    """Integration-style tests for the hash-based change detection pipeline."""

    def test_baseline_then_no_change(self):
        """Simulate: take baseline hash, poll again with same content → no new response."""
        items = _make_listitems(["Initial response content"])
        from tools.skynet_realtime import get_conversation_hash, extract_new_response

        with _mock_snapshot(items):
            baseline = get_conversation_hash(12345)
            text, current = extract_new_response(12345, baseline)
            self.assertIsNone(text)
            self.assertEqual(current, baseline)

    def test_baseline_then_change_detected(self):
        """Simulate: take baseline, content changes → new response extracted."""
        initial = _make_listitems(["Initial content"])
        updated = _make_listitems(["Initial content", "Brand new response from worker"])
        from tools.skynet_realtime import get_conversation_hash, extract_new_response

        with _mock_snapshot(initial):
            baseline = get_conversation_hash(12345)

        with _mock_snapshot(updated):
            text, current = extract_new_response(12345, baseline)
            self.assertIsNotNone(text)
            self.assertIn("Brand new response", text)
            self.assertNotEqual(current, baseline)

    def test_multiple_changes_tracked(self):
        """Simulate: multiple rounds of content changes, each detected correctly."""
        from tools.skynet_realtime import get_conversation_hash, extract_new_response

        contents = [
            _make_listitems(["Response round 1"]),
            _make_listitems(["Response round 1", "Response round 2"]),
            _make_listitems(["Response round 1", "Response round 2", "Response round 3"]),
        ]

        prev_hash = ""
        for i, items in enumerate(contents):
            with _mock_snapshot(items):
                text, current = extract_new_response(12345, prev_hash)
                self.assertIsNotNone(text, f"Round {i+1} should detect change")
                self.assertNotEqual(current, prev_hash)
                prev_hash = current


# ─── RealtimeCollector (unit-level) ──────────────────────────────────────────

class TestRealtimeCollectorInit(TestCase):
    """Tests for RealtimeCollector initialization and configuration."""

    def test_default_config(self):
        workers_data = [{"name": "alpha", "hwnd": 100}]
        with patch("tools.skynet_realtime._load_workers", return_value=(workers_data, 999)):
            from tools.skynet_realtime import RealtimeCollector
            rc = RealtimeCollector(poll_interval=1.0)
            self.assertEqual(rc.poll_interval, 1.0)
            self.assertTrue(rc.auto_recover)

    def test_snapshot_baselines(self):
        workers_data = [{"name": "alpha", "hwnd": 100}, {"name": "beta", "hwnd": 200}]
        items = _make_listitems(["Some existing content"])
        with patch("tools.skynet_realtime._load_workers", return_value=(workers_data, 999)), \
             _mock_snapshot(items):
            from tools.skynet_realtime import RealtimeCollector
            rc = RealtimeCollector()
            rc.snapshot_baselines(["alpha", "beta"])
            # Baselines stored in rc._baselines (method returns None)
            self.assertIn("alpha", rc._baselines)
            self.assertIn("beta", rc._baselines)
            for v in rc._baselines.values():
                self.assertIsInstance(v, str)
                self.assertGreater(len(v), 0)


WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]


if __name__ == "__main__":
    unittest_main(verbosity=2)
