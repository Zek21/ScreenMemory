import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class TestModelGuardFixStatuses(unittest.TestCase):
    def test_fix_model_reports_picker_not_ready(self):
        import tools.skynet_model_guard as model_guard

        with patch.object(
            model_guard,
            "_hidden_run",
            return_value=SimpleNamespace(stdout="MODEL_PICKER_NOT_READY\n", stderr="", returncode=0),
        ):
            result = model_guard.fix_model(123, 456)

        self.assertEqual(result, "MODEL_PICKER_NOT_READY")


class TestMonitorGuardrails(unittest.TestCase):
    def test_fix_model_via_uia_rejects_unverified_guard(self):
        import tools.skynet_monitor as monitor

        with patch("tools.skynet_model_guard.fix_model", return_value="MODEL_PICKER_NOT_READY"):
            self.assertFalse(monitor.fix_model_via_uia(123, 0))

    def test_acquire_monitor_pid_guard_refuses_live_matching_owner(self):
        import tools.skynet_monitor as monitor

        with tempfile.TemporaryDirectory() as tmpdir:
            pid_file = Path(tmpdir) / "monitor.pid"
            pid_file.write_text("999", encoding="utf-8")

            old_pid_file = monitor.PID_FILE
            monitor.PID_FILE = pid_file
            try:
                with patch.object(monitor, "_monitor_pid_alive", return_value=True), \
                     patch.object(monitor, "_monitor_pid_matches", return_value=True):
                    result = monitor._acquire_monitor_pid_guard()
                    contents = pid_file.read_text(encoding="utf-8")
            finally:
                monitor.PID_FILE = old_pid_file

        self.assertFalse(result)
        self.assertEqual(contents, "999")

    def test_cleanup_monitor_pid_guard_only_removes_owned_file(self):
        import tools.skynet_monitor as monitor

        with tempfile.TemporaryDirectory() as tmpdir:
            pid_file = Path(tmpdir) / "monitor.pid"
            old_pid_file = monitor.PID_FILE
            monitor.PID_FILE = pid_file
            try:
                with patch.object(monitor.os, "getpid", return_value=123):
                    pid_file.write_text("456", encoding="utf-8")
                    monitor._cleanup_monitor_pid_guard()
                    self.assertTrue(pid_file.exists())

                    pid_file.write_text("123", encoding="utf-8")
                    monitor._cleanup_monitor_pid_guard()
                    self.assertFalse(pid_file.exists())
            finally:
                monitor.PID_FILE = old_pid_file


if __name__ == "__main__":
    unittest.main()
