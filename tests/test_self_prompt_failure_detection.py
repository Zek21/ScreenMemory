"""Tests for smart failure detection in _update_worker_model_from_result().

Verifies that recovery/positive keywords negate false failure counting,
and that system senders are not counted as worker failures.
"""
# signed: beta
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import skynet_self_prompt as sp


class TestFailureDetection(unittest.TestCase):
    """Test _update_worker_model_from_result() smart keyword matching."""

    def setUp(self):
        """Create a SelfPromptDaemon and inject worker models."""
        self.daemon = sp.SelfPromptDaemon()
        # Inject worker models for alpha and beta
        for name in ("alpha", "beta"):
            m = sp.WorkerCognitiveModel(name, [])
            m.tasks_completed = 0
            m.tasks_failed = 0
            m.last_result_quality = None
            self.daemon.worker_models[name] = m

    def _msg(self, sender, content):
        return {"sender": sender, "content": content}

    # --- True failures (should be counted) ---

    def test_plain_error_counted_as_failure(self):
        self.daemon._update_worker_model_from_result(
            self._msg("alpha", "error in module X"))
        m = self.daemon.worker_models["alpha"]
        self.assertEqual(m.tasks_failed, 1)
        self.assertEqual(m.last_result_quality, "failure")

    def test_plain_failed_counted_as_failure(self):
        self.daemon._update_worker_model_from_result(
            self._msg("alpha", "Task failed completely"))
        m = self.daemon.worker_models["alpha"]
        self.assertEqual(m.tasks_failed, 1)

    def test_plain_timeout_counted_as_failure(self):
        self.daemon._update_worker_model_from_result(
            self._msg("beta", "Request timeout after 30s"))
        m = self.daemon.worker_models["beta"]
        self.assertEqual(m.tasks_failed, 1)

    # --- False positives (recovery keywords negate failure) ---

    def test_fixed_errors_not_counted_as_failure(self):
        self.daemon._update_worker_model_from_result(
            self._msg("alpha", "Fixed 3 errors in the code"))
        m = self.daemon.worker_models["alpha"]
        self.assertEqual(m.tasks_failed, 0)
        self.assertEqual(m.tasks_completed, 1)
        self.assertEqual(m.last_result_quality, "success")

    def test_clipboard_verify_failed_recovery_not_failure(self):
        self.daemon._update_worker_model_from_result(
            self._msg("alpha", "CLIPBOARD_VERIFY_FAILED recovery succeeded"))
        m = self.daemon.worker_models["alpha"]
        self.assertEqual(m.tasks_failed, 0)
        self.assertEqual(m.tasks_completed, 1)
        self.assertEqual(m.last_result_quality, "success")

    def test_all_tests_passed_no_errors_not_failure(self):
        self.daemon._update_worker_model_from_result(
            self._msg("beta", "All tests passed, no errors found"))
        m = self.daemon.worker_models["beta"]
        self.assertEqual(m.tasks_failed, 0)
        self.assertEqual(m.tasks_completed, 1)

    def test_timeout_increased_for_results_not_failure(self):
        self.daemon._update_worker_model_from_result(
            self._msg("alpha", "Timeout increased to 90s for better results, resolved"))
        m = self.daemon.worker_models["alpha"]
        self.assertEqual(m.tasks_failed, 0)
        self.assertEqual(m.tasks_completed, 1)

    def test_error_resolved_not_failure(self):
        self.daemon._update_worker_model_from_result(
            self._msg("beta", "error encountered but resolved successfully"))
        m = self.daemon.worker_models["beta"]
        self.assertEqual(m.tasks_failed, 0)
        self.assertEqual(m.tasks_completed, 1)

    def test_failed_then_passed_not_failure(self):
        self.daemon._update_worker_model_from_result(
            self._msg("alpha", "Build failed initially but passed on retry"))
        m = self.daemon.worker_models["alpha"]
        self.assertEqual(m.tasks_failed, 0)
        self.assertEqual(m.tasks_completed, 1)

    # --- System senders (should be ignored entirely) ---

    def test_system_sender_ignored(self):
        self.daemon._update_worker_model_from_result(
            self._msg("system", "failed=42 errors=3"))
        # system is not in worker_models, so nothing should change
        for m in self.daemon.worker_models.values():
            self.assertEqual(m.tasks_failed, 0)
            self.assertEqual(m.tasks_completed, 0)

    def test_self_prompt_sender_ignored(self):
        self.daemon._update_worker_model_from_result(
            self._msg("self_prompt", "SELF_PROMPT_HEALTH failed=5"))
        for m in self.daemon.worker_models.values():
            self.assertEqual(m.tasks_failed, 0)
            self.assertEqual(m.tasks_completed, 0)

    def test_monitor_sender_ignored(self):
        self.daemon._update_worker_model_from_result(
            self._msg("monitor", "heartbeat timeout detected"))
        for m in self.daemon.worker_models.values():
            self.assertEqual(m.tasks_failed, 0)
            self.assertEqual(m.tasks_completed, 0)

    def test_introspection_sender_ignored(self):
        self.daemon._update_worker_model_from_result(
            self._msg("introspection", "error rate high"))
        for m in self.daemon.worker_models.values():
            self.assertEqual(m.tasks_failed, 0)
            self.assertEqual(m.tasks_completed, 0)

    # --- Normal success (no failure keywords) ---

    def test_clean_success_counted(self):
        self.daemon._update_worker_model_from_result(
            self._msg("alpha", "Task completed: all 5 files updated"))
        m = self.daemon.worker_models["alpha"]
        self.assertEqual(m.tasks_completed, 1)
        self.assertEqual(m.tasks_failed, 0)
        self.assertEqual(m.last_result_quality, "success")

    # --- Cumulative counting ---

    def test_mixed_results_counted_correctly(self):
        # Real failure
        self.daemon._update_worker_model_from_result(
            self._msg("alpha", "error: disk full"))
        # Recovery (not a failure)
        self.daemon._update_worker_model_from_result(
            self._msg("alpha", "Fixed error from previous run"))
        # Another real failure
        self.daemon._update_worker_model_from_result(
            self._msg("alpha", "timeout connecting to server"))
        m = self.daemon.worker_models["alpha"]
        self.assertEqual(m.tasks_failed, 2)
        self.assertEqual(m.tasks_completed, 1)


if __name__ == "__main__":
    unittest.main()
# signed: beta
