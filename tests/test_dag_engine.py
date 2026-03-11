# signed: alpha
"""
Tests for core/dag_engine.py — Dynamic DAG Engine for workflow execution.

Covers:
- NodeStatus / EdgeType enums
- DAGNode: properties, defaults, elapsed timing
- DAGEdge: structure
- ExecutionContext: shared state
- DAG: add_node, add_edge, get_root_nodes, get_ready_nodes, topological_sort,
       is_complete, stats, chaining API
- DAGExecutor: sequential execution, retries with backoff, conditional edges,
               feedback loops, timeout, skipping, error handling, execution log
- DAGBuilder: from_workflow_plan for all 5 operator types, domain specialists
"""

import time
import pytest
from unittest.mock import MagicMock
from core.dag_engine import (
    NodeStatus, EdgeType, DAGNode, DAGEdge, ExecutionContext,
    DAG, DAGExecutor, DAGBuilder,
)


# ── Enum tests ──


class TestEnums:
    def test_node_status_values(self):
        assert NodeStatus.PENDING.value == "pending"
        assert NodeStatus.RUNNING.value == "running"
        assert NodeStatus.COMPLETED.value == "completed"
        assert NodeStatus.FAILED.value == "failed"
        assert NodeStatus.SKIPPED.value == "skipped"
        assert NodeStatus.RETRYING.value == "retrying"

    def test_edge_type_values(self):
        assert EdgeType.SEQUENTIAL.value == "sequential"
        assert EdgeType.CONDITIONAL.value == "conditional"
        assert EdgeType.PARALLEL.value == "parallel"
        assert EdgeType.FEEDBACK.value == "feedback"


# ── DAGNode tests ──


class TestDAGNode:
    def test_defaults(self):
        node = DAGNode(id="n1", role="reasoner", description="test")
        assert node.status == NodeStatus.PENDING
        assert node.max_retries == 2
        assert node.timeout_seconds == 60.0
        assert node.output is None
        assert node.error is None
        assert node.retry_count == 0
        assert node.handler is None
        assert node.metadata == {}

    def test_elapsed_no_times(self):
        node = DAGNode(id="n1", role="r", description="d")
        assert node.elapsed_seconds == 0.0

    def test_elapsed_with_both_times(self):
        node = DAGNode(id="n1", role="r", description="d")
        node.start_time = 100.0
        node.end_time = 105.5
        assert node.elapsed_seconds == 5.5

    def test_elapsed_running(self):
        node = DAGNode(id="n1", role="r", description="d")
        node.start_time = time.time() - 2.0
        # No end_time means still running
        assert node.elapsed_seconds >= 1.5


# ── DAGEdge tests ──


class TestDAGEdge:
    def test_defaults(self):
        edge = DAGEdge(source="a", target="b")
        assert edge.edge_type == EdgeType.SEQUENTIAL
        assert edge.condition is None

    def test_conditional_edge(self):
        cond = lambda x: x is not None
        edge = DAGEdge(source="a", target="b", edge_type=EdgeType.CONDITIONAL,
                       condition=cond)
        assert edge.edge_type == EdgeType.CONDITIONAL
        assert edge.condition is cond


# ── ExecutionContext tests ──


class TestExecutionContext:
    def test_defaults(self):
        ctx = ExecutionContext(query="test query")
        assert ctx.query == "test query"
        assert ctx.node_outputs == {}
        assert ctx.shared_state == {}
        assert ctx.errors == []
        assert ctx.start_time > 0

    def test_shared_state_mutable(self):
        ctx = ExecutionContext(query="q")
        ctx.shared_state["key"] = "value"
        ctx.node_outputs["node1"] = {"result": 42}
        assert ctx.shared_state["key"] == "value"
        assert ctx.node_outputs["node1"]["result"] == 42


# ── DAG tests ──


