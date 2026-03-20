# signed: consultant
# expanded: signed: gamma
"""Comprehensive tests for tools/skynet_idle_monitor.py."""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, call

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import tools.skynet_idle_monitor as idle_monitor


# ── Original test ────────────────────────────────────────────────────────

def test_self_invoke_task_requires_joint_consultant_dashboard_verification():
    prompt = idle_monitor.SELF_INVOKE_TASK

    assert "Codex Consultant" in prompt
    assert "Gemini Consultant" in prompt
    assert "data/consultant_state.json" in prompt
    assert "data/gemini_consultant_state.json" in prompt
    assert "http://localhost:8421/consultants" in prompt
    assert "http://localhost:8421/leadership" in prompt
    assert "http://localhost:8421/dashboard/data" in prompt
    assert "http://localhost:8420/bus/messages?limit=30" in prompt
    assert "BOTH consultants" in prompt
    assert "reporting to Skynet" in prompt


# ── NEW: SELF_INVOKE_TASK content checks ──────────────────────────────────

def test_self_invoke_task_is_nonempty_string():
    assert isinstance(idle_monitor.SELF_INVOKE_TASK, str)
    assert len(idle_monitor.SELF_INVOKE_TASK) > 100


def test_self_invoke_task_mentions_bus():
    assert "bus" in idle_monitor.SELF_INVOKE_TASK.lower()


def test_self_invoke_task_mentions_workers():
    prompt = idle_monitor.SELF_INVOKE_TASK
    # Should reference worker names or worker concept
    assert any(w in prompt.lower() for w in ["worker", "alpha", "beta", "gamma", "delta"])


# ── NEW: PID_FILE and LOG_FILE constants ──────────────────────────────────

def test_pid_file_path_exists():
    assert hasattr(idle_monitor, "PID_FILE")
    assert "idle_monitor" in str(idle_monitor.PID_FILE)


def test_log_file_path_exists():
    assert hasattr(idle_monitor, "LOG_FILE")


# ── NEW: log() function ──────────────────────────────────────────────────

def test_log_writes_to_file(tmp_path):
    old_log_file = idle_monitor.LOG_FILE
    try:
        idle_monitor.LOG_FILE = tmp_path / "test_idle.log"
        idle_monitor.log("test message 123")
        content = idle_monitor.LOG_FILE.read_text(encoding="utf-8")
        assert "test message 123" in content
    finally:
        idle_monitor.LOG_FILE = old_log_file


def test_log_prints_to_stdout(capsys):
    idle_monitor.log("stdout check")
    captured = capsys.readouterr()
    assert "stdout check" in captured.out


def test_log_survives_unwritable_path(capsys):
    old_log_file = idle_monitor.LOG_FILE
    try:
        idle_monitor.LOG_FILE = Path("Z:\\nonexistent\\dir\\file.log")
        # Should not raise — prints warning instead
        idle_monitor.log("should not crash")
        captured = capsys.readouterr()
        assert "should not crash" in captured.out
    finally:
        idle_monitor.LOG_FILE = old_log_file


# ── NEW: scan_worker() ───────────────────────────────────────────────────

def test_scan_worker_returns_state():
    mock_engine = MagicMock()
    mock_scan = MagicMock()
    mock_scan.state = "IDLE"
    mock_engine.scan.return_value = mock_scan

    result = idle_monitor.scan_worker(mock_engine, 12345)
    assert result == "IDLE"
    mock_engine.scan.assert_called_once_with(12345)


def test_scan_worker_returns_unknown_on_error():
    mock_engine = MagicMock()
    mock_engine.scan.side_effect = RuntimeError("COM failed")

    result = idle_monitor.scan_worker(mock_engine, 99999)
    assert result == "UNKNOWN"


