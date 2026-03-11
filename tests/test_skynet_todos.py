"""Tests for tools/skynet_todos.py — Per-worker TODO sync system.

Tests cover: add/update/mark_done operations, list filtering, summary stats,
bulk_update, pending_count, can_stop, cleanup, and priority validation.

Created by worker delta — infrastructure test coverage.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def todos_file(tmp_path):
    """Create a temporary todos.json and patch the module to use it."""
    fp = tmp_path / "todos.json"
    fp.write_text(json.dumps({"todos": [], "version": 1}), encoding="utf-8")
    return fp


@pytest.fixture
def patched_todos(todos_file):
    """Patch skynet_todos to use temporary file for all I/O."""
    import tools.skynet_todos as st

    def _mock_load():
        try:
            return json.loads(todos_file.read_text(encoding="utf-8"))
        except Exception:
            return {"todos": [], "version": 1}

    def _mock_save(data):
        todos_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    with patch.object(st, '_load', side_effect=_mock_load), \
         patch.object(st, '_save', side_effect=_mock_save):
        yield st


# ── Add TODO Tests ──────────────────────────────────────────────────────────

class TestAddTodo:
    """Tests for add_todo()."""

    def test_add_creates_item(self, patched_todos):
        """Adding a TODO creates a properly structured item."""
        item = patched_todos.add_todo("alpha", "Fix dashboard CSS")
        assert item["worker"] == "alpha"
        assert item["task"] == "Fix dashboard CSS"
        assert item["status"] == "pending"
        assert item["priority"] == "normal"
        assert "id" in item
        assert len(item["id"]) == 8
        assert item["completed_at"] is None

    def test_add_with_priority(self, patched_todos):
        """Priority is set correctly."""
        item = patched_todos.add_todo("beta", "Security audit", priority="critical")
        assert item["priority"] == "critical"

    def test_add_invalid_priority_defaults_normal(self, patched_todos):
        """Invalid priority falls back to 'normal'."""
        item = patched_todos.add_todo("gamma", "Some task", priority="ultra")
        assert item["priority"] == "normal"

    def test_add_lowercases_worker(self, patched_todos):
        """Worker name is lowercased."""
        item = patched_todos.add_todo("DELTA", "Test task")
        assert item["worker"] == "delta"

    def test_add_persists(self, patched_todos, todos_file):
        """Added TODO is persisted to file."""
        patched_todos.add_todo("alpha", "Persistent task")
        data = json.loads(todos_file.read_text(encoding="utf-8"))
        assert len(data["todos"]) == 1
        assert data["todos"][0]["task"] == "Persistent task"

    def test_add_multiple(self, patched_todos):
        """Multiple TODOs can be added."""
        patched_todos.add_todo("alpha", "Task 1")
        patched_todos.add_todo("alpha", "Task 2")
        patched_todos.add_todo("beta", "Task 3")
        items = patched_todos.list_todos()
        assert len(items) == 3

    def test_add_unique_ids(self, patched_todos):
        """Each TODO gets a unique ID."""
        i1 = patched_todos.add_todo("alpha", "Task 1")
        i2 = patched_todos.add_todo("alpha", "Task 2")
        assert i1["id"] != i2["id"]


# ── Update Status Tests ─────────────────────────────────────────────────────

class TestUpdateStatus:
    """Tests for update_status()."""

    def test_update_to_active(self, patched_todos):
        """Status updates from pending to active."""
        item = patched_todos.add_todo("alpha", "My task")
        result = patched_todos.update_status(item["id"], "active")
        assert result is not None
        assert result["status"] == "active"

    def test_update_to_done_sets_completed_at(self, patched_todos):
        """Marking done sets completed_at timestamp."""
        item = patched_todos.add_todo("alpha", "Complete me")
        with patch("tools.skynet_scoring.award_zero_ticket_clear", return_value={}):
            result = patched_todos.update_status(item["id"], "done")
        assert result["status"] == "done"
        assert result["completed_at"] is not None

    def test_update_to_done_records_completed_by_when_provided(self, patched_todos):
        """Done updates may explicitly attribute the closer."""
        item = patched_todos.add_todo("alpha", "Attribute me")
        with patch("tools.skynet_scoring.award_zero_ticket_clear", return_value={}) as award:
            result = patched_todos.update_status(item["id"], "done", completed_by="beta")
        assert result["completed_by"] == "beta"
        award.assert_called_once_with(item["id"], "beta", "god")

    def test_update_nonexistent_returns_none(self, patched_todos):
        """Updating nonexistent ID returns None."""
        result = patched_todos.update_status("nonexistent_id", "done")
        assert result is None

    def test_update_to_cancelled(self, patched_todos):
        """Status can be set to cancelled."""
        item = patched_todos.add_todo("beta", "Cancel me")
        result = patched_todos.update_status(item["id"], "cancelled")
        assert result["status"] == "cancelled"


# ── Mark Done Tests ─────────────────────────────────────────────────────────

class TestMarkDone:
    """Tests for mark_done()."""

    def test_mark_done_correct_worker(self, patched_todos):
        """mark_done works when worker matches."""
        item = patched_todos.add_todo("gamma", "Finish this")
        with patch("tools.skynet_scoring.award_zero_ticket_clear", return_value={}) as award:
            result = patched_todos.mark_done("gamma", item["id"])
        assert result is not None
        assert result["status"] == "done"
        assert result["completed_at"] is not None
        assert result["completed_by"] == "gamma"
        award.assert_called_once_with(item["id"], "gamma", "god")

    def test_mark_done_wrong_worker(self, patched_todos):
        """mark_done returns None when worker doesn't match."""
        item = patched_todos.add_todo("gamma", "Not yours")
        result = patched_todos.mark_done("alpha", item["id"])
        assert result is None

    def test_mark_done_nonexistent_id(self, patched_todos):
        """mark_done returns None for nonexistent ID."""
        result = patched_todos.mark_done("alpha", "no_such_id")
        assert result is None

    def test_mark_done_does_not_award_zero_ticket_bonus_with_open_backlog(self, patched_todos):
        """Closing a non-final ticket must not trigger the zero-ticket bonus."""
        first = patched_todos.add_todo("gamma", "Finish this")
        patched_todos.add_todo("gamma", "Still open")
        with patch("tools.skynet_scoring.award_zero_ticket_clear", return_value={}) as award:
            result = patched_todos.mark_done("gamma", first["id"])
        assert result is not None
        award.assert_not_called()


