# signed: alpha
"""
Tests for core/feedback_loop.py — Learning from task outcomes to improve routing.

Covers:
- TaskOutcome dataclass
- FeedbackLoop: DB init, record_outcome, agent_stats upsert, task_patterns upsert
- Query methods: get_best_agent, get_agent_report, get_system_report
- Improvement suggestions: reassignments, slow types, idle rebalancing, rate warnings
- Dashboard JSON writing
- Thread safety (lock usage)
- Edge cases: empty DB, single record, multiple agents
"""

import json
import os
import time
import tempfile
import pytest
from pathlib import Path
from core.feedback_loop import FeedbackLoop, TaskOutcome


# ── Fixtures ──


@pytest.fixture
def tmp_db(tmp_path):
    """Return a temp DB path for isolated tests."""
    return str(tmp_path / "test_feedback.db")


@pytest.fixture
def loop(tmp_db):
    """FeedbackLoop with a temp database."""
    return FeedbackLoop(db_path=tmp_db)


def _make_outcome(task_id="t1", agent_id="alpha", task_type="code",
                  description="test task", success=True,
                  duration_ms=500.0, error=None, output_summary="ok"):
    return TaskOutcome(
        task_id=task_id,
        agent_id=agent_id,
        task_type=task_type,
        description=description,
        success=success,
        duration_ms=duration_ms,
        error=error,
        output_summary=output_summary,
        timestamp=time.time(),
    )


# ── TaskOutcome ──


class TestTaskOutcome:
    def test_fields(self):
        outcome = _make_outcome()
        assert outcome.task_id == "t1"
        assert outcome.agent_id == "alpha"
        assert outcome.task_type == "code"
        assert outcome.success is True
        assert outcome.duration_ms == 500.0
        assert outcome.error is None


# ── FeedbackLoop DB init ──


class TestFeedbackLoopInit:
    def test_creates_tables(self, loop, tmp_db):
        """Tables should exist after init."""
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t[0] for t in tables}
        assert "outcomes" in table_names
        assert "agent_stats" in table_names
        assert "task_patterns" in table_names
        conn.close()

    def test_idempotent_init(self, tmp_db):
        """Creating FeedbackLoop twice on same DB should not error."""
        loop1 = FeedbackLoop(db_path=tmp_db)
        loop2 = FeedbackLoop(db_path=tmp_db)
        # Both should work fine
        loop1.record_outcome(_make_outcome(task_id="t1"))
        loop2.record_outcome(_make_outcome(task_id="t2"))


# ── Record outcome ──


class TestRecordOutcome:
    def test_record_single(self, loop):
        outcome = _make_outcome()
        loop.record_outcome(outcome)
        report = loop.get_agent_report("alpha")
        assert report["total"] == 1
        assert report["success_pct"] == 100.0

    def test_record_multiple_same_agent(self, loop):
        loop.record_outcome(_make_outcome(task_id="t1", success=True))
        loop.record_outcome(_make_outcome(task_id="t2", success=True))
        loop.record_outcome(_make_outcome(task_id="t3", success=False, error="fail"))
        report = loop.get_agent_report("alpha")
        assert report["total"] == 3
        assert report["success_pct"] == pytest.approx(66.7, abs=0.1)
        assert report["fail_pct"] == pytest.approx(33.3, abs=0.1)

    def test_record_multiple_agents(self, loop):
        loop.record_outcome(_make_outcome(task_id="t1", agent_id="alpha"))
        loop.record_outcome(_make_outcome(task_id="t2", agent_id="beta"))
        report_a = loop.get_agent_report("alpha")
        report_b = loop.get_agent_report("beta")
        assert report_a["total"] == 1
        assert report_b["total"] == 1

    def test_replace_outcome(self, loop):
        loop.record_outcome(_make_outcome(task_id="t1", success=True))
        loop.record_outcome(_make_outcome(task_id="t1", success=False, error="updated"))
        # Same task_id, should replace
        report = loop.get_agent_report("alpha")
        assert report["total"] == 2  # stats still increment


# ── Task patterns ──


class TestTaskPatterns:
    def test_pattern_created(self, loop):
        loop.record_outcome(_make_outcome(task_id="t1", task_type="code"))
        best = loop.get_best_agent("code", min_samples=1)
        assert best == "alpha"

    def test_pattern_success_rate(self, loop):
        loop.record_outcome(_make_outcome(task_id="t1", task_type="code", success=True))
        loop.record_outcome(_make_outcome(task_id="t2", task_type="code", success=False))
        loop.record_outcome(_make_outcome(task_id="t3", task_type="code", success=True))
        # 66.7% success rate
        best = loop.get_best_agent("code", min_samples=3)
        assert best == "alpha"

    def test_best_agent_min_samples(self, loop):
        loop.record_outcome(_make_outcome(task_id="t1", task_type="code"))
        # Only 1 sample, min_samples=3 should return None
        best = loop.get_best_agent("code", min_samples=3)
        assert best is None

    def test_best_agent_multiple_agents(self, loop):
        # Alpha: 50% success on code
        loop.record_outcome(_make_outcome(task_id="t1", agent_id="alpha",
                                          task_type="code", success=True))
        loop.record_outcome(_make_outcome(task_id="t2", agent_id="alpha",
                                          task_type="code", success=False))
        # Beta: 100% success on code
        loop.record_outcome(_make_outcome(task_id="t3", agent_id="beta",
                                          task_type="code", success=True))
        loop.record_outcome(_make_outcome(task_id="t4", agent_id="beta",
                                          task_type="code", success=True))
        best = loop.get_best_agent("code", min_samples=2)
        assert best == "beta"


