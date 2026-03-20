# signed: gamma
"""Comprehensive tests for tools/skynet_supervisor.py — Skynet Supervision Trees."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.skynet_supervisor import (
    SupervisorTree,
    ChildSpec,
    ChildState,
    ChildStatus,
    RestartStrategy,
    RestartType,
    MaxRestarts,
    _format_uptime,
    _read_pid_file,
    _pid_alive,
    DEFAULT_CHECK_INTERVAL_S,
    DEFAULT_SHUTDOWN_TIMEOUT_S,
    DEFAULT_MAX_RESTARTS,
    DEFAULT_MAX_RESTART_WINDOW_S,
    STARTUP_GRACE_S,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _make_spec(name="test_child", **kwargs):
    """Create a ChildSpec with defaults suitable for testing."""
    defaults = {
        "module_path": "tools/fake_daemon.py",
        "restart_type": RestartType.PERMANENT,
        "max_restarts": MaxRestarts(count=3, window_s=60),
    }
    defaults.update(kwargs)
    return ChildSpec(name=name, **defaults)


def _make_tree(strategy=RestartStrategy.ONE_FOR_ONE, specs=None):
    """Create a SupervisorTree with given specs."""
    if specs is None:
        specs = [_make_spec("child_a"), _make_spec("child_b"), _make_spec("child_c")]
    return SupervisorTree(name="test_tree", strategy=strategy, children=specs)


# ── Enum and Data Structure Tests ────────────────────────────────────

class TestEnums:
    def test_restart_strategy_values(self):
        assert RestartStrategy.ONE_FOR_ONE.value == "one_for_one"
        assert RestartStrategy.ONE_FOR_ALL.value == "one_for_all"
        assert RestartStrategy.REST_FOR_ONE.value == "rest_for_one"

    def test_restart_type_values(self):
        assert RestartType.PERMANENT.value == "permanent"
        assert RestartType.TEMPORARY.value == "temporary"
        assert RestartType.TRANSIENT.value == "transient"

    def test_child_status_values(self):
        assert ChildStatus.RUNNING.value == "running"
        assert ChildStatus.STOPPED.value == "stopped"
        assert ChildStatus.FAILED.value == "failed"
        assert ChildStatus.CIRCUIT_OPEN.value == "circuit_open"


class TestMaxRestarts:
    def test_default_values(self):
        mr = MaxRestarts()
        assert mr.count == DEFAULT_MAX_RESTARTS
        assert mr.window_s == DEFAULT_MAX_RESTART_WINDOW_S

    def test_custom_values(self):
        mr = MaxRestarts(count=10, window_s=120)
        assert mr.count == 10
        assert mr.window_s == 120


# ── ChildSpec Tests ──────────────────────────────────────────────────

class TestChildSpec:
    def test_default_fields(self):
        spec = ChildSpec(name="monitor")
        assert spec.name == "monitor"
        assert spec.restart_type == RestartType.PERMANENT
        assert spec.shutdown_timeout == DEFAULT_SHUTDOWN_TIMEOUT_S
        assert spec.module_path == ""
        assert spec.args == []
        assert spec.depends_on == []
        assert spec.criticality == "MODERATE"
        assert spec.is_binary is False

    def test_to_dict(self):
        spec = _make_spec("svc", criticality="HIGH", port=8420)
        d = spec.to_dict()
        assert d["name"] == "svc"
        assert d["criticality"] == "HIGH"
        assert d["port"] == 8420
        assert d["restart_type"] == "permanent"
        assert "count" in d["max_restarts"]
        assert "window_s" in d["max_restarts"]

    def test_binary_spec(self):
        spec = ChildSpec(name="backend", is_binary=True,
                         binary_path="Skynet/skynet.exe", cwd="Skynet")
        assert spec.is_binary is True
        assert spec.binary_path == "Skynet/skynet.exe"

    def test_depends_on(self):
        spec = ChildSpec(name="relay", depends_on=["backend", "console"])
        assert spec.depends_on == ["backend", "console"]


# ── ChildState Tests ─────────────────────────────────────────────────

class TestChildState:
    def test_initial_state(self):
        spec = _make_spec("svc")
        state = ChildState(spec=spec)
        assert state.status == ChildStatus.STOPPED
        assert state.pid == 0
        assert state.total_restarts == 0
        assert state.uptime_s == 0.0

    def test_uptime_when_running(self):
        spec = _make_spec("svc")
        state = ChildState(spec=spec, status=ChildStatus.RUNNING,
                           start_time=time.time() - 60)
        assert 59 <= state.uptime_s <= 62

    def test_uptime_zero_when_stopped(self):
        spec = _make_spec("svc")
        state = ChildState(spec=spec, status=ChildStatus.STOPPED,
                           start_time=time.time() - 100)
        assert state.uptime_s == 0.0

    def test_recent_restart_count(self):
        spec = _make_spec("svc", max_restarts=MaxRestarts(count=5, window_s=60))
        state = ChildState(spec=spec)
        now = time.time()
        state.restart_timestamps = [now - 10, now - 20, now - 30, now - 100]
        # 3 within window (last 60s), 1 outside
        assert state.recent_restart_count() == 3

    def test_circuit_tripped_true(self):
        spec = _make_spec("svc", max_restarts=MaxRestarts(count=3, window_s=60))
        state = ChildState(spec=spec)
        now = time.time()
        state.restart_timestamps = [now - 5, now - 10, now - 15]
        assert state.circuit_tripped() is True

    def test_circuit_not_tripped(self):
        spec = _make_spec("svc", max_restarts=MaxRestarts(count=5, window_s=60))
        state = ChildState(spec=spec)
        now = time.time()
        state.restart_timestamps = [now - 10, now - 20]
        assert state.circuit_tripped() is False


# ── SupervisorTree Construction ──────────────────────────────────────

class TestSupervisorTreeConstruction:
    def test_empty_tree(self):
        tree = SupervisorTree(name="empty")
        assert tree.name == "empty"
        assert tree.children == []
        assert tree.strategy == RestartStrategy.ONE_FOR_ONE

    def test_add_child(self):
        tree = SupervisorTree(name="t")
        spec = _make_spec("daemon_a")
        tree.add_child(spec)
        assert len(tree.children) == 1
        assert tree.children[0].spec.name == "daemon_a"

    def test_get_child(self):
        tree = _make_tree()
        child = tree.get_child("child_a")
        assert child is not None
        assert child.spec.name == "child_a"

    def test_get_child_missing(self):
        tree = _make_tree()
        assert tree.get_child("nonexistent") is None

    def test_children_in_order(self):
        specs = [_make_spec("z_last"), _make_spec("a_first"), _make_spec("m_mid")]
        tree = _make_tree(specs=specs)
        names = [c.spec.name for c in tree.children]
        assert names == ["z_last", "a_first", "m_mid"]

    def test_duplicate_add_child_no_duplicate_order(self):
        tree = SupervisorTree(name="t")
        spec = _make_spec("dup")
        tree.add_child(spec)
        tree.add_child(spec)  # re-add same name
        assert len(tree._child_order) == 1

    def test_strategy_assignment(self):
        tree = SupervisorTree(name="t",
                              strategy=RestartStrategy.ONE_FOR_ALL)
        assert tree.strategy == RestartStrategy.ONE_FOR_ALL

    def test_parent_assignment(self):
        parent = SupervisorTree(name="parent")
        child_tree = SupervisorTree(name="child", parent=parent)
        assert child_tree.parent is parent


# ── Health Checking ──────────────────────────────────────────────────

class TestHealthChecking:
    def test_check_unknown_child(self):
        tree = _make_tree()
        assert tree.check_child_health("nonexistent") is False

    def test_check_circuit_open_returns_false(self):
        tree = _make_tree()
        child = tree.get_child("child_a")
        child.status = ChildStatus.CIRCUIT_OPEN
        assert tree.check_child_health("child_a") is False

    @patch("tools.skynet_supervisor._pid_alive", return_value=True)
    def test_healthy_child_with_live_pid(self, mock_alive):
        tree = _make_tree()
        child = tree.get_child("child_a")
        child.pid = 12345
        child.status = ChildStatus.RUNNING

        assert tree.check_child_health("child_a") is True
        assert child.last_healthy > 0
        assert child.status == ChildStatus.RUNNING

    @patch("tools.skynet_supervisor._pid_alive", return_value=False)
    @patch("tools.skynet_supervisor._read_pid_file", return_value=0)
    def test_dead_pid_marks_failed(self, mock_read, mock_alive):
        tree = _make_tree()
        child = tree.get_child("child_a")
        child.pid = 99999
        child.status = ChildStatus.RUNNING

        assert tree.check_child_health("child_a") is False
        assert child.status == ChildStatus.FAILED
        assert "not alive" in child.failure_reason

    @patch("tools.skynet_supervisor._pid_alive")
    @patch("tools.skynet_supervisor._read_pid_file", return_value=77777)
    def test_pid_file_refresh_on_self_restart(self, mock_read, mock_alive):
        """When daemon restarts itself with a new PID, supervisor detects it."""
        mock_alive.side_effect = lambda pid: pid == 77777  # only new PID alive
        spec = _make_spec("refresher", pid_file="data/refresher.pid")
        tree = SupervisorTree(name="t", children=[spec])
        child = tree.get_child("refresher")
        child.pid = 11111  # old PID
        child.status = ChildStatus.RUNNING

        assert tree.check_child_health("refresher") is True
        assert child.pid == 77777

    @patch("tools.skynet_supervisor._pid_alive", return_value=True)
    @patch("tools.skynet_supervisor._check_health_url", return_value=False)
    def test_health_url_failure(self, mock_url, mock_alive):
        spec = _make_spec("http_svc", health_url="http://localhost:9999/health")
        tree = SupervisorTree(name="t", children=[spec])
        child = tree.get_child("http_svc")
        child.pid = 100
        child.status = ChildStatus.RUNNING

        assert tree.check_child_health("http_svc") is False
        assert "health URL" in child.failure_reason

    @patch("tools.skynet_supervisor._pid_alive", return_value=True)
    @patch("tools.skynet_supervisor._check_port", return_value=False)
    def test_port_check_failure(self, mock_port, mock_alive):
        spec = _make_spec("port_svc", port=8420)
        tree = SupervisorTree(name="t", children=[spec])
        child = tree.get_child("port_svc")
        child.pid = 200
        child.status = ChildStatus.RUNNING

        assert tree.check_child_health("port_svc") is False
        assert "port 8420" in child.failure_reason

    def test_check_all_health(self):
        tree = _make_tree()
        # All children have pid=0, no health_url, no port — PID check skipped,
        # falls through to True (no failing checks)
        results = tree.check_all_health()
        assert len(results) == 3
        assert all(v is True for v in results.values())


# ── Restart Logic ────────────────────────────────────────────────────

class TestRestartLogic:
    @patch("tools.skynet_supervisor.SupervisorTree.start_child", return_value=True)
    @patch("tools.skynet_supervisor.SupervisorTree.stop_child", return_value=True)
    def test_restart_child_increments_counters(self, mock_stop, mock_start):
        tree = _make_tree()
        child = tree.get_child("child_a")
        child.status = ChildStatus.FAILED

        result = tree._restart_child("child_a")
        assert result is True
        assert child.total_restarts == 1
        assert len(child.restart_timestamps) == 1

    @patch("tools.skynet_supervisor.SupervisorTree.start_child", return_value=True)
    @patch("tools.skynet_supervisor.SupervisorTree.stop_child", return_value=True)
    def test_circuit_breaker_trips_after_max_restarts(self, mock_stop, mock_start):
        spec = _make_spec("breaker", max_restarts=MaxRestarts(count=2, window_s=60))
        tree = SupervisorTree(name="t", children=[spec])
        child = tree.get_child("breaker")

        # Simulate 2 recent restarts already
        now = time.time()
        child.restart_timestamps = [now - 5, now - 10]
        child.total_restarts = 2

        with patch.object(tree, "_escalate") as mock_esc:
            result = tree._restart_child("breaker")
            assert result is False
            assert child.status == ChildStatus.CIRCUIT_OPEN
            mock_esc.assert_called_once_with("breaker")

    def test_temporary_child_not_restarted(self):
        spec = _make_spec("temp", restart_type=RestartType.TEMPORARY)
        tree = SupervisorTree(name="t", children=[spec])
        child = tree.get_child("temp")

        result = tree._restart_child("temp")
        assert result is False
        assert child.status == ChildStatus.STOPPED

    def test_transient_normal_exit_not_restarted(self):
        spec = _make_spec("trans", restart_type=RestartType.TRANSIENT)
        tree = SupervisorTree(name="t", children=[spec])
        child = tree.get_child("trans")
        child.failure_reason = "normal_exit"

        result = tree._restart_child("trans")
        assert result is False
        assert child.status == ChildStatus.STOPPED

    @patch("tools.skynet_supervisor.SupervisorTree.start_child", return_value=True)
    @patch("tools.skynet_supervisor.SupervisorTree.stop_child", return_value=True)
    def test_transient_abnormal_exit_is_restarted(self, mock_stop, mock_start):
        spec = _make_spec("trans", restart_type=RestartType.TRANSIENT)
        tree = SupervisorTree(name="t", children=[spec])
        child = tree.get_child("trans")
        child.failure_reason = "segfault"

        result = tree._restart_child("trans")
        assert result is True

    def test_restart_nonexistent_child(self):
        tree = _make_tree()
        assert tree._restart_child("ghost") is False


# ── Failure Handling Strategies ──────────────────────────────────────

class TestFailureHandling:
    @patch("tools.skynet_supervisor.SupervisorTree._restart_child", return_value=True)
    def test_one_for_one_restarts_only_failed(self, mock_restart):
        tree = _make_tree(strategy=RestartStrategy.ONE_FOR_ONE)
        results = tree.handle_failure("child_b")
        assert "child_b" in results
        assert results["child_b"] is True
        mock_restart.assert_called_once_with("child_b")

    @patch("tools.skynet_supervisor.SupervisorTree._restart_child", return_value=True)
    @patch("tools.skynet_supervisor.SupervisorTree.stop_child", return_value=True)
    def test_one_for_all_restarts_everyone(self, mock_stop, mock_restart):
        tree = _make_tree(strategy=RestartStrategy.ONE_FOR_ALL)
        results = tree.handle_failure("child_b")
        assert len(results) == 3
        # stop_child called for all in reverse, restart for all in order
        assert mock_stop.call_count == 3
        assert mock_restart.call_count == 3

    @patch("tools.skynet_supervisor.SupervisorTree._restart_child", return_value=True)
    @patch("tools.skynet_supervisor.SupervisorTree.stop_child", return_value=True)
    def test_rest_for_one_restarts_failed_and_after(self, mock_stop, mock_restart):
        tree = _make_tree(strategy=RestartStrategy.REST_FOR_ONE)
        # child_b is at index 1, so child_b + child_c should be affected
        results = tree.handle_failure("child_b")
        assert "child_b" in results
        assert "child_c" in results
        assert "child_a" not in results
        assert mock_stop.call_count == 2  # child_c then child_b
        assert mock_restart.call_count == 2  # child_b then child_c

    def test_handle_failure_unknown_child_rest_for_one(self):
        tree = _make_tree(strategy=RestartStrategy.REST_FOR_ONE)
        results = tree.handle_failure("nonexistent")
        assert results == {}


# ── Escalation ───────────────────────────────────────────────────────

class TestEscalation:
    @patch("tools.skynet_spam_guard.guarded_publish")
    def test_escalate_posts_to_bus(self, mock_publish):
        tree = _make_tree()
        child = tree.get_child("child_a")
        child.restart_timestamps = [time.time()] * 5

        tree._escalate("child_a")
        mock_publish.assert_called_once()
        msg = mock_publish.call_args[0][0]
        assert msg["sender"] == "supervisor"
        assert msg["topic"] == "orchestrator"
        assert "ESCALATION" in msg["content"]

    def test_escalate_to_parent(self):
        parent = SupervisorTree(name="parent")
        child_tree = _make_tree()
        child_tree.parent = parent

        with patch.object(parent, "_handle_child_escalation") as mock_handle:
            child_tree._escalate("child_a")
            mock_handle.assert_called_once_with("test_tree", "child_a")

    def test_escalate_survives_bus_failure(self):
        tree = _make_tree()
        child = tree.get_child("child_a")
        child.restart_timestamps = [time.time()]

        with patch("tools.skynet_spam_guard.guarded_publish",
                   side_effect=Exception("bus down")):
            tree._escalate("child_a")  # Should not raise


# ── Monitor ──────────────────────────────────────────────────────────

class TestMonitor:
    def test_monitor_once_skips_stopped(self):
        tree = _make_tree()
        # All are STOPPED by default — should skip them
        failed = tree.monitor_once()
        assert failed == []

    def test_monitor_once_skips_circuit_open(self):
        tree = _make_tree()
        child = tree.get_child("child_a")
        child.status = ChildStatus.CIRCUIT_OPEN
        failed = tree.monitor_once()
        assert "child_a" not in failed

    @patch("tools.skynet_supervisor._pid_alive", return_value=False)
    @patch("tools.skynet_supervisor._read_pid_file", return_value=0)
    def test_monitor_once_detects_dead_child(self, mock_read, mock_alive):
        tree = _make_tree()
        child = tree.get_child("child_a")
        child.status = ChildStatus.RUNNING
        child.pid = 99999

        failed = tree.monitor_once()
        assert "child_a" in failed

    @patch("tools.skynet_supervisor.time.sleep")
    def test_monitor_runs_limited_iterations(self, mock_sleep):
        tree = _make_tree()

        with patch.object(tree, "monitor_once", return_value=[]) as mock_once, \
             patch.object(tree, "_save_state"):
            tree.monitor(interval_s=1, max_iterations=3)
            assert mock_once.call_count == 3
            assert mock_sleep.call_count == 2  # not called after last iteration

    @patch("tools.skynet_supervisor.time.sleep")
    def test_monitor_handles_failures(self, mock_sleep):
        tree = _make_tree()

        with patch.object(tree, "monitor_once", return_value=["child_a"]) as mock_once, \
             patch.object(tree, "handle_failure", return_value={"child_a": True}) as mock_fail, \
             patch.object(tree, "_save_state"):
            tree.monitor(interval_s=1, max_iterations=1)
            mock_fail.assert_called_once_with("child_a")


# ── Start / Stop ─────────────────────────────────────────────────────

class TestStartStop:
    @patch("tools.skynet_supervisor._pid_alive", return_value=True)
    @patch("tools.skynet_supervisor._read_pid_file", return_value=12345)
    def test_start_child_already_running_via_pid(self, mock_read, mock_alive):
        spec = _make_spec("running", pid_file="data/running.pid")
        tree = SupervisorTree(name="t", children=[spec])

        assert tree.start_child("running") is True
        child = tree.get_child("running")
        assert child.pid == 12345
        assert child.status == ChildStatus.RUNNING

    @patch("tools.skynet_supervisor._check_port", return_value=True)
    def test_start_child_already_running_via_port(self, mock_port):
        spec = _make_spec("portsvc", port=8420)
        tree = SupervisorTree(name="t", children=[spec])

        assert tree.start_child("portsvc") is True
        child = tree.get_child("portsvc")
        assert child.status == ChildStatus.RUNNING

    def test_start_nonexistent_child(self):
        tree = _make_tree()
        assert tree.start_child("ghost") is False

    def test_stop_nonexistent_child(self):
        tree = _make_tree()
        # stop_child for unknown returns... let's check:
        # It does: child = self._children.get(name); if not child: ... but line 402 has "or child.status == STOPPED"
        # So for None child, it returns True (falsy check)
        result = tree.stop_child("ghost")
        assert result is True  # treats missing as already stopped

    def test_stop_already_stopped(self):
        tree = _make_tree()
        child = tree.get_child("child_a")
        child.status = ChildStatus.STOPPED
        assert tree.stop_child("child_a") is True

    @patch("tools.skynet_supervisor._pid_alive", return_value=False)
    def test_stop_dead_process(self, mock_alive):
        tree = _make_tree()
        child = tree.get_child("child_a")
        child.status = ChildStatus.RUNNING
        child.pid = 99999

        assert tree.stop_child("child_a") is True
        assert child.status == ChildStatus.STOPPED
        assert child.pid == 0

    def test_start_all_respects_dependencies(self):
        spec_a = _make_spec("backend")
        spec_b = _make_spec("console", depends_on=["backend"])
        tree = SupervisorTree(name="t", children=[spec_a, spec_b])

        with patch.object(tree, "start_child") as mock_start:
            mock_start.return_value = False  # backend fails to start
            results = tree.start_all()
            # console should fail because backend didn't start
            assert results.get("console") is False

    @patch("tools.skynet_supervisor.SupervisorTree.start_child", return_value=True)
    def test_start_all_order(self, mock_start):
        specs = [_make_spec("first"), _make_spec("second"), _make_spec("third")]
        tree = _make_tree(specs=specs)
        results = tree.start_all()
        assert list(results.keys()) == ["first", "second", "third"]
        assert all(v is True for v in results.values())

    @patch("tools.skynet_supervisor.SupervisorTree.stop_child", return_value=True)
    def test_stop_all_reverse_order(self, mock_stop):
        specs = [_make_spec("first"), _make_spec("second"), _make_spec("third")]
        tree = _make_tree(specs=specs)
        results = tree.stop_all()
        # Stop order should be reversed
        calls = [c.args[0] for c in mock_stop.call_args_list]
        assert calls == ["third", "second", "first"]


# ── State Persistence ────────────────────────────────────────────────

class TestStatePersistence:
    def test_save_state(self, tmp_path, monkeypatch):
        import tools.skynet_supervisor as sup
        monkeypatch.setattr(sup, "SUPERVISOR_STATE_PATH", tmp_path / "state.json")

        tree = _make_tree()
        child = tree.get_child("child_a")
        child.status = ChildStatus.RUNNING
        child.pid = 1234
        child.total_restarts = 2

        tree._save_state()

        state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert state["supervisor"] == "test_tree"
        assert state["strategy"] == "one_for_one"
        assert "child_a" in state["children"]
        assert state["children"]["child_a"]["pid"] == 1234
        assert state["children"]["child_a"]["total_restarts"] == 2
        assert state["children"]["child_a"]["status"] == "running"

    def test_save_state_survives_io_error(self, monkeypatch):
        import tools.skynet_supervisor as sup
        monkeypatch.setattr(sup, "SUPERVISOR_STATE_PATH",
                            Path("Z:\\nonexistent\\state.json"))
        tree = _make_tree()
        tree._save_state()  # Should not raise


# ── Display Methods ──────────────────────────────────────────────────

class TestDisplay:
    def test_status_table_structure(self):
        tree = _make_tree()
        child = tree.get_child("child_a")
        child.status = ChildStatus.RUNNING
        child.pid = 5555
        child.start_time = time.time() - 120

        table = tree.status_table()
        assert "test_tree" in table
        assert "child_a" in table
        assert "RUNNING" in table
        assert "5555" in table

    def test_tree_view_structure(self):
        tree = _make_tree()
        child = tree.get_child("child_b")
        child.status = ChildStatus.RUNNING
        child.pid = 9999

        view = tree.tree_view()
        assert "supervisor:test_tree" in view
        assert "[--]" in view  # STOPPED children
        assert "[OK]" in view  # RUNNING child_b
        assert "child_a" in view
        assert "child_b" in view
        assert "child_c" in view

    def test_tree_view_with_indent(self):
        tree = _make_tree()
        view = tree.tree_view(indent=2)
        assert view.startswith("    ")  # 4 spaces for indent=2

    def test_status_table_shows_all_statuses(self):
        specs = [
            _make_spec("running_svc"),
            _make_spec("stopped_svc"),
            _make_spec("failed_svc"),
            _make_spec("circuit_svc"),
        ]
        tree = _make_tree(specs=specs)
        tree.get_child("running_svc").status = ChildStatus.RUNNING
        tree.get_child("stopped_svc").status = ChildStatus.STOPPED
        tree.get_child("failed_svc").status = ChildStatus.FAILED
        tree.get_child("circuit_svc").status = ChildStatus.CIRCUIT_OPEN

        table = tree.status_table()
        assert "RUNNING" in table
        assert "STOPPED" in table
        assert "FAILED" in table
        assert "CIRCUIT_OPEN" in table


# ── Utility Functions ────────────────────────────────────────────────

class TestUtilities:
    def test_format_uptime_seconds(self):
        assert _format_uptime(45) == "45s"

    def test_format_uptime_minutes(self):
        assert _format_uptime(125) == "2m5s"

    def test_format_uptime_hours(self):
        assert _format_uptime(3661) == "1h1m"

    def test_read_pid_file_valid(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345")
        with patch("tools.skynet_supervisor._REPO", tmp_path):
            result = _read_pid_file(str(pid_file))
            assert result == 12345

    def test_read_pid_file_missing(self):
        result = _read_pid_file("nonexistent_path/fake.pid")
        assert result == 0

    def test_read_pid_file_invalid_content(self, tmp_path):
        pid_file = tmp_path / "bad.pid"
        pid_file.write_text("not_a_number")
        result = _read_pid_file(str(pid_file))
        assert result == 0

    def test_pid_alive_zero_or_negative(self):
        assert _pid_alive(0) is False
        assert _pid_alive(-1) is False


# ── Constants ────────────────────────────────────────────────────────

class TestConstants:
    def test_default_check_interval(self):
        assert DEFAULT_CHECK_INTERVAL_S == 10

    def test_default_shutdown_timeout(self):
        assert DEFAULT_SHUTDOWN_TIMEOUT_S == 10

    def test_default_max_restarts(self):
        assert DEFAULT_MAX_RESTARTS == 5

    def test_default_restart_window(self):
        assert DEFAULT_MAX_RESTART_WINDOW_S == 300

    def test_startup_grace(self):
        assert STARTUP_GRACE_S == 3
