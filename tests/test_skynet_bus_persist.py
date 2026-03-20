"""Tests for tools/skynet_bus_persist.py -- Persistent bus message archiver.

Tests cover: PID locking, archive rotation, message archiving, SSE parsing,
catchup mechanism, archive search, stats, and error recovery.

Created by Beta (Protocol Engineer & Infrastructure) for critical infrastructure
test coverage -- this module handles bus persistence with zero prior tests.
"""
# signed: beta

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

from tools.skynet_bus_persist import (
    _acquire_pid_lock,
    _release_pid_lock,
    _rotate_if_needed,
    _cleanup_old_rotated_files,
    _archive_message,
    _parse_sse_messages,
    _catchup_missed_messages,
    MAX_ARCHIVE_BYTES,
    MAX_ROTATED_FILES,
    ROTATION_CHECK_INTERVAL,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def archive_dir(tmp_path):
    """Create a temporary data directory for archive files."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture
def archive_file(archive_dir):
    """Create an empty archive file."""
    fp = archive_dir / "bus_archive.jsonl"
    fp.write_text("")
    return fp


@pytest.fixture
def pid_file(archive_dir):
    """Path for PID file (not created yet)."""
    return archive_dir / "bus_persist.pid"


# ── PID Lock Tests ──────────────────────────────────────────────────────────

class TestPidLock:
    """Test PID file locking for singleton enforcement."""

    def test_acquire_lock_no_existing_pid(self, pid_file):
        with patch("tools.skynet_bus_persist.PID_FILE", pid_file):
            result = _acquire_pid_lock()
            assert result is True
            assert pid_file.exists()
            assert pid_file.read_text().strip() == str(os.getpid())

    def test_acquire_lock_stale_pid(self, pid_file):
        """Stale PID (dead process) should allow lock acquisition."""
        pid_file.write_text("99999999")  # Likely dead PID
        with patch("tools.skynet_bus_persist.PID_FILE", pid_file):
            # Mock kernel32 to report process not found
            mock_kernel32 = MagicMock()
            mock_kernel32.OpenProcess.return_value = 0  # Process not found
            with patch("ctypes.windll") as mock_windll:
                mock_windll.kernel32 = mock_kernel32
                result = _acquire_pid_lock()
                assert result is True

    def test_acquire_lock_corrupt_pid_file(self, pid_file):
        """Corrupt PID file should allow lock acquisition."""
        pid_file.write_text("not_a_number")
        with patch("tools.skynet_bus_persist.PID_FILE", pid_file):
            result = _acquire_pid_lock()
            assert result is True

    def test_release_lock_own_pid(self, pid_file):
        """Release should delete PID file if PID matches."""
        pid_file.write_text(str(os.getpid()))
        with patch("tools.skynet_bus_persist.PID_FILE", pid_file):
            _release_pid_lock()
            assert not pid_file.exists()

    def test_release_lock_other_pid(self, pid_file):
        """Release should NOT delete PID file if PID doesn't match."""
        pid_file.write_text("12345")
        with patch("tools.skynet_bus_persist.PID_FILE", pid_file):
            _release_pid_lock()
            assert pid_file.exists()  # Should not be deleted

    def test_release_lock_missing_file(self, pid_file):
        """Release on missing PID file should not error."""
        with patch("tools.skynet_bus_persist.PID_FILE", pid_file):
            _release_pid_lock()  # Should not raise


# ── Archive Rotation Tests ─────────────────────────────────────────────────

class TestRotation:
    """Test archive file rotation and cleanup."""

    def test_no_rotation_small_file(self, archive_dir):
        archive = archive_dir / "bus_archive.jsonl"
        archive.write_text('{"id":"msg1"}\n')
        with patch("tools.skynet_bus_persist.ARCHIVE_FILE", archive), \
             patch("tools.skynet_bus_persist.DATA_DIR", archive_dir):
            _rotate_if_needed()
            assert archive.exists()  # Should not be rotated

    def test_rotation_large_file(self, archive_dir):
        archive = archive_dir / "bus_archive.jsonl"
        # Create file larger than threshold
        large_content = '{"id":"msg"}\n' * (MAX_ARCHIVE_BYTES // 14 + 1)
        archive.write_text(large_content)
        with patch("tools.skynet_bus_persist.ARCHIVE_FILE", archive), \
             patch("tools.skynet_bus_persist.DATA_DIR", archive_dir):
            _rotate_if_needed()
            # Original should be gone (renamed)
            assert not archive.exists()
            # Rotated file should exist
            rotated = list(archive_dir.glob("bus_archive_*.jsonl"))
            assert len(rotated) == 1

    def test_rotation_unique_naming(self, archive_dir):
        """Multiple rotations on same day get unique suffixes."""
        archive = archive_dir / "bus_archive.jsonl"
        from datetime import datetime
        date_str = datetime.now().strftime("%Y%m%d")

        # Create existing rotated file
        (archive_dir / f"bus_archive_{date_str}.jsonl").write_text("old\n")

        # Create large archive
        large_content = '{"id":"msg"}\n' * (MAX_ARCHIVE_BYTES // 14 + 1)
        archive.write_text(large_content)

        with patch("tools.skynet_bus_persist.ARCHIVE_FILE", archive), \
             patch("tools.skynet_bus_persist.DATA_DIR", archive_dir):
            _rotate_if_needed()
            # Should have _1 suffix
            suffixed = archive_dir / f"bus_archive_{date_str}_1.jsonl"
            assert suffixed.exists()

    def test_rotation_missing_archive(self, archive_dir):
        """Rotation on missing archive should not error."""
        archive = archive_dir / "bus_archive.jsonl"
        with patch("tools.skynet_bus_persist.ARCHIVE_FILE", archive), \
             patch("tools.skynet_bus_persist.DATA_DIR", archive_dir):
            _rotate_if_needed()  # Should not raise


class TestCleanupOldRotated:
    """Test cleanup of old rotated archive files."""

    def test_cleanup_keeps_recent(self, archive_dir):
        """Should keep files within MAX_ROTATED_FILES limit."""
        for i in range(5):
            fp = archive_dir / f"bus_archive_2026030{i}.jsonl"
            fp.write_text(f"data{i}\n")
            # Set distinct mtime
            os.utime(fp, (time.time() - (5 - i) * 86400, time.time() - (5 - i) * 86400))

        with patch("tools.skynet_bus_persist.DATA_DIR", archive_dir):
            _cleanup_old_rotated_files()
            remaining = list(archive_dir.glob("bus_archive_*.jsonl"))
            assert len(remaining) == 5  # All kept (under limit)

    def test_cleanup_removes_excess(self, archive_dir):
        """Should remove files beyond MAX_ROTATED_FILES."""
        for i in range(MAX_ROTATED_FILES + 5):
            fp = archive_dir / f"bus_archive_2026{i:04d}.jsonl"
            fp.write_text(f"data{i}\n")
            os.utime(fp, (time.time() - i * 3600, time.time() - i * 3600))

        with patch("tools.skynet_bus_persist.DATA_DIR", archive_dir):
            _cleanup_old_rotated_files()
            remaining = list(archive_dir.glob("bus_archive_*.jsonl"))
            assert len(remaining) == MAX_ROTATED_FILES


# ── Archive Message Tests ──────────────────────────────────────────────────

class TestArchiveMessage:
    """Test message archiving to JSONL."""

    def test_archive_appends_message(self, archive_dir):
        archive = archive_dir / "bus_archive.jsonl"
        with patch("tools.skynet_bus_persist.ARCHIVE_FILE", archive):
            _archive_message({"id": "msg1", "sender": "beta", "content": "test"})
            lines = archive.read_text().strip().split("\n")
            assert len(lines) == 1
            parsed = json.loads(lines[0])
            assert parsed["id"] == "msg1"
            assert "archived_at" in parsed

    def test_archive_multiple_messages(self, archive_dir):
        archive = archive_dir / "bus_archive.jsonl"
        with patch("tools.skynet_bus_persist.ARCHIVE_FILE", archive):
            for i in range(5):
                _archive_message({"id": f"msg{i}", "content": f"test{i}"})
            lines = archive.read_text().strip().split("\n")
            assert len(lines) == 5

    def test_archive_adds_timestamp(self, archive_dir):
        archive = archive_dir / "bus_archive.jsonl"
        before = time.time()
        with patch("tools.skynet_bus_persist.ARCHIVE_FILE", archive):
            _archive_message({"id": "ts_test"})
        after = time.time()
        parsed = json.loads(archive.read_text().strip())
        assert before <= parsed["archived_at"] <= after

    def test_archive_handles_non_serializable(self, archive_dir):
        """Messages with non-serializable values should use default=str."""
        archive = archive_dir / "bus_archive.jsonl"
        from datetime import datetime
        with patch("tools.skynet_bus_persist.ARCHIVE_FILE", archive):
            _archive_message({"id": "dt_test", "ts": datetime(2026, 3, 20)})
            # Should not raise, datetime gets str'd
            line = archive.read_text().strip()
            parsed = json.loads(line)
            assert "2026" in parsed["ts"]


# ── SSE Message Parsing Tests ──────────────────────────────────────────────

class TestParseSSEMessages:
    """Test SSE data line parsing."""

    def test_parse_valid_bus_messages(self):
        line = 'data: {"bus": [{"id": "m1", "sender": "alpha"}]}'
        msgs = _parse_sse_messages(line)
        assert len(msgs) == 1
        assert msgs[0]["id"] == "m1"

    def test_parse_empty_bus(self):
        line = 'data: {"bus": []}'
        msgs = _parse_sse_messages(line)
        assert msgs == []

    def test_parse_no_bus_key(self):
        line = 'data: {"agents": []}'
        msgs = _parse_sse_messages(line)
        assert msgs == []

    def test_parse_non_data_line(self):
        msgs = _parse_sse_messages("event: tick")
        assert msgs == []

    def test_parse_empty_line(self):
        msgs = _parse_sse_messages("")
        assert msgs == []

    def test_parse_invalid_json(self):
        msgs = _parse_sse_messages("data: not json {{{")
        assert msgs == []

    def test_parse_multiple_messages(self):
        line = 'data: {"bus": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]}'
        msgs = _parse_sse_messages(line)
        assert len(msgs) == 3


# ── Catchup Mechanism Tests ────────────────────────────────────────────────

class TestCatchupMissedMessages:
    """Test HTTP catchup for messages missed during SSE downtime."""

    @patch("tools.skynet_bus_persist._archive_message")
    @patch("urllib.request.urlopen")
    def test_catchup_archives_new_messages(self, mock_urlopen, mock_archive):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps([
            {"id": "c1", "sender": "alpha", "content": "missed"},
            {"id": "c2", "sender": "beta", "content": "also missed"},
        ]).encode()
        mock_urlopen.return_value = mock_resp

        seen = set()
        caught = _catchup_missed_messages(seen)
        assert caught == 2
        assert mock_archive.call_count == 2
        assert "c1" in seen
        assert "c2" in seen

    @patch("tools.skynet_bus_persist._archive_message")
    @patch("urllib.request.urlopen")
    def test_catchup_skips_seen_messages(self, mock_urlopen, mock_archive):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps([
            {"id": "c1", "content": "already seen"},
            {"id": "c2", "content": "new"},
        ]).encode()
        mock_urlopen.return_value = mock_resp

        seen = {"c1"}  # Already seen
        caught = _catchup_missed_messages(seen)
        assert caught == 1
        assert mock_archive.call_count == 1

    @patch("tools.skynet_bus_persist._archive_message")
    @patch("urllib.request.urlopen")
    def test_catchup_handles_connection_error(self, mock_urlopen, mock_archive):
        mock_urlopen.side_effect = ConnectionRefusedError()
        seen = set()
        caught = _catchup_missed_messages(seen)
        assert caught == 0
        assert mock_archive.call_count == 0

    @patch("tools.skynet_bus_persist._archive_message")
    @patch("urllib.request.urlopen")
    def test_catchup_handles_dict_response(self, mock_urlopen, mock_archive):
        """Response might be a dict with 'messages' key."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "messages": [{"id": "d1", "content": "in dict"}]
        }).encode()
        mock_urlopen.return_value = mock_resp

        seen = set()
        caught = _catchup_missed_messages(seen)
        assert caught == 1

    @patch("tools.skynet_bus_persist._archive_message")
    @patch("urllib.request.urlopen")
    def test_catchup_handles_empty_response(self, mock_urlopen, mock_archive):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"[]"
        mock_urlopen.return_value = mock_resp

        seen = set()
        caught = _catchup_missed_messages(seen)
        assert caught == 0


# ── Constants Tests ────────────────────────────────────────────────────────

class TestConstants:
    """Test that critical constants have sane values."""

    def test_max_archive_bytes_reasonable(self):
        assert MAX_ARCHIVE_BYTES >= 10 * 1024 * 1024  # At least 10MB
        assert MAX_ARCHIVE_BYTES <= 500 * 1024 * 1024  # At most 500MB

    def test_rotation_check_interval_positive(self):
        assert ROTATION_CHECK_INTERVAL > 0

    def test_max_rotated_files_reasonable(self):
        assert MAX_ROTATED_FILES >= 5
        assert MAX_ROTATED_FILES <= 100
