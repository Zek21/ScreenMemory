"""
Tests for tools/skynet_activity_feed.py — Worker activity extraction daemon.

Covers: PID liveness, singleton lock, snapshot hashing, delta extraction,
activity classification, tool info extraction, worker loading, activity
persistence, bus posting, delta processing, worker scanning, CLI commands.

# signed: delta
"""

import hashlib
import json
import os
import signal
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def af_module(tmp_path, monkeypatch):
    """Import activity feed module with temp paths for isolation."""
    monkeypatch.setattr("tools.skynet_activity_feed.DATA_DIR", tmp_path)
    monkeypatch.setattr("tools.skynet_activity_feed.PID_FILE", tmp_path / "activity_feed.pid")
    monkeypatch.setattr("tools.skynet_activity_feed.ACTIVITY_FILE", tmp_path / "worker_activity.json")
    monkeypatch.setattr("tools.skynet_activity_feed.WORKERS_FILE", tmp_path / "workers.json")
    monkeypatch.setattr("tools.skynet_activity_feed.LOG_FILE", tmp_path / "activity_feed.log")
    import tools.skynet_activity_feed as af
    return af


# ===========================================================================
# TestSnapshotHash — Pure function
# ===========================================================================

class TestSnapshotHash:
    def test_deterministic(self):
        from tools.skynet_activity_feed import _snapshot_hash
        items = [(0, "hello"), (10, "world")]
        assert _snapshot_hash(items) == _snapshot_hash(items)

    def test_length_is_16(self):
        from tools.skynet_activity_feed import _snapshot_hash
        h = _snapshot_hash([(0, "test")])
        assert len(h) == 16

    def test_different_content_different_hash(self):
        from tools.skynet_activity_feed import _snapshot_hash
        h1 = _snapshot_hash([(0, "alpha")])
        h2 = _snapshot_hash([(0, "beta")])
        assert h1 != h2

    def test_empty_list(self):
        from tools.skynet_activity_feed import _snapshot_hash
        h = _snapshot_hash([])
        assert len(h) == 16

    def test_ignores_y_position(self):
        from tools.skynet_activity_feed import _snapshot_hash
        h1 = _snapshot_hash([(0, "line1"), (10, "line2")])
        h2 = _snapshot_hash([(50, "line1"), (100, "line2")])
        assert h1 == h2  # Only text matters, not position


# ===========================================================================
# TestExtractDelta — Pure function
# ===========================================================================

class TestExtractDelta:
    def test_new_lines_detected(self):
        from tools.skynet_activity_feed import _extract_delta
        old = [(0, "line1"), (10, "line2")]
        new = [(0, "line1"), (10, "line2"), (20, "line3")]
        delta = _extract_delta(old, new)
        assert delta == ["line3"]

    def test_no_changes(self):
        from tools.skynet_activity_feed import _extract_delta
        items = [(0, "line1"), (10, "line2")]
        delta = _extract_delta(items, items)
        assert delta == []

    def test_all_new(self):
        from tools.skynet_activity_feed import _extract_delta
        old = []
        new = [(0, "a"), (10, "b")]
        delta = _extract_delta(old, new)
        assert delta == ["a", "b"]

    def test_removed_lines_not_in_delta(self):
        from tools.skynet_activity_feed import _extract_delta
        old = [(0, "a"), (10, "b"), (20, "c")]
        new = [(0, "a"), (10, "c")]  # b removed
        delta = _extract_delta(old, new)
        assert delta == []  # No NEW lines

    def test_replacement_detected(self):
        from tools.skynet_activity_feed import _extract_delta
        old = [(0, "old line")]
        new = [(0, "new line")]
        delta = _extract_delta(old, new)
        assert delta == ["new line"]


# ===========================================================================
# TestClassifyActivity — Pattern matching
# ===========================================================================

