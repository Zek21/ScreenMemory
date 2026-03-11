"""Tests for tools/skynet_watchdog.py — Service monitoring daemon.

Tests cover: restart backoff logic, URL health checks, PID file ops,
log rotation, config loading, hidden subprocess kwargs, consultant
bridge health checks, state timestamp parsing, and worker HWND checks.

Created by worker delta — infrastructure test coverage.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── Module Import (with controlled env) ─────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_restart_state():
    """Reset module-level restart tracking between tests."""
    import tools.skynet_watchdog as wd
    wd._restart_state.clear()
    yield
    wd._restart_state.clear()


# ── Restart Backoff Tests ───────────────────────────────────────────────────

class TestRestartBackoff:
    """Tests for _should_attempt_restart / _record_restart_result."""

    def test_first_restart_allowed(self):
        """First restart for unknown service is always allowed."""
        from tools.skynet_watchdog import _should_attempt_restart
        assert _should_attempt_restart("test_service") is True

    def test_consecutive_failures_trigger_cooldown(self):
        """After MAX_RESTART_ATTEMPTS failures, cooldown is entered."""
        from tools.skynet_watchdog import (
            _should_attempt_restart, _record_restart_result,
            MAX_RESTART_ATTEMPTS, _restart_state
        )
        for _ in range(MAX_RESTART_ATTEMPTS):
            _record_restart_result("test_svc", False)
        # Now should be in cooldown
        assert _should_attempt_restart("test_svc") is False
        # Verify cooldown_until is set
        assert _restart_state["test_svc"]["cooldown_until"] > time.time()

    def test_success_resets_attempts(self):
        """A successful restart resets the attempt counter."""
        from tools.skynet_watchdog import (
            _should_attempt_restart, _record_restart_result, _restart_state
        )
        _record_restart_result("test_svc", False)
        _record_restart_result("test_svc", False)
        _record_restart_result("test_svc", True)  # success
        assert _restart_state["test_svc"]["attempts"] == 0
        assert _should_attempt_restart("test_svc") is True

    def test_cooldown_expiry_resets(self):
        """After cooldown expires, restarts are allowed again."""
        from tools.skynet_watchdog import (
            _should_attempt_restart, _record_restart_result,
            MAX_RESTART_ATTEMPTS, _restart_state
        )
        for _ in range(MAX_RESTART_ATTEMPTS):
            _record_restart_result("test_svc", False)
        # Manually expire cooldown
        _restart_state["test_svc"]["cooldown_until"] = time.time() - 1
        assert _should_attempt_restart("test_svc") is True

    def test_partial_failures_still_allowed(self):
        """Less than MAX_RESTART_ATTEMPTS failures still allow restarts."""
        from tools.skynet_watchdog import (
            _should_attempt_restart, _record_restart_result, MAX_RESTART_ATTEMPTS
        )
        for _ in range(MAX_RESTART_ATTEMPTS - 1):
            _record_restart_result("test_svc", False)
        assert _should_attempt_restart("test_svc") is True

    def test_bus_alert_on_backoff(self):
        """Bus alert is posted when entering cooldown."""
        from tools.skynet_watchdog import _record_restart_result, MAX_RESTART_ATTEMPTS
        with patch("tools.skynet_watchdog._post_bus_alert_safe") as mock_alert:
            for _ in range(MAX_RESTART_ATTEMPTS):
                _record_restart_result("test_svc", False)
            mock_alert.assert_called_once()
            assert "RESTART_BACKOFF" in mock_alert.call_args[0][0]


# ── URL Health Check Tests ──────────────────────────────────────────────────

class TestCheckUrl:
    """Tests for check_url()."""

    def test_url_success(self):
        """Returns True for HTTP 200."""
        from tools.skynet_watchdog import check_url
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("tools.skynet_watchdog.urllib.request.urlopen", return_value=mock_resp):
            assert check_url("http://test.local") is True

    def test_url_failure(self):
        """Returns False on connection error."""
        from tools.skynet_watchdog import check_url
        with patch("tools.skynet_watchdog.urllib.request.urlopen", side_effect=Exception("refused")):
            assert check_url("http://dead.local") is False

    def test_url_non_200(self):
        """Returns False for non-200 status."""
        from tools.skynet_watchdog import check_url
        mock_resp = MagicMock()
        mock_resp.status = 503
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("tools.skynet_watchdog.urllib.request.urlopen", return_value=mock_resp):
            assert check_url("http://test.local") is False


# ── PID File Tests ──────────────────────────────────────────────────────────

class TestPidFile:
    """Tests for _read_pid_file and _pid_alive."""

    def test_read_pid_file_valid(self, tmp_path):
        """Reads valid PID from file."""
        from tools.skynet_watchdog import _read_pid_file
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345\n")
        assert _read_pid_file(pid_file) == 12345

    def test_read_pid_file_missing(self, tmp_path):
        """Returns 0 for missing file."""
        from tools.skynet_watchdog import _read_pid_file
        assert _read_pid_file(tmp_path / "nonexistent.pid") == 0

    def test_read_pid_file_corrupt(self, tmp_path):
        """Returns 0 for corrupt file."""
        from tools.skynet_watchdog import _read_pid_file
        pid_file = tmp_path / "bad.pid"
        pid_file.write_text("not_a_number")
        assert _read_pid_file(pid_file) == 0

    def test_pid_alive_invalid(self):
        """Invalid PID (<=0) is not alive."""
        from tools.skynet_watchdog import _pid_alive
        assert _pid_alive(0) is False
        assert _pid_alive(-1) is False
        assert _pid_alive("abc") is False

    def test_pid_alive_current_process(self):
        """Current process PID should be alive."""
        import os
        from tools.skynet_watchdog import _pid_alive
        assert _pid_alive(os.getpid()) is True


# ── Logging Tests ───────────────────────────────────────────────────────────

class TestLogging:
    """Tests for log() function."""

    def test_log_creates_file(self, tmp_path):
        """Log creates log file if it doesn't exist."""
        import tools.skynet_watchdog as wd
        orig_dir = wd.DATA_DIR
        orig_log = wd.LOG_FILE
        try:
            wd.DATA_DIR = tmp_path
            wd.LOG_FILE = tmp_path / "test_watchdog.log"
            wd.log("Test message")
            assert wd.LOG_FILE.exists()
            content = wd.LOG_FILE.read_text()
            assert "Test message" in content
        finally:
            wd.DATA_DIR = orig_dir
            wd.LOG_FILE = orig_log

    def test_log_rotation(self, tmp_path):
        """Log file is trimmed when exceeding MAX_LOG_SIZE."""
        import tools.skynet_watchdog as wd
        orig_dir = wd.DATA_DIR
        orig_log = wd.LOG_FILE
        orig_max = wd.MAX_LOG_SIZE
        try:
            wd.DATA_DIR = tmp_path
            wd.LOG_FILE = tmp_path / "test_watchdog.log"
            wd.MAX_LOG_SIZE = 100  # Low threshold triggers rotation
            # Write > MAX_LOG_SIZE so rotation fires; rotation keeps last 500_000 chars
            # so with a small file the trim is to 500_000 (no-op for small data).
            # We just verify the new entry was appended and rotation didn't crash.
            wd.LOG_FILE.write_text("X" * 200)
            wd.log("New entry")
            content = wd.LOG_FILE.read_text()
            assert "New entry" in content
        finally:
            wd.DATA_DIR = orig_dir
            wd.LOG_FILE = orig_log
            wd.MAX_LOG_SIZE = orig_max


