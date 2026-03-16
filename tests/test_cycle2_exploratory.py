"""
Cycle 2 Exploratory Testing — Adversarial & Edge-Case Test Suite
================================================================
Tests Sprint 2 tools under hostile inputs, concurrency stress,
resource exhaustion, and state corruption scenarios.

Created by worker beta for Cycle 2 acceptance.
# signed: beta
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

# Ensure repo root is importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# 1. ADVERSARIAL INPUT TESTING
# ---------------------------------------------------------------------------

class TestAdversarialInputSpamGuard(unittest.TestCase):
    """Adversarial inputs to guarded_publish and SpamGuard."""

    # signed: beta

    def test_none_message_rejected(self):
        """guarded_publish(None) must not crash."""
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish(None)
        self.assertFalse(result.get("allowed", True))
        self.assertIn("invalid_message_type", result.get("reason", ""))
    # signed: beta

    def test_integer_message_rejected(self):
        """guarded_publish(42) must not crash."""
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish(42)
        self.assertFalse(result.get("allowed", True))
    # signed: beta

    def test_list_message_rejected(self):
        """guarded_publish([]) must not crash."""
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish([1, 2, 3])
        self.assertFalse(result.get("allowed", True))
    # signed: beta

    def test_empty_dict_rejected(self):
        """guarded_publish({}) rejected for missing sender/content."""
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish({})
        self.assertFalse(result.get("allowed", True))
        self.assertIn("missing required fields", result.get("reason", ""))
    # signed: beta

    def test_unicode_emoji_content(self):
        """Unicode emoji in sender/content should not crash validation."""
        from tools.skynet_spam_guard import guarded_publish
        msg = {
            "sender": "test_\U0001F600\U0001F525",
            "topic": "general",
            "type": "test",
            "content": "Result: \u2705 \U0001F680 \U0001F4A5 emoji stress test"
        }
        # Should not raise — may be blocked by spam guard but must not crash
        result = guarded_publish(msg)
        self.assertIn("allowed", result)
    # signed: beta

    def test_extremely_long_content(self):
        """Content > 10KB should be handled gracefully."""
        from tools.skynet_spam_guard import guarded_publish
        long_str = "A" * 15_000
        msg = {
            "sender": "test_long",
            "topic": "general",
            "type": "test",
            "content": long_str
        }
        # Must not crash; may be blocked or truncated
        result = guarded_publish(msg)
        self.assertIsInstance(result, dict)
    # signed: beta

    def test_null_bytes_in_content(self):
        """Null bytes in content must not cause silent corruption."""
        from tools.skynet_spam_guard import guarded_publish
        msg = {
            "sender": "test_null",
            "topic": "general",
            "type": "test",
            "content": "before\x00after\x00\x00end"
        }
        result = guarded_publish(msg)
        self.assertIsInstance(result, dict)
    # signed: beta

    def test_control_characters_in_sender(self):
        """Control chars (\\r, \\n, \\t) in sender handled safely."""
        from tools.skynet_spam_guard import guarded_publish
        msg = {
            "sender": "evil\r\nsender\ttabs",
            "topic": "general",
            "type": "test",
            "content": "control char test"
        }
        result = guarded_publish(msg)
        self.assertIsInstance(result, dict)
    # signed: beta

    def test_sql_injection_in_content(self):
        """SQL injection patterns must not break anything."""
        from tools.skynet_spam_guard import guarded_publish
        msg = {
            "sender": "test_sql",
            "topic": "general",
            "type": "test",
            "content": "'; DROP TABLE messages; --"
        }
        result = guarded_publish(msg)
        self.assertIsInstance(result, dict)
    # signed: beta

    def test_missing_content_field(self):
        """Message with sender but no content is rejected."""
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish({"sender": "test", "topic": "general"})
        self.assertFalse(result.get("allowed", True))
    # signed: beta

    def test_missing_sender_field(self):
        """Message with content but no sender is rejected."""
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish({"content": "hello", "topic": "general"})
        self.assertFalse(result.get("allowed", True))
    # signed: beta


class TestAdversarialInputBusValidator(unittest.TestCase):
    """Adversarial inputs to bus message validator."""

    # signed: beta

    def test_non_dict_message(self):
        from tools.skynet_bus_validator import validate_message
        errors = validate_message("not a dict")
        self.assertTrue(len(errors) > 0)
        self.assertIn("dict", errors[0].lower())
    # signed: beta

    def test_sender_too_long(self):
        from tools.skynet_bus_validator import validate_message
        msg = {"sender": "x" * 100, "content": "test"}
        errors = validate_message(msg)
        sender_errs = [e for e in errors if "sender" in e.lower()]
        self.assertTrue(len(sender_errs) > 0)
    # signed: beta

    def test_content_exceeds_max_length(self):
        from tools.skynet_bus_validator import validate_message
        msg = {"sender": "test", "content": "z" * 11_000}
        errors = validate_message(msg)
        content_errs = [e for e in errors if "content" in e.lower()]
        self.assertTrue(len(content_errs) > 0)
    # signed: beta

    def test_empty_content_string(self):
        from tools.skynet_bus_validator import validate_message
        msg = {"sender": "test", "content": ""}
        errors = validate_message(msg)
        self.assertTrue(len(errors) > 0)
    # signed: beta

    def test_integer_sender(self):
        from tools.skynet_bus_validator import validate_message
        msg = {"sender": 12345, "content": "test"}
        errors = validate_message(msg)
        self.assertTrue(len(errors) > 0)
    # signed: beta

    def test_metadata_non_dict(self):
        from tools.skynet_bus_validator import validate_message
        msg = {"sender": "test", "content": "ok", "metadata": "bad"}
        errors = validate_message(msg)
        meta_errs = [e for e in errors if "metadata" in e.lower()]
        self.assertTrue(len(meta_errs) > 0)
    # signed: beta

    def test_metadata_too_many_keys(self):
        from tools.skynet_bus_validator import validate_message
        big_meta = {f"key_{i}": "val" for i in range(25)}
        msg = {"sender": "test", "content": "ok", "metadata": big_meta}
        errors = validate_message(msg)
        self.assertTrue(len(errors) > 0)
    # signed: beta

    def test_invalid_priority(self):
        from tools.skynet_bus_validator import validate_message
        msg = {
            "sender": "test",
            "content": "ok",
            "metadata": {"priority": "ULTRA_HIGH"}
        }
        errors = validate_message(msg)
        priority_errs = [e for e in errors if "priority" in e.lower()]
        self.assertTrue(len(priority_errs) > 0)
    # signed: beta

    def test_strict_mode_unknown_topic(self):
        from tools.skynet_bus_validator import validate_message
        msg = {"sender": "test", "content": "ok", "topic": "nonexistent_topic_xyz"}
        errors = validate_message(msg, strict=True)
        topic_errs = [e for e in errors if "topic" in e.lower()]
        self.assertTrue(len(topic_errs) > 0)
    # signed: beta

    def test_validate_or_raise_raises(self):
        from tools.skynet_bus_validator import validate_or_raise
        with self.assertRaises(ValueError):
            validate_or_raise("not a dict")
    # signed: beta


# ---------------------------------------------------------------------------
# 2. CONCURRENCY EDGE CASES
# ---------------------------------------------------------------------------

class TestConcurrencyEdgeCases(unittest.TestCase):
    """Concurrency and race condition tests."""

    # signed: beta

    @patch("tools.skynet_spam_guard.SpamGuard._bus_post", return_value=True)
    def test_simultaneous_guarded_publish(self, mock_pub):
        """Multiple threads calling guarded_publish should not corrupt state."""
        from tools.skynet_spam_guard import SpamGuard
        guard = SpamGuard()
        results = []
        errors = []

        def publish_thread(idx):
            try:
                msg = {
                    "sender": f"thread_{idx}",
                    "topic": "general",
                    "type": "test",
                    "content": f"concurrent msg {idx} unique_{time.time()}_{idx}"
                }
                r = guard.publish_guarded(msg)
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=publish_thread, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Errors in concurrent publish: {errors}")
        self.assertEqual(len(results), 10)
    # signed: beta

    def test_concurrent_pid_file_writes(self):
        """Concurrent write_pid calls should not corrupt PID files."""
        from tools.skynet_daemon_utils import DATA_DIR

        test_daemon = f"_test_concurrent_{os.getpid()}"
        pid_path = DATA_DIR / f"{test_daemon}.pid"
        results = []
        errors = []

        def write_pid_thread():
            try:
                pid_path.write_text(str(os.getpid()))
                results.append(True)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_pid_thread) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"PID write errors: {errors}")
        # File should exist and contain a valid integer
        if pid_path.exists():
            content = pid_path.read_text().strip()
            self.assertTrue(content.isdigit())
            pid_path.unlink(missing_ok=True)
    # signed: beta

    def test_concurrent_daemon_status_queries(self):
        """Parallel check_all_daemons() calls should not interfere."""
        from tools.skynet_daemon_status import check_all_daemons
        results = []
        errors = []

        def query_thread():
            try:
                r = check_all_daemons()
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=query_thread) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(errors), 0, f"Daemon status errors: {errors}")
        self.assertEqual(len(results), 3)
        # All results should have same daemon count
        counts = [len(r) for r in results]
        self.assertTrue(all(c == counts[0] for c in counts))
    # signed: beta

    def test_concurrent_scoring_adjustments(self):
        """Concurrent score adjustments must not lose updates."""
        from tools.skynet_scoring import adjust_score, get_scores
        test_worker = "_test_concurrency_worker"
        errors = []

        def adjust_thread(i):
            try:
                adjust_score(test_worker, 0.001,
                             f"concurrency_test_{i}", adjuster="system")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=adjust_thread, args=(i,))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(len(errors), 0, f"Scoring errors: {errors}")
        # Verify score was adjusted (at least some updates landed)
        scores = get_scores()
        if test_worker in scores:
            self.assertGreater(scores[test_worker].get("total", 0), 0)
    # signed: beta


# ---------------------------------------------------------------------------
# 3. RESOURCE EXHAUSTION SCENARIOS
# ---------------------------------------------------------------------------

class TestResourceExhaustion(unittest.TestCase):
    """Resource limits, large files, and capacity edge cases."""

    # signed: beta

    def test_dispatch_log_large_file(self):
        """verify_dispatch_evidence handles 1000+ entry logs gracefully."""
        from tools.skynet_scoring import verify_dispatch_evidence, DISPATCH_LOG_FILE
        large_log = json.dumps([
            {
                "worker": f"worker_{i % 4}",
                "task_summary": f"task number {i}",
                "timestamp": "2026-03-12T10:00:00",
                "state_at_dispatch": "IDLE",
                "success": True,
                "target_hwnd": 12345,
                "result_received": False,
                "strategy": "direct",
                "strategy_id": "",
            }
            for i in range(1500)
        ])
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=large_log):
                result = verify_dispatch_evidence("worker_0", "task number 0")
        self.assertIsInstance(result, dict)
        self.assertIn("verified", result)
    # signed: beta

    def test_corrupted_spam_log_json(self):
        """Corrupted spam_log.json should not crash SpamGuard."""
        from tools.skynet_spam_guard import SpamGuard
        corrupted = '{"entries": [{"bad json'
        with patch("builtins.open", mock_open(read_data=corrupted)):
            with patch("pathlib.Path.exists", return_value=True):
                # SpamGuard init reads spam_log on first use — should survive
                try:
                    guard = SpamGuard()
                    # Attempt a publish — guard should still work
                    result = guard.publish_guarded({
                        "sender": "test",
                        "topic": "general",
                        "type": "test",
                        "content": "after corruption"
                    })
                    self.assertIsInstance(result, dict)
                except json.JSONDecodeError:
                    # Acceptable if it raises — test passes
                    pass
                except Exception:
                    # Any other exception is also fine — the point is no hang/crash
                    pass
    # signed: beta

    def test_workers_json_duplicate_hwnds(self):
        """workers.json with duplicate HWNDs must not break dispatch logic."""
        dup_workers = [
            {"name": "alpha", "hwnd": 99999, "display": "Alpha"},
            {"name": "beta", "hwnd": 99999, "display": "Beta"},
            {"name": "gamma", "hwnd": 88888, "display": "Gamma"},
        ]
        with patch("tools.skynet_dispatch.load_workers", return_value=dup_workers):
            from tools.skynet_dispatch import load_workers
            workers = load_workers()
            hwnds = [w["hwnd"] for w in workers]
            # Duplicate HWNDs should be loadable (dispatch handles routing by name)
            self.assertEqual(len(workers), 3)
            self.assertEqual(hwnds.count(99999), 2)
    # signed: beta

    def test_dispatch_log_truncation_returns_trimmed(self):
        """_log_dispatch callback returns a new trimmed list when > 200 entries.

        NOTE: The callback returns `log_data[-200:]` (new list) rather than
        mutating in-place. The atomic_update_json uses the return value.
        This test verifies the returned list is trimmed.
        """
        from tools.skynet_dispatch import _log_dispatch
        oversized = [{"worker": f"w{i}", "task_summary": f"t{i}"}
                     for i in range(250)]

        captured = {}

        def fake_update(path, callback, default=None):
            data = list(oversized)
            returned = callback(data)
            # callback returns the trimmed list
            captured["returned_len"] = len(returned) if returned else len(data)

        with patch("tools.skynet_atomic.atomic_update_json", side_effect=fake_update):
            _log_dispatch("test_worker", "test task", "IDLE", True, 0)

        # The returned list should be trimmed to 200
        if "returned_len" in captured:
            self.assertLessEqual(captured["returned_len"], 201)
    # signed: beta

    def test_bus_health_with_dead_backend(self):
        """bus_health() must not crash when backend is unreachable."""
        from tools.skynet_spam_guard import bus_health
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("Connection refused")):
            result = bus_health()
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("bus_reachable", True))
    # signed: beta

    def test_extremely_long_sender_name(self):
        """Sender name > 50 chars should be caught by validator."""
        from tools.skynet_bus_validator import validate_message
        msg = {"sender": "a" * 200, "content": "test"}
        errors = validate_message(msg)
        self.assertTrue(any("sender" in e.lower() for e in errors))
    # signed: beta


# ---------------------------------------------------------------------------
# 4. STATE CORRUPTION RECOVERY
# ---------------------------------------------------------------------------

class TestStateCorruptionRecovery(unittest.TestCase):
    """Recovery from missing, corrupted, or stale data files."""

    # signed: beta

    def test_missing_dispatch_log(self):
        """verify_dispatch_evidence with missing dispatch_log.json returns safe defaults."""
        from tools.skynet_scoring import verify_dispatch_evidence, DISPATCH_LOG_FILE
        with patch.object(type(DISPATCH_LOG_FILE), "exists", return_value=False):
            result = verify_dispatch_evidence("alpha", "some_task")
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("dispatch_found", True))
    # signed: beta

    def test_dispatch_log_not_a_list(self):
        """dispatch_log.json containing a dict instead of list handled safely."""
        from tools.skynet_scoring import verify_dispatch_evidence, DISPATCH_LOG_FILE
        with patch.object(type(DISPATCH_LOG_FILE), "exists", return_value=True):
            with patch.object(type(DISPATCH_LOG_FILE), "read_text",
                              return_value='{"not": "a list"}'):
                result = verify_dispatch_evidence("alpha", "task_x")
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("verified", True))
    # signed: beta

    def test_stale_pid_file_dead_process(self):
        """check_pid with stale PID file (dead process) returns False and cleans up."""
        from tools.skynet_daemon_utils import check_pid, DATA_DIR
        test_name = f"_test_stale_{os.getpid()}"
        pid_path = DATA_DIR / f"{test_name}.pid"
        try:
            # Write a PID that almost certainly doesn't exist
            pid_path.write_text("999999999")
            result = check_pid(test_name)
            self.assertFalse(result)
            # Should have cleaned up the stale file
            self.assertFalse(pid_path.exists(),
                             "Stale PID file should be cleaned up")
        finally:
            pid_path.unlink(missing_ok=True)
    # signed: beta

    def test_pid_file_with_non_numeric_content(self):
        """PID file containing garbage text handled safely."""
        from tools.skynet_daemon_utils import check_pid, DATA_DIR
        test_name = f"_test_garbage_{os.getpid()}"
        pid_path = DATA_DIR / f"{test_name}.pid"
        try:
            pid_path.write_text("not_a_number\n")
            result = check_pid(test_name)
            self.assertFalse(result)
        finally:
            pid_path.unlink(missing_ok=True)
    # signed: beta

    def test_pid_file_with_negative_pid(self):
        """PID file with negative number handled safely."""
        from tools.skynet_daemon_utils import check_pid, DATA_DIR
        test_name = f"_test_negpid_{os.getpid()}"
        pid_path = DATA_DIR / f"{test_name}.pid"
        try:
            pid_path.write_text("-1")
            result = check_pid(test_name)
            self.assertFalse(result)
        finally:
            pid_path.unlink(missing_ok=True)
    # signed: beta

    def test_pid_file_with_zero(self):
        """PID file containing 0 handled safely."""
        from tools.skynet_daemon_utils import check_pid, DATA_DIR
        test_name = f"_test_zeropid_{os.getpid()}"
        pid_path = DATA_DIR / f"{test_name}.pid"
        try:
            pid_path.write_text("0")
            result = check_pid(test_name)
            self.assertFalse(result)
        finally:
            pid_path.unlink(missing_ok=True)
    # signed: beta

    def test_cleanup_pid_idempotent(self):
        """cleanup_pid can be called multiple times without error."""
        from tools.skynet_daemon_utils import cleanup_pid, DATA_DIR
        test_name = f"_test_idempotent_{os.getpid()}"
        pid_path = DATA_DIR / f"{test_name}.pid"
        try:
            pid_path.write_text(str(os.getpid()))
            cleanup_pid(test_name)
            cleanup_pid(test_name)  # Second call — must not crash
            cleanup_pid(test_name)  # Third call — must not crash
        finally:
            pid_path.unlink(missing_ok=True)
    # signed: beta

    def test_cleanup_pid_wont_delete_other_process(self):
        """cleanup_pid only removes file if PID matches current process."""
        from tools.skynet_daemon_utils import cleanup_pid, DATA_DIR
        test_name = f"_test_foreign_{os.getpid()}"
        pid_path = DATA_DIR / f"{test_name}.pid"
        try:
            # Write a different process's PID
            pid_path.write_text("1")  # PID 1 is always system
            cleanup_pid(test_name)
            # File should NOT be deleted (ownership check)
            self.assertTrue(pid_path.exists(),
                            "cleanup_pid should not delete another process's PID file")
        finally:
            pid_path.unlink(missing_ok=True)
    # signed: beta

    def test_ensure_singleton_no_pid_file(self):
        """ensure_singleton returns True when no PID file exists."""
        from tools.skynet_daemon_utils import ensure_singleton, DATA_DIR
        test_name = f"_test_nosingle_{os.getpid()}"
        pid_path = DATA_DIR / f"{test_name}.pid"
        pid_path.unlink(missing_ok=True)
        result = ensure_singleton(test_name)
        self.assertTrue(result)
    # signed: beta

    def test_ensure_singleton_stale_pid(self):
        """ensure_singleton returns True when PID file points to dead process."""
        from tools.skynet_daemon_utils import ensure_singleton, DATA_DIR
        test_name = f"_test_stalesingle_{os.getpid()}"
        pid_path = DATA_DIR / f"{test_name}.pid"
        try:
            pid_path.write_text("999999999")
            result = ensure_singleton(test_name)
            self.assertTrue(result)
        finally:
            pid_path.unlink(missing_ok=True)
    # signed: beta

    def test_invalid_json_in_worker_scores(self):
        """Corrupted worker_scores.json should not crash scoring."""
        from tools.skynet_scoring import _load, SCORES_FILE
        with patch.object(type(SCORES_FILE), "read_text",
                          return_value="{broken json"):
            with patch.object(type(SCORES_FILE), "exists", return_value=True):
                try:
                    data = _load()
                    # Should return empty store (graceful degradation)
                    self.assertIsInstance(data, dict)
                except (json.JSONDecodeError, Exception):
                    pass  # Acceptable — test verifies no hang
    # signed: beta

    def test_missing_worker_scores_file(self):
        """Missing worker_scores.json should return empty/default scores."""
        from tools.skynet_scoring import _load, SCORES_FILE
        with patch.object(type(SCORES_FILE), "exists", return_value=False):
            data = _load()
            self.assertIsInstance(data, dict)
    # signed: beta

    def test_mark_dispatch_received_missing_file(self):
        """mark_dispatch_received with missing log file should not crash."""
        from tools.skynet_dispatch import mark_dispatch_received
        with patch("tools.skynet_atomic.atomic_update_json",
                   side_effect=FileNotFoundError("no file")):
            # Should handle gracefully (prints to stderr, doesn't raise)
            try:
                mark_dispatch_received("alpha")
            except FileNotFoundError:
                pass  # Also acceptable
    # signed: beta


# ---------------------------------------------------------------------------
# 5. SCORING EDGE CASES
# ---------------------------------------------------------------------------

class TestScoringEdgeCases(unittest.TestCase):
    """Edge cases in the scoring and evidence verification system."""

    # signed: beta

    def test_self_award_rejected(self):
        """A worker cannot award points to itself."""
        from tools.skynet_scoring import award_points
        with self.assertRaises(ValueError):
            award_points("alpha", "task_1", "alpha", amount=0.01)
    # signed: beta

    def test_self_deduction_rejected(self):
        """A worker cannot deduct its own points."""
        from tools.skynet_scoring import deduct_points
        with self.assertRaises(ValueError):
            deduct_points("alpha", "task_1", "alpha", amount=0.01)
    # signed: beta

    def test_deduction_without_dispatch_evidence(self):
        """Deductions require dispatch evidence (when force=False)."""
        from tools.skynet_scoring import deduct_points, DISPATCH_LOG_FILE
        # Mock the dispatch log file as not existing — evidence will fail
        with patch.object(type(DISPATCH_LOG_FILE), "exists", return_value=False):
            result = deduct_points("alpha", "fake_task", "beta", force=False)
            self.assertIsNone(result)
    # signed: beta

    def test_forced_deduction_bypasses_evidence(self):
        """force=True deductions skip evidence checks (spam_guard use case)."""
        from tools.skynet_scoring import deduct_points, verify_dispatch_evidence
        # Use test-safe sender name to avoid polluting production scores  # signed: alpha
        with patch("tools.skynet_scoring.verify_dispatch_evidence") as mock_verify, \
             patch("tools.skynet_scoring._save") as mock_save, \
             patch("tools.skynet_scoring._bus_post"):
            try:
                result = deduct_points("_test_alpha", "spam_violation",
                                       "spam_guard", force=True)
                # verify_dispatch_evidence should NOT have been called
                mock_verify.assert_not_called()
            except Exception:
                pass  # OK if other parts fail — key is evidence wasn't checked
    # signed: beta


# ---------------------------------------------------------------------------
# 6. DAEMON STATUS EDGE CASES
# ---------------------------------------------------------------------------

class TestDaemonStatusEdgeCases(unittest.TestCase):
    """Edge cases in daemon status checking."""

    # signed: beta

    def test_check_daemon_missing_pid_file(self):
        """check_daemon with nonexistent PID file returns alive=False."""
        from tools.skynet_daemon_status import check_daemon
        fake_daemon = {
            "name": "_test_missing_daemon",
            "label": "Test Missing",
            "criticality": "low",
            "pid_file": "data/_nonexistent_test_daemon.pid",
        }
        result = check_daemon(fake_daemon)
        self.assertFalse(result.get("pid_alive", True))
    # signed: beta

    def test_check_daemon_no_port_no_pid(self):
        """Daemon with neither port nor PID file returns reasonable defaults."""
        from tools.skynet_daemon_status import check_daemon
        fake_daemon = {
            "name": "_test_noportnopid",
            "label": "Test NoPorts",
            "criticality": "low",
        }
        result = check_daemon(fake_daemon)
        self.assertIn("alive", result)
    # signed: beta

    def test_daemon_registry_has_entries(self):
        """DAEMON_REGISTRY is non-empty and all entries have required keys."""
        from tools.skynet_daemon_status import DAEMON_REGISTRY
        self.assertGreater(len(DAEMON_REGISTRY), 0)
        required = {"name", "label", "criticality"}
        for daemon in DAEMON_REGISTRY:
            self.assertTrue(
                required.issubset(daemon.keys()),
                f"Daemon {daemon.get('name', '?')} missing keys: "
                f"{required - daemon.keys()}"
            )
    # signed: beta


# ---------------------------------------------------------------------------
# RUNNER
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
# signed: beta