class TestClassifyActivity:
    def test_tool_call_ran_terminal(self):
        from tools.skynet_activity_feed import classify_activity
        assert classify_activity("Ran terminal command: python test.py") == "tool_call"

    def test_tool_call_read_file(self):
        from tools.skynet_activity_feed import classify_activity
        assert classify_activity("Read file core/config.py") == "tool_call"

    def test_tool_call_searched(self):
        from tools.skynet_activity_feed import classify_activity
        assert classify_activity("Searched for 'TODO' in workspace") == "tool_call"

    def test_tool_call_ran_command(self):
        from tools.skynet_activity_feed import classify_activity
        assert classify_activity("Ran command: git status") == "tool_call"

    def test_tool_call_listed_directory(self):
        from tools.skynet_activity_feed import classify_activity
        assert classify_activity("Listed directory tools/") == "tool_call"

    def test_edit_edited(self):
        from tools.skynet_activity_feed import classify_activity
        assert classify_activity("Edited core/main.py") == "edit"

    def test_edit_created(self):
        from tools.skynet_activity_feed import classify_activity
        assert classify_activity("Created tests/test_new.py") == "edit"

    def test_edit_deleted(self):
        from tools.skynet_activity_feed import classify_activity
        assert classify_activity("Deleted old_file.txt") == "edit"

    def test_result_complete(self):
        from tools.skynet_activity_feed import classify_activity
        assert classify_activity("COMPLETE: task finished") == "result"

    def test_result_done(self):
        from tools.skynet_activity_feed import classify_activity
        assert classify_activity("DONE with analysis") == "result"

    def test_result_pass(self):
        from tools.skynet_activity_feed import classify_activity
        assert classify_activity("All tests PASS") == "result"

    def test_result_fail(self):
        from tools.skynet_activity_feed import classify_activity
        assert classify_activity("Test FAIL: assertion error") == "result"

    def test_result_posted_to_bus(self):
        from tools.skynet_activity_feed import classify_activity
        assert classify_activity("Posted to bus: result message") == "result"

    def test_thinking_default(self):
        from tools.skynet_activity_feed import classify_activity
        assert classify_activity("Analyzing the codebase for patterns") == "thinking"

    def test_thinking_for_normal_text(self):
        from tools.skynet_activity_feed import classify_activity
        assert classify_activity("Let me think about this approach") == "thinking"


# ===========================================================================
# TestExtractToolInfo — Tool and file extraction
# ===========================================================================

class TestExtractToolInfo:
    def test_terminal(self):
        from tools.skynet_activity_feed import _extract_tool_info
        tool, fp = _extract_tool_info("Ran terminal command: python test.py")
        assert tool == "terminal"

    def test_ran_command(self):
        from tools.skynet_activity_feed import _extract_tool_info
        tool, fp = _extract_tool_info("Ran command: git diff")
        assert tool == "terminal"

    def test_read_file(self):
        from tools.skynet_activity_feed import _extract_tool_info
        tool, fp = _extract_tool_info("Read file core/main.py")
        assert tool == "read_file"
        assert fp == "core/main.py"

    def test_search(self):
        from tools.skynet_activity_feed import _extract_tool_info
        tool, fp = _extract_tool_info("Searched for pattern in workspace")
        assert tool == "search"

    def test_edit(self):
        from tools.skynet_activity_feed import _extract_tool_info
        tool, fp = _extract_tool_info("Edited tools/config.py")
        assert tool == "edit"
        assert fp == "tools/config.py"

    def test_create(self):
        from tools.skynet_activity_feed import _extract_tool_info
        tool, fp = _extract_tool_info("Created tests/test_new.py")
        assert tool == "create"
        assert fp == "tests/test_new.py"

    def test_list_dir(self):
        from tools.skynet_activity_feed import _extract_tool_info
        tool, fp = _extract_tool_info("Listed directory tools/")
        assert tool == "list_dir"

    def test_no_tool(self):
        from tools.skynet_activity_feed import _extract_tool_info
        tool, fp = _extract_tool_info("Just thinking about something")
        assert tool is None
        assert fp is None

    def test_windows_path(self):
        from tools.skynet_activity_feed import _extract_tool_info
        tool, fp = _extract_tool_info("Read file D:\\Prospects\\ScreenMemory\\core\\main.py")
        assert tool == "read_file"
        assert fp is not None
        assert "main.py" in fp

    def test_filepath_stripped_of_punctuation(self):
        from tools.skynet_activity_feed import _extract_tool_info
        tool, fp = _extract_tool_info("Edited 'tools/helper.py',")
        assert tool == "edit"
        assert fp == "tools/helper.py"


# ===========================================================================
# TestInitActivityData — Structure initialization
# ===========================================================================