# ── List Todos Tests ────────────────────────────────────────────────────────

class TestListTodos:
    """Tests for list_todos()."""

    def test_list_all(self, patched_todos):
        """Lists all TODOs when no filter."""
        patched_todos.add_todo("alpha", "Task A")
        patched_todos.add_todo("beta", "Task B")
        items = patched_todos.list_todos()
        assert len(items) == 2

    def test_list_by_worker(self, patched_todos):
        """Filters by worker name."""
        patched_todos.add_todo("alpha", "Alpha task")
        patched_todos.add_todo("beta", "Beta task")
        items = patched_todos.list_todos(worker="alpha")
        assert len(items) == 1
        assert items[0]["worker"] == "alpha"

    def test_list_by_status(self, patched_todos):
        """Filters by status."""
        i1 = patched_todos.add_todo("alpha", "Pending")
        i2 = patched_todos.add_todo("alpha", "Will be done")
        patched_todos.update_status(i2["id"], "done")
        items = patched_todos.list_todos(status="pending")
        assert len(items) == 1
        assert items[0]["id"] == i1["id"]

    def test_list_by_worker_and_status(self, patched_todos):
        """Filters by both worker and status."""
        patched_todos.add_todo("alpha", "Alpha pending")
        patched_todos.add_todo("beta", "Beta pending")
        i3 = patched_todos.add_todo("alpha", "Alpha done")
        patched_todos.update_status(i3["id"], "done")
        items = patched_todos.list_todos(worker="alpha", status="pending")
        assert len(items) == 1

    def test_list_empty(self, patched_todos):
        """Empty list returns empty."""
        items = patched_todos.list_todos()
        assert items == []

    def test_list_worker_case_insensitive(self, patched_todos):
        """Worker filter is case-insensitive (stored lowercase)."""
        patched_todos.add_todo("ALPHA", "My task")
        items = patched_todos.list_todos(worker="alpha")
        assert len(items) == 1


# ── Summary Tests ───────────────────────────────────────────────────────────

class TestGetSummary:
    """Tests for get_summary()."""

    def test_summary_empty(self, patched_todos):
        """Summary of empty TODO list."""
        s = patched_todos.get_summary()
        assert s["total"] == 0
        assert s["pending"] == 0
        assert s["active"] == 0
        assert s["done"] == 0
        assert "timestamp" in s

    def test_summary_counts(self, patched_todos):
        """Summary counts are accurate."""
        patched_todos.add_todo("alpha", "T1")
        patched_todos.add_todo("alpha", "T2")
        i3 = patched_todos.add_todo("beta", "T3")
        patched_todos.update_status(i3["id"], "done")
        s = patched_todos.get_summary()
        assert s["total"] == 3
        assert s["pending"] == 2
        assert s["done"] == 1

    def test_summary_by_worker(self, patched_todos):
        """Summary breaks down by worker."""
        patched_todos.add_todo("alpha", "T1")
        patched_todos.add_todo("beta", "T2")
        s = patched_todos.get_summary()
        assert "alpha" in s["by_worker"]
        assert "beta" in s["by_worker"]
        assert s["by_worker"]["alpha"]["pending"] == 1


# ── Pending Count and Can Stop Tests ────────────────────────────────────────

