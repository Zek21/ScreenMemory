"""Tests for tools/skynet_todos.py — claim_todo() and auto_claim() functions.

Tests cover: basic claiming, priority ranking, oldest-first tie-breaking,
not-found handling, empty claimable list, assignee normalization,
status transitions, and corrupted status fail-safe in pending_count.

Created by worker alpha — zero-coverage gap fill for critical TODO claim system.
"""
# signed: alpha

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

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


def _make_todo(id, title="task", status="pending", priority="normal",
               assignee="", created_at="2026-01-01T00:00:00"):
    """Helper to build a TODO dict."""
    return {
        "id": id,
        "title": title,
        "task": title,
        "status": status,
        "priority": priority,
        "assignee": assignee,
        "worker": assignee,
        "created_at": created_at,
        "updated_at": created_at,
        "completed_at": "",
    }


def _seed(todos_file, items):
    """Write items into the temp todos.json."""
    todos_file.write_text(
        json.dumps({"todos": items, "version": 1}), encoding="utf-8"
    )


# ── claim_todo Tests ────────────────────────────────────────────────────────

class TestClaimTodo:
    """Tests for claim_todo()."""

    def test_claim_sets_assignee_and_status(self, patched_todos, todos_file):
        """Claiming sets assignee, worker, status=active, and timestamps."""
        _seed(todos_file, [_make_todo("t1", assignee="shared")])
        result = patched_todos.claim_todo("t1", "alpha")
        assert result is not None
        assert result["assignee"] == "alpha"
        assert result["worker"] == "alpha"
        assert result["status"] == "active"
        assert result["claimed_at"] != ""
        assert result["updated_at"] != ""
        # signed: alpha

    def test_claim_nonexistent_returns_none(self, patched_todos, todos_file):
        """Claiming a TODO that doesn't exist returns None."""
        _seed(todos_file, [_make_todo("t1")])
        result = patched_todos.claim_todo("nonexistent", "alpha")
        assert result is None
        # signed: alpha

    def test_claim_persists_to_disk(self, patched_todos, todos_file):
        """Claimed TODO is saved to disk."""
        _seed(todos_file, [_make_todo("t1", assignee="")])
        patched_todos.claim_todo("t1", "beta")
        data = json.loads(todos_file.read_text(encoding="utf-8"))
        found = [t for t in data["todos"] if t["id"] == "t1"]
        assert len(found) == 1
        assert found[0]["assignee"] == "beta"
        assert found[0]["status"] == "active"
        # signed: alpha

    def test_claim_normalizes_worker_name(self, patched_todos, todos_file):
        """Worker name is lowercased on claim."""
        _seed(todos_file, [_make_todo("t1", assignee="shared")])
        result = patched_todos.claim_todo("t1", "ALPHA")
        assert result["assignee"] == "alpha"
        assert result["worker"] == "alpha"
        # signed: alpha

    def test_claim_empty_list(self, patched_todos, todos_file):
        """Claiming from empty list returns None."""
        result = patched_todos.claim_todo("t1", "alpha")
        assert result is None
        # signed: alpha


# ── auto_claim Tests ────────────────────────────────────────────────────────