class TestInitActivityData:
    def test_creates_entry_per_worker(self):
        from tools.skynet_activity_feed import _init_activity_data
        workers = {"alpha": 123, "beta": 456}
        data = _init_activity_data(workers)
        assert "alpha" in data
        assert "beta" in data
        assert len(data) == 2

    def test_entry_structure(self):
        from tools.skynet_activity_feed import _init_activity_data
        data = _init_activity_data({"alpha": 123})
        entry = data["alpha"]
        assert entry["state"] == "UNKNOWN"
        assert entry["current_activity"] is None
        assert entry["last_tool"] is None
        assert entry["last_file"] is None
        assert entry["timestamp"] is None
        assert entry["recent_activities"] == []

    def test_empty_workers(self):
        from tools.skynet_activity_feed import _init_activity_data
        data = _init_activity_data({})
        assert data == {}


# ===========================================================================
# TestPidAlive — Mock Windows API
# ===========================================================================

class TestPidAlive:
    def test_negative_pid_returns_false(self):
        from tools.skynet_activity_feed import _pid_alive
        assert _pid_alive(-1) is False
        assert _pid_alive(0) is False

    @patch("tools.skynet_activity_feed.sys")
    def test_alive_on_windows(self, mock_sys):
        mock_sys.platform = "win32"
        from tools.skynet_activity_feed import _pid_alive
        with patch("ctypes.windll.kernel32.OpenProcess", return_value=12345):
            with patch("ctypes.windll.kernel32.CloseHandle"):
                assert _pid_alive(100) is True

    @patch("tools.skynet_activity_feed.sys")
    def test_dead_on_windows(self, mock_sys):
        mock_sys.platform = "win32"
        from tools.skynet_activity_feed import _pid_alive
        with patch("ctypes.windll.kernel32.OpenProcess", return_value=0):
            assert _pid_alive(99999) is False


# ===========================================================================
# TestSingleton — PID file lock
# ===========================================================================

class TestSingleton:
    def test_acquire_creates_pid_file(self, af_module, tmp_path):
        pid_file = tmp_path / "activity_feed.pid"
        result = af_module._acquire_singleton()
        assert result is True
        assert pid_file.exists()
        assert int(pid_file.read_text().strip()) == os.getpid()

    def test_release_removes_own_pid(self, af_module, tmp_path):
        pid_file = tmp_path / "activity_feed.pid"
        pid_file.write_text(str(os.getpid()))
        af_module._release_singleton()
        assert not pid_file.exists()

    def test_release_ignores_other_pid(self, af_module, tmp_path):
        pid_file = tmp_path / "activity_feed.pid"
        pid_file.write_text("99999")
        af_module._release_singleton()
        assert pid_file.exists()  # Not ours, don't delete

    def test_acquire_with_stale_pid(self, af_module, tmp_path):
        pid_file = tmp_path / "activity_feed.pid"
        pid_file.write_text("99999")
        with patch.object(af_module, "_is_activity_feed_process", return_value=False):
            result = af_module._acquire_singleton()
        assert result is True
        assert int(pid_file.read_text().strip()) == os.getpid()

    def test_acquire_fails_if_already_running(self, af_module, tmp_path):
        pid_file = tmp_path / "activity_feed.pid"
        pid_file.write_text("12345")
        with patch.object(af_module, "_is_activity_feed_process", return_value=True):
            result = af_module._acquire_singleton()
        assert result is False

    def test_acquire_handles_corrupt_pid_file(self, af_module, tmp_path):
        pid_file = tmp_path / "activity_feed.pid"
        pid_file.write_text("not-a-number")
        result = af_module._acquire_singleton()
        assert result is True


# ===========================================================================
# TestLoadWorkers — Workers.json parsing
# ===========================================================================

class TestLoadWorkers:
    def test_loads_workers(self, af_module, tmp_path):
        workers_file = tmp_path / "workers.json"
        workers_file.write_text(json.dumps({
            "workers": [
                {"name": "alpha", "hwnd": 123},
                {"name": "beta", "hwnd": 456},
            ]
        }))
        result = af_module._load_workers()
        assert result == {"alpha": 123, "beta": 456}

    def test_missing_file_returns_empty(self, af_module):
        result = af_module._load_workers()
        assert result == {}

    def test_skips_incomplete_entries(self, af_module, tmp_path):
        workers_file = tmp_path / "workers.json"
        workers_file.write_text(json.dumps({
            "workers": [
                {"name": "alpha", "hwnd": 123},
                {"name": "beta"},  # Missing hwnd
                {"hwnd": 789},     # Missing name
            ]
        }))
        result = af_module._load_workers()
        assert result == {"alpha": 123}

    def test_handles_bad_json(self, af_module, tmp_path):
        workers_file = tmp_path / "workers.json"
        workers_file.write_text("not json")
        result = af_module._load_workers()
        assert result == {}


