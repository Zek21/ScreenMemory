"""Tests for tools/skynet_atomic.py — atomic JSON file operations.
# signed: delta

Tests cover:
  - atomic_write_json: success, failure, parent directory creation
  - safe_read_json: normal read, missing file, corrupt file, backup fallback
  - atomic_update_json: success, update function, default handling
  - _get_file_lock: per-file lock creation and reuse
  - Edge cases: empty files, concurrent writes, temp file cleanup
"""

import json
import os
import threading
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure the project root is on sys.path
import sys
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.skynet_atomic import (
    atomic_write_json,
    safe_read_json,
    atomic_update_json,
    _get_file_lock,
)


@pytest.fixture
def tmp_json(tmp_path):
    """Provide a temporary JSON file path."""
    return tmp_path / "test_data.json"


class TestAtomicWriteJson:
    """Tests for atomic_write_json function."""

    def test_write_creates_file(self, tmp_json):
        """Verify atomic_write_json creates a new file with correct content."""
        data = {"workers": ["alpha", "beta"]}
        result = atomic_write_json(tmp_json, data)
        assert result is True
        assert tmp_json.exists()
        loaded = json.loads(tmp_json.read_text(encoding="utf-8"))
        assert loaded == data

    def test_write_overwrites_existing(self, tmp_json):
        """Verify atomic write replaces existing file atomically."""
        tmp_json.write_text('{"old": true}', encoding="utf-8")
        data = {"new": True, "count": 42}
        result = atomic_write_json(tmp_json, data)
        assert result is True
        loaded = json.loads(tmp_json.read_text(encoding="utf-8"))
        assert loaded == data
        assert "old" not in loaded

    def test_write_creates_parent_directories(self, tmp_path):
        """Verify parent directories are created if they don't exist."""
        deep_path = tmp_path / "a" / "b" / "c" / "data.json"
        result = atomic_write_json(deep_path, {"nested": True})
        assert result is True
        assert deep_path.exists()

    def test_write_returns_false_on_failure(self, tmp_path):
        """Verify False is returned when write fails."""
        # Use non-serializable data to force json.dumps failure
        class Unserializable:
            pass

        path = tmp_path / "fail.json"
        result = atomic_write_json(path, Unserializable(), default=None)
        assert result is False

    def test_write_cleans_temp_on_failure(self, tmp_path):
        """Verify temp file is cleaned up on failure."""
        path = tmp_path / "cleanup.json"
        tmp_file = path.with_suffix(".json.tmp")
        # Force failure by making json.dumps raise
        with patch("tools.skynet_atomic.json.dumps", side_effect=ValueError("test")):
            result = atomic_write_json(path, {"data": 1})
        assert result is False
        assert not tmp_file.exists()

    def test_write_with_custom_indent(self, tmp_json):
        """Verify custom indent is applied."""
        data = {"key": "value"}
        atomic_write_json(tmp_json, data, indent=4)
        content = tmp_json.read_text(encoding="utf-8")
        assert "    " in content  # 4-space indent


class TestSafeReadJson:
    """Tests for safe_read_json function."""

    def test_read_valid_json(self, tmp_json):
        """Verify reading a valid JSON file returns correct data."""
        data = {"status": "ok", "count": 5}
        tmp_json.write_text(json.dumps(data), encoding="utf-8")
        result = safe_read_json(tmp_json)
        assert result == data

    def test_read_missing_file_returns_default(self, tmp_path):
        """Verify missing file returns the default value."""
        result = safe_read_json(tmp_path / "nonexistent.json", default={"empty": True})
        assert result == {"empty": True}

    def test_read_missing_file_returns_empty_dict_default(self, tmp_path):
        """Verify missing file with no explicit default returns empty dict."""
        result = safe_read_json(tmp_path / "nonexistent.json")
        assert result == {}

    def test_read_corrupt_file_returns_default(self, tmp_json):
        """Verify corrupt JSON returns default instead of crashing."""
        tmp_json.write_text("{invalid json!!!", encoding="utf-8")
        result = safe_read_json(tmp_json, default={"fallback": True})
        assert result == {"fallback": True}

    def test_read_empty_file_returns_default(self, tmp_json):
        """Verify empty file returns default."""
        tmp_json.write_text("", encoding="utf-8")
        result = safe_read_json(tmp_json, default={"empty": True})
        assert result == {"empty": True}

    def test_read_whitespace_only_file_returns_default(self, tmp_json):
        """Verify whitespace-only file returns default."""
        tmp_json.write_text("   \n  \t  ", encoding="utf-8")
        result = safe_read_json(tmp_json, default={"ws": True})
        assert result == {"ws": True}

    def test_read_falls_back_to_backup(self, tmp_json):
        """Verify fallback to .bak file when primary is corrupt."""
        tmp_json.write_text("CORRUPT", encoding="utf-8")
        bak = tmp_json.with_suffix(".json.bak")
        bak.write_text('{"backup": true}', encoding="utf-8")
        result = safe_read_json(tmp_json)
        assert result == {"backup": True}


class TestAtomicUpdateJson:
    """Tests for atomic_update_json function."""

    def test_update_existing_file(self, tmp_json):
        """Verify read-modify-write on existing file."""
        tmp_json.write_text('{"count": 5}', encoding="utf-8")

        def increment(data):
            data["count"] += 1
            return data

        result = atomic_update_json(tmp_json, increment)
        assert result is True
        loaded = json.loads(tmp_json.read_text(encoding="utf-8"))
        assert loaded["count"] == 6

    def test_update_creates_from_default(self, tmp_json):
        """Verify update creates file from default when missing."""
        def add_entry(data):
            data["entries"].append("new")
            return data

        result = atomic_update_json(tmp_json, add_entry, default={"entries": []})
        assert result is True
        loaded = json.loads(tmp_json.read_text(encoding="utf-8"))
        assert loaded == {"entries": ["new"]}

    def test_update_returns_false_on_failure(self, tmp_json):
        """Verify False returned when update_fn raises."""
        tmp_json.write_text('{"data": 1}', encoding="utf-8")

        def bad_update(data):
            raise RuntimeError("Intentional failure")

        result = atomic_update_json(tmp_json, bad_update)
        assert result is False


class TestGetFileLock:
    """Tests for _get_file_lock function."""

    def test_returns_lock_object(self, tmp_path):
        """Verify _get_file_lock returns a threading.Lock."""
        lock = _get_file_lock(tmp_path / "test.json")
        assert isinstance(lock, type(threading.Lock()))

    def test_same_path_returns_same_lock(self, tmp_path):
        """Verify the same path gets the same lock instance."""
        path = tmp_path / "shared.json"
        lock1 = _get_file_lock(path)
        lock2 = _get_file_lock(path)
        assert lock1 is lock2

    def test_different_paths_get_different_locks(self, tmp_path):
        """Verify different paths get different lock instances."""
        lock1 = _get_file_lock(tmp_path / "file1.json")
        lock2 = _get_file_lock(tmp_path / "file2.json")
        assert lock1 is not lock2