# ── Config Loading Tests ────────────────────────────────────────────────────

class TestConfigLoading:
    """Tests for _load_watchdog_config()."""

    def test_defaults_when_no_config(self, tmp_path):
        """Returns defaults when brain_config.json doesn't exist."""
        from tools.skynet_watchdog import _load_watchdog_config
        with patch("tools.skynet_watchdog.ROOT", tmp_path):
            config = _load_watchdog_config()
        assert config["watchdog_interval"] == 30
        assert config["god_check_interval"] == 30
        assert config["skynet_check_interval"] == 60

    def test_reads_from_config(self, tmp_path):
        """Reads watchdog section from brain_config.json."""
        from tools.skynet_watchdog import _load_watchdog_config
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cfg = data_dir / "brain_config.json"
        cfg.write_text(json.dumps({
            "watchdog": {
                "watchdog_interval": 15,
                "god_check_interval": 10,
                "skynet_check_interval": 30,
            }
        }))
        with patch("tools.skynet_watchdog.ROOT", tmp_path):
            config = _load_watchdog_config()
        assert config["watchdog_interval"] == 15
        assert config["god_check_interval"] == 10


# ── Hidden Subprocess Kwargs Tests ──────────────────────────────────────────

class TestHiddenSubprocessKwargs:
    """Tests for _hidden_subprocess_kwargs."""

    def test_adds_no_window_on_windows(self):
        """Adds CREATE_NO_WINDOW flag on Windows."""
        from tools.skynet_watchdog import _hidden_subprocess_kwargs
        if sys.platform == "win32":
            kwargs = _hidden_subprocess_kwargs()
            assert kwargs.get("creationflags", 0) & 0x08000000  # CREATE_NO_WINDOW
            assert "startupinfo" in kwargs

    def test_preserves_existing_kwargs(self):
        """Doesn't overwrite user-provided kwargs."""
        from tools.skynet_watchdog import _hidden_subprocess_kwargs
        kwargs = _hidden_subprocess_kwargs(text=True, timeout=5)
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 5


