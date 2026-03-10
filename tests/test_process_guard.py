"""Tests for process guard: is_process_protected() and guard_process_kill()."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from skynet_dispatch import is_process_protected, guard_process_kill

# Fixture data matching real critical_processes.json structure
MOCK_CRITICAL = {
    "protected_names": [
        "skynet.exe",
        "god_console.py",
        "skynet_watchdog.py",
        "skynet_sse_daemon.py",
        "skynet_monitor.py",
        "skynet_overseer.py",
    ],
    "processes": [
        {"pid": 1001, "name": "skynet.exe", "role": "backend", "protected": True},
        {"pid": 2002, "name": "god_console.py", "role": "god_console", "protected": True},
        {"pid": 3003, "hwnd": 99999, "name": "orchestrator", "role": "orchestrator", "protected": True},
    ],
}


@pytest.fixture(autouse=True)
def mock_critical_procs():
    """Patch _load_critical_processes to return deterministic test data."""
    with patch("skynet_dispatch._load_critical_processes", return_value=MOCK_CRITICAL):
        yield


# ── is_process_protected() tests ───────────────────────────────────────────

class TestIsProcessProtected:
    def test_protected_by_exact_name(self):
        protected, reason = is_process_protected(name="skynet.exe")
        assert protected is True
        assert "skynet.exe" in reason

    def test_protected_by_name_substring(self):
        protected, _ = is_process_protected(name="god_console.py")
        assert protected is True

    def test_protected_by_name_case_insensitive(self):
        protected, _ = is_process_protected(name="SKYNET.EXE")
        assert protected is True

    def test_protected_by_pid(self):
        protected, reason = is_process_protected(pid=1001)
        assert protected is True
        assert "backend" in reason

    def test_protected_by_hwnd(self):
        protected, reason = is_process_protected(pid=99999)
        assert protected is True
        assert "orchestrator" in reason

    def test_unknown_name_allowed(self):
        protected, reason = is_process_protected(name="notepad.exe")
        assert protected is False
        assert reason == ""

    def test_unknown_pid_allowed(self):
        protected, reason = is_process_protected(pid=77777)
        assert protected is False
        assert reason == ""

    def test_none_args_allowed(self):
        protected, reason = is_process_protected(pid=None, name=None)
        assert protected is False

    def test_all_protected_names_blocked(self):
        for name in MOCK_CRITICAL["protected_names"]:
            protected, _ = is_process_protected(name=name)
            assert protected is True, f"{name} should be protected"


# ── guard_process_kill() tests ─────────────────────────────────────────────

class TestGuardProcessKill:
    @patch("urllib.request.urlopen")
    def test_protected_returns_false(self, mock_urlopen):
        result = guard_process_kill(name="skynet.exe", caller="test")
        assert result is False

    def test_unknown_returns_true(self):
        result = guard_process_kill(name="notepad.exe", caller="test")
        assert result is True

    @patch("urllib.request.urlopen")
    def test_protected_pid_returns_false(self, mock_urlopen):
        result = guard_process_kill(pid=2002, caller="test")
        assert result is False

    def test_unknown_pid_returns_true(self):
        result = guard_process_kill(pid=55555, caller="test")
        assert result is True

    @patch("urllib.request.urlopen")
    def test_blocked_posts_alert_to_bus(self, mock_urlopen):
        guard_process_kill(name="skynet_watchdog.py", caller="worker_delta")
        mock_urlopen.assert_called_once()
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        body = json.loads(req.data)
        assert body["sender"] == "process_guard"
        assert body["topic"] == "orchestrator"
        assert body["type"] == "alert"
        assert "BLOCKED" in body["content"]
        assert "worker_delta" in body["content"]