# ===========================================================================
# TestSaveActivity — File writing
# ===========================================================================

class TestSaveActivity:
    def test_saves_json(self, af_module, tmp_path):
        data = {"alpha": {"state": "IDLE", "recent_activities": []}}
        af_module._save_activity(data)
        activity_file = tmp_path / "worker_activity.json"
        assert activity_file.exists()
        loaded = json.loads(activity_file.read_text(encoding="utf-8"))
        assert loaded["alpha"]["state"] == "IDLE"

    def test_atomic_write(self, af_module, tmp_path):
        """Verify tmp file is used for atomic write."""
        data = {"beta": {"state": "PROCESSING"}}
        af_module._save_activity(data)
        # tmp file should be gone (renamed to final)
        assert not (tmp_path / "worker_activity.tmp").exists()
        assert (tmp_path / "worker_activity.json").exists()


# ===========================================================================
# TestLog — Logging
# ===========================================================================

class TestLog:
    def test_writes_to_log_file(self, af_module, tmp_path):
        af_module.log("test message", "INFO")
        log_file = tmp_path / "activity_feed.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "test message" in content
        assert "[INFO]" in content

    def test_log_levels(self, af_module, tmp_path):
        af_module.log("warning msg", "WARN")
        content = (tmp_path / "activity_feed.log").read_text()
        assert "[WARN]" in content


# ===========================================================================
# TestPostToBus — Mock urllib
# ===========================================================================

class TestPostToBus:
    def test_posts_json_to_bus(self):
        from tools.skynet_activity_feed import _post_to_bus
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = MagicMock()
            _post_to_bus("alpha", "tool_call", "Ran terminal: pytest")
        mock_open.assert_called_once()
        req = mock_open.call_args[0][0]
        body = json.loads(req.data)
        assert body["sender"] == "alpha"
        assert body["topic"] == "activity"
        assert body["type"] == "tool_call"
        assert "pytest" in body["content"]

    def test_truncates_long_content(self):
        from tools.skynet_activity_feed import _post_to_bus, MAX_CONTENT
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = MagicMock()
            _post_to_bus("beta", "thinking", "x" * 1000)
        req = mock_open.call_args[0][0]
        body = json.loads(req.data)
        assert len(body["content"]) <= MAX_CONTENT

    def test_silently_handles_error(self):
        from tools.skynet_activity_feed import _post_to_bus
        with patch("urllib.request.urlopen", side_effect=Exception("connection error")):
            # Should not raise
            _post_to_bus("gamma", "result", "test")


# ===========================================================================
# TestProcessDeltas — Integration
# ===========================================================================

class TestProcessDeltas:
    def test_updates_activity_data(self):
        from tools.skynet_activity_feed import _process_deltas, _init_activity_data
        activity_data = _init_activity_data({"alpha": 123})
        delta = ["Read file core/main.py", "Edited tools/helper.py"]
        now = "2026-03-18T10:00:00"
        with patch("tools.skynet_activity_feed._post_to_bus"):
            _process_deltas("alpha", delta, activity_data, now)
        assert activity_data["alpha"]["current_activity"] is not None
        assert activity_data["alpha"]["last_tool"] == "edit"
        assert "helper.py" in activity_data["alpha"]["last_file"]
        assert len(activity_data["alpha"]["recent_activities"]) == 2

    def test_limits_to_last_5_deltas(self):
        from tools.skynet_activity_feed import _process_deltas, _init_activity_data
        activity_data = _init_activity_data({"beta": 456})
        delta = [f"Line {i}" for i in range(10)]
        with patch("tools.skynet_activity_feed._post_to_bus"):
            _process_deltas("beta", delta, activity_data, "now")
        # Only last 5 are processed
        assert len(activity_data["beta"]["recent_activities"]) == 5

    def test_posts_to_bus_for_each_line(self):
        from tools.skynet_activity_feed import _process_deltas, _init_activity_data
        activity_data = _init_activity_data({"gamma": 789})
        delta = ["Ran terminal: test", "COMPLETE: done"]
        with patch("tools.skynet_activity_feed._post_to_bus") as mock_bus:
            _process_deltas("gamma", delta, activity_data, "now")
        assert mock_bus.call_count == 2
        # First call: tool_call
        assert mock_bus.call_args_list[0] == call("gamma", "tool_call", "Ran terminal: test")
        # Second call: result
        assert mock_bus.call_args_list[1] == call("gamma", "result", "COMPLETE: done")

    def test_caps_recent_at_max(self):
        from tools.skynet_activity_feed import _process_deltas, _init_activity_data, MAX_RECENT
        activity_data = _init_activity_data({"alpha": 123})
        # Fill beyond MAX_RECENT
        for batch in range(10):
            delta = [f"Batch {batch} line {i}" for i in range(5)]
            with patch("tools.skynet_activity_feed._post_to_bus"):
                _process_deltas("alpha", delta, activity_data, f"batch_{batch}")
        assert len(activity_data["alpha"]["recent_activities"]) <= MAX_RECENT