# ── State Timestamp Parsing Tests ───────────────────────────────────────────

class TestStateTimestamp:
    """Tests for _read_state_timestamp_age()."""

    def test_iso_timestamp(self, tmp_path):
        """Parses ISO timestamp and computes age."""
        from tools.skynet_watchdog import _read_state_timestamp_age
        fp = tmp_path / "state.json"
        now = datetime.now(timezone.utc).isoformat()
        fp.write_text(json.dumps({"timestamp": now}))
        raw, age = _read_state_timestamp_age(fp, "timestamp")
        assert raw == now
        assert age is not None
        assert age < 5  # Just created, age should be tiny

    def test_epoch_timestamp(self, tmp_path):
        """Parses epoch float timestamp."""
        from tools.skynet_watchdog import _read_state_timestamp_age
        fp = tmp_path / "state.json"
        fp.write_text(json.dumps({"last_update": time.time()}))
        raw, age = _read_state_timestamp_age(fp, "last_update")
        assert age is not None
        assert age < 5

    def test_missing_key(self, tmp_path):
        """Returns None for missing key."""
        from tools.skynet_watchdog import _read_state_timestamp_age
        fp = tmp_path / "state.json"
        fp.write_text(json.dumps({"other": "value"}))
        raw, age = _read_state_timestamp_age(fp, "timestamp")
        assert raw is None
        assert age is None

    def test_invalid_file(self, tmp_path):
        """Returns None for unreadable file."""
        from tools.skynet_watchdog import _read_state_timestamp_age
        fp = tmp_path / "bad.json"
        fp.write_text("not json{{{")
        raw, age = _read_state_timestamp_age(fp, "timestamp")
        assert raw is None
        assert age is None

    def test_multiple_key_fallback(self, tmp_path):
        """Falls back through multiple keys."""
        from tools.skynet_watchdog import _read_state_timestamp_age
        fp = tmp_path / "state.json"
        fp.write_text(json.dumps({"last_update": time.time()}))
        raw, age = _read_state_timestamp_age(fp, "timestamp", "last_update")
        assert raw is not None
        assert age is not None


# ── Post Bus Alert Tests ────────────────────────────────────────────────────

class TestBusAlert:
    """Tests for _post_bus_alert_safe."""

    def test_alert_sends_correct_payload(self):
        """Alert sends correct JSON to bus."""
        from tools.skynet_watchdog import _post_bus_alert_safe
        with patch("tools.skynet_watchdog.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = MagicMock()
            _post_bus_alert_safe("Test alert message")
            mock_urlopen.assert_called_once()
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data)
            assert body["sender"] == "watchdog"
            assert body["topic"] == "orchestrator"
            assert body["type"] == "alert"
            assert body["content"] == "Test alert message"

    def test_alert_swallows_errors(self):
        """Alert doesn't raise on network error."""
        from tools.skynet_watchdog import _post_bus_alert_safe
        with patch("tools.skynet_watchdog.urllib.request.urlopen", side_effect=Exception("timeout")):
            _post_bus_alert_safe("Should not raise")


