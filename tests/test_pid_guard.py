"""Tests for tools/skynet_pid_guard.py — atomic PID guard for Skynet daemons.

Tests cover: acquire/release lifecycle, stale PID cleanup, atomic creation,
double-acquire prevention, owned-by-current-process check, and cleanup helpers.

Created by worker delta as part of codebase test coverage audit.
"""
# signed: delta

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from tools.skynet_pid_guard import (
    acquire_pid_guard,
    release_pid_guard,
    _pid_alive,
    _pid_matches_daemon,
    _owned_by_current_process,
    _cleanup_pid_file,
    _active_guards,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_pid_dir(tmp_path):
    """Provide a temporary directory for PID files."""
    pid_dir = tmp_path / "data"
    pid_dir.mkdir()
    return pid_dir


@pytest.fixture
def pid_file(tmp_pid_dir):
    """Return an absolute path to a fresh PID file location."""
    return tmp_pid_dir / "test_daemon.pid"


# ── acquire_pid_guard tests ─────────────────────────────────────────────────


class TestAcquirePidGuard:
    """Test the main acquire_pid_guard function."""

    def test_acquire_creates_pid_file(self, pid_file):
        """acquire_pid_guard should create a PID file with current PID."""
        result = acquire_pid_guard(str(pid_file), "test_daemon")
        assert result is True
        assert pid_file.exists()
        assert int(pid_file.read_text().strip()) == os.getpid()
        # Cleanup
        release_pid_guard(str(pid_file))
        # signed: delta

    def test_acquire_returns_false_when_live_instance(self, pid_file):
        """If a live instance holds the PID file, acquire should return False."""
        # Write current PID — simulating a live holder
        pid_file.write_text(str(os.getpid()))

        # Mock _pid_matches_daemon to return True (pretend it's our daemon)
        with patch("tools.skynet_pid_guard._pid_matches_daemon", return_value=True):
            result = acquire_pid_guard(str(pid_file), "test_daemon")
        assert result is False
        # signed: delta

    def test_acquire_clears_stale_pid(self, pid_file):
        """If PID file has a dead PID, acquire should clear it and succeed."""
        # Write a PID that doesn't exist
        pid_file.write_text("99999999")

        with patch("tools.skynet_pid_guard._pid_alive", return_value=False):
            result = acquire_pid_guard(str(pid_file), "test_daemon")
        assert result is True
        assert int(pid_file.read_text().strip()) == os.getpid()
        release_pid_guard(str(pid_file))
        # signed: delta

    def test_acquire_clears_stale_pid_wrong_daemon(self, pid_file):
        """If PID is alive but doesn't match daemon name, treat as stale."""
        pid_file.write_text(str(os.getpid()))

        # pid_alive returns True, but _pid_matches_daemon returns False
        with patch("tools.skynet_pid_guard._pid_alive", return_value=True), \
             patch("tools.skynet_pid_guard._pid_matches_daemon", return_value=False):
            result = acquire_pid_guard(str(pid_file), "test_daemon")
        assert result is True
        release_pid_guard(str(pid_file))
        # signed: delta

    def test_acquire_creates_parent_dirs(self, tmp_path):
        """acquire_pid_guard should create parent directories if needed."""
        deep_pid = tmp_path / "a" / "b" / "c" / "daemon.pid"
        result = acquire_pid_guard(str(deep_pid), "test_daemon")
        assert result is True
        assert deep_pid.exists()
        release_pid_guard(str(deep_pid))
        # signed: delta


# ── release_pid_guard tests ─────────────────────────────────────────────────


class TestReleasePidGuard:
    """Test the release_pid_guard function."""

    def test_release_removes_owned_pid_file(self, pid_file):
        """release should remove the PID file if we own it."""
        acquire_pid_guard(str(pid_file), "test_daemon")
        assert pid_file.exists()
        release_pid_guard(str(pid_file))
        assert not pid_file.exists()
        # signed: delta

    def test_release_noop_if_not_owned(self, pid_file):
        """release should NOT remove PID file owned by another process."""
        pid_file.write_text("12345")  # Not our PID
        release_pid_guard(str(pid_file))
        assert pid_file.exists()  # File should still be there
        # signed: delta

    def test_release_noop_if_missing(self, pid_file):
        """release should not raise if PID file doesn't exist."""
        release_pid_guard(str(pid_file))  # Should not raise
        # signed: delta

    def test_release_idempotent(self, pid_file):
        """Calling release twice should not raise."""
        acquire_pid_guard(str(pid_file), "test_daemon")
        release_pid_guard(str(pid_file))
        release_pid_guard(str(pid_file))  # Second call should be safe
        # signed: delta


# ── Helper function tests ───────────────────────────────────────────────────


class TestHelpers:
    """Test internal helper functions."""

    def test_pid_alive_current_process(self):
        """Current process PID should be alive."""
        assert _pid_alive(os.getpid()) is True
        # signed: delta

    def test_pid_alive_zero(self):
        """PID 0 should return False."""
        assert _pid_alive(0) is False
        # signed: delta

    def test_pid_alive_negative(self):
        """Negative PID should return False."""
        assert _pid_alive(-1) is False
        # signed: delta

    def test_owned_by_current_process_true(self, pid_file):
        """File with our PID should be owned."""
        pid_file.write_text(str(os.getpid()))
        assert _owned_by_current_process(pid_file) is True
        # signed: delta

    def test_owned_by_current_process_false(self, pid_file):
        """File with different PID should not be owned."""
        pid_file.write_text("12345")
        assert _owned_by_current_process(pid_file) is False
        # signed: delta

    def test_owned_by_current_process_missing(self, pid_file):
        """Missing file should return False."""
        assert _owned_by_current_process(pid_file) is False
        # signed: delta

    def test_cleanup_pid_file_owned(self, pid_file):
        """cleanup should remove file if owned."""
        pid_file.write_text(str(os.getpid()))
        _cleanup_pid_file(pid_file)
        assert not pid_file.exists()
        # signed: delta

    def test_cleanup_pid_file_not_owned(self, pid_file):
        """cleanup should NOT remove file if not owned."""
        pid_file.write_text("12345")
        _cleanup_pid_file(pid_file)
        assert pid_file.exists()
        # signed: delta

    def test_pid_matches_daemon_mocked(self):
        """_pid_matches_daemon should check psutil process info."""
        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["python", "tools/skynet_monitor.py"]
        mock_proc.name.return_value = "python.exe"

        with patch("psutil.Process", return_value=mock_proc):
            assert _pid_matches_daemon(os.getpid(), "skynet_monitor") is True
        # signed: delta

    def test_pid_matches_daemon_wrong_name(self):
        """_pid_matches_daemon should return False for wrong daemon name."""
        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["python", "tools/skynet_watchdog.py"]
        mock_proc.name.return_value = "python.exe"

        with patch("psutil.Process", return_value=mock_proc):
            assert _pid_matches_daemon(os.getpid(), "skynet_monitor") is False
        # signed: delta

    def test_pid_matches_daemon_not_python(self):
        """_pid_matches_daemon should return False if process is not Python."""
        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["node", "server.js"]
        mock_proc.name.return_value = "node.exe"

        with patch("psutil.Process", return_value=mock_proc):
            assert _pid_matches_daemon(os.getpid(), "skynet_monitor") is False
        # signed: delta


# ── Logger parameter test ───────────────────────────────────────────────────


class TestLogger:
    """Test that the logger parameter works correctly."""

    def test_custom_logger_receives_messages(self, pid_file):
        """When a live instance exists, logger should receive warning."""
        pid_file.write_text(str(os.getpid()))
        log_messages = []

        def my_logger(msg, level="INFO"):
            log_messages.append((msg, level))

        with patch("tools.skynet_pid_guard._pid_matches_daemon", return_value=True):
            result = acquire_pid_guard(str(pid_file), "test_daemon", logger=my_logger)

        assert result is False
        assert len(log_messages) > 0
        assert any("already running" in m[0] for m in log_messages)
        # signed: delta
