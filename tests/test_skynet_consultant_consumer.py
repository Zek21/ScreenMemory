"""Tests for tools/skynet_consultant_consumer.py — Consultant bridge prompt queue consumer.

Tests cover: queue polling, ACK flow, bus relay via guarded_publish, mark-complete
lifecycle, graceful shutdown with PID cleanup, signal handling, singleton PID lock,
and error handling for unreachable bridges.

# signed: delta
"""

import json
import os
import signal
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from tools.skynet_consultant_consumer import (
    _pid_path,
    _acquire_pid_lock,
    _release_pid_lock,
    _http_get,
    _http_post,
    _guarded_bus_publish,
    _process_prompt,
    _signal_handler,
    run_consumer,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_data(tmp_path):
    """Set up temporary data directory."""
    return tmp_path


@pytest.fixture(autouse=True)
def reset_shutdown():
    """Reset the global _shutdown flag before each test."""
    import tools.skynet_consultant_consumer as cc
    cc._shutdown = False
    yield
    cc._shutdown = False


# ── PID Lock Tests ──────────────────────────────────────────────────────────

class TestPidLock:
    """Tests for PID file singleton enforcement."""

    def test_pid_path_includes_port(self, tmp_data):
        """PID file path includes the port number."""
        with patch("tools.skynet_consultant_consumer.ROOT", tmp_data):
            p = _pid_path(8422)
        assert "8422" in str(p)
        assert p.name == "consultant_consumer_8422.pid"
        # signed: delta

    def test_acquire_lock_creates_pid_file(self, tmp_data):
        """Acquiring lock creates PID file with current PID."""
        with patch("tools.skynet_consultant_consumer.ROOT", tmp_data):
            pid_file = tmp_data / "data" / "consultant_consumer_9999.pid"
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            with patch("tools.skynet_consultant_consumer._pid_path", return_value=pid_file):
                result = _acquire_pid_lock(9999)
        assert result is True
        assert pid_file.exists()
        assert pid_file.read_text().strip() == str(os.getpid())
        # Cleanup
        pid_file.unlink()
        # signed: delta

    def test_release_lock_removes_pid_file(self, tmp_data):
        """Releasing lock removes PID file."""
        pid_file = tmp_data / "consultant_consumer_9999.pid"
        pid_file.write_text(str(os.getpid()))
        with patch("tools.skynet_consultant_consumer._pid_path", return_value=pid_file):
            _release_pid_lock(9999)
        assert not pid_file.exists()
        # signed: delta

    def test_release_lock_only_own_pid(self, tmp_data):
        """Release only removes if PID matches current process."""
        pid_file = tmp_data / "consultant_consumer_9999.pid"
        pid_file.write_text("99999")  # Different PID
        with patch("tools.skynet_consultant_consumer._pid_path", return_value=pid_file):
            _release_pid_lock(9999)
        assert pid_file.exists()  # Should NOT be removed
        # Cleanup
        pid_file.unlink()
        # signed: delta

    def test_acquire_lock_stale_pid(self, tmp_data):
        """Stale PID file (dead process) is overwritten."""
        pid_file = tmp_data / "data" / "consultant_consumer_9999.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("1")  # PID 1 unlikely to be accessible on Windows

        with patch("tools.skynet_consultant_consumer._pid_path", return_value=pid_file):
            # Mock OpenProcess to return 0 (process not found)
            with patch("ctypes.windll.kernel32.OpenProcess", return_value=0):
                result = _acquire_pid_lock(9999)
        assert result is True
        assert pid_file.read_text().strip() == str(os.getpid())
        # Cleanup
        pid_file.unlink()
        # signed: delta


# ── HTTP Helper Tests ───────────────────────────────────────────────────────

class TestHttpHelpers:
    """Tests for _http_get() and _http_post() helper functions."""

    def test_http_get_returns_json(self):
        """Successful GET returns parsed JSON."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"status": "ok"}).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = _http_get("http://localhost:9999/health")
        assert result == {"status": "ok"}
        # signed: delta

    def test_http_get_returns_none_on_error(self):
        """Failed GET returns None."""
        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            result = _http_get("http://localhost:9999/health")
        assert result is None
        # signed: delta

    def test_http_post_sends_json(self):
        """POST sends JSON data and returns parsed response."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"ok": True}).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response) as mock_url:
            result = _http_post("http://localhost:9999/api", {"key": "val"})
        assert result == {"ok": True}
        # Verify the request was created with POST data
        call_args = mock_url.call_args
        req = call_args[0][0]
        assert req.data is not None
        # signed: delta

    def test_http_post_returns_none_on_error(self):
        """Failed POST returns None."""
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            result = _http_post("http://localhost:9999/api", {"key": "val"})
        assert result is None
        # signed: delta


# ── Bus Relay Tests ─────────────────────────────────────────────────────────

class TestGuardedBusPublish:
    """Tests for _guarded_bus_publish() using SpamGuard."""

    def test_successful_publish(self):
        """Successful publish returns True."""
        with patch("tools.skynet_spam_guard.guarded_publish",
                   return_value={"allowed": True}):
            result = _guarded_bus_publish({"sender": "test", "content": "hello"})
        assert result is True
        # signed: delta

    def test_failed_publish_returns_false(self):
        """Failed publish returns False without crashing."""
        with patch("tools.skynet_spam_guard.guarded_publish",
                   side_effect=Exception("guard failed")):
            result = _guarded_bus_publish({"sender": "test", "content": "hello"})
        assert result is False
        # signed: delta

    def test_no_raw_fallback(self):
        """When guarded_publish fails, NO raw fallback is attempted."""
        with patch("tools.skynet_spam_guard.guarded_publish",
                   side_effect=Exception("guard broken")):
            with patch("urllib.request.urlopen") as mock_url:
                _guarded_bus_publish({"sender": "test", "content": "hello"})
        mock_url.assert_not_called()
        # signed: delta


# ── Prompt Processing Lifecycle Tests ───────────────────────────────────────

class TestProcessPrompt:
    """Tests for _process_prompt() — ACK → bus relay → complete lifecycle."""

    def _make_prompt(self, prompt_id="p1", content="test prompt",
                     sender="orchestrator", ptype="directive"):
        return {
            "id": prompt_id,
            "content": content,
            "sender": sender,
            "type": ptype,
            "metadata": {},
        }

    def test_full_lifecycle_success(self):
        """Full ACK → relay → complete lifecycle succeeds."""
        prompt = self._make_prompt()
        with patch("tools.skynet_consultant_consumer._http_post",
                   return_value={"ok": True}) as mock_post:
            with patch("tools.skynet_consultant_consumer._guarded_bus_publish",
                       return_value=True):
                result = _process_prompt(
                    "http://localhost:8422", "consultant", prompt
                )
        assert result is True
        # Should have called POST twice: ACK + complete
        assert mock_post.call_count == 2
        # signed: delta

    def test_ack_failure_aborts(self):
        """If ACK fails after all retries, processing aborts."""
        prompt = self._make_prompt()
        with patch("tools.skynet_consultant_consumer._http_post",
                   return_value=None):
            with patch("tools.skynet_consultant_consumer.MAX_RETRIES", 1):
                with patch("tools.skynet_consultant_consumer.RETRY_DELAY", 0):
                    result = _process_prompt(
                        "http://localhost:8422", "consultant", prompt
                    )
        assert result is False
        # signed: delta

    def test_bus_relay_failure_still_completes(self):
        """If bus relay fails, prompt is still marked complete."""
        prompt = self._make_prompt()
        with patch("tools.skynet_consultant_consumer._http_post",
                   return_value={"ok": True}) as mock_post:
            with patch("tools.skynet_consultant_consumer._guarded_bus_publish",
                       return_value=False):
                result = _process_prompt(
                    "http://localhost:8422", "consultant", prompt
                )
        assert result is True
        # Complete still called with bus_relay_failed status
        complete_call = mock_post.call_args_list[-1]
        assert "bus_relay_failed" in str(complete_call)
        # signed: delta

    def test_missing_prompt_id_skipped(self):
        """Prompt with missing ID is skipped."""
        prompt = {"id": "", "content": "test", "sender": "orch"}
        result = _process_prompt("http://localhost:8422", "consultant", prompt)
        assert result is False
        # signed: delta

    def test_missing_content_skipped(self):
        """Prompt with missing content is skipped."""
        prompt = {"id": "p1", "content": "", "sender": "orch"}
        result = _process_prompt("http://localhost:8422", "consultant", prompt)
        assert result is False
        # signed: delta

    def test_bus_message_format(self):
        """Bus relay message has correct fields."""
        prompt = self._make_prompt(prompt_id="p42", content="research task")
        published_msg = None

        def capture_publish(msg):
            nonlocal published_msg
            published_msg = msg
            return True

        with patch("tools.skynet_consultant_consumer._http_post",
                   return_value={"ok": True}):
            with patch("tools.skynet_consultant_consumer._guarded_bus_publish",
                       side_effect=capture_publish):
                _process_prompt("http://localhost:8422", "consultant", prompt)

        assert published_msg is not None
        assert published_msg["topic"] == "consultant"
        assert published_msg["type"] == "directive"
        assert published_msg["content"] == "research task"
        assert published_msg["metadata"]["prompt_id"] == "p42"
        assert published_msg["metadata"]["consultant_id"] == "consultant"
        # signed: delta

    def test_complete_failure_returns_false(self):
        """If mark-complete fails after all retries, returns False."""
        prompt = self._make_prompt()
        call_count = [0]

        def mock_post(url, data, **kwargs):
            call_count[0] += 1
            if "ack" in url:
                return {"ok": True}
            return None  # complete fails

        with patch("tools.skynet_consultant_consumer._http_post",
                   side_effect=mock_post):
            with patch("tools.skynet_consultant_consumer._guarded_bus_publish",
                       return_value=True):
                with patch("tools.skynet_consultant_consumer.MAX_RETRIES", 1):
                    with patch("tools.skynet_consultant_consumer.RETRY_DELAY", 0):
                        result = _process_prompt(
                            "http://localhost:8422", "consultant", prompt
                        )
        assert result is False
        # signed: delta


# ── Signal Handler Tests ────────────────────────────────────────────────────

class TestSignalHandler:
    """Tests for graceful shutdown signal handling."""

    def test_signal_sets_shutdown_flag(self):
        """Signal handler sets _shutdown to True."""
        import tools.skynet_consultant_consumer as cc
        assert cc._shutdown is False
        _signal_handler(signal.SIGTERM, None)
        assert cc._shutdown is True
        # signed: delta


# ── Consumer Loop Tests ─────────────────────────────────────────────────────

class TestRunConsumer:
    """Tests for run_consumer() main loop behavior."""

    def test_loop_exits_on_shutdown(self):
        """Consumer exits cleanly when _shutdown is set."""
        import tools.skynet_consultant_consumer as cc

        call_count = [0]

        def mock_get(url, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 2:
                cc._shutdown = True
            return None  # No prompts

        with patch("tools.skynet_consultant_consumer._http_get",
                   side_effect=mock_get):
            with patch("tools.skynet_consultant_consumer._guarded_bus_publish",
                       return_value=True):
                with patch("tools.skynet_consultant_consumer.POLL_INTERVAL", 0.01):
                    run_consumer(8422, "consultant")

        assert call_count[0] >= 2
        # signed: delta

    def test_processes_prompt_from_queue(self):
        """Consumer processes a prompt when one is available."""
        import tools.skynet_consultant_consumer as cc

        call_count = [0]
        prompt_data = {
            "prompt": {
                "id": "test1",
                "content": "test content",
                "sender": "orchestrator",
                "type": "directive",
                "metadata": {},
            }
        }

        def mock_get(url, **kwargs):
            nonlocal call_count
            call_count[0] += 1
            if "health" in url:
                return {"service": "consultant_bridge"}
            if call_count[0] == 2:
                return prompt_data
            if call_count[0] >= 3:
                cc._shutdown = True
            return {"prompt": None}

        with patch("tools.skynet_consultant_consumer._http_get",
                   side_effect=mock_get):
            with patch("tools.skynet_consultant_consumer._http_post",
                       return_value={"ok": True}):
                with patch("tools.skynet_consultant_consumer._guarded_bus_publish",
                           return_value=True):
                    with patch("tools.skynet_consultant_consumer.POLL_INTERVAL", 0.01):
                        run_consumer(8422, "consultant")
        # signed: delta

    def test_handles_bridge_unreachable(self):
        """Consumer handles unreachable bridge gracefully."""
        import tools.skynet_consultant_consumer as cc

        call_count = [0]

        def mock_get(url, **kwargs):
            nonlocal call_count
            call_count[0] += 1
            if call_count[0] >= 3:
                cc._shutdown = True
            return None  # Bridge unreachable

        with patch("tools.skynet_consultant_consumer._http_get",
                   side_effect=mock_get):
            with patch("tools.skynet_consultant_consumer._guarded_bus_publish",
                       return_value=True):
                with patch("tools.skynet_consultant_consumer.POLL_INTERVAL", 0.01):
                    run_consumer(8422, "consultant")

        # Should have attempted multiple times without crashing
        assert call_count[0] >= 3
        # signed: delta

    def test_exception_in_loop_continues(self):
        """Unexpected exception in loop doesn't crash the consumer."""
        import tools.skynet_consultant_consumer as cc

        call_count = [0]

        def mock_get(url, **kwargs):
            nonlocal call_count
            call_count[0] += 1
            if call_count[0] == 1:
                return {"service": "bridge"}  # health check
            if call_count[0] == 2:
                raise RuntimeError("unexpected error")
            if call_count[0] >= 3:
                cc._shutdown = True
            return {"prompt": None}

        with patch("tools.skynet_consultant_consumer._http_get",
                   side_effect=mock_get):
            with patch("tools.skynet_consultant_consumer._guarded_bus_publish",
                       return_value=True):
                with patch("tools.skynet_consultant_consumer.POLL_INTERVAL", 0.01):
                    run_consumer(8422, "consultant")

        assert call_count[0] >= 3
        # signed: delta
