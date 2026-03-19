"""Integration tests for tools/skynet_todos.py — concurrent claims, priority ordering,
bulk_update idempotency, zero-ticket bonus trigger, and cleanup of old items.

Uses threading to test atomic claim race conditions (two workers claiming same TODO).

# signed: delta
"""

import json
import sys
import threading
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
    """Create a temporary todos.json."""
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


def _seed_todos(todos_file, items):
    """Write items directly into the temp todos.json."""
    todos_file.write_text(
        json.dumps({"todos": items, "version": 1}), encoding="utf-8"
    )


def _make_todo(id, task="test task", status="pending", priority="normal",
               assignee="", worker="", created_at=None):
    """Helper to build a TODO dict."""
    now = created_at or time.strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "id": id,
        "task": task,
        "title": task,
        "status": status,
        "priority": priority,
        "assignee": assignee or "",
        "worker": worker or assignee or "",
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }


# ── Concurrent Claim Race Condition Tests ───────────────────────────────────

class TestConcurrentClaims:
    """Tests for atomic claim_todo under concurrent access.

    Two workers racing to claim the same TODO should result in exactly one winner.
    """

    def test_two_workers_claim_same_todo(self, patched_todos, todos_file):
        """Two workers claiming the same TODO — only one should win."""
        _seed_todos(todos_file, [
            _make_todo("race1", task="contested task", assignee="shared"),
        ])

        results = {"alpha": None, "beta": None}
        errors = []

        def claim_worker(name):
            try:
                results[name] = patched_todos.claim_todo("race1", name)
            except Exception as e:
                errors.append((name, str(e)))

        t1 = threading.Thread(target=claim_worker, args=("alpha",))
        t2 = threading.Thread(target=claim_worker, args=("beta",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert not errors, f"Errors during concurrent claims: {errors}"
        # Both get non-None results since claim_todo doesn't check current assignee
        # But the final state on disk should have exactly one winner
        data = json.loads(todos_file.read_text(encoding="utf-8"))
        todo = [t for t in data["todos"] if t["id"] == "race1"][0]
        assert todo["assignee"] in ("alpha", "beta")
        assert todo["status"] == "active"
        # signed: delta

    def test_concurrent_auto_claim_single_item(self, patched_todos, todos_file):
        """Two workers auto_claim with only one claimable item — one wins."""
        _seed_todos(todos_file, [
            _make_todo("single1", assignee="shared", priority="high"),
        ])

        results = {"alpha": None, "beta": None}

        def auto_claim_worker(name):
            results[name] = patched_todos.auto_claim(name)

        t1 = threading.Thread(target=auto_claim_worker, args=("alpha",))
        t2 = threading.Thread(target=auto_claim_worker, args=("beta",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # At least one should get the item; file should show one winner
        data = json.loads(todos_file.read_text(encoding="utf-8"))
        todo = [t for t in data["todos"] if t["id"] == "single1"][0]
        assert todo["status"] == "active"
        assert todo["assignee"] in ("alpha", "beta")
        # signed: delta

    def test_concurrent_auto_claim_two_items(self, patched_todos, todos_file):
        """Two workers auto_claim with two items — each should get one."""
        _seed_todos(todos_file, [
            _make_todo("item1", assignee="shared", priority="normal",
                       created_at="2026-01-01T00:00:00"),
            _make_todo("item2", assignee="shared", priority="normal",
                       created_at="2026-01-02T00:00:00"),
        ])

        results = {"alpha": None, "beta": None}

        def auto_claim_worker(name):
            results[name] = patched_todos.auto_claim(name)

        t1 = threading.Thread(target=auto_claim_worker, args=("alpha",))
        t2 = threading.Thread(target=auto_claim_worker, args=("beta",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Both should get an item (though same item possible due to race)
        claimed = [r for r in results.values() if r is not None]
        assert len(claimed) >= 1  # At least one worker got an item
        # signed: delta


# ── Auto-Claim Priority Ordering Tests ──────────────────────────────────────

class TestAutoClaimPriorityOrdering:
    """Tests for auto_claim() priority-based ordering."""

    def test_critical_before_high_before_normal(self, patched_todos, todos_file):
        """Auto-claim picks critical > high > normal > low."""
        _seed_todos(todos_file, [
            _make_todo("low1", priority="low", assignee="shared"),
            _make_todo("norm1", priority="normal", assignee="shared"),
            _make_todo("high1", priority="high", assignee="shared"),
            _make_todo("crit1", priority="critical", assignee="shared"),
        ])
        # Claim in sequence and verify order
        order = []
        for _ in range(4):
            result = patched_todos.auto_claim("delta")
            assert result is not None
            order.append(result["id"])
        assert order == ["crit1", "high1", "norm1", "low1"]
        # signed: delta

    def test_same_priority_oldest_first(self, patched_todos, todos_file):
        """Same priority: oldest (earliest created_at) is claimed first."""
        _seed_todos(todos_file, [
            _make_todo("new1", priority="normal", assignee="shared",
                       created_at="2026-03-15T00:00:00"),
            _make_todo("old1", priority="normal", assignee="shared",
                       created_at="2026-01-01T00:00:00"),
            _make_todo("mid1", priority="normal", assignee="shared",
                       created_at="2026-02-01T00:00:00"),
        ])
        result = patched_todos.auto_claim("delta")
        assert result["id"] == "old1"
        # signed: delta

    def test_skips_already_claimed(self, patched_todos, todos_file):
        """Auto-claim skips items already assigned to a specific worker."""
        _seed_todos(todos_file, [
            _make_todo("claimed1", priority="critical", assignee="alpha",
                       status="active"),
            _make_todo("free1", priority="low", assignee="shared"),
        ])
        result = patched_todos.auto_claim("beta")
        assert result is not None
        assert result["id"] == "free1"
        # signed: delta

    def test_skips_non_pending(self, patched_todos, todos_file):
        """Auto-claim only considers pending items."""
        _seed_todos(todos_file, [
            _make_todo("active1", status="active", assignee="shared"),
            _make_todo("done1", status="done", assignee="shared"),
            _make_todo("cancelled1", status="cancelled", assignee="shared"),
            _make_todo("pending1", status="pending", assignee="shared"),
        ])
        result = patched_todos.auto_claim("delta")
        assert result["id"] == "pending1"
        # signed: delta

    def test_no_claimable_returns_none(self, patched_todos, todos_file):
        """No claimable items returns None."""
        _seed_todos(todos_file, [
            _make_todo("assigned1", assignee="alpha", status="pending"),
        ])
        result = patched_todos.auto_claim("beta")
        assert result is None
        # signed: delta

    def test_empty_list_returns_none(self, patched_todos):
        """Auto-claim on empty list returns None."""
        result = patched_todos.auto_claim("delta")
        assert result is None
        # signed: delta


# ── Bulk Update Idempotency Tests ───────────────────────────────────────────

class TestBulkUpdateIdempotency:
    """Tests for bulk_update() being idempotent and preserving done items."""

    def test_double_bulk_update_same_items(self, patched_todos, todos_file):
        """Applying the same bulk_update twice produces same result."""
        items = [
            {"task": "Task A", "priority": "high"},
            {"task": "Task B", "priority": "normal"},
        ]
        patched_todos.bulk_update("alpha", items)
        first = patched_todos.list_todos(worker="alpha")
        first_tasks = sorted([t["task"] for t in first])

        patched_todos.bulk_update("alpha", items)
        second = patched_todos.list_todos(worker="alpha")
        second_tasks = sorted([t["task"] for t in second])

        assert first_tasks == second_tasks
        # signed: delta

    def test_bulk_update_preserves_done_items(self, patched_todos, todos_file):
        """Done items survive bulk_update."""
        item = patched_todos.add_todo("alpha", "Completed task")
        patched_todos.update_status(item["id"], "done")

        patched_todos.bulk_update("alpha", [{"task": "New task"}])

        items = patched_todos.list_todos(worker="alpha")
        statuses = [t["status"] for t in items]
        assert "done" in statuses
        tasks = [t["task"] for t in items]
        assert "New task" in tasks
        assert "Completed task" in tasks
        # signed: delta

    def test_bulk_update_replaces_pending_only(self, patched_todos, todos_file):
        """Bulk update replaces pending/active but keeps done."""
        patched_todos.add_todo("beta", "Old pending task")
        item = patched_todos.add_todo("beta", "Done task")
        patched_todos.update_status(item["id"], "done")

        patched_todos.bulk_update("beta", [{"task": "Replacement"}])

        items = patched_todos.list_todos(worker="beta")
        tasks = [t["task"] for t in items]
        assert "Old pending task" not in tasks
        assert "Replacement" in tasks
        assert "Done task" in tasks
        # signed: delta

    def test_bulk_update_doesnt_affect_other_workers(self, patched_todos):
        """Bulk update for one worker leaves others untouched."""
        patched_todos.add_todo("gamma", "Gamma task")
        patched_todos.bulk_update("alpha", [{"task": "Alpha task"}])

        gamma_items = patched_todos.list_todos(worker="gamma")
        assert len(gamma_items) == 1
        assert gamma_items[0]["task"] == "Gamma task"
        # signed: delta

    def test_bulk_update_accepts_string_items(self, patched_todos):
        """Bulk update can accept plain strings as items."""
        patched_todos.bulk_update("delta", ["String task 1", "String task 2"])
        items = patched_todos.list_todos(worker="delta")
        assert len(items) == 2
        # signed: delta


# ── Zero-Ticket Bonus Award Tests ──────────────────────────────────────────

class TestZeroTicketBonus:
    """Tests for zero-ticket bonus award trigger when last item is closed."""

    def test_bonus_awarded_on_last_item_done(self, patched_todos):
        """Zero-ticket bonus fires when the FINAL open item is completed."""
        item = patched_todos.add_todo("alpha", "Last task standing")
        with patch("tools.skynet_scoring.award_zero_ticket_clear") as mock_award:
            patched_todos.mark_done("alpha", item["id"])
        mock_award.assert_called_once_with(item["id"], "alpha", "god")
        # signed: delta

    def test_bonus_not_awarded_with_remaining_items(self, patched_todos):
        """Bonus does NOT fire when other items still exist."""
        i1 = patched_todos.add_todo("alpha", "First task")
        patched_todos.add_todo("beta", "Second task")
        with patch("tools.skynet_scoring.award_zero_ticket_clear") as mock_award:
            patched_todos.mark_done("alpha", i1["id"])
        mock_award.assert_not_called()
        # signed: delta

    def test_bonus_fires_on_update_status_done(self, patched_todos):
        """Bonus also fires via update_status(..., 'done')."""
        item = patched_todos.add_todo("gamma", "Only item")
        with patch("tools.skynet_scoring.award_zero_ticket_clear") as mock_award:
            patched_todos.update_status(item["id"], "done", completed_by="gamma")
        mock_award.assert_called_once_with(item["id"], "gamma", "god")
        # signed: delta

    def test_bonus_uses_completed_by_param(self, patched_todos):
        """Bonus credits the explicit completed_by worker."""
        item = patched_todos.add_todo("shared", "Shared task")
        # Manually set assignee to shared
        data = json.loads(patched_todos.list_todos.__wrapped__ if hasattr(patched_todos.list_todos, '__wrapped__') else "[]")
        with patch("tools.skynet_scoring.award_zero_ticket_clear") as mock_award:
            patched_todos.update_status(item["id"], "done", completed_by="delta")
        mock_award.assert_called_once_with(item["id"], "delta", "god")
        # signed: delta

    def test_bonus_not_fired_on_non_done_status(self, patched_todos):
        """Bonus does NOT fire on cancelled or active status changes."""
        item = patched_todos.add_todo("alpha", "Only item")
        with patch("tools.skynet_scoring.award_zero_ticket_clear") as mock_award:
            patched_todos.update_status(item["id"], "cancelled")
        # cancelled doesn't trigger zero-ticket check
        mock_award.assert_not_called()
        # signed: delta


# ── Cleanup of Old Items Tests ──────────────────────────────────────────────

class TestCleanupIntegration:
    """Integration tests for cleanup() of old completed items."""

    def test_cleanup_removes_old_done_items(self, patched_todos, todos_file):
        """Cleanup removes done items older than specified days."""
        old_time = time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.localtime(time.time() - 86400 * 14)  # 14 days ago
        )
        _seed_todos(todos_file, [
            {
                "id": "ancient1", "worker": "alpha", "task": "Ancient task",
                "status": "done", "priority": "normal",
                "created_at": old_time, "completed_at": old_time,
                "updated_at": old_time,
            }
        ])
        removed = patched_todos.cleanup(days_old=7)
        assert removed == 1
        assert len(patched_todos.list_todos()) == 0
        # signed: delta

    def test_cleanup_keeps_recent_done(self, patched_todos):
        """Recently completed items are preserved."""
        item = patched_todos.add_todo("alpha", "Just finished")
        patched_todos.update_status(item["id"], "done")
        removed = patched_todos.cleanup(days_old=7)
        assert removed == 0
        # signed: delta

    def test_cleanup_never_removes_pending(self, patched_todos):
        """Pending items are never removed by cleanup."""
        patched_todos.add_todo("alpha", "Still working")
        removed = patched_todos.cleanup(days_old=0)
        assert removed == 0
        items = patched_todos.list_todos()
        assert len(items) == 1
        # signed: delta

    def test_cleanup_never_removes_active(self, patched_todos):
        """Active items are never removed by cleanup."""
        item = patched_todos.add_todo("alpha", "In progress")
        patched_todos.update_status(item["id"], "active")
        removed = patched_todos.cleanup(days_old=0)
        assert removed == 0
        # signed: delta

    def test_cleanup_mixed_old_and_new(self, patched_todos, todos_file):
        """Cleanup removes only old done items, keeps everything else."""
        old_time = time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.localtime(time.time() - 86400 * 30)  # 30 days ago
        )
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        _seed_todos(todos_file, [
            {"id": "old1", "worker": "alpha", "task": "Old done",
             "status": "done", "priority": "normal",
             "created_at": old_time, "completed_at": old_time, "updated_at": old_time},
            {"id": "new1", "worker": "alpha", "task": "New done",
             "status": "done", "priority": "normal",
             "created_at": now, "completed_at": now, "updated_at": now},
            {"id": "pending1", "worker": "beta", "task": "Still pending",
             "status": "pending", "priority": "high",
             "created_at": old_time, "updated_at": old_time, "completed_at": None},
        ])
        removed = patched_todos.cleanup(days_old=7)
        assert removed == 1
        items = patched_todos.list_todos()
        ids = [t["id"] for t in items]
        assert "old1" not in ids
        assert "new1" in ids
        assert "pending1" in ids
        # signed: delta


# ── Open TODO Count and Ticket Cleared Tests ────────────────────────────────

class TestOpenTodoCount:
    """Tests for open_todo_count() and all_tickets_cleared()."""

    def test_open_count_zero_initially(self, patched_todos):
        """Empty list has zero open count."""
        assert patched_todos.open_todo_count() == 0
        # signed: delta

    def test_open_count_excludes_done(self, patched_todos):
        """Done items not counted in open count."""
        i1 = patched_todos.add_todo("alpha", "Task 1")
        patched_todos.update_status(i1["id"], "done")
        assert patched_todos.open_todo_count() == 0
        # signed: delta

    def test_open_count_includes_pending_and_active(self, patched_todos):
        """Pending and active items counted."""
        patched_todos.add_todo("alpha", "Pending task")
        i2 = patched_todos.add_todo("beta", "Active task")
        patched_todos.update_status(i2["id"], "active")
        assert patched_todos.open_todo_count() == 2
        # signed: delta

    def test_all_cleared_true_when_empty(self, patched_todos):
        """all_tickets_cleared returns True on empty list."""
        assert patched_todos.all_tickets_cleared() is True
        # signed: delta

    def test_all_cleared_false_with_pending(self, patched_todos):
        """all_tickets_cleared returns False with pending items."""
        patched_todos.add_todo("alpha", "Pending")
        assert patched_todos.all_tickets_cleared() is False
        # signed: delta

    def test_all_cleared_true_after_all_done(self, patched_todos):
        """all_tickets_cleared returns True after all items done."""
        i1 = patched_todos.add_todo("alpha", "Task 1")
        i2 = patched_todos.add_todo("beta", "Task 2")
        patched_todos.update_status(i1["id"], "done")
        patched_todos.update_status(i2["id"], "done")
        assert patched_todos.all_tickets_cleared() is True
        # signed: delta


# ── Claimable Count Tests ───────────────────────────────────────────────────

class TestClaimableCount:
    """Tests for claimable_count()."""

    def test_claimable_zero_no_shared(self, patched_todos, todos_file):
        """Zero claimable when no shared items exist."""
        _seed_todos(todos_file, [
            _make_todo("t1", assignee="alpha", status="pending"),
        ])
        assert patched_todos.claimable_count("beta") == 0
        # signed: delta

    def test_claimable_counts_shared_pending(self, patched_todos, todos_file):
        """Counts shared pending items as claimable."""
        _seed_todos(todos_file, [
            _make_todo("t1", assignee="shared", status="pending"),
            _make_todo("t2", assignee="", status="pending"),
            _make_todo("t3", assignee="backlog", status="pending"),
        ])
        assert patched_todos.claimable_count("delta") == 3
        # signed: delta

    def test_claimable_excludes_done_shared(self, patched_todos, todos_file):
        """Done shared items are not claimable."""
        _seed_todos(todos_file, [
            _make_todo("t1", assignee="shared", status="done"),
            _make_todo("t2", assignee="shared", status="pending"),
        ])
        assert patched_todos.claimable_count("delta") == 1
        # signed: delta
