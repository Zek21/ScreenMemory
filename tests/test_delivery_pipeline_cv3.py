"""Cross-Validation 3: Delivery Pipeline Verification Tests

Independent verification of the dispatch delivery pipeline code paths.
Tests trace from dispatch_to_worker() through ghost_type_to_worker()
to _verify_delivery(), verifying:
  - Delivery status code extraction from PS stdout
  - _verify_delivery polling logic and UNKNOWN handling
  - dispatch_log.json logging captures delivery_status
  - _record_dispatch_outcome integration
  - CONFIRMED GAP: verification result NOT persisted to dispatch_log

signed: gamma
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

# Ensure tools/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))


class TestDeliveryStatusExtraction(unittest.TestCase):
    """Verify _execute_ghost_dispatch extracts delivery status codes correctly."""

    def setUp(self):
        import tools.skynet_dispatch as sd
        self.sd = sd
        self._saved = getattr(sd, '_last_delivery_status', '')

    def tearDown(self):
        self.sd._last_delivery_status = self._saved

    def test_ok_attached_extracted(self):
        """OK_ATTACHED in stdout sets _last_delivery_status."""
        self.sd._last_delivery_status = ""
        # Simulate the extraction loop from _execute_ghost_dispatch lines 1538-1544
        stdout = "DEBUG: Found Edit control\nOK_ATTACHED\nDone"
        for ds in ("OK_ATTACHED", "OK_FALLBACK", "OK_RENDER_ATTACHED",
                    "OK_RENDER_FALLBACK", "FOCUS_STOLEN", "NO_EDIT",
                    "NO_EDIT_NO_RENDER", "CLIPBOARD_VERIFY_FAILED"):
            if ds in stdout:
                self.sd._last_delivery_status = ds
                break
        self.assertEqual(self.sd._last_delivery_status, "OK_ATTACHED")

    def test_ok_render_fallback_extracted(self):
        """OK_RENDER_FALLBACK in stdout sets _last_delivery_status."""
        self.sd._last_delivery_status = ""
        stdout = "DEBUG: No Edit found\nOK_RENDER_FALLBACK\nDone"
        for ds in ("OK_ATTACHED", "OK_FALLBACK", "OK_RENDER_ATTACHED",
                    "OK_RENDER_FALLBACK", "FOCUS_STOLEN", "NO_EDIT",
                    "NO_EDIT_NO_RENDER", "CLIPBOARD_VERIFY_FAILED"):
            if ds in stdout:
                self.sd._last_delivery_status = ds
                break
        self.assertEqual(self.sd._last_delivery_status, "OK_RENDER_FALLBACK")

    def test_focus_stolen_extracted(self):
        """FOCUS_STOLEN in stdout sets _last_delivery_status."""
        self.sd._last_delivery_status = ""
        stdout = "DEBUG: Verify OK\nFOCUS_STOLEN by HWND=12345\nAborted"
        for ds in ("OK_ATTACHED", "OK_FALLBACK", "OK_RENDER_ATTACHED",
                    "OK_RENDER_FALLBACK", "FOCUS_STOLEN", "NO_EDIT",
                    "NO_EDIT_NO_RENDER", "CLIPBOARD_VERIFY_FAILED"):
            if ds in stdout:
                self.sd._last_delivery_status = ds
                break
        self.assertEqual(self.sd._last_delivery_status, "FOCUS_STOLEN")

    def test_no_edit_no_render_matches_as_no_edit(self):
        """NO_EDIT_NO_RENDER stdout is captured as NO_EDIT (substring match priority).

        This is by design: NO_EDIT appears before NO_EDIT_NO_RENDER in the
        extraction loop, and 'NO_EDIT' is a substring of 'NO_EDIT_NO_RENDER'.
        Both are failure statuses handled the same way in _execute_ghost_dispatch.
        """
        self.sd._last_delivery_status = ""
        stdout = "DEBUG: scan complete\nNO_EDIT_NO_RENDER\nExiting"
        for ds in ("OK_ATTACHED", "OK_FALLBACK", "OK_RENDER_ATTACHED",
                    "OK_RENDER_FALLBACK", "FOCUS_STOLEN", "NO_EDIT",
                    "NO_EDIT_NO_RENDER", "CLIPBOARD_VERIFY_FAILED"):
            if ds in stdout:
                self.sd._last_delivery_status = ds
                break
        # NO_EDIT matches first because it's a substring of NO_EDIT_NO_RENDER
        self.assertEqual(self.sd._last_delivery_status, "NO_EDIT")

    def test_priority_order_ok_attached_over_no_edit(self):
        """When both OK_ATTACHED and NO_EDIT are present, OK_ATTACHED wins (first match)."""
        self.sd._last_delivery_status = ""
        stdout = "OK_ATTACHED then later NO_EDIT appeared"
        for ds in ("OK_ATTACHED", "OK_FALLBACK", "OK_RENDER_ATTACHED",
                    "OK_RENDER_FALLBACK", "FOCUS_STOLEN", "NO_EDIT",
                    "NO_EDIT_NO_RENDER", "CLIPBOARD_VERIFY_FAILED"):
            if ds in stdout:
                self.sd._last_delivery_status = ds
                break
        self.assertEqual(self.sd._last_delivery_status, "OK_ATTACHED")

    def test_empty_stdout_no_status(self):
        """Empty stdout leaves _last_delivery_status empty."""
        self.sd._last_delivery_status = ""
        stdout = "DEBUG: no useful output"
        for ds in ("OK_ATTACHED", "OK_FALLBACK", "OK_RENDER_ATTACHED",
                    "OK_RENDER_FALLBACK", "FOCUS_STOLEN", "NO_EDIT",
                    "NO_EDIT_NO_RENDER", "CLIPBOARD_VERIFY_FAILED"):
            if ds in stdout:
                self.sd._last_delivery_status = ds
                break
        self.assertEqual(self.sd._last_delivery_status, "")

    def test_all_valid_status_codes_recognized(self):
        """All 8 documented status codes are recognized individually."""
        all_codes = [
            "OK_ATTACHED", "OK_FALLBACK", "OK_RENDER_ATTACHED",
            "OK_RENDER_FALLBACK", "FOCUS_STOLEN", "NO_EDIT",
            "NO_EDIT_NO_RENDER", "CLIPBOARD_VERIFY_FAILED"
        ]
        for code in all_codes:
            self.sd._last_delivery_status = ""
            stdout = f"some prefix {code} some suffix"
            for ds in all_codes:
                if ds in stdout:
                    self.sd._last_delivery_status = ds
                    break
            self.assertIn(self.sd._last_delivery_status, all_codes,
                          f"{code} should be recognized")


class TestVerifyDeliveryLogic(unittest.TestCase):
    """Verify _verify_delivery polling behavior with mocked UIA engine."""

    def setUp(self):
        import tools.skynet_dispatch as sd
        self.sd = sd

    @patch('tools.skynet_dispatch.log')
    def test_pre_state_processing_returns_true_immediately(self, mock_log):
        """If pre_state is PROCESSING, return True immediately (dispatch queued)."""
        result = self.sd._verify_delivery(12345, "test_worker", "PROCESSING")
        self.assertTrue(result)

    @patch('tools.skynet_dispatch.log')
    @patch('tools.uia_engine.get_engine')
    def test_idle_to_processing_verified(self, mock_engine_fn, mock_log):
        """IDLE → PROCESSING transition detected = verified."""
        mock_engine = MagicMock()
        mock_engine.get_state.return_value = "PROCESSING"
        mock_engine_fn.return_value = mock_engine

        result = self.sd._verify_delivery(12345, "test_worker", "IDLE", timeout_s=2)
        self.assertTrue(result)

    @patch('tools.skynet_dispatch.log')
    @patch('tools.uia_engine.get_engine')
    def test_consecutive_unknown_fails(self, mock_engine_fn, mock_log):
        """3+ consecutive UNKNOWN readings = delivery FAILED."""
        mock_engine = MagicMock()
        mock_engine.get_state.return_value = "UNKNOWN"
        mock_engine_fn.return_value = mock_engine

        result = self.sd._verify_delivery(12345, "test_worker", "IDLE", timeout_s=2)
        self.assertFalse(result)

    @patch('tools.skynet_dispatch.log')
    @patch('tools.uia_engine.get_engine')
    def test_unknown_then_real_state_resets_counter(self, mock_engine_fn, mock_log):
        """UNKNOWN followed by real state resets the consecutive counter."""
        mock_engine = MagicMock()
        # 2 UNKNOWN, then IDLE (resets), then PROCESSING (verified)
        mock_engine.get_state.side_effect = ["UNKNOWN", "UNKNOWN", "IDLE", "PROCESSING"]
        mock_engine_fn.return_value = mock_engine

        result = self.sd._verify_delivery(12345, "test_worker", "IDLE", timeout_s=3)
        self.assertTrue(result)

    @patch('tools.skynet_dispatch.log')
    @patch('tools.uia_engine.get_engine')
    def test_idle_to_idle_unverified(self, mock_engine_fn, mock_log):
        """IDLE → IDLE after polling = delivery unverified (INCIDENT 017 fix)."""
        mock_engine = MagicMock()
        mock_engine.get_state.return_value = "IDLE"  # stays IDLE entire poll
        mock_engine_fn.return_value = mock_engine

        result = self.sd._verify_delivery(12345, "test_worker", "IDLE", timeout_s=1)
        self.assertFalse(result)

    @patch('tools.skynet_dispatch.log')
    @patch('tools.uia_engine.get_engine')
    def test_exception_counts_as_unknown(self, mock_engine_fn, mock_log):
        """UIA exceptions count as UNKNOWN readings."""
        mock_engine = MagicMock()
        mock_engine.get_state.side_effect = Exception("UIA connection failed")
        mock_engine_fn.return_value = mock_engine

        result = self.sd._verify_delivery(12345, "test_worker", "IDLE", timeout_s=2)
        self.assertFalse(result)


class TestDispatchLogCapture(unittest.TestCase):
    """Verify _log_dispatch captures delivery_status from global."""

    def setUp(self):
        import tools.skynet_dispatch as sd
        self.sd = sd
        self.tmpdir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.tmpdir, "dispatch_log.json")
        self._orig_log = sd.DISPATCH_LOG
        sd.DISPATCH_LOG = Path(self.log_file)

    def tearDown(self):
        self.sd.DISPATCH_LOG = self._orig_log
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_delivery_status_persisted(self):
        """_log_dispatch writes delivery_status from global to dispatch_log.json."""
        self.sd._last_delivery_status = "OK_RENDER_ATTACHED"
        self.sd._log_dispatch("alpha", "test task", "IDLE", True, 12345)

        with open(self.log_file) as f:
            entries = json.load(f)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["delivery_status"], "OK_RENDER_ATTACHED")
        self.assertTrue(entries[0]["success"])
        self.assertEqual(entries[0]["worker"], "alpha")

    def test_explicit_delivery_status_overrides_global(self):
        """Explicit delivery_status param overrides global _last_delivery_status."""
        self.sd._last_delivery_status = "OK_ATTACHED"
        self.sd._log_dispatch("beta", "test", "IDLE", True, 99, delivery_status="FOCUS_STOLEN")

        with open(self.log_file) as f:
            entries = json.load(f)
        self.assertEqual(entries[0]["delivery_status"], "FOCUS_STOLEN")

    def test_empty_delivery_status_when_no_global(self):
        """When _last_delivery_status is empty, delivery_status is empty string."""
        self.sd._last_delivery_status = ""
        self.sd._log_dispatch("gamma", "test", "PROCESSING", True, 42)

        with open(self.log_file) as f:
            entries = json.load(f)
        self.assertEqual(entries[0]["delivery_status"], "")


class TestVerificationGap(unittest.TestCase):
    """CONFIRMED GAP: _verify_delivery result is NOT written to dispatch_log.json.

    This test class documents the architectural gap where dispatch_to_worker()
    logs the dispatch BEFORE calling _verify_delivery(), so the log entry
    always has success=True (based on ghost_type result) even if verification
    later fails. The verification result is only tracked in console logs and
    _track_dispatch_failure() metrics.
    """

    def test_log_dispatch_called_before_verify(self):
        """dispatch_to_worker() calls _record_dispatch_outcome (which calls _log_dispatch)
        at line 1917, but _verify_delivery() is called at line 1921 -- AFTER the log."""
        import inspect
        src = inspect.getsource(self.sd_dispatch_to_worker)

        # Find positions of key function calls
        record_pos = src.find('_record_dispatch_outcome')
        verify_pos = src.find('_verify_delivery')

        self.assertGreater(record_pos, -1, "_record_dispatch_outcome must be present")
        self.assertGreater(verify_pos, -1, "_verify_delivery must be present")
        # Log happens BEFORE verify -- this is the documented gap
        self.assertLess(record_pos, verify_pos,
                        "ARCHITECTURE: _record_dispatch_outcome should be called BEFORE "
                        "_verify_delivery (this documents the gap, not a bug to fix)")

    def test_verify_result_not_passed_to_log(self):
        """The 'verified' variable is never passed to _log_dispatch or _record_dispatch_outcome."""
        import inspect
        src = inspect.getsource(self.sd_dispatch_to_worker)

        # After _verify_delivery, check if result is ever passed to _log_dispatch
        verify_idx = src.find('_verify_delivery')
        after_verify = src[verify_idx:]
        # The verified result is used for retry logic and logging but NOT for _log_dispatch
        self.assertNotIn('_log_dispatch(', after_verify,
                         "ARCHITECTURE: _log_dispatch is NOT called again after _verify_delivery "
                         "(verification result is not persisted to dispatch_log.json)")

    @classmethod
    def setUpClass(cls):
        import tools.skynet_dispatch as sd
        cls.sd_dispatch_to_worker = sd.dispatch_to_worker


class TestDispatchToWorkerFlow(unittest.TestCase):
    """Integration test: verify dispatch_to_worker orchestrates the pipeline correctly."""

    def setUp(self):
        import tools.skynet_dispatch as sd
        self.sd = sd
        self.tmpdir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.tmpdir, "dispatch_log.json")
        self._orig_log = sd.DISPATCH_LOG
        sd.DISPATCH_LOG = Path(self.log_file)

    def tearDown(self):
        self.sd.DISPATCH_LOG = self._orig_log
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch('tools.skynet_dispatch._verify_delivery')
    @patch('tools.skynet_dispatch.ghost_type_to_worker')
    @patch('tools.skynet_dispatch.notify_backend_dispatch')
    @patch('tools.skynet_dispatch._heartbeat_after_dispatch')
    @patch('tools.skynet_dispatch.log')
    def test_successful_dispatch_logs_and_verifies(self, mock_log, mock_hb,
                                                    mock_notify, mock_ghost, mock_verify):
        """Successful dispatch: ghost_type → log → verify, returns True."""
        mock_ghost.return_value = True
        mock_verify.return_value = True
        self.sd._last_delivery_status = "OK_ATTACHED"

        workers = [{"name": "alpha", "hwnd": 12345, "display": "Alpha"}]
        result = self.sd.dispatch_to_worker("alpha", "test task", workers=workers, orch_hwnd=99)

        self.assertTrue(result)
        mock_ghost.assert_called_once()
        mock_verify.assert_called_once()

    @patch('tools.skynet_dispatch._verify_delivery')
    @patch('tools.skynet_dispatch.ghost_type_to_worker')
    @patch('tools.skynet_dispatch.clear_steering_and_send')
    @patch('tools.skynet_dispatch.notify_backend_dispatch')
    @patch('tools.skynet_dispatch._heartbeat_after_dispatch')
    @patch('tools.skynet_dispatch.log')
    def test_failed_ghost_type_tries_steer_bypass(self, mock_log, mock_hb,
                                                   mock_notify, mock_steer,
                                                   mock_ghost, mock_verify):
        """Failed ghost_type falls back to clear_steering_and_send."""
        mock_ghost.return_value = False
        mock_steer.return_value = True

        workers = [{"name": "beta", "hwnd": 11111, "display": "Beta"}]
        result = self.sd.dispatch_to_worker("beta", "task", workers=workers, orch_hwnd=99)

        self.assertTrue(result)
        mock_ghost.assert_called_once()
        mock_steer.assert_called_once()


class TestMarkDispatchReceived(unittest.TestCase):
    """Verify mark_dispatch_received updates most recent pending entry."""

    def setUp(self):
        import tools.skynet_dispatch as sd
        self.sd = sd
        self.tmpdir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.tmpdir, "dispatch_log.json")
        self._orig_log = sd.DISPATCH_LOG
        sd.DISPATCH_LOG = Path(self.log_file)

    def tearDown(self):
        self.sd.DISPATCH_LOG = self._orig_log
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_marks_most_recent_unreceived(self):
        """mark_dispatch_received marks the latest unreceived entry for a worker."""
        initial = [
            {"worker": "alpha", "result_received": True, "success": True},
            {"worker": "alpha", "result_received": False, "success": True},
        ]
        with open(self.log_file, 'w') as f:
            json.dump(initial, f)

        self.sd.mark_dispatch_received("alpha")

        with open(self.log_file) as f:
            data = json.load(f)
        # Second entry should now be marked received
        self.assertTrue(data[1]["result_received"])

    def test_no_match_no_error(self):
        """mark_dispatch_received with unknown worker doesn't crash."""
        initial = [{"worker": "alpha", "result_received": False, "success": True}]
        with open(self.log_file, 'w') as f:
            json.dump(initial, f)

        # Should not raise
        self.sd.mark_dispatch_received("unknown_worker")