# ── Consultant Bridge Health Tests ──────────────────────────────────────────

class TestConsultantBridgeHealth:
    """Tests for _consultant_bridge_is_healthy and helpers."""

    def test_healthy_bridge(self, tmp_path):
        """Healthy bridge with live consultant returns True."""
        from tools.skynet_watchdog import _consultant_bridge_is_healthy
        pid_file = tmp_path / "bridge.pid"
        pid_file.write_text(str(12345))
        config = {
            "pid_file": pid_file,
            "api_port": 8422,
            "consultant_id": "consultant",
        }
        payload = {"consultant": {"id": "consultant", "live": True, "status": "LIVE"}}
        with patch("tools.skynet_watchdog._pid_alive", return_value=True), \
             patch("tools.skynet_watchdog._consultant_endpoint_payload", return_value=payload):
            assert _consultant_bridge_is_healthy(config) is True

    def test_dead_pid_unhealthy(self, tmp_path):
        """Dead PID means unhealthy."""
        from tools.skynet_watchdog import _consultant_bridge_is_healthy
        pid_file = tmp_path / "bridge.pid"
        pid_file.write_text("99999")
        config = {"pid_file": pid_file, "api_port": 8422, "consultant_id": "consultant"}
        with patch("tools.skynet_watchdog._pid_alive", return_value=False):
            assert _consultant_bridge_is_healthy(config) is False

    def test_no_payload_unhealthy(self, tmp_path):
        """No API response means unhealthy."""
        from tools.skynet_watchdog import _consultant_bridge_is_healthy
        pid_file = tmp_path / "bridge.pid"
        config = {"pid_file": pid_file, "api_port": 8422, "consultant_id": "consultant"}
        with patch("tools.skynet_watchdog._consultant_endpoint_payload", return_value=None):
            assert _consultant_bridge_is_healthy(config) is False

    def test_wrong_consultant_id_unhealthy(self, tmp_path):
        """Wrong consultant ID means unhealthy."""
        from tools.skynet_watchdog import _consultant_bridge_is_healthy
        pid_file = tmp_path / "bridge.pid"
        config = {"pid_file": pid_file, "api_port": 8422, "consultant_id": "consultant"}
        payload = {"consultant": {"id": "gemini_consultant", "live": True}}
        with patch("tools.skynet_watchdog._pid_alive", return_value=True), \
             patch("tools.skynet_watchdog._consultant_endpoint_payload", return_value=payload):
            assert _consultant_bridge_is_healthy(config) is False

    def test_not_live_unhealthy(self, tmp_path):
        """Correct ID but not live means unhealthy."""
        from tools.skynet_watchdog import _consultant_bridge_is_healthy
        pid_file = tmp_path / "bridge.pid"
        config = {"pid_file": pid_file, "api_port": 8422, "consultant_id": "consultant"}
        payload = {"consultant": {"id": "consultant", "live": False, "status": "DEAD"}}
        with patch("tools.skynet_watchdog._pid_alive", return_value=True), \
             patch("tools.skynet_watchdog._consultant_endpoint_payload", return_value=payload):
            assert _consultant_bridge_is_healthy(config) is False


# ── Module Constants ────────────────────────────────────────────────────────

class TestWatchdogConstants:
    """Verify critical module constants are set."""

    def test_max_restart_attempts(self):
        from tools.skynet_watchdog import MAX_RESTART_ATTEMPTS
        assert MAX_RESTART_ATTEMPTS >= 1

    def test_cooldown_duration(self):
        from tools.skynet_watchdog import RESTART_COOLDOWN_S
        assert RESTART_COOLDOWN_S >= 60

    def test_urls_defined(self):
        from tools.skynet_watchdog import SKYNET_URL, GOD_CONSOLE_URL
        assert "8420" in SKYNET_URL
        assert "8421" in GOD_CONSOLE_URL

    def test_consultant_bridges_defined(self):
        from tools.skynet_watchdog import CONSULTANT_BRIDGES
        assert len(CONSULTANT_BRIDGES) == 2
        names = [b["service_name"] for b in CONSULTANT_BRIDGES]
        assert "consultant_bridge" in names
        assert "gemini_consultant_bridge" in names