class TestDAG:
    def test_empty_dag(self):
        dag = DAG("test")
        assert dag.name == "test"
        assert len(dag.nodes) == 0
        assert len(dag.edges) == 0
        assert dag.is_complete is True

    def test_add_node_returns_self(self):
        dag = DAG()
        result = dag.add_node(DAGNode(id="n1", role="r", description="d"))
        assert result is dag

    def test_add_edge_returns_self(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        result = dag.add_edge("a", "b")
        assert result is dag

    def test_chaining(self):
        dag = DAG("chain")
        dag.add_node(DAGNode(id="a", role="r", description="d")) \
           .add_node(DAGNode(id="b", role="r", description="d")) \
           .add_edge("a", "b")
        assert len(dag.nodes) == 2
        assert len(dag.edges) == 1

    def test_get_root_nodes(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.add_node(DAGNode(id="c", role="r", description="d"))
        dag.add_edge("a", "b")
        dag.add_edge("a", "c")
        roots = dag.get_root_nodes()
        assert roots == ["a"]

    def test_get_root_nodes_multiple(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.add_node(DAGNode(id="c", role="r", description="d"))
        dag.add_edge("a", "c")
        dag.add_edge("b", "c")
        roots = dag.get_root_nodes()
        assert set(roots) == {"a", "b"}

    def test_get_ready_nodes_all_pending_roots(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.add_edge("a", "b")
        ready = dag.get_ready_nodes()
        assert ready == ["a"]

    def test_get_ready_nodes_after_parent_complete(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.add_edge("a", "b")
        dag.nodes["a"].status = NodeStatus.COMPLETED
        ready = dag.get_ready_nodes()
        assert ready == ["b"]

    def test_get_ready_nodes_parent_not_complete(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.add_edge("a", "b")
        dag.nodes["a"].status = NodeStatus.RUNNING
        ready = dag.get_ready_nodes()
        assert ready == []

    def test_topological_sort_linear(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.add_node(DAGNode(id="c", role="r", description="d"))
        dag.add_edge("a", "b")
        dag.add_edge("b", "c")
        order = dag.topological_sort()
        assert order == ["a", "b", "c"]

    def test_topological_sort_diamond(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.add_node(DAGNode(id="c", role="r", description="d"))
        dag.add_node(DAGNode(id="d", role="r", description="d"))
        dag.add_edge("a", "b")
        dag.add_edge("a", "c")
        dag.add_edge("b", "d")
        dag.add_edge("c", "d")
        order = dag.topological_sort()
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_is_complete_false_when_pending(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        assert dag.is_complete is False  # PENDING is not complete

    def test_is_complete_true_when_all_done(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.nodes["a"].status = NodeStatus.COMPLETED
        dag.nodes["b"].status = NodeStatus.FAILED
        assert dag.is_complete is True

    def test_is_complete_with_skipped(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.nodes["a"].status = NodeStatus.SKIPPED
        assert dag.is_complete is True

    def test_stats(self):
        dag = DAG("stats_test")
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.add_edge("a", "b")
        dag.nodes["a"].status = NodeStatus.COMPLETED
        stats = dag.stats
        assert stats["name"] == "stats_test"
        assert stats["nodes"] == 2
        assert stats["edges"] == 1
        assert stats["statuses"]["completed"] == 1
        assert stats["statuses"]["pending"] == 1
        assert stats["complete"] is False


# ── DAGExecutor tests ──


class TestDAGExecutor:
    def test_execute_simple_chain(self):
        dag = DAG("test")
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.add_edge("a", "b")

        executor = DAGExecutor()
        ctx = ExecutionContext(query="test")
        result = executor.execute(dag, ctx)

        assert dag.nodes["a"].status == NodeStatus.COMPLETED
        assert dag.nodes["b"].status == NodeStatus.COMPLETED
        assert "a" in result.node_outputs
        assert "b" in result.node_outputs
        assert dag.is_complete

    def test_execute_with_handler(self):
        def handler_a(ctx):
            return {"value": 42}

        def handler_b(ctx):
            return {"doubled": ctx.node_outputs["a"]["value"] * 2}

        dag = DAG("handler_test")
        dag.add_node(DAGNode(id="a", role="r", description="d", handler=handler_a))
        dag.add_node(DAGNode(id="b", role="r", description="d", handler=handler_b))
        dag.add_edge("a", "b")

        executor = DAGExecutor()
        ctx = ExecutionContext(query="test")
        result = executor.execute(dag, ctx)

        assert result.node_outputs["a"]["value"] == 42
        assert result.node_outputs["b"]["doubled"] == 84

    def test_execute_handler_failure_retries(self):
        call_count = {"n": 0}

        def flaky_handler(ctx):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise RuntimeError("Transient failure")
            return {"ok": True}

        dag = DAG("retry_test")
        dag.add_node(DAGNode(id="a", role="r", description="d",
                             handler=flaky_handler, max_retries=2))

        executor = DAGExecutor()
        ctx = ExecutionContext(query="test")
        result = executor.execute(dag, ctx)

        assert dag.nodes["a"].status == NodeStatus.COMPLETED
        assert dag.nodes["a"].retry_count == 1
        assert result.node_outputs["a"]["ok"] is True

    def test_execute_handler_exhausts_retries(self):
        def always_fail(ctx):
            raise RuntimeError("Permanent failure")

        dag = DAG("fail_test")
        dag.add_node(DAGNode(id="a", role="r", description="d",
                             handler=always_fail, max_retries=1))

        executor = DAGExecutor()
        ctx = ExecutionContext(query="test")
        result = executor.execute(dag, ctx)

        assert dag.nodes["a"].status == NodeStatus.FAILED
        assert "a" not in result.node_outputs
        assert len(result.errors) == 1
        assert "Permanent failure" in result.errors[0]

    def test_execute_conditional_edge_true(self):
        def handler_a(ctx):
            return {"pass": True}

        dag = DAG("cond_true")
        dag.add_node(DAGNode(id="a", role="r", description="d", handler=handler_a))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.add_edge("a", "b", edge_type=EdgeType.CONDITIONAL,
                     condition=lambda output: output and output.get("pass"))

        executor = DAGExecutor()
        ctx = ExecutionContext(query="test")
        executor.execute(dag, ctx)

        assert dag.nodes["b"].status == NodeStatus.COMPLETED

    def test_execute_conditional_edge_false(self):
        def handler_a(ctx):
            return {"pass": False}

        dag = DAG("cond_false")
        dag.add_node(DAGNode(id="a", role="r", description="d", handler=handler_a))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.add_edge("a", "b", edge_type=EdgeType.CONDITIONAL,
                     condition=lambda output: output and output.get("pass"))

        executor = DAGExecutor()
        ctx = ExecutionContext(query="test")
        executor.execute(dag, ctx)

        assert dag.nodes["b"].status == NodeStatus.SKIPPED

    def test_execute_conditional_edge_exception(self):
        def handler_a(ctx):
            return "not a dict"

        dag = DAG("cond_err")
        dag.add_node(DAGNode(id="a", role="r", description="d", handler=handler_a))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.add_edge("a", "b", edge_type=EdgeType.CONDITIONAL,
                     condition=lambda output: output["key"])  # will raise

        executor = DAGExecutor()
        ctx = ExecutionContext(query="test")
        executor.execute(dag, ctx)

        assert dag.nodes["b"].status == NodeStatus.SKIPPED

    def test_execute_default_handler_placeholder(self):
        dag = DAG("placeholder")
        dag.add_node(DAGNode(id="a", role="reasoner", description="d"))

        executor = DAGExecutor()
        ctx = ExecutionContext(query="test")
        result = executor.execute(dag, ctx)

        output = result.node_outputs["a"]
        assert output["node"] == "a"
        assert output["role"] == "reasoner"
        assert output["status"] == "executed"

    def test_execution_history(self):
        dag = DAG("history_test")
        dag.add_node(DAGNode(id="a", role="r", description="d"))

        executor = DAGExecutor()
        assert len(executor.execution_history) == 0

        ctx = ExecutionContext(query="test")
        executor.execute(dag, ctx)

        history = executor.execution_history
        assert len(history) == 1
        assert history[0]["dag"] == "history_test"
        assert "elapsed_ms" in history[0]
        assert "stats" in history[0]

    def test_execute_no_retries(self):
        def fail_handler(ctx):
            raise RuntimeError("fail")

        dag = DAG("no_retry")
        dag.add_node(DAGNode(id="a", role="r", description="d",
                             handler=fail_handler, max_retries=0))

        executor = DAGExecutor()
        ctx = ExecutionContext(query="test")
        executor.execute(dag, ctx)

        assert dag.nodes["a"].status == NodeStatus.FAILED
        assert dag.nodes["a"].retry_count == 0

    def test_node_timing(self):
        def slow_handler(ctx):
            time.sleep(0.05)
            return {"done": True}

        dag = DAG("timing")
        dag.add_node(DAGNode(id="a", role="r", description="d",
                             handler=slow_handler, max_retries=0))

        executor = DAGExecutor()
        ctx = ExecutionContext(query="test")
        executor.execute(dag, ctx)

        node = dag.nodes["a"]
        assert node.start_time > 0
        assert node.end_time > node.start_time
        assert node.elapsed_seconds >= 0.04

    def test_shared_context_between_nodes(self):
        def handler_a(ctx):
            ctx.shared_state["from_a"] = "hello"
            return {"step": 1}

        def handler_b(ctx):
            return {"received": ctx.shared_state.get("from_a")}

        dag = DAG("shared_ctx")
        dag.add_node(DAGNode(id="a", role="r", description="d", handler=handler_a))
        dag.add_node(DAGNode(id="b", role="r", description="d", handler=handler_b))
        dag.add_edge("a", "b")

        executor = DAGExecutor()
        ctx = ExecutionContext(query="test")
        result = executor.execute(dag, ctx)

        assert result.node_outputs["b"]["received"] == "hello"

    def test_feedback_loop_on_failure(self):
        call_log = []

        def proposer(ctx):
            call_log.append("propose")
            feedback = ctx.shared_state.get("feedback")
            if feedback:
                return {"proposal": "revised based on feedback"}
            return {"proposal": "initial"}

        def critic(ctx):
            call_log.append("critique")
            raise RuntimeError("Proposal is weak")

        dag = DAG("feedback")
        dag.add_node(DAGNode(id="propose", role="proposer", description="d",
                             handler=proposer, max_retries=0))
        dag.add_node(DAGNode(id="critique", role="critic", description="d",
                             handler=critic, max_retries=0))
        dag.add_edge("propose", "critique")
        dag.add_edge("critique", "propose", EdgeType.FEEDBACK)

        executor = DAGExecutor(max_feedback_loops=2)
        ctx = ExecutionContext(query="test")
        executor.execute(dag, ctx)

        # proposer should have been called initially, then critique fails,
        # then feedback re-executes proposer
        assert "propose" in call_log
        assert "critique" in call_log


# ── DAGBuilder tests ──


class TestDAGBuilder:
    def _make_plan(self, operator_value, roles=None, query="test query"):
        """Create a mock WorkflowPlan."""
        from core.difficulty_router import OperatorType
        plan = MagicMock()
        plan.operator = OperatorType(operator_value)
        plan.agent_roles = roles or []
        plan.query = query
        return plan

    def test_build_direct(self):
        plan = self._make_plan("direct", roles=["reasoner"])
        dag = DAGBuilder.from_workflow_plan(plan)
        assert "execute" in dag.nodes
        assert dag.nodes["execute"].max_retries == 0
        assert len(dag.edges) == 0

    def test_build_chain_of_thought(self):
        plan = self._make_plan("cot", roles=["reasoner"])
        dag = DAGBuilder.from_workflow_plan(plan)
        assert "reason" in dag.nodes
        assert "synthesize" in dag.nodes
        assert len(dag.edges) == 1

    def test_build_tool_augmented(self):
        plan = self._make_plan("tool_augmented", roles=["planner", "tool_executor", "reasoner"])
        dag = DAGBuilder.from_workflow_plan(plan)
        assert "plan" in dag.nodes
        assert "execute_tools" in dag.nodes
        assert "synthesize" in dag.nodes
        assert len(dag.edges) == 2
        order = dag.topological_sort()
        assert order.index("plan") < order.index("execute_tools")
        assert order.index("execute_tools") < order.index("synthesize")

    def test_build_multi_agent(self):
        plan = self._make_plan("multi_agent",
                               roles=["planner", "specialist", "validator"])
        dag = DAGBuilder.from_workflow_plan(plan)
        assert "plan" in dag.nodes
        assert "validate" in dag.nodes
        # specialist_1 is the specialist node
        specialist_nodes = [n for n in dag.nodes if n.startswith("specialist_")]
        assert len(specialist_nodes) >= 1

    def test_build_debate(self):
        plan = self._make_plan("debate",
                               roles=["proposer", "critic", "judge", "tool_executor"])
        dag = DAGBuilder.from_workflow_plan(plan)
        assert "propose" in dag.nodes
        assert "critique" in dag.nodes
        assert "judge" in dag.nodes
        # Should have a feedback edge from judge back to propose
        feedback_edges = [e for e in dag.edges if e.edge_type == EdgeType.FEEDBACK]
        assert len(feedback_edges) == 1
        assert feedback_edges[0].source == "judge"
        assert feedback_edges[0].target == "propose"

    def test_domain_specialists_added(self):
        plan = self._make_plan("direct", roles=["reasoner", "code_specialist"])
        dag = DAGBuilder.from_workflow_plan(plan)
        assert "domain_code_specialist" in dag.nodes

    def test_domain_specialist_not_duplicated(self):
        plan = self._make_plan("tool_augmented",
                               roles=["planner", "tool_executor", "reasoner",
                                      "code_specialist"])
        dag = DAGBuilder.from_workflow_plan(plan)
        specialist_nodes = [n for n in dag.nodes.values()
                           if n.role == "code_specialist"]
        assert len(specialist_nodes) == 1
