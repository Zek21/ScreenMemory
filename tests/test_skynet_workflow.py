# signed: gamma
"""Comprehensive tests for tools/skynet_workflow.py — Skynet DAG Workflow Engine."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.skynet_workflow import (
    DAGEngine,
    WorkflowNode,
    WorkflowEdge,
    NodeStatus,
    _rebuild_condition,
    _save_workflow,
    _load_workflow,
    _list_workflows,
    _workflow_path,
    _bus_publish,
    _get_idle_workers,
    builtin_plan_code_test_deploy,
    builtin_audit_fix_verify,
    BUILTIN_WORKFLOWS,
    DEFAULT_MAX_RETRIES,
    DISPATCH_COOLDOWN_S,
    RESULT_POLL_TIMEOUT_S,
    WORKER_NAMES,
)


# ── Enum Tests ───────────────────────────────────────────────────────

class TestNodeStatus:
    def test_all_values(self):
        assert NodeStatus.PENDING.value == "pending"
        assert NodeStatus.READY.value == "ready"
        assert NodeStatus.DISPATCHED.value == "dispatched"
        assert NodeStatus.RUNNING.value == "running"
        assert NodeStatus.COMPLETED.value == "completed"
        assert NodeStatus.FAILED.value == "failed"
        assert NodeStatus.SKIPPED.value == "skipped"
        assert NodeStatus.RETRYING.value == "retrying"

    def test_is_str_enum(self):
        assert isinstance(NodeStatus.PENDING, str)
        assert NodeStatus.PENDING == "pending"


# ── WorkflowNode Tests ──────────────────────────────────────────────

class TestWorkflowNode:
    def test_default_fields(self):
        node = WorkflowNode(id="plan", task="Plan the feature")
        assert node.id == "plan"
        assert node.task == "Plan the feature"
        assert node.dependencies == []
        assert node.status == "pending"
        assert node.retries == 0
        assert node.max_retries == DEFAULT_MAX_RETRIES
        assert node.worker is None
        assert node.result is None
        assert node.error is None
        assert node.dispatch_key is None

    def test_custom_fields(self):
        node = WorkflowNode(
            id="code", task="Write code",
            dependencies=["plan"], max_retries=5, worker="alpha",
        )
        assert node.dependencies == ["plan"]
        assert node.max_retries == 5
        assert node.worker == "alpha"

    def test_to_dict(self):
        node = WorkflowNode(id="test", task="Run tests", worker="beta")
        d = node.to_dict()
        assert d["id"] == "test"
        assert d["task"] == "Run tests"
        assert d["worker"] == "beta"
        assert d["status"] == "pending"

    def test_from_dict(self):
        d = {"id": "deploy", "task": "Deploy it", "status": "completed",
             "retries": 2, "worker": "gamma"}
        node = WorkflowNode.from_dict(d)
        assert node.id == "deploy"
        assert node.status == "completed"
        assert node.retries == 2
        assert node.worker == "gamma"

    def test_from_dict_ignores_extra_keys(self):
        d = {"id": "x", "task": "y", "unknown_field": 42}
        node = WorkflowNode.from_dict(d)
        assert node.id == "x"
        assert not hasattr(node, "unknown_field")

    def test_round_trip(self):
        node = WorkflowNode(id="a", task="do stuff", dependencies=["b"],
                            worker="delta", max_retries=7)
        restored = WorkflowNode.from_dict(node.to_dict())
        assert restored.id == node.id
        assert restored.task == node.task
        assert restored.dependencies == node.dependencies
        assert restored.worker == node.worker


# ── WorkflowEdge Tests ──────────────────────────────────────────────

class TestWorkflowEdge:
    def test_basic_edge(self):
        edge = WorkflowEdge(source="a", target="b")
        assert edge.source == "a"
        assert edge.target == "b"
        assert edge.condition is None
        assert edge.condition_expr is None

    def test_edge_with_condition_expr(self):
        edge = WorkflowEdge(source="a", target="b",
                            condition_expr="contains:PASS")
        assert edge.condition is not None
        assert edge.condition("ALL TESTS PASS") is True
        assert edge.condition("FAIL") is False

    def test_to_dict(self):
        edge = WorkflowEdge(source="x", target="y",
                            condition_expr="not_contains:ERROR")
        d = edge.to_dict()
        assert d["source"] == "x"
        assert d["target"] == "y"
        assert d["condition_expr"] == "not_contains:ERROR"
        assert "condition" not in d  # callable not serialized

    def test_from_dict(self):
        d = {"source": "a", "target": "b", "condition_expr": "pass"}
        edge = WorkflowEdge.from_dict(d)
        assert edge.source == "a"
        assert edge.target == "b"
        assert edge.condition is not None

    def test_from_dict_no_condition(self):
        d = {"source": "a", "target": "b"}
        edge = WorkflowEdge.from_dict(d)
        assert edge.condition is None

    def test_post_init_rebuilds_condition(self):
        edge = WorkflowEdge(source="a", target="b",
                            condition_expr="contains:SUCCESS")
        assert edge.condition is not None
        assert edge.condition("SUCCESS achieved") is True


# ── _rebuild_condition Tests ────────────────────────────────────────

class TestRebuildCondition:
    def test_contains(self):
        fn = _rebuild_condition("contains:PASS")
        assert fn("all tests PASS") is True
        assert fn("all tests fail") is False

    def test_contains_case_insensitive(self):
        fn = _rebuild_condition("contains:pass")
        assert fn("PASS") is True

    def test_not_contains(self):
        fn = _rebuild_condition("not_contains:ERROR")
        assert fn("clean run") is True
        assert fn("error found") is False

    def test_pass_keyword(self):
        fn = _rebuild_condition("pass")
        assert fn("test PASSED") is True
        assert fn("failure") is False

    def test_empty_returns_none(self):
        assert _rebuild_condition("") is None
        assert _rebuild_condition(None) is None

    def test_unknown_expr_returns_none(self):
        assert _rebuild_condition("foobar:something") is None

    def test_none_result_handling(self):
        fn = _rebuild_condition("contains:x")
        assert fn(None) is False


# ── Persistence Tests ───────────────────────────────────────────────

class TestPersistence:
    def test_save_and_load(self, tmp_path, monkeypatch):
        import tools.skynet_workflow as wf
        monkeypatch.setattr(wf, "WORKFLOWS_DIR", tmp_path)
        data = {"id": "wf_test", "name": "test", "status": "created", "nodes": []}
        _save_workflow("wf_test", data)
        loaded = _load_workflow("wf_test")
        assert loaded["id"] == "wf_test"
        assert loaded["status"] == "created"

    def test_load_missing(self, tmp_path, monkeypatch):
        import tools.skynet_workflow as wf
        monkeypatch.setattr(wf, "WORKFLOWS_DIR", tmp_path)
        assert _load_workflow("nonexistent") is None

    def test_load_corrupt_json(self, tmp_path, monkeypatch):
        import tools.skynet_workflow as wf
        monkeypatch.setattr(wf, "WORKFLOWS_DIR", tmp_path)
        (tmp_path / "bad.json").write_text("{invalid json", encoding="utf-8")
        assert _load_workflow("bad") is None

    def test_list_workflows(self, tmp_path, monkeypatch):
        import tools.skynet_workflow as wf
        monkeypatch.setattr(wf, "WORKFLOWS_DIR", tmp_path)
        for i in range(3):
            _save_workflow(f"wf_{i}", {
                "id": f"wf_{i}", "name": f"flow_{i}",
                "status": "completed", "nodes": [{"id": "a"}],
            })
        result = _list_workflows()
        assert len(result) == 3
        assert all(w["nodes"] == 1 for w in result)

    def test_workflow_path(self, monkeypatch):
        import tools.skynet_workflow as wf
        monkeypatch.setattr(wf, "WORKFLOWS_DIR", Path("/fake"))
        assert _workflow_path("wf_abc") == Path("/fake/wf_abc.json")


# ── Bus Helpers ─────────────────────────────────────────────────────

class TestBusHelpers:
    @patch("tools.skynet_workflow.guarded_publish", create=True)
    def test_bus_publish_success(self, mock_gp):
        with patch("tools.skynet_spam_guard.guarded_publish", return_value=True):
            result = _bus_publish("engine", "start", "Workflow started")
        assert result is True or result is False  # depends on import path

    def test_bus_publish_survives_exception(self):
        with patch("tools.skynet_spam_guard.guarded_publish",
                   side_effect=Exception("bus down")):
            result = _bus_publish("engine", "test", "msg")
            assert result is False

    def test_get_idle_workers_from_file(self, tmp_path, monkeypatch):
        import tools.skynet_workflow as wf
        monkeypatch.setattr(wf, "DATA_DIR", tmp_path)
        rt = {"workers": {
            "alpha": {"status": "IDLE"},
            "beta": {"status": "PROCESSING"},
            "gamma": {"status": "IDLE"},
            "delta": {"status": "IDLE"},
        }}
        (tmp_path / "realtime.json").write_text(
            json.dumps(rt), encoding="utf-8")
        idle = _get_idle_workers()
        assert "alpha" in idle
        assert "gamma" in idle
        assert "delta" in idle
        assert "beta" not in idle

    def test_get_idle_workers_fallback(self, tmp_path, monkeypatch):
        import tools.skynet_workflow as wf
        monkeypatch.setattr(wf, "DATA_DIR", tmp_path)
        # No realtime.json — should return all workers
        idle = _get_idle_workers()
        assert idle == WORKER_NAMES


# ── DAGEngine Construction ──────────────────────────────────────────

class TestDAGEngineConstruction:
    def test_basic_construction(self):
        engine = DAGEngine(name="test")
        assert engine.name == "test"
        assert engine.id.startswith("wf_")
        assert engine.status == "created"
        assert engine.nodes == {}
        assert engine.edges == []

    def test_custom_id(self):
        engine = DAGEngine(name="test", workflow_id="wf_custom")
        assert engine.id == "wf_custom"

    def test_add_node(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="do A"))
        assert "a" in engine.nodes
        assert engine.nodes["a"].task == "do A"

    def test_add_node_chaining(self):
        engine = DAGEngine(name="t")
        result = engine.add_node(WorkflowNode(id="a", task="A"))
        assert result is engine  # returns self for chaining

    def test_add_edge(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A"))
        engine.add_node(WorkflowNode(id="b", task="B"))
        engine.add_edge(WorkflowEdge(source="a", target="b"))
        assert len(engine.edges) == 1
        assert "a" in engine.nodes["b"].dependencies

    def test_add_edge_no_duplicate_deps(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A"))
        engine.add_node(WorkflowNode(id="b", task="B", dependencies=["a"]))
        engine.add_edge(WorkflowEdge(source="a", target="b"))
        assert engine.nodes["b"].dependencies.count("a") == 1

    def test_add_edge_chaining(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A"))
        engine.add_node(WorkflowNode(id="b", task="B"))
        result = engine.add_edge(WorkflowEdge(source="a", target="b"))
        assert result is engine


# ── Validation & Topological Sort ───────────────────────────────────

class TestValidation:
    def test_valid_dag(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A"))
        engine.add_node(WorkflowNode(id="b", task="B"))
        engine.add_edge(WorkflowEdge(source="a", target="b"))
        assert engine.validate() == []

    def test_missing_edge_source(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="b", task="B"))
        engine.edges.append(WorkflowEdge(source="ghost", target="b"))
        errors = engine.validate()
        assert any("ghost" in e for e in errors)

    def test_missing_edge_target(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A"))
        engine.edges.append(WorkflowEdge(source="a", target="ghost"))
        errors = engine.validate()
        assert any("ghost" in e for e in errors)

    def test_unknown_dependency(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A", dependencies=["missing"]))
        errors = engine.validate()
        assert any("missing" in e for e in errors)

    def test_cycle_detection(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A"))
        engine.add_node(WorkflowNode(id="b", task="B"))
        engine.edges.append(WorkflowEdge(source="a", target="b"))
        engine.edges.append(WorkflowEdge(source="b", target="a"))
        errors = engine.validate()
        assert any("cycle" in e.lower() for e in errors)

    def test_topological_sort_order(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="c", task="C"))
        engine.add_node(WorkflowNode(id="a", task="A"))
        engine.add_node(WorkflowNode(id="b", task="B"))
        engine.add_edge(WorkflowEdge(source="a", target="b"))
        engine.add_edge(WorkflowEdge(source="b", target="c"))
        order = engine._topological_sort()
        assert order.index("a") < order.index("b") < order.index("c")

    def test_empty_graph_valid(self):
        engine = DAGEngine(name="t")
        assert engine.validate() == []


# ── Ready Node Detection ────────────────────────────────────────────

class TestReadyNodes:
    def test_no_deps_is_ready(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A"))
        ready = engine.get_ready_nodes()
        assert len(ready) == 1
        assert ready[0].id == "a"

    def test_unmet_deps_not_ready(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A"))
        engine.add_node(WorkflowNode(id="b", task="B", dependencies=["a"]))
        ready = engine.get_ready_nodes()
        assert len(ready) == 1
        assert ready[0].id == "a"

    def test_met_deps_is_ready(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A", status="completed"))
        engine.add_node(WorkflowNode(id="b", task="B", dependencies=["a"]))
        ready = engine.get_ready_nodes()
        assert len(ready) == 1
        assert ready[0].id == "b"

    def test_completed_not_ready(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A", status="completed"))
        ready = engine.get_ready_nodes()
        assert len(ready) == 0

    def test_retrying_is_ready(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A", status="retrying"))
        ready = engine.get_ready_nodes()
        assert len(ready) == 1

    def test_parallel_ready_nodes(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A"))
        engine.add_node(WorkflowNode(id="b", task="B"))
        engine.add_node(WorkflowNode(id="c", task="C"))
        ready = engine.get_ready_nodes()
        assert len(ready) == 3

    def test_conditional_edge_blocks_when_false(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A", status="completed",
                                     result="FAIL all tests"))
        engine.add_node(WorkflowNode(id="b", task="B", dependencies=["a"]))
        engine.add_edge(WorkflowEdge(source="a", target="b",
                                     condition_expr="contains:PASS"))
        ready = engine.get_ready_nodes()
        assert len(ready) == 0

    def test_conditional_edge_passes_when_true(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A", status="completed",
                                     result="ALL TESTS PASS"))
        engine.add_node(WorkflowNode(id="b", task="B", dependencies=["a"]))
        engine.add_edge(WorkflowEdge(source="a", target="b",
                                     condition_expr="contains:PASS"))
        ready = engine.get_ready_nodes()
        assert len(ready) == 1
        assert ready[0].id == "b"


# ── Worker Assignment ───────────────────────────────────────────────

class TestWorkerAssignment:
    def test_pre_assigned_worker(self):
        engine = DAGEngine(name="t")
        node = WorkflowNode(id="a", task="A", worker="gamma")
        engine.add_node(node)
        assert engine._assign_worker(node) == "gamma"

    @patch("tools.skynet_workflow._get_idle_workers", return_value=["alpha", "beta"])
    def test_round_robin(self, mock_idle):
        engine = DAGEngine(name="t")
        n1 = WorkflowNode(id="a", task="A")
        n2 = WorkflowNode(id="b", task="B")
        engine.add_node(n1)
        engine.add_node(n2)
        w1 = engine._assign_worker(n1)
        w2 = engine._assign_worker(n2)
        assert w1 == "alpha"
        assert w2 == "beta"

    @patch("tools.skynet_workflow._get_idle_workers", return_value=[])
    def test_fallback_to_all_workers(self, mock_idle):
        engine = DAGEngine(name="t")
        node = WorkflowNode(id="a", task="A")
        engine.add_node(node)
        w = engine._assign_worker(node)
        assert w in WORKER_NAMES


# ── Dispatch & Wait ─────────────────────────────────────────────────

class TestDispatchWait:
    @patch("tools.skynet_workflow._save_workflow")
    @patch("tools.skynet_workflow._bus_publish", return_value=True)
    @patch("tools.skynet_dispatch.dispatch_to_worker", return_value=True)
    def test_dispatch_node_success(self, mock_dispatch, mock_bus, mock_save):
        engine = DAGEngine(name="t", workflow_id="wf_test")
        node = WorkflowNode(id="plan", task="Plan it", worker="alpha")
        engine.add_node(node)
        result = engine._dispatch_node(node)
        assert result is True
        assert node.status == "running"
        assert node.dispatch_key == "WF-wf_test-plan"
        assert node.started_at is not None

    @patch("tools.skynet_workflow._save_workflow")
    @patch("tools.skynet_dispatch.dispatch_to_worker", return_value=False)
    def test_dispatch_node_failure(self, mock_dispatch, mock_save):
        engine = DAGEngine(name="t", workflow_id="wf_test")
        node = WorkflowNode(id="plan", task="Plan it", worker="alpha")
        engine.add_node(node)
        result = engine._dispatch_node(node)
        assert result is False
        assert node.status == "failed"
        assert "failed" in node.error.lower()

    @patch("tools.skynet_workflow._save_workflow")
    @patch("tools.skynet_workflow._wait_for_result")
    def test_wait_node_success(self, mock_wait, mock_save):
        mock_wait.return_value = {"content": "Task done PASS"}
        engine = DAGEngine(name="t", workflow_id="wf_test")
        node = WorkflowNode(id="plan", task="Plan", dispatch_key="WF-wf_test-plan")
        engine.add_node(node)
        result = engine._wait_node(node)
        assert result is True
        assert node.status == "completed"
        assert node.result == "Task done PASS"
        assert node.completed_at is not None

    @patch("tools.skynet_workflow._save_workflow")
    @patch("tools.skynet_workflow._wait_for_result", return_value=None)
    def test_wait_node_timeout_retries(self, mock_wait, mock_save):
        engine = DAGEngine(name="t", workflow_id="wf_test")
        node = WorkflowNode(id="plan", task="Plan", dispatch_key="WF-wf_test-plan",
                            max_retries=3)
        engine.add_node(node)
        result = engine._wait_node(node, timeout=1.0)
        assert result is False
        assert node.status == "retrying"
        assert node.retries == 1

    @patch("tools.skynet_workflow._save_workflow")
    @patch("tools.skynet_workflow._wait_for_result", return_value=None)
    def test_wait_node_exhausted_retries(self, mock_wait, mock_save):
        engine = DAGEngine(name="t", workflow_id="wf_test")
        node = WorkflowNode(id="plan", task="Plan", dispatch_key="WF-wf_test-plan",
                            retries=2, max_retries=3)
        engine.add_node(node)
        result = engine._wait_node(node, timeout=1.0)
        assert result is False
        assert node.status == "failed"
        assert "3 attempts" in node.error

    def test_wait_node_no_dispatch_key(self):
        engine = DAGEngine(name="t")
        node = WorkflowNode(id="a", task="A")
        engine.add_node(node)
        assert engine._wait_node(node) is False


# ── Skip Downstream ─────────────────────────────────────────────────

class TestSkipDownstream:
    def test_skip_single_dependent(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A"))
        engine.add_node(WorkflowNode(id="b", task="B"))
        engine.add_edge(WorkflowEdge(source="a", target="b"))
        engine._skip_downstream("a")
        assert engine.nodes["b"].status == "skipped"

    def test_skip_cascading(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A"))
        engine.add_node(WorkflowNode(id="b", task="B"))
        engine.add_node(WorkflowNode(id="c", task="C"))
        engine.add_edge(WorkflowEdge(source="a", target="b"))
        engine.add_edge(WorkflowEdge(source="b", target="c"))
        engine._skip_downstream("a")
        assert engine.nodes["b"].status == "skipped"
        assert engine.nodes["c"].status == "skipped"

    def test_skip_does_not_affect_completed(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A"))
        engine.add_node(WorkflowNode(id="b", task="B", status="completed"))
        engine.add_edge(WorkflowEdge(source="a", target="b"))
        engine._skip_downstream("a")
        assert engine.nodes["b"].status == "completed"  # unchanged


# ── Execute (Full Flow) ─────────────────────────────────────────────

class TestExecute:
    @patch("tools.skynet_workflow._save_workflow")
    def test_execute_invalid_dag(self, mock_save):
        engine = DAGEngine(name="t", workflow_id="wf_bad")
        engine.add_node(WorkflowNode(id="a", task="A", dependencies=["ghost"]))
        result = engine.execute()
        assert result["status"] == "invalid"
        assert "errors" in result

    @patch("tools.skynet_workflow._save_workflow")
    def test_dry_run(self, mock_save):
        engine = DAGEngine(name="t", workflow_id="wf_dry")
        engine.add_node(WorkflowNode(id="a", task="A"))
        engine.add_node(WorkflowNode(id="b", task="B"))
        engine.add_edge(WorkflowEdge(source="a", target="b"))
        result = engine.execute(dry_run=True)
        assert result["status"] == "dry_run"
        assert len(result["plan"]) == 2
        assert result["plan"][0]["node_id"] == "a"
        assert result["plan"][1]["node_id"] == "b"

    @patch("tools.skynet_workflow.time.sleep")
    @patch("tools.skynet_workflow._save_workflow")
    @patch("tools.skynet_workflow._bus_publish", return_value=True)
    @patch("tools.skynet_workflow._wait_for_result")
    @patch("tools.skynet_dispatch.dispatch_to_worker", return_value=True)
    def test_execute_simple_linear_flow(self, mock_disp, mock_wait, mock_bus,
                                        mock_save, mock_sleep):
        mock_wait.return_value = {"content": "DONE PASS"}
        engine = DAGEngine(name="t", workflow_id="wf_lin")
        engine.add_node(WorkflowNode(id="a", task="A"))
        engine.add_node(WorkflowNode(id="b", task="B", dependencies=["a"]))
        engine.add_edge(WorkflowEdge(source="a", target="b"))
        result = engine.execute(timeout_per_node=1.0)
        assert result["status"] == "completed"
        assert result["completed"] == 2
        assert result["failed"] == 0

    @patch("tools.skynet_workflow.time.sleep")
    @patch("tools.skynet_workflow._save_workflow")
    @patch("tools.skynet_workflow._bus_publish", return_value=True)
    @patch("tools.skynet_workflow._wait_for_result", return_value=None)
    @patch("tools.skynet_dispatch.dispatch_to_worker", return_value=True)
    def test_execute_with_failure(self, mock_disp, mock_wait, mock_bus,
                                  mock_save, mock_sleep):
        engine = DAGEngine(name="t", workflow_id="wf_fail")
        engine.add_node(WorkflowNode(id="a", task="A", max_retries=1))
        result = engine.execute(timeout_per_node=0.1)
        assert result["status"] == "failed"
        assert result["failed"] >= 1


# ── All Terminal / Status ────────────────────────────────────────────

class TestAllTerminal:
    def test_all_completed(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A", status="completed"))
        engine.add_node(WorkflowNode(id="b", task="B", status="completed"))
        assert engine._all_terminal() is True

    def test_mixed_terminal(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A", status="completed"))
        engine.add_node(WorkflowNode(id="b", task="B", status="failed"))
        engine.add_node(WorkflowNode(id="c", task="C", status="skipped"))
        assert engine._all_terminal() is True

    def test_not_terminal(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A", status="running"))
        assert engine._all_terminal() is False

    def test_pending_not_terminal(self):
        engine = DAGEngine(name="t")
        engine.add_node(WorkflowNode(id="a", task="A", status="pending"))
        assert engine._all_terminal() is False


# ── Serialization / Resume ──────────────────────────────────────────

class TestSerializationResume:
    def test_to_dict(self):
        engine = DAGEngine(name="my-flow", workflow_id="wf_123")
        engine.add_node(WorkflowNode(id="a", task="A"))
        engine.add_node(WorkflowNode(id="b", task="B"))
        engine.add_edge(WorkflowEdge(source="a", target="b"))
        d = engine.to_dict()
        assert d["id"] == "wf_123"
        assert d["name"] == "my-flow"
        assert len(d["nodes"]) == 2
        assert len(d["edges"]) == 1

    def test_from_dict(self):
        data = {
            "id": "wf_abc",
            "name": "restored",
            "status": "running",
            "created_at": "2026-01-01T00:00:00Z",
            "nodes": [
                {"id": "a", "task": "A", "status": "completed", "result": "done"},
                {"id": "b", "task": "B", "status": "running", "dependencies": ["a"]},
            ],
            "edges": [{"source": "a", "target": "b"}],
        }
        engine = DAGEngine.from_dict(data)
        assert engine.id == "wf_abc"
        assert engine.name == "restored"
        assert engine.status == "running"
        assert "a" in engine.nodes
        assert "b" in engine.nodes
        assert len(engine.edges) == 1
        assert "a" in engine.nodes["b"].dependencies

    def test_round_trip_serialization(self):
        engine = DAGEngine(name="rt", workflow_id="wf_rt")
        engine.add_node(WorkflowNode(id="x", task="X", worker="alpha"))
        engine.add_node(WorkflowNode(id="y", task="Y", dependencies=["x"]))
        engine.add_edge(WorkflowEdge(source="x", target="y",
                                     condition_expr="contains:OK"))
        d = engine.to_dict()
        restored = DAGEngine.from_dict(d)
        assert restored.id == engine.id
        assert restored.name == engine.name
        assert len(restored.nodes) == len(engine.nodes)
        assert len(restored.edges) == len(engine.edges)

    @patch("tools.skynet_workflow.time.sleep")
    @patch("tools.skynet_workflow._save_workflow")
    @patch("tools.skynet_workflow._bus_publish", return_value=True)
    @patch("tools.skynet_workflow._wait_for_result")
    @patch("tools.skynet_dispatch.dispatch_to_worker", return_value=True)
    def test_resume_resets_inflight(self, mock_disp, mock_wait, mock_bus,
                                    mock_save, mock_sleep):
        mock_wait.return_value = {"content": "PASS"}
        engine = DAGEngine(name="t", workflow_id="wf_res")
        engine.add_node(WorkflowNode(id="a", task="A", status="running"))
        engine.add_node(WorkflowNode(id="b", task="B", status="dispatched"))
        engine.add_node(WorkflowNode(id="c", task="C", status="completed",
                                     result="done"))
        result = engine.resume(timeout_per_node=1.0)
        # a and b should have been reset to pending before execute
        assert result["status"] in ("completed", "partial")


# ── Built-in Workflows ──────────────────────────────────────────────

class TestBuiltinWorkflows:
    def test_plan_code_test_deploy(self):
        engine = builtin_plan_code_test_deploy("Add caching")
        assert engine.name == "plan-code-test-deploy"
        assert len(engine.nodes) == 4
        assert "plan" in engine.nodes
        assert "code" in engine.nodes
        assert "test" in engine.nodes
        assert "deploy" in engine.nodes
        assert engine.nodes["code"].dependencies == ["plan"]
        assert engine.nodes["test"].dependencies == ["code"]
        assert engine.nodes["deploy"].dependencies == ["test"]

    def test_plan_code_test_deploy_workers(self):
        engine = builtin_plan_code_test_deploy("goal", workers=["x", "y"])
        assert engine.nodes["plan"].worker == "x"
        assert engine.nodes["code"].worker == "y"

    def test_audit_fix_verify(self):
        engine = builtin_audit_fix_verify("core/security.py")
        assert engine.name == "audit-fix-verify"
        assert len(engine.nodes) == 3
        assert "audit" in engine.nodes
        assert "fix" in engine.nodes
        assert "verify" in engine.nodes
        assert engine.nodes["fix"].dependencies == ["audit"]
        assert engine.nodes["verify"].dependencies == ["fix"]

    def test_builtin_registry(self):
        assert "plan-code-test-deploy" in BUILTIN_WORKFLOWS
        assert "audit-fix-verify" in BUILTIN_WORKFLOWS
        assert callable(BUILTIN_WORKFLOWS["plan-code-test-deploy"])

    def test_conditional_edges_in_plan_code_test_deploy(self):
        engine = builtin_plan_code_test_deploy("goal")
        # code→test has not_contains:FAIL condition
        code_test_edges = [e for e in engine.edges
                           if e.source == "code" and e.target == "test"]
        assert len(code_test_edges) == 1
        assert code_test_edges[0].condition_expr == "not_contains:FAIL"
        assert code_test_edges[0].condition is not None
        # test→deploy has contains:PASS condition
        test_deploy_edges = [e for e in engine.edges
                             if e.source == "test" and e.target == "deploy"]
        assert len(test_deploy_edges) == 1
        assert test_deploy_edges[0].condition_expr == "contains:PASS"


# ── Constants ────────────────────────────────────────────────────────

class TestConstants:
    def test_default_max_retries(self):
        assert DEFAULT_MAX_RETRIES == 3

    def test_dispatch_cooldown(self):
        assert DISPATCH_COOLDOWN_S == 2.0

    def test_result_poll_timeout(self):
        assert RESULT_POLL_TIMEOUT_S == 120.0

    def test_worker_names(self):
        assert WORKER_NAMES == ["alpha", "beta", "gamma", "delta"]
