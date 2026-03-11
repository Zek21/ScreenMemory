import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import god_console
import tools.skynet_agent_telemetry as agent_telemetry


class AgentTelemetrySingletonTests(unittest.TestCase):
    def test_claim_pid_replaces_stale_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "agent_telemetry.pid"
            pid_file.write_text("99999", encoding="utf-8")
            with (
                mock.patch.object(agent_telemetry, "DATA_DIR", Path(tmp)),
                mock.patch.object(agent_telemetry, "PID_FILE", pid_file),
                mock.patch.object(agent_telemetry, "_pid_alive", return_value=False),
            ):
                self.assertTrue(agent_telemetry._claim_pid("agent telemetry"))
                self.assertEqual(pid_file.read_text(encoding="utf-8"), str(os.getpid()))

    def test_cleanup_only_removes_own_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "agent_telemetry.pid"
            with mock.patch.object(agent_telemetry, "PID_FILE", pid_file):
                pid_file.write_text("4242", encoding="utf-8")
                agent_telemetry._cleanup_pid()
                self.assertTrue(pid_file.exists())
                pid_file.write_text(str(os.getpid()), encoding="utf-8")
                agent_telemetry._cleanup_pid()
                self.assertFalse(pid_file.exists())


class GodConsoleSingletonTests(unittest.TestCase):
    def test_claim_pid_rejects_live_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "god_console.pid"
            pid_file.write_text("4242", encoding="utf-8")
            with (
                mock.patch.object(god_console, "DATA_DIR", Path(tmp)),
                mock.patch.object(god_console, "PID_FILE", pid_file),
                mock.patch.object(god_console, "_pid_alive", return_value=True),
            ):
                self.assertFalse(god_console._claim_pid("god-console"))
                self.assertEqual(pid_file.read_text(encoding="utf-8"), "4242")

    def test_cleanup_only_removes_own_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "god_console.pid"
            with mock.patch.object(god_console, "PID_FILE", pid_file):
                pid_file.write_text("4242", encoding="utf-8")
                god_console._cleanup_pid()
                self.assertTrue(pid_file.exists())
                pid_file.write_text(str(os.getpid()), encoding="utf-8")
                god_console._cleanup_pid()
                self.assertFalse(pid_file.exists())


if __name__ == "__main__":
    unittest.main()