class TestPendingAndCanStop:
    """Tests for pending_count() and can_stop()."""

    def test_pending_count_zero(self, patched_todos):
        """Zero pending when no TODOs."""
        assert patched_todos.pending_count("delta") == 0

    def test_pending_count_accurate(self, patched_todos):
        """Counts pending and active items."""
        patched_todos.add_todo("delta", "T1")
        i2 = patched_todos.add_todo("delta", "T2")
        patched_todos.update_status(i2["id"], "active")
        assert patched_todos.pending_count("delta") == 2

    def test_pending_excludes_done(self, patched_todos):
        """Done items not counted as pending."""
        i1 = patched_todos.add_todo("delta", "Done task")
        patched_todos.update_status(i1["id"], "done")
        assert patched_todos.pending_count("delta") == 0

    def test_can_stop_true_when_empty(self, patched_todos):
        """Worker can stop when no pending TODOs."""
        assert patched_todos.can_stop("delta") is True

    def test_can_stop_false_when_pending(self, patched_todos):
        """Worker cannot stop when pending TODOs exist."""
        patched_todos.add_todo("delta", "Must do this")
        assert patched_todos.can_stop("delta") is False

    def test_can_stop_true_when_all_done(self, patched_todos):
        """Worker can stop when all TODOs are done."""
        i1 = patched_todos.add_todo("delta", "Finished")
        patched_todos.update_status(i1["id"], "done")
        assert patched_todos.can_stop("delta") is True


# ── Bulk Update Tests ───────────────────────────────────────────────────────

class TestBulkUpdate:
    """Tests for bulk_update()."""

    def test_bulk_replaces_pending(self, patched_todos):
        """Bulk update replaces pending items for worker."""
        patched_todos.add_todo("alpha", "Old task 1")
        patched_todos.add_todo("alpha", "Old task 2")
        patched_todos.bulk_update("alpha", [
            {"task": "New task A", "priority": "high"},
            {"task": "New task B"},
        ])
        items = patched_todos.list_todos(worker="alpha")
        tasks = [t["task"] for t in items]
        assert "New task A" in tasks
        assert "New task B" in tasks
        assert "Old task 1" not in tasks

    def test_bulk_preserves_done_items(self, patched_todos):
        """Bulk update keeps done items."""
        i1 = patched_todos.add_todo("alpha", "Completed task")
        patched_todos.update_status(i1["id"], "done")
        patched_todos.bulk_update("alpha", [{"task": "New task"}])
        items = patched_todos.list_todos(worker="alpha")
        statuses = [t["status"] for t in items]
        assert "done" in statuses

    def test_bulk_with_string_items(self, patched_todos):
        """Bulk update accepts plain string items."""
        patched_todos.bulk_update("beta", ["Task X", "Task Y"])
        items = patched_todos.list_todos(worker="beta")
        assert len(items) == 2

    def test_bulk_doesnt_affect_other_workers(self, patched_todos):
        """Bulk update for alpha doesn't affect beta."""
        patched_todos.add_todo("beta", "Beta task")
        patched_todos.bulk_update("alpha", [{"task": "Alpha task"}])
        beta_items = patched_todos.list_todos(worker="beta")
        assert len(beta_items) == 1


# ── Cleanup Tests ───────────────────────────────────────────────────────────

class TestCleanup:
    """Tests for cleanup()."""

    def test_cleanup_removes_old_done(self, patched_todos, todos_file):
        """Cleanup removes done items older than threshold."""
        # Create a done item with old timestamp
        old_time = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 86400 * 10))
        data = {
            "todos": [{
                "id": "old1",
                "worker": "alpha",
                "task": "Ancient task",
                "status": "done",
                "priority": "normal",
                "created_at": old_time,
                "completed_at": old_time,
            }],
            "version": 1,
        }
        todos_file.write_text(json.dumps(data), encoding="utf-8")
        removed = patched_todos.cleanup(days_old=7)
        assert removed == 1

    def test_cleanup_keeps_recent_done(self, patched_todos):
        """Cleanup keeps recently completed items."""
        i1 = patched_todos.add_todo("alpha", "Recent done")
        patched_todos.update_status(i1["id"], "done")
        removed = patched_todos.cleanup(days_old=7)
        assert removed == 0

    def test_cleanup_keeps_pending(self, patched_todos):
        """Cleanup never removes pending items."""
        patched_todos.add_todo("alpha", "Still pending")
        removed = patched_todos.cleanup(days_old=0)
        assert removed == 0
        items = patched_todos.list_todos()
        assert len(items) == 1


# ── Valid Constants Tests ───────────────────────────────────────────────────

class TestConstants:
    """Tests for module constants."""

    def test_valid_statuses(self):
        """All expected statuses are defined."""
        from tools.skynet_todos import VALID_STATUSES
        assert "pending" in VALID_STATUSES
        assert "active" in VALID_STATUSES
        assert "done" in VALID_STATUSES
        assert "cancelled" in VALID_STATUSES

    def test_valid_priorities(self):
        """All expected priorities are defined."""
        from tools.skynet_todos import VALID_PRIORITIES
        assert "low" in VALID_PRIORITIES
        assert "normal" in VALID_PRIORITIES
        assert "high" in VALID_PRIORITIES
        assert "critical" in VALID_PRIORITIES