# ── get_agent_report ──


class TestAgentReport:
    def test_empty_agent(self, loop):
        report = loop.get_agent_report("unknown")
        assert report["total"] == 0
        assert report["success_pct"] == 0
        assert report["avg_duration_ms"] == 0
        assert report["recent_errors"] == []

    def test_report_with_errors(self, loop):
        loop.record_outcome(_make_outcome(task_id="t1", success=False,
                                          error="Something broke"))
        report = loop.get_agent_report("alpha")
        assert len(report["recent_errors"]) == 1
        assert report["recent_errors"][0]["error"] == "Something broke"

    def test_report_duration(self, loop):
        loop.record_outcome(_make_outcome(task_id="t1", duration_ms=100))
        loop.record_outcome(_make_outcome(task_id="t2", duration_ms=300))
        report = loop.get_agent_report("alpha")
        assert report["avg_duration_ms"] > 0

    def test_report_errors_limit_5(self, loop):
        for i in range(10):
            loop.record_outcome(_make_outcome(
                task_id=f"t{i}", success=False, error=f"Error {i}"))
        report = loop.get_agent_report("alpha")
        assert len(report["recent_errors"]) == 5


# ── get_system_report ──


class TestSystemReport:
    def test_empty_system(self, loop):
        report = loop.get_system_report()
        assert report["total_tasks"] == 0
        assert report["success_rate"] == 0
        assert report["busiest_agent"] is None

    def test_system_with_data(self, loop):
        loop.record_outcome(_make_outcome(task_id="t1", agent_id="alpha", success=True))
        loop.record_outcome(_make_outcome(task_id="t2", agent_id="alpha", success=True))
        loop.record_outcome(_make_outcome(task_id="t3", agent_id="beta", success=False,
                                          error="fail"))
        report = loop.get_system_report()
        assert report["total_tasks"] == 3
        assert report["success_rate"] == pytest.approx(66.7, abs=0.1)
        assert report["busiest_agent"] == "alpha"

    def test_system_error_prone_agent(self, loop):
        # alpha: 2 success
        loop.record_outcome(_make_outcome(task_id="t1", agent_id="alpha", success=True))
        loop.record_outcome(_make_outcome(task_id="t2", agent_id="alpha", success=True))
        # beta: 2 failures
        loop.record_outcome(_make_outcome(task_id="t3", agent_id="beta", success=False,
                                          error="e1"))
        loop.record_outcome(_make_outcome(task_id="t4", agent_id="beta", success=False,
                                          error="e2"))
        report = loop.get_system_report()
        assert report["most_error_prone_agent"] == "beta"

    def test_system_recent_failures_limit(self, loop):
        for i in range(15):
            loop.record_outcome(_make_outcome(
                task_id=f"t{i}", success=False, error=f"err{i}"))
        report = loop.get_system_report(recent_failures=5)
        assert len(report["recent_failures"]) == 5


# ── Improvement suggestions ──