def test_scan_worker_returns_processing():
    mock_engine = MagicMock()
    mock_scan = MagicMock()
    mock_scan.state = "PROCESSING"
    mock_engine.scan.return_value = mock_scan

    result = idle_monitor.scan_worker(mock_engine, 54321)
    assert result == "PROCESSING"


# ── NEW: bus_post() ──────────────────────────────────────────────────────

def test_bus_post_uses_spam_guard():
    with patch("tools.skynet_idle_monitor.bus_post.__module__", "tools.skynet_idle_monitor"):
        with patch("tools.skynet_spam_guard.guarded_publish") as mock_gp:
            idle_monitor.bus_post("test_sender", "test_topic", "test_type", "test_content")
            mock_gp.assert_called_once()
            msg = mock_gp.call_args[0][0]
            assert msg["sender"] == "test_sender"
            assert msg["topic"] == "test_topic"
            assert msg["type"] == "test_type"
            assert msg["content"] == "test_content"


def test_bus_post_falls_back_on_spam_guard_failure():
    with patch("tools.skynet_spam_guard.guarded_publish", side_effect=Exception("spam guard down")):
        with patch("urllib.request.urlopen") as mock_url:
            idle_monitor.bus_post("fallback", "topic", "type", "content")
            mock_url.assert_called_once()


# ── NEW: get_workers() ───────────────────────────────────────────────────

def test_get_workers_returns_worker_list(tmp_path, monkeypatch):
    workers_file = tmp_path / "data" / "workers.json"
    workers_file.parent.mkdir(parents=True, exist_ok=True)
    workers_data = {
        "workers": [
            {"name": "alpha", "hwnd": 1111},
            {"name": "beta", "hwnd": 2222},
        ]
    }
    workers_file.write_text(json.dumps(workers_data), encoding="utf-8")
    monkeypatch.setattr(idle_monitor, "ROOT", tmp_path)

    result = idle_monitor.get_workers()
    assert len(result) == 2
    assert result[0]["name"] == "alpha"


# ── NEW: dispatch_self_invoke() ───────────────────────────────────────────

def test_dispatch_self_invoke_with_pending_work():
    with patch("tools.skynet_delivery.pull_pending_work", return_value="Fix bug #42") as mock_pull, \
         patch("tools.skynet_delivery.deliver_self_invoke", return_value={"success": True, "latency_ms": 50}) as mock_deliver, \
         patch.object(idle_monitor, "bus_post"):

        result = idle_monitor.dispatch_self_invoke("alpha")
        assert result is True
        mock_pull.assert_called_once_with("alpha")
        mock_deliver.assert_called_once()
        # Should use the pending task, not generic SELF_INVOKE_TASK
        call_args = mock_deliver.call_args
        assert call_args[0][1] == "Fix bug #42"


def test_dispatch_self_invoke_falls_back_to_default():
    with patch("tools.skynet_delivery.pull_pending_work", return_value=None), \
         patch("tools.skynet_delivery.deliver_self_invoke", return_value={"success": True, "latency_ms": 30}) as mock_deliver, \
         patch.object(idle_monitor, "bus_post"):

        result = idle_monitor.dispatch_self_invoke("beta")
        assert result is True
        call_args = mock_deliver.call_args
        assert call_args[0][1] == idle_monitor.SELF_INVOKE_TASK


def test_dispatch_self_invoke_handles_delivery_failure():
    with patch("tools.skynet_delivery.pull_pending_work", return_value=None), \
         patch("tools.skynet_delivery.deliver_self_invoke", return_value={"success": False, "detail": "HWND dead"}) as mock_deliver, \
         patch.object(idle_monitor, "bus_post"):

        result = idle_monitor.dispatch_self_invoke("gamma")
        assert result is False


def test_dispatch_self_invoke_handles_exception():
    with patch("tools.skynet_delivery.pull_pending_work", side_effect=Exception("boom")), \
         patch.object(idle_monitor, "bus_post"):

        result = idle_monitor.dispatch_self_invoke("delta")
        assert result is False
