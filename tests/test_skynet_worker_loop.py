"""Tests for tools/skynet_worker_loop.py -- Worker autonomy daemon.

Tests cover: bus polling, TODO checking, proposal checking, action prioritization,
standing-by cooldown, rate limiting, error handling, and daemon lifecycle.

Created by Beta (Protocol Engineer & Infrastructure) for critical infrastructure
test coverage -- this module had ZERO tests despite controlling worker autonomy.
"""
# signed: beta

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from tools.skynet_worker_loop import (
    WorkerLoop,
    _fetch_json,
    _post_bus,
    _load_json,
    TASK_POLL_INTERVAL,
    TODO_CHECK_INTERVAL,
    PROPOSAL_CHECK_INTERVAL,
    STANDING_BY_COOLDOWN,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def loop():
    """Create a fresh WorkerLoop instance for beta."""
    return WorkerLoop("beta")


@pytest.fixture
def todos_file(tmp_path):
    """Create a temporary todos.json."""
    fp = tmp_path / "todos.json"
    fp.write_text(json.dumps({
        "version": "1.0",
        "todos": [
            {"id": 1, "worker": "beta", "task": "Fix dispatch", "status": "pending", "priority": "high"},
            {"id": 2, "worker": "alpha", "task": "Dashboard", "status": "pending", "priority": "normal"},
            {"id": 3, "worker": "beta", "task": "Old task", "status": "done", "priority": "normal"},
        ]
    }))
    return fp


@pytest.fixture
def task_queue_file(tmp_path):
    """Create a temporary task_queue.json."""
    fp = tmp_path / "task_queue.json"
    fp.write_text(json.dumps({
        "tasks": [
            {"task_id": "t1", "target": "beta", "task": "Run tests", "status": "pending", "priority": "urgent"},
            {"task_id": "t2", "target": "all", "task": "Broadcast", "status": "pending", "priority": "normal"},
            {"task_id": "t3", "target": "beta", "task": "Old", "status": "done", "priority": "normal"},
        ]
    }))
    return fp


# ── _fetch_json Tests ───────────────────────────────────────────────────────

class TestFetchJson:
    """Test the HTTP JSON fetcher utility."""

    @patch("tools.skynet_worker_loop.urllib.request.urlopen")
    def test_fetch_json_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'[{"id": "msg1"}]'
        mock_urlopen.return_value = mock_resp

        result = _fetch_json("http://localhost:8420/bus/messages")
        assert result == [{"id": "msg1"}]

    @patch("tools.skynet_worker_loop.urllib.request.urlopen")
    def test_fetch_json_timeout(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("Connection timed out")
        result = _fetch_json("http://localhost:8420/bus/messages")
        assert result is None

    @patch("tools.skynet_worker_loop.urllib.request.urlopen")
    def test_fetch_json_invalid_json(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_urlopen.return_value = mock_resp

        result = _fetch_json("http://localhost:8420/bus/messages")
        assert result is None

    @patch("tools.skynet_worker_loop.urllib.request.urlopen")
    def test_fetch_json_connection_refused(self, mock_urlopen):
        mock_urlopen.side_effect = ConnectionRefusedError()
        result = _fetch_json("http://localhost:8420/bus/messages")
        assert result is None


# ── _post_bus Tests ─────────────────────────────────────────────────────────

class TestPostBus:
    """Test bus message posting with SpamGuard + raw fallback."""

    @patch("tools.skynet_worker_loop.guarded_publish" if hasattr(sys.modules.get("tools.skynet_worker_loop", None), "guarded_publish") else "tools.skynet_spam_guard.guarded_publish")
    def test_post_bus_via_spamguard(self, mock_guard):
        mock_guard.return_value = {"allowed": True, "published": True}
        result = _post_bus("beta", "orchestrator", "result", "task done")
        assert result is True

    @patch("tools.skynet_worker_loop.urllib.request.urlopen")
    def test_post_bus_raw_fallback_on_spamguard_fail(self, mock_urlopen):
        """When SpamGuard raises, fall back to raw HTTP."""
        mock_urlopen.return_value = MagicMock()
        with patch("tools.skynet_spam_guard.guarded_publish", side_effect=RuntimeError("guard down")):
            result = _post_bus("beta", "orchestrator", "result", "test")
            assert result is True  # Raw fallback should succeed

    @patch("tools.skynet_worker_loop.urllib.request.urlopen")
    def test_post_bus_total_failure(self, mock_urlopen):
        """Both SpamGuard and raw HTTP fail."""
        mock_urlopen.side_effect = ConnectionRefusedError()
        with patch("tools.skynet_spam_guard.guarded_publish", side_effect=Exception("fail")):
            result = _post_bus("beta", "orchestrator", "result", "test")
            assert result is False


# ── _load_json Tests ────────────────────────────────────────────────────────

class TestLoadJson:
    """Test JSON file loading."""

    def test_load_json_valid(self, tmp_path):
        fp = tmp_path / "test.json"
        fp.write_text('{"key": "value"}')
        result = _load_json(fp)
        assert result == {"key": "value"}

    def test_load_json_missing_file(self, tmp_path):
        result = _load_json(tmp_path / "nonexistent.json")
        assert result is None

    def test_load_json_invalid_json(self, tmp_path):
        fp = tmp_path / "bad.json"
        fp.write_text("not json {{{")
        result = _load_json(fp)
        assert result is None

    def test_load_json_empty_file(self, tmp_path):
        fp = tmp_path / "empty.json"
        fp.write_text("")
        result = _load_json(fp)
        assert result is None


# ── WorkerLoop.poll_bus_tasks Tests ─────────────────────────────────────────

class TestPollBusTasks:
    """Test bus task polling and filtering."""

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_poll_finds_direct_task(self, mock_fetch, loop):
        mock_fetch.return_value = [
            {"id": "m1", "topic": "beta", "type": "task", "sender": "orchestrator",
             "content": "Fix dispatch pipeline"}
        ]
        tasks = loop.poll_bus_tasks()
        assert len(tasks) == 1
        assert tasks[0]["content"] == "Fix dispatch pipeline"

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_poll_finds_broadcast_task(self, mock_fetch, loop):
        mock_fetch.return_value = [
            {"id": "m2", "topic": "workers", "type": "task", "sender": "orchestrator",
             "content": "Run audit"}
        ]
        tasks = loop.poll_bus_tasks()
        assert len(tasks) == 1

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_poll_ignores_other_worker_tasks(self, mock_fetch, loop):
        mock_fetch.return_value = [
            {"id": "m3", "topic": "alpha", "type": "task", "content": "Alpha task"}
        ]
        tasks = loop.poll_bus_tasks()
        assert len(tasks) == 0

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_poll_deduplicates_seen_tasks(self, mock_fetch, loop):
        msgs = [{"id": "m1", "topic": "beta", "type": "task", "content": "Do thing"}]
        mock_fetch.return_value = msgs

        tasks1 = loop.poll_bus_tasks()
        assert len(tasks1) == 1

        tasks2 = loop.poll_bus_tasks()
        assert len(tasks2) == 0  # Already seen

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_poll_urgent_tasks_first(self, mock_fetch, loop):
        mock_fetch.return_value = [
            {"id": "m1", "topic": "beta", "type": "task", "content": "Normal"},
            {"id": "m2", "topic": "beta", "type": "urgent-task", "content": "Urgent!"},
        ]
        tasks = loop.poll_bus_tasks()
        assert len(tasks) == 2
        assert tasks[0]["type"] == "urgent-task"

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_poll_handles_bus_down(self, mock_fetch, loop):
        mock_fetch.return_value = None
        tasks = loop.poll_bus_tasks()
        assert tasks == []

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_poll_handles_non_list_response(self, mock_fetch, loop):
        mock_fetch.return_value = {"error": "bad request"}
        tasks = loop.poll_bus_tasks()
        assert tasks == []

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_poll_matches_worker_prefix_topic(self, mock_fetch, loop):
        mock_fetch.return_value = [
            {"id": "m1", "topic": "worker_beta", "type": "task", "content": "Prefixed"}
        ]
        tasks = loop.poll_bus_tasks()
        assert len(tasks) == 1

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_poll_ignores_non_task_types(self, mock_fetch, loop):
        mock_fetch.return_value = [
            {"id": "m1", "topic": "beta", "type": "result", "content": "Not a task"},
            {"id": "m2", "topic": "beta", "type": "status", "content": "Also not"},
        ]
        tasks = loop.poll_bus_tasks()
        assert len(tasks) == 0


# ── WorkerLoop.check_todos Tests ───────────────────────────────────────────

class TestCheckTodos:
    """Test TODO file checking and prioritization."""

    def test_check_todos_finds_assigned_pending(self, loop, todos_file):
        with patch("tools.skynet_worker_loop.TODOS_FILE", todos_file), \
             patch("tools.skynet_worker_loop.TASK_QUEUE_FILE", Path("/nonexistent")):
            pending = loop.check_todos()
            assert len(pending) == 1
            assert pending[0]["id"] == 1
            assert pending[0]["source"] == "todos"

    def test_check_todos_ignores_done(self, loop, todos_file):
        with patch("tools.skynet_worker_loop.TODOS_FILE", todos_file), \
             patch("tools.skynet_worker_loop.TASK_QUEUE_FILE", Path("/nonexistent")):
            pending = loop.check_todos()
            ids = [p["id"] for p in pending]
            assert 3 not in ids  # id=3 is "done"

    def test_check_todos_ignores_other_workers(self, loop, todos_file):
        with patch("tools.skynet_worker_loop.TODOS_FILE", todos_file), \
             patch("tools.skynet_worker_loop.TASK_QUEUE_FILE", Path("/nonexistent")):
            pending = loop.check_todos()
            for p in pending:
                assert p["source"] != "todos" or p["id"] != 2  # alpha's task

    def test_check_todos_includes_task_queue(self, loop, task_queue_file):
        with patch("tools.skynet_worker_loop.TODOS_FILE", Path("/nonexistent")), \
             patch("tools.skynet_worker_loop.TASK_QUEUE_FILE", task_queue_file):
            pending = loop.check_todos()
            # Should find t1 (beta, pending) and t2 (all, pending), not t3 (done)
            assert len(pending) >= 2
            task_ids = [p["id"] for p in pending]
            assert "t1" in task_ids
            assert "t2" in task_ids
            assert "t3" not in task_ids

    def test_check_todos_urgent_first(self, loop, task_queue_file):
        with patch("tools.skynet_worker_loop.TODOS_FILE", Path("/nonexistent")), \
             patch("tools.skynet_worker_loop.TASK_QUEUE_FILE", task_queue_file):
            pending = loop.check_todos()
            if len(pending) >= 2:
                assert pending[0]["priority"] == "urgent"

    def test_check_todos_missing_file(self, loop):
        with patch("tools.skynet_worker_loop.TODOS_FILE", Path("/no/such/file")), \
             patch("tools.skynet_worker_loop.TASK_QUEUE_FILE", Path("/no/such/file")):
            pending = loop.check_todos()
            assert pending == []


# ── WorkerLoop.check_proposals Tests ───────────────────────────────────────

class TestCheckProposals:
    """Test planning proposal detection."""

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_finds_proposals(self, mock_fetch, loop):
        mock_fetch.return_value = [
            {"id": "p1", "topic": "planning", "type": "proposal",
             "content": "Refactor bus relay"}
        ]
        proposals = loop.check_proposals()
        assert len(proposals) == 1

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_deduplicates_proposals(self, mock_fetch, loop):
        msgs = [{"id": "p1", "topic": "planning", "type": "proposal", "content": "X"}]
        mock_fetch.return_value = msgs

        proposals1 = loop.check_proposals()
        assert len(proposals1) == 1

        proposals2 = loop.check_proposals()
        assert len(proposals2) == 0

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_ignores_non_proposals(self, mock_fetch, loop):
        mock_fetch.return_value = [
            {"id": "r1", "topic": "planning", "type": "result", "content": "Not proposal"},
            {"id": "r2", "topic": "orchestrator", "type": "proposal", "content": "Wrong topic"},
        ]
        proposals = loop.check_proposals()
        assert len(proposals) == 0

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_handles_bus_down(self, mock_fetch, loop):
        mock_fetch.return_value = None
        proposals = loop.check_proposals()
        assert proposals == []


# ── WorkerLoop.next_action Tests ───────────────────────────────────────────

class TestNextAction:
    """Test action prioritization and rate limiting."""

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_bus_task_highest_priority(self, mock_fetch, loop):
        mock_fetch.return_value = [
            {"id": "t1", "topic": "beta", "type": "task", "content": "Do it"}
        ]
        loop._last_task_poll = 0  # Force poll
        action = loop.next_action()
        assert action is not None
        assert action[0] == "bus_task"

    def test_rate_limiting_prevents_check(self, loop):
        """When all timers are recent, returns None."""
        now = time.time()
        loop._last_task_poll = now
        loop._last_todo_check = now
        loop._last_proposal_check = now
        action = loop.next_action()
        assert action is None

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_standing_by_when_nothing_to_do(self, mock_fetch, loop):
        mock_fetch.return_value = []
        loop._last_task_poll = 0
        loop._last_todo_check = 0
        loop._last_proposal_check = 0
        loop._last_standing_by = 0

        with patch("tools.skynet_worker_loop.TODOS_FILE", Path("/nonexistent")), \
             patch("tools.skynet_worker_loop.TASK_QUEUE_FILE", Path("/nonexistent")):
            action = loop.next_action()
            assert action is not None
            assert action[0] == "standing_by"

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_standing_by_cooldown(self, mock_fetch, loop):
        """STANDING_BY should not repeat within cooldown window."""
        mock_fetch.return_value = []
        loop._last_task_poll = 0
        loop._last_todo_check = 0
        loop._last_proposal_check = 0
        loop._last_standing_by = time.time()  # Just posted standing by

        with patch("tools.skynet_worker_loop.TODOS_FILE", Path("/nonexistent")), \
             patch("tools.skynet_worker_loop.TASK_QUEUE_FILE", Path("/nonexistent")):
            action = loop.next_action()
            # Should return None (standing by on cooldown) since proposal check
            # just ran but standing_by is on cooldown
            assert action is None or action[0] != "standing_by"


# ── WorkerLoop.run_once Tests ──────────────────────────────────────────────

class TestRunOnce:
    """Test single-cycle execution."""

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_run_once_resets_timers(self, mock_fetch, loop):
        mock_fetch.return_value = [
            {"id": "t1", "topic": "beta", "type": "task", "content": "Quick task"}
        ]
        action = loop.run_once()
        assert action is not None
        assert action[0] == "bus_task"

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_run_once_no_work(self, mock_fetch, loop):
        mock_fetch.return_value = []
        with patch("tools.skynet_worker_loop.TODOS_FILE", Path("/nonexistent")), \
             patch("tools.skynet_worker_loop.TASK_QUEUE_FILE", Path("/nonexistent")):
            action = loop.run_once()
            assert action is not None
            assert action[0] == "standing_by"


# ── WorkerLoop Constructor Tests ───────────────────────────────────────────

class TestWorkerLoopInit:
    """Test WorkerLoop initialization."""

    def test_stores_worker_name(self):
        loop = WorkerLoop("gamma")
        assert loop.name == "gamma"

    def test_initial_timers_are_zero(self):
        loop = WorkerLoop("delta")
        assert loop._last_task_poll == 0
        assert loop._last_todo_check == 0
        assert loop._last_proposal_check == 0
        assert loop._last_standing_by == 0

    def test_initial_seen_sets_empty(self):
        loop = WorkerLoop("alpha")
        assert len(loop._seen_task_ids) == 0
        assert len(loop._seen_proposal_ids) == 0

    def test_current_action_initially_none(self):
        loop = WorkerLoop("beta")
        assert loop._current_action is None


# ── Integration Tests ──────────────────────────────────────────────────────

class TestIntegration:
    """Integration tests for worker loop behavior."""

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_todo_checked_after_bus_tasks(self, mock_fetch, loop, todos_file):
        """If no bus tasks, TODOs should be checked."""
        mock_fetch.return_value = []
        loop._last_task_poll = 0
        loop._last_todo_check = 0

        with patch("tools.skynet_worker_loop.TODOS_FILE", todos_file), \
             patch("tools.skynet_worker_loop.TASK_QUEUE_FILE", Path("/nonexistent")):
            action = loop.next_action()
            # Bus poll happens first (returns empty), then TODO check
            assert action is not None
            assert action[0] == "todo"

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_bus_task_takes_precedence_over_todo(self, mock_fetch, loop, todos_file):
        """Bus tasks should be returned before TODOs."""
        mock_fetch.return_value = [
            {"id": "bt1", "topic": "beta", "type": "task", "content": "Bus task"}
        ]
        loop._last_task_poll = 0
        loop._last_todo_check = 0

        with patch("tools.skynet_worker_loop.TODOS_FILE", todos_file), \
             patch("tools.skynet_worker_loop.TASK_QUEUE_FILE", Path("/nonexistent")):
            action = loop.next_action()
            assert action[0] == "bus_task"

    @patch("tools.skynet_worker_loop._fetch_json")
    def test_multiple_cycles_discover_new_tasks(self, mock_fetch, loop):
        """New tasks appearing across cycles should be discovered."""
        mock_fetch.return_value = [
            {"id": "cycle1", "topic": "beta", "type": "task", "content": "First"}
        ]
        loop._last_task_poll = 0
        action1 = loop.next_action()
        assert action1[0] == "bus_task"
        assert action1[1]["id"] == "cycle1"

        # New task appears
        mock_fetch.return_value = [
            {"id": "cycle1", "topic": "beta", "type": "task", "content": "First"},
            {"id": "cycle2", "topic": "beta", "type": "task", "content": "Second"},
        ]
        loop._last_task_poll = 0
        action2 = loop.next_action()
        assert action2[0] == "bus_task"
        assert action2[1]["id"] == "cycle2"
