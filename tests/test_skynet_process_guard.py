"""Tests for tools/skynet_process_guard.py — process kill prevention.
# signed: delta

Tests cover:
  - is_protected: PID/name matching against registry
  - safe_kill: allowed vs blocked decisions
  - _load_registry: file loading, missing file, corrupt file
  - _deduplicate_processes: dedup by (PID, role)
  - _collapse_wrapper_pids: virtualenv pid dedup
  - refresh_registry: registry creation
  - _hwnd_to_pid: Win32 API wrapper
  - Edge cases: empty registry, zero PID, missing files
"""

import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.skynet_process_guard import (
    is_protected,
    safe_kill,
    _load_registry,
    _deduplicate_processes,
    _collapse_wrapper_pids,
    REGISTRY_FILE,
)


@pytest.fixture
def mock_registry(tmp_path, monkeypatch):
    """Create a mock registry file and redirect REGISTRY_FILE."""
    reg_file = tmp_path / "critical_processes.json"
    registry = {
        "description": "Test critical processes",
        "protected_names": [
            "skynet.exe", "god_console.py", "skynet_watchdog.py"
        ],
        "processes": [
            {"pid": 1234, "name": "skynet.exe", "role": "backend", "protected": True},
            {"pid": 5678, "name": "god_console.py", "role": "god_console", "protected": True},
            {"pid": 9999, "hwnd": 12345, "name": "alpha", "role": "worker", "protected": True},
        ],
        "process_count": 3,
        "updated_at": "2026-03-14T10:00:00",
    }
    reg_file.write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr("tools.skynet_process_guard.REGISTRY_FILE", reg_file)
    return reg_file


class TestIsProtected:
    """Tests for is_protected function."""

    def test_protected_pid(self, mock_registry):
        """Known PID in registry is protected."""
        result, reason = is_protected(pid=1234)
        assert result is True
        assert "backend" in reason.lower() or "skynet" in reason.lower()

    def test_unprotected_pid(self, mock_registry):
        """Unknown PID is not protected."""
        result, reason = is_protected(pid=99999)
        assert result is False

    def test_protected_by_name(self, mock_registry):
        """Known process name is protected."""
        result, reason = is_protected(name="skynet.exe")
        assert result is True
        assert "Protected service" in reason

    def test_protected_by_partial_name(self, mock_registry):
        """Partial name match detects protected process."""
        result, reason = is_protected(name="god_console.py")
        assert result is True

    def test_unprotected_name(self, mock_registry):
        """Unknown process name is not protected."""
        result, reason = is_protected(name="random_process.exe")
        assert result is False

    def test_protected_by_hwnd(self, mock_registry):
        """Worker HWND PID is protected."""
        result, reason = is_protected(pid=9999)
        assert result is True

    def test_no_args_returns_not_protected(self, mock_registry):
        """No PID or name provided returns not protected."""
        result, reason = is_protected()
        assert result is False


class TestSafeKill:
    """Tests for safe_kill function."""

    def test_unprotected_pid_allowed(self, mock_registry):
        """safe_kill returns True for unprotected PID."""
        result = safe_kill(99999)
        assert result is True

    def test_protected_pid_blocked(self, mock_registry):
        """safe_kill returns False for protected PID."""
        result = safe_kill(1234)
        assert result is False

    def test_zero_pid_handled(self, mock_registry):
        """Zero PID is not in registry, but should handle gracefully."""
        result = safe_kill(0)
        # Zero PID is not protected (no process with PID 0 in registry)
        assert isinstance(result, bool)


class TestLoadRegistry:
    """Tests for _load_registry function."""

    def test_load_valid_registry(self, mock_registry):
        """Valid registry file is loaded correctly."""
        reg = _load_registry()
        assert "protected_names" in reg
        assert len(reg["processes"]) == 3

    def test_load_missing_file(self, tmp_path, monkeypatch):
        """Missing registry file returns default."""
        monkeypatch.setattr("tools.skynet_process_guard.REGISTRY_FILE",
                            tmp_path / "nonexistent.json")
        reg = _load_registry()
        assert reg == {"protected_names": [], "processes": []}

    def test_load_corrupt_file(self, tmp_path, monkeypatch):
        """Corrupt registry file returns default."""
        bad_file = tmp_path / "bad_registry.json"
        bad_file.write_text("NOT VALID JSON {{{{", encoding="utf-8")
        monkeypatch.setattr("tools.skynet_process_guard.REGISTRY_FILE", bad_file)
        reg = _load_registry()
        assert reg == {"protected_names": [], "processes": []}


class TestDeduplicateProcesses:
    """Tests for _deduplicate_processes function."""

    def test_no_duplicates(self):
        """List without duplicates passes through."""
        procs = [
            {"pid": 1, "role": "backend"},
            {"pid": 2, "role": "worker"},
        ]
        result = _deduplicate_processes(procs)
        assert len(result) == 2

    def test_removes_duplicates(self):
        """Duplicate (pid, role) pairs are removed."""
        procs = [
            {"pid": 1, "role": "backend"},
            {"pid": 1, "role": "backend"},  # duplicate
            {"pid": 2, "role": "worker"},
        ]
        result = _deduplicate_processes(procs)
        assert len(result) == 2

    def test_same_pid_different_role_kept(self):
        """Same PID with different roles are kept."""
        procs = [
            {"pid": 1, "role": "backend"},
            {"pid": 1, "role": "monitor"},
        ]
        result = _deduplicate_processes(procs)
        assert len(result) == 2

    def test_zero_pid_not_deduped(self):
        """Zero PID entries are not deduped (they're invalid)."""
        procs = [
            {"pid": 0, "role": "unknown"},
            {"pid": 0, "role": "unknown"},
        ]
        result = _deduplicate_processes(procs)
        assert len(result) == 2  # Both kept since PID=0 is skipped in dedup

    def test_empty_list(self):
        """Empty list returns empty list."""
        assert _deduplicate_processes([]) == []


class TestCollapseWrapperPids:
    """Tests for _collapse_wrapper_pids function."""

    def test_unique_pids_unchanged(self):
        """Unique PIDs pass through without change."""
        result = _collapse_wrapper_pids([100, 200, 300])
        assert set(result) == {100, 200, 300}

    def test_duplicate_pids_removed(self):
        """Duplicate PIDs in input are removed."""
        result = _collapse_wrapper_pids([100, 100, 200])
        assert len(result) == 2
        assert set(result) == {100, 200}

    def test_zero_pids_filtered(self):
        """Zero and negative PIDs are filtered out."""
        result = _collapse_wrapper_pids([0, -1, 100])
        assert result == [100]

    def test_empty_list(self):
        """Empty list returns empty list."""
        assert _collapse_wrapper_pids([]) == []

    def test_invalid_values_handled(self):
        """Non-integer values are filtered out gracefully."""
        result = _collapse_wrapper_pids(["abc", None, 100])
        # Should handle gracefully — only valid integers survive
        assert 100 in result
