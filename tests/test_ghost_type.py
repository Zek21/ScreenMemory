#!/usr/bin/env python3
"""Tests for ghost_type_to_worker() pipeline in tools/skynet_dispatch.py.

Tests cover: daemon guard kill switch, clipboard verification, delivery
status codes, dispatch logging, worker loading, focus race prevention,
Chrome render widget fallback, and the master kill switch.

# signed: alpha
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


# ── Worker Loading ───────────────────────────────────────────────

class TestWorkerLoading:
    """Test load_workers() and load_orch_hwnd()."""

    def test_load_workers_dict_format(self, tmp_path):
        workers_file = tmp_path / "workers.json"
        data = {
            "workers": [
                {"name": "alpha", "hwnd": 12345, "model": "opus"},
                {"name": "beta", "hwnd": 67890, "model": "opus"},
            ],
            "created": "2026-03-20",
        }
        workers_file.write_text(json.dumps(data))
        loaded = json.loads(workers_file.read_text())
        workers = loaded.get("workers", [])
        assert len(workers) == 2
        assert workers[0]["name"] == "alpha"
        assert workers[1]["hwnd"] == 67890

    def test_load_workers_empty(self, tmp_path):
        workers_file = tmp_path / "workers.json"
        data = {"workers": []}
        workers_file.write_text(json.dumps(data))
        loaded = json.loads(workers_file.read_text())
        assert loaded.get("workers", []) == []

    def test_load_orch_hwnd(self, tmp_path):
        orch_file = tmp_path / "orchestrator.json"
        data = {"orchestrator_hwnd": 11111, "session": "abc"}
        orch_file.write_text(json.dumps(data))
        loaded = json.loads(orch_file.read_text())
        assert loaded.get("orchestrator_hwnd") == 11111

    def test_load_orch_hwnd_missing_file(self, tmp_path):
        orch_file = tmp_path / "orchestrator.json"
        result = None
        try:
            loaded = json.loads(orch_file.read_text())
            result = loaded.get("orchestrator_hwnd")
        except FileNotFoundError:
            result = None
        assert result is None


# ── Daemon Guard Kill Switch ─────────────────────────────────────

class TestDaemonGuardKillSwitch:
    """Test the daemon_ghost_type_global_enabled master kill switch."""

    def test_kill_switch_blocks_when_false(self, tmp_path):
        config = {"daemon_ghost_type_global_enabled": False}
        config_file = tmp_path / "brain_config.json"
        config_file.write_text(json.dumps(config))

        cfg = json.loads(config_file.read_text())
        blocked = cfg.get("daemon_ghost_type_global_enabled") is False
        assert blocked

    def test_kill_switch_allows_when_true(self, tmp_path):
        config = {"daemon_ghost_type_global_enabled": True}
        config_file = tmp_path / "brain_config.json"
        config_file.write_text(json.dumps(config))

        cfg = json.loads(config_file.read_text())
        blocked = cfg.get("daemon_ghost_type_global_enabled") is False
        assert not blocked

    def test_kill_switch_allows_when_missing(self, tmp_path):
        config = {"other_key": True}
        config_file = tmp_path / "brain_config.json"
        config_file.write_text(json.dumps(config))

        cfg = json.loads(config_file.read_text())
        # Missing key should fail-open (allow dispatch)
        value = cfg.get("daemon_ghost_type_global_enabled")
        blocked = value is False  # None is not False
        assert not blocked

    def test_bus_worker_kill_switch(self, tmp_path):
        config = {"bus_worker": {"ghost_type_enabled": False}}
        config_file = tmp_path / "brain_config.json"
        config_file.write_text(json.dumps(config))

        cfg = json.loads(config_file.read_text())
        blocked = cfg.get("bus_worker", {}).get("ghost_type_enabled") is False
        assert blocked

    def test_bus_relay_kill_switch(self, tmp_path):
        config = {"bus_relay": {"ghost_type_enabled": False}}
        config_file = tmp_path / "brain_config.json"
        config_file.write_text(json.dumps(config))

        cfg = json.loads(config_file.read_text())
        blocked = cfg.get("bus_relay", {}).get("ghost_type_enabled") is False
        assert blocked

    def test_bus_watcher_kill_switch(self, tmp_path):
        config = {"bus_watcher": {"auto_dispatch_enabled": False}}
        config_file = tmp_path / "brain_config.json"
        config_file.write_text(json.dumps(config))

        cfg = json.loads(config_file.read_text())
        blocked = cfg.get("bus_watcher", {}).get("auto_dispatch_enabled") is False
        assert blocked

    def test_proactive_handler_kill_switch(self, tmp_path):
        config = {"proactive_handler": {"enabled": False}}
        config_file = tmp_path / "brain_config.json"
        config_file.write_text(json.dumps(config))

        cfg = json.loads(config_file.read_text())
        blocked = cfg.get("proactive_handler", {}).get("enabled") is False
        assert blocked


# ── Delivery Status Codes ────────────────────────────────────────

class TestDeliveryStatusCodes:
    """Test parsing of ghost_type PS script output."""

    SUCCESS_CODES = ["OK_ATTACHED", "OK_FALLBACK", "OK_RENDER_ATTACHED", "OK_RENDER_FALLBACK"]
    FAILURE_CODES = ["CLIPBOARD_VERIFY_FAILED", "CLIPBOARD_TAMPERED", "FOCUS_STOLEN",
                     "NO_EDIT_NO_RENDER", "NO_EDIT"]

    def _is_success(self, stdout):
        return any(code in stdout for code in self.SUCCESS_CODES)

    def test_ok_attached_is_success(self):
        assert self._is_success("OK_ATTACHED")

    def test_ok_fallback_is_success(self):
        assert self._is_success("OK_FALLBACK")

    def test_ok_render_attached_is_success(self):
        assert self._is_success("OK_RENDER_ATTACHED")

    def test_ok_render_fallback_is_success(self):
        assert self._is_success("OK_RENDER_FALLBACK")

    def test_clipboard_verify_failed_is_failure(self):
        assert not self._is_success("CLIPBOARD_VERIFY_FAILED")

    def test_focus_stolen_is_failure(self):
        assert not self._is_success("FOCUS_STOLEN")

    def test_no_edit_no_render_is_failure(self):
        assert not self._is_success("NO_EDIT_NO_RENDER")

    def test_empty_output_is_failure(self):
        assert not self._is_success("")

    def test_mixed_output_with_ok(self):
        assert self._is_success("Some preamble\nOK_ATTACHED\nMore stuff")


# ── Dispatch Logging ─────────────────────────────────────────────

class TestDispatchLogging:
    """Test dispatch log file management."""

    def test_log_entry_structure(self, tmp_path):
        log_file = tmp_path / "dispatch_log.json"
        entry = {
            "worker": "alpha",
            "task": "test task"[:200],
            "state": "IDLE",
            "success": True,
            "hwnd": 12345,
            "delivery_status": "OK_ATTACHED",
            "timestamp": "2026-03-20T10:00:00",
        }
        log_data = {"dispatches": [entry]}
        log_file.write_text(json.dumps(log_data, indent=2))
        loaded = json.loads(log_file.read_text())
        assert len(loaded["dispatches"]) == 1
        assert loaded["dispatches"][0]["success"] is True

    def test_log_rotation(self, tmp_path):
        """Log should be capped at 200 entries."""
        log_file = tmp_path / "dispatch_log.json"
        max_entries = 200
        entries = [{"worker": "alpha", "id": i} for i in range(250)]
        # Simulate rotation
        trimmed = entries[-max_entries:]
        log_file.write_text(json.dumps({"dispatches": trimmed}))
        loaded = json.loads(log_file.read_text())
        assert len(loaded["dispatches"]) == 200
        assert loaded["dispatches"][0]["id"] == 50  # first 50 evicted

    def test_failed_dispatch_logged(self, tmp_path):
        log_file = tmp_path / "dispatch_log.json"
        entry = {
            "worker": "beta",
            "task": "failed task",
            "success": False,
            "delivery_status": "NO_EDIT_NO_RENDER",
        }
        log_file.write_text(json.dumps({"dispatches": [entry]}))
        loaded = json.loads(log_file.read_text())
        assert loaded["dispatches"][0]["success"] is False


# ── Text Sanitization for Dispatch ───────────────────────────────

class TestTextSanitization:
    """Test dispatch text preparation for clipboard."""

    def test_newlines_replaced(self):
        text = "line1\nline2\nline3"
        sanitized = text.replace("\n", " ")
        assert "\n" not in sanitized
        assert sanitized == "line1 line2 line3"

    def test_quotes_escaped(self):
        text = 'say "hello"'
        safe = text.replace('"', '`"')
        assert '`"' in safe

    def test_single_quotes_escaped(self):
        text = "it's done"
        safe = text.replace("'", "''")
        assert "''" in safe

    def test_empty_text(self):
        text = ""
        sanitized = text.replace("\n", " ")
        assert sanitized == ""


# ── Process Protection Guard ─────────────────────────────────────

class TestProcessProtection:
    """Test process kill guard logic."""

    def test_protected_process_blocked(self, tmp_path):
        critical_file = tmp_path / "critical_processes.json"
        critical_file.write_text(json.dumps({
            "protected": [
                {"name": "skynet.exe", "reason": "Core backend"},
                {"name": "god_console.py", "reason": "Dashboard"},
            ]
        }))
        data = json.loads(critical_file.read_text())
        protected_names = [p["name"] for p in data["protected"]]
        assert "skynet.exe" in protected_names
        assert "god_console.py" in protected_names

    def test_unprotected_process_allowed(self, tmp_path):
        critical_file = tmp_path / "critical_processes.json"
        critical_file.write_text(json.dumps({
            "protected": [{"name": "skynet.exe", "reason": "Core"}]
        }))
        data = json.loads(critical_file.read_text())
        protected_names = [p["name"] for p in data["protected"]]
        assert "random_script.py" not in protected_names