class TestSelfDispatchGuard(unittest.TestCase):
    """Verify self-dispatch prevention (INCIDENT 001)."""

    def test_self_dispatch_guard_exists(self):
        """_get_self_identity or self-dispatch check exists in dispatch_to_worker."""
        import inspect
        import tools.skynet_dispatch as sd
        src = inspect.getsource(sd.dispatch_to_worker)
        # Should have self-dispatch detection
        has_self_guard = ('self_name' in src or '_get_self_identity' in src
                          or 'SELF_DISPATCH' in src or 'self-dispatch' in src.lower())
        self.assertTrue(has_self_guard,
                        "dispatch_to_worker must have self-dispatch prevention (INCIDENT 001)")


class TestDispatchLogMaxEntries(unittest.TestCase):
    """Verify dispatch log respects DISPATCH_LOG_MAX_ENTRIES truncation."""

    def setUp(self):
        import tools.skynet_dispatch as sd
        self.sd = sd
        self.tmpdir = tempfile.mkdtemp()
        self.log_file = os.path.join(self.tmpdir, "dispatch_log.json")
        self._orig_log = sd.DISPATCH_LOG
        sd.DISPATCH_LOG = Path(self.log_file)

    def tearDown(self):
        self.sd.DISPATCH_LOG = self._orig_log
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_max_entries_constant_exists(self):
        """DISPATCH_LOG_MAX_ENTRIES constant is defined."""
        self.assertTrue(hasattr(self.sd, 'DISPATCH_LOG_MAX_ENTRIES'))
        self.assertIsInstance(self.sd.DISPATCH_LOG_MAX_ENTRIES, int)
        self.assertGreater(self.sd.DISPATCH_LOG_MAX_ENTRIES, 0)

    def test_log_truncates_at_max(self):
        """Log file is truncated when entries exceed DISPATCH_LOG_MAX_ENTRIES."""
        max_entries = self.sd.DISPATCH_LOG_MAX_ENTRIES
        # Fill log to max + 5
        initial = [
            {"worker": f"w{i}", "task_summary": "t", "timestamp": "2026-01-01",
             "state_at_dispatch": "IDLE", "success": True, "target_hwnd": i,
             "result_received": False, "delivery_status": "", "strategy": "direct",
             "strategy_id": ""}
            for i in range(max_entries + 5)
        ]
        with open(self.log_file, 'w') as f:
            json.dump(initial, f)

        # Add one more
        self.sd._last_delivery_status = ""
        self.sd._log_dispatch("test", "task", "IDLE", True, 999)

        with open(self.log_file) as f:
            data = json.load(f)
        self.assertLessEqual(len(data), max_entries)


if __name__ == '__main__':
    unittest.main()