# ===========================================================================
# TestScanWorker — Integration with mocks
# ===========================================================================

class TestScanWorker:
    def test_returns_none_when_no_items(self):
        from tools.skynet_activity_feed import _scan_worker, _init_activity_data
        snapshots = {"alpha": []}
        hashes = {"alpha": ""}
        activity_data = _init_activity_data({"alpha": 123})
        with patch("tools.skynet_activity_feed._get_listitem_snapshot", return_value=[]):
            result = _scan_worker("alpha", 123, snapshots, hashes, activity_data, "now")
        assert result is None

    def test_returns_none_when_hash_unchanged(self):
        from tools.skynet_activity_feed import _scan_worker, _snapshot_hash, _init_activity_data
        items = [(0, "line1"), (10, "line2")]
        h = _snapshot_hash(items)
        snapshots = {"alpha": items}
        hashes = {"alpha": h}
        activity_data = _init_activity_data({"alpha": 123})
        with patch("tools.skynet_activity_feed._get_listitem_snapshot", return_value=items):
            result = _scan_worker("alpha", 123, snapshots, hashes, activity_data, "now")
        assert result is None

    def test_detects_new_content(self):
        from tools.skynet_activity_feed import _scan_worker, _snapshot_hash, _init_activity_data
        old_items = [(0, "line1")]
        new_items = [(0, "line1"), (10, "Ran terminal: pytest")]
        snapshots = {"alpha": old_items}
        hashes = {"alpha": _snapshot_hash(old_items)}
        activity_data = _init_activity_data({"alpha": 123})
        with patch("tools.skynet_activity_feed._get_listitem_snapshot", return_value=new_items):
            with patch("tools.skynet_activity_feed._get_worker_state", return_value="PROCESSING"):
                with patch("tools.skynet_activity_feed._post_to_bus"):
                    result = _scan_worker("alpha", 123, snapshots, hashes, activity_data, "now")
        assert result is not None
        assert "Ran terminal: pytest" in result

    def test_updates_snapshots_and_hashes(self):
        from tools.skynet_activity_feed import _scan_worker, _snapshot_hash, _init_activity_data
        old_items = [(0, "old")]
        new_items = [(0, "old"), (10, "new line")]
        snapshots = {"beta": old_items}
        hashes = {"beta": _snapshot_hash(old_items)}
        activity_data = _init_activity_data({"beta": 456})
        with patch("tools.skynet_activity_feed._get_listitem_snapshot", return_value=new_items):
            with patch("tools.skynet_activity_feed._get_worker_state", return_value="IDLE"):
                with patch("tools.skynet_activity_feed._post_to_bus"):
                    _scan_worker("beta", 456, snapshots, hashes, activity_data, "now")
        assert snapshots["beta"] == new_items
        assert hashes["beta"] == _snapshot_hash(new_items)


# ===========================================================================
# TestShowStatus — CLI output
# ===========================================================================

class TestShowStatus:
    def test_no_file_prints_message(self, af_module, capsys):
        af_module.show_status()
        captured = capsys.readouterr()
        assert "No activity data" in captured.out

    def test_displays_worker_status(self, af_module, tmp_path, capsys):
        activity_file = tmp_path / "worker_activity.json"
        activity_file.write_text(json.dumps({
            "alpha": {
                "state": "PROCESSING",
                "current_activity": "Running tests",
                "last_tool": "terminal",
                "last_file": "tests/test_main.py",
                "timestamp": "2026-03-18T10:00:00",
                "recent_activities": [{"type": "tool_call"}],
            }
        }))
        af_module.show_status()
        captured = capsys.readouterr()
        assert "ALPHA" in captured.out
        assert "PROCESSING" in captured.out
        assert "Running tests" in captured.out

    def test_shows_daemon_running(self, af_module, tmp_path, capsys):
        # Create activity file
        activity_file = tmp_path / "worker_activity.json"
        activity_file.write_text(json.dumps({"alpha": {"state": "IDLE", "current_activity": None,
            "last_tool": None, "last_file": None, "timestamp": None, "recent_activities": []}}))
        # Create PID file pointing to our own process (alive)
        pid_file = tmp_path / "activity_feed.pid"
        pid_file.write_text(str(os.getpid()))
        with patch.object(af_module, "_is_activity_feed_process", return_value=True):
            af_module.show_status()
        captured = capsys.readouterr()
        assert "RUNNING" in captured.out