class TestAutoClaim:
    """Tests for auto_claim()."""

    def test_auto_claim_picks_highest_priority(self, patched_todos, todos_file):
        """auto_claim picks critical before normal."""
        _seed(todos_file, [
            _make_todo("low1", priority="low", assignee="shared"),
            _make_todo("crit1", priority="critical", assignee="shared"),
            _make_todo("norm1", priority="normal", assignee=""),
        ])
        result = patched_todos.auto_claim("alpha")
        assert result is not None
        assert result["id"] == "crit1"
        assert result["assignee"] == "alpha"
        assert result["status"] == "active"
        # signed: alpha

    def test_auto_claim_tiebreak_oldest_first(self, patched_todos, todos_file):
        """Same priority: oldest (earliest created_at) claimed first."""
        _seed(todos_file, [
            _make_todo("new", priority="normal", assignee="shared",
                       created_at="2026-03-02T00:00:00"),
            _make_todo("old", priority="normal", assignee="shared",
                       created_at="2026-01-01T00:00:00"),
        ])
        result = patched_todos.auto_claim("gamma")
        assert result["id"] == "old"
        # signed: alpha

    def test_auto_claim_skips_assigned_todos(self, patched_todos, todos_file):
        """Items assigned to a specific worker are NOT claimable."""
        _seed(todos_file, [
            _make_todo("assigned", priority="critical", assignee="delta"),
            _make_todo("shared1", priority="low", assignee="shared"),
        ])
        result = patched_todos.auto_claim("alpha")
        assert result["id"] == "shared1"
        # signed: alpha

    def test_auto_claim_skips_non_pending(self, patched_todos, todos_file):
        """Only pending items are claimable; active/done/cancelled skipped."""
        _seed(todos_file, [
            _make_todo("active1", status="active", assignee="shared"),
            _make_todo("done1", status="done", assignee="shared"),
            _make_todo("ok1", status="pending", assignee="shared"),
        ])
        result = patched_todos.auto_claim("alpha")
        assert result["id"] == "ok1"
        # signed: alpha

    def test_auto_claim_empty_returns_none(self, patched_todos, todos_file):
        """No claimable items returns None."""
        _seed(todos_file, [
            _make_todo("t1", assignee="delta", status="pending"),
        ])
        result = patched_todos.auto_claim("alpha")
        assert result is None
        # signed: alpha

    def test_auto_claim_all_shared_assignees(self, patched_todos, todos_file):
        """All SHARED_ASSIGNEES values are claimable."""
        for assignee in ("", "all", "shared", "any", "unassigned", "backlog"):
            _seed(todos_file, [
                _make_todo("t1", assignee=assignee, priority="normal"),
            ])
            result = patched_todos.auto_claim("alpha")
            assert result is not None, f"assignee={assignee!r} should be claimable"
            assert result["assignee"] == "alpha"
        # signed: alpha

    def test_auto_claim_priority_order(self, patched_todos, todos_file):
        """Full priority order: critical > high > normal > low."""
        _seed(todos_file, [
            _make_todo("p_low", priority="low", assignee="shared"),
            _make_todo("p_norm", priority="normal", assignee="shared"),
            _make_todo("p_high", priority="high", assignee="shared"),
            _make_todo("p_crit", priority="critical", assignee="shared"),
        ])
        # Claim all 4 in sequence, verify order
        order = []
        for _ in range(4):
            result = patched_todos.auto_claim("alpha")
            assert result is not None
            order.append(result["id"])
        assert order == ["p_crit", "p_high", "p_norm", "p_low"]
        # signed: alpha


# ── pending_count corrupted status Tests ────────────────────────────────────

class TestPendingCountCorruptedStatus:
    """Tests for pending_count handling of corrupted/unknown statuses."""

    def test_corrupted_status_counts_as_blocking(self, patched_todos, todos_file):
        """Unrecognized status is treated as blocking (fail-safe)."""
        _seed(todos_file, [
            _make_todo("t1", status="pendig", assignee="alpha"),  # typo
        ])
        count = patched_todos.pending_count("alpha", include_claimable=False)
        assert count == 1  # Corrupted status must block
        # signed: alpha

    def test_unknown_status_blocks_can_stop(self, patched_todos, todos_file):
        """can_stop returns False when corrupted TODO exists."""
        _seed(todos_file, [
            _make_todo("t1", status="UNKNOWN", assignee="alpha"),
        ])
        assert patched_todos.can_stop("alpha", include_claimable=False) is False
        # signed: alpha

    def test_valid_done_does_not_block(self, patched_todos, todos_file):
        """Done status does not block can_stop."""
        _seed(todos_file, [
            _make_todo("t1", status="done", assignee="alpha"),
        ])
        assert patched_todos.can_stop("alpha", include_claimable=False) is True
        # signed: alpha

    def test_include_claimable_false_excludes_shared(self, patched_todos, todos_file):
        """include_claimable=False ignores shared/unassigned items."""
        _seed(todos_file, [
            _make_todo("shared1", status="pending", assignee="shared"),
        ])
        count = patched_todos.pending_count("alpha", include_claimable=False)
        assert count == 0
        # signed: alpha

    def test_include_claimable_true_includes_shared(self, patched_todos, todos_file):
        """include_claimable=True counts shared pending items."""
        _seed(todos_file, [
            _make_todo("shared1", status="pending", assignee="shared"),
        ])
        count = patched_todos.pending_count("alpha", include_claimable=True)
        assert count == 1
        # signed: alpha