class TestSuggestImprovements:
    def test_no_suggestions_empty(self, loop):
        suggestions = loop.suggest_improvements()
        assert suggestions == []

    def test_reassignment_suggestion(self, loop):
        # alpha fails a lot on "deploy" type
        for i in range(3):
            loop.record_outcome(_make_outcome(
                task_id=f"deploy_fail_{i}", agent_id="alpha",
                task_type="deploy", success=False, error="fail"))
        loop.record_outcome(_make_outcome(
            task_id="deploy_ok", agent_id="alpha",
            task_type="deploy", success=True))
        suggestions = loop.suggest_improvements()
        deploy_suggestions = [s for s in suggestions if "deploy" in s]
        assert len(deploy_suggestions) >= 1
        assert "failure rate" in deploy_suggestions[0]

    def test_reassignment_with_better_agent(self, loop):
        # alpha: poor at "deploy"
        for i in range(3):
            loop.record_outcome(_make_outcome(
                task_id=f"a_deploy_{i}", agent_id="alpha",
                task_type="deploy", success=False, error="fail"))
        # beta: good at "deploy"
        for i in range(3):
            loop.record_outcome(_make_outcome(
                task_id=f"b_deploy_{i}", agent_id="beta",
                task_type="deploy", success=True))
        suggestions = loop.suggest_improvements()
        reassign = [s for s in suggestions if "reassigning" in s.lower()]
        assert len(reassign) >= 1
        assert "beta" in reassign[0].lower() or "Beta" in reassign[0]

    def test_slow_type_suggestion(self, loop):
        # Record slow tasks
        for i in range(3):
            loop.record_outcome(_make_outcome(
                task_id=f"slow_{i}", task_type="heavy_compute",
                duration_ms=60000, success=True))
        suggestions = loop.suggest_improvements()
        slow_suggestions = [s for s in suggestions if "heavy_compute" in s]
        assert len(slow_suggestions) >= 1
        assert "bottleneck" in slow_suggestions[0].lower()

    def test_overall_rate_warning(self, loop):
        # Create mostly failing outcomes
        for i in range(5):
            loop.record_outcome(_make_outcome(
                task_id=f"fail_{i}", success=False, error="fail"))
        loop.record_outcome(_make_outcome(task_id="ok", success=True))
        suggestions = loop.suggest_improvements()
        rate_warnings = [s for s in suggestions if "success rate" in s.lower()]
        assert len(rate_warnings) >= 1

    def test_no_warning_above_70(self, loop):
        # 4 success, 1 failure = 80%
        for i in range(4):
            loop.record_outcome(_make_outcome(
                task_id=f"ok_{i}", success=True))
        loop.record_outcome(_make_outcome(
            task_id="fail", success=False, error="one fail"))
        suggestions = loop.suggest_improvements()
        rate_warnings = [s for s in suggestions if "success rate" in s.lower()
                        and "review" in s.lower()]
        assert len(rate_warnings) == 0

    def test_idle_rebalancing_suggestion(self, loop):
        # Record an agent with old last_updated timestamp
        import sqlite3
        conn = sqlite3.connect(loop._db_path)
        old_time = time.time() - 10000  # ~2.8 hours ago
        conn.execute(
            """INSERT OR REPLACE INTO agent_stats
               (agent_id, total_tasks, success_count, fail_count,
                avg_duration_ms, last_updated)
               VALUES (?, 5, 5, 0, 100, ?)""",
            ("stale_agent", old_time))
        conn.commit()
        conn.close()

        suggestions = loop.suggest_improvements()
        idle_suggestions = [s for s in suggestions if "stale_agent" in s]
        assert len(idle_suggestions) >= 1
        assert "idle" in idle_suggestions[0].lower()


# ── Dashboard JSON ──


class TestDashboardJSON:
    def test_write_dashboard_json(self, loop, tmp_path):
        # Override the feedback JSON path
        import core.feedback_loop as fl
        original_json = fl.FEEDBACK_JSON
        test_json = tmp_path / "feedback.json"
        fl.FEEDBACK_JSON = test_json
        try:
            loop.record_outcome(_make_outcome(task_id="t1"))
            assert test_json.exists()
            data = json.loads(test_json.read_text())
            assert isinstance(data, list)
            assert len(data) == 1
            assert data[0]["task_id"] == "t1"
        finally:
            fl.FEEDBACK_JSON = original_json

    def test_dashboard_json_appends(self, loop, tmp_path):
        import core.feedback_loop as fl
        original_json = fl.FEEDBACK_JSON
        test_json = tmp_path / "feedback.json"
        fl.FEEDBACK_JSON = test_json
        try:
            loop.record_outcome(_make_outcome(task_id="t1"))
            loop.record_outcome(_make_outcome(task_id="t2"))
            data = json.loads(test_json.read_text())
            assert len(data) == 2
        finally:
            fl.FEEDBACK_JSON = original_json

    def test_dashboard_json_limit_100(self, loop, tmp_path):
        import core.feedback_loop as fl
        original_json = fl.FEEDBACK_JSON
        test_json = tmp_path / "feedback.json"
        fl.FEEDBACK_JSON = test_json
        try:
            for i in range(105):
                loop.record_outcome(_make_outcome(task_id=f"t{i}"))
            data = json.loads(test_json.read_text())
            assert len(data) <= 101  # 99 kept + 1 new per write
        finally:
            fl.FEEDBACK_JSON = original_json


# ── Edge cases ──


class TestEdgeCases:
    def test_zero_duration(self, loop):
        loop.record_outcome(_make_outcome(task_id="t1", duration_ms=0.0))
        report = loop.get_agent_report("alpha")
        assert report["avg_duration_ms"] == 0.0

    def test_very_long_error_string(self, loop):
        long_error = "E" * 10000
        loop.record_outcome(_make_outcome(
            task_id="t1", success=False, error=long_error))
        report = loop.get_agent_report("alpha")
        assert len(report["recent_errors"]) == 1

    def test_special_chars_in_task_type(self, loop):
        loop.record_outcome(_make_outcome(
            task_id="t1", task_type="code/review+test"))
        best = loop.get_best_agent("code/review+test", min_samples=1)
        assert best == "alpha"

    def test_concurrent_db_access(self, tmp_db):
        """Verify lock prevents corruption with sequential writes."""
        loop = FeedbackLoop(db_path=tmp_db)
        for i in range(50):
            loop.record_outcome(_make_outcome(
                task_id=f"t{i}", agent_id=f"agent_{i % 4}"))
        report = loop.get_system_report()
        assert report["total_tasks"] == 50