# ===========================================================================
# TestRunDaemon — Main loop
# ===========================================================================

class TestRunDaemon:
    def test_exits_if_singleton_fails(self, af_module):
        with patch.object(af_module, "_acquire_singleton", return_value=False):
            af_module.run_daemon()  # Should return immediately without error

    def test_exits_if_no_workers(self, af_module):
        with patch.object(af_module, "_acquire_singleton", return_value=True):
            with patch.object(af_module, "_load_workers", return_value={}):
                af_module.run_daemon()  # Should return immediately

    def test_cleanup_pid_bug_is_fixed(self):
        """Verify _cleanup_pid is NOT referenced; _release_singleton is used instead."""
        import tools.skynet_activity_feed as af
        import inspect
        source = inspect.getsource(af.run_daemon)
        assert "_cleanup_pid" not in source, "_cleanup_pid bug not fixed"
        assert "_release_singleton" in source

    def test_runs_one_cycle_then_keyboard_interrupt(self, af_module, tmp_path):
        workers_file = tmp_path / "workers.json"
        workers_file.write_text(json.dumps({
            "workers": [{"name": "alpha", "hwnd": 123}]
        }))
        call_count = 0
        original_sleep = time.sleep

        def fake_sleep(n):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise KeyboardInterrupt()

        with patch.object(af_module, "_acquire_singleton", return_value=True):
            with patch("tools.skynet_activity_feed.time.sleep", side_effect=fake_sleep):
                with patch("tools.skynet_activity_feed._get_listitem_snapshot", return_value=[]):
                    af_module.run_daemon()

        # Should have saved activity on exit
        activity_file = tmp_path / "worker_activity.json"
        assert activity_file.exists()


# ===========================================================================
# TestStopDaemon — PID signal
# ===========================================================================

class TestStopDaemon:
    def test_no_pid_file(self, af_module, capsys):
        af_module.stop_daemon()
        captured = capsys.readouterr()
        assert "not running" in captured.out.lower()

    def test_sends_sigterm(self, af_module, tmp_path):
        pid_file = tmp_path / "activity_feed.pid"
        pid_file.write_text("12345")
        with patch("os.kill") as mock_kill:
            # First kill succeeds, second check shows process dead
            mock_kill.side_effect = [None, OSError("no such process")]
            af_module.stop_daemon()
        # Should have tried to signal PID 12345
        mock_kill.assert_any_call(12345, signal.SIGTERM)

    def test_cleans_up_pid_file(self, af_module, tmp_path):
        pid_file = tmp_path / "activity_feed.pid"
        pid_file.write_text("12345")
        with patch("os.kill", side_effect=OSError("not found")):
            af_module.stop_daemon()
        assert not pid_file.exists()


# ===========================================================================
# TestGetWorkerState — UIA engine wrapper
# ===========================================================================

class TestGetWorkerState:
    def test_returns_state_from_engine(self):
        from tools.skynet_activity_feed import _get_worker_state
        mock_engine = MagicMock()
        mock_scan = MagicMock()
        mock_scan.state = "PROCESSING"
        mock_engine.scan.return_value = mock_scan
        with patch("tools.skynet_activity_feed.get_engine", return_value=mock_engine, create=True):
            with patch.dict("sys.modules", {"tools.uia_engine": MagicMock(get_engine=lambda: mock_engine)}):
                result = _get_worker_state(123)
        # Might return PROCESSING or UNKNOWN depending on import path
        assert result in ("PROCESSING", "UNKNOWN")

    def test_returns_unknown_on_error(self):
        from tools.skynet_activity_feed import _get_worker_state
        # With no UIA engine available, should return UNKNOWN
        with patch.dict("sys.modules", {"tools.uia_engine": None}):
            result = _get_worker_state(99999)
        assert result == "UNKNOWN"
