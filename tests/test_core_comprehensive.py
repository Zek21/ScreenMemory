"""Comprehensive test suite for core modules:
orchestrator, dag_engine, difficulty_router, database.

Target: 40+ tests covering all major code paths, edge cases,
error handling, and integration scenarios.
"""

import hashlib
import json
import os
import sqlite3
import struct
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))


# ============================================================
# DAG ENGINE TESTS
# ============================================================

from core.dag_engine import (
    DAG,
    DAGNode,
    DAGEdge,
    DAGBuilder,
    DAGExecutor,
    ExecutionContext,
    NodeStatus,
    EdgeType,
)


class TestDAGCreation:
    def test_empty_dag(self):
        dag = DAG("empty")
        assert dag.name == "empty"
        assert len(dag.nodes) == 0
        assert len(dag.edges) == 0

    def test_add_single_node(self):
        dag = DAG()
        node = DAGNode(id="n1", role="worker", description="test")
        result = dag.add_node(node)
        assert result is dag  # chaining
        assert "n1" in dag.nodes
        assert dag.nodes["n1"].role == "worker"

    def test_add_edge_updates_adjacency(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.add_edge("a", "b")
        assert "b" in dag._adjacency["a"]
        assert "a" in dag._reverse_adjacency["b"]

    def test_add_edge_chaining(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        result = dag.add_edge("a", "b")
        assert result is dag

    def test_multiple_edges(self):
        dag = DAG()
        for nid in ("a", "b", "c"):
            dag.add_node(DAGNode(id=nid, role="r", description="d"))
        dag.add_edge("a", "b")
        dag.add_edge("a", "c")
        assert len(dag.edges) == 2
        assert set(dag._adjacency["a"]) == {"b", "c"}


class TestDAGTopologicalSort:
    def test_linear_chain(self):
        dag = DAG()
        for nid in ("a", "b", "c"):
            dag.add_node(DAGNode(id=nid, role="r", description="d"))
        dag.add_edge("a", "b")
        dag.add_edge("b", "c")
        order = dag.topological_sort()
        assert order.index("a") < order.index("b") < order.index("c")

    def test_diamond_dependency(self):
        dag = DAG()
        for nid in ("root", "left", "right", "join"):
            dag.add_node(DAGNode(id=nid, role="r", description="d"))
        dag.add_edge("root", "left")
        dag.add_edge("root", "right")
        dag.add_edge("left", "join")
        dag.add_edge("right", "join")
        order = dag.topological_sort()
        assert order.index("root") < order.index("left")
        assert order.index("root") < order.index("right")
        assert order.index("left") < order.index("join")
        assert order.index("right") < order.index("join")

    def test_cycle_detection(self):
        dag = DAG()
        for nid in ("a", "b", "c"):
            dag.add_node(DAGNode(id=nid, role="r", description="d"))
        dag.add_edge("a", "b")
        dag.add_edge("b", "c")
        dag.add_edge("c", "a")  # cycle
        order = dag.topological_sort()
        # On cycle, returns all nodes but order != len(nodes) internally
        assert len(order) == 3  # falls back to list(nodes.keys())

    def test_single_node_no_edges(self):
        dag = DAG()
        dag.add_node(DAGNode(id="solo", role="r", description="d"))
        order = dag.topological_sort()
        assert order == ["solo"]

    def test_parallel_independent_nodes(self):
        dag = DAG()
        for nid in ("a", "b", "c"):
            dag.add_node(DAGNode(id=nid, role="r", description="d"))
        # No edges — all independent
        order = dag.topological_sort()
        assert set(order) == {"a", "b", "c"}


class TestDAGReadyNodes:
    def test_root_nodes_ready(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.add_edge("a", "b")
        ready = dag.get_ready_nodes()
        assert "a" in ready
        assert "b" not in ready

    def test_child_ready_after_parent_completes(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.add_edge("a", "b")
        dag.nodes["a"].status = NodeStatus.COMPLETED
        ready = dag.get_ready_nodes()
        assert "b" in ready

    def test_diamond_join_needs_both_parents(self):
        dag = DAG()
        for nid in ("root", "left", "right", "join"):
            dag.add_node(DAGNode(id=nid, role="r", description="d"))
        dag.add_edge("root", "left")
        dag.add_edge("root", "right")
        dag.add_edge("left", "join")
        dag.add_edge("right", "join")
        dag.nodes["root"].status = NodeStatus.COMPLETED
        dag.nodes["left"].status = NodeStatus.COMPLETED
        # right not complete yet
        ready = dag.get_ready_nodes()
        assert "join" not in ready
        dag.nodes["right"].status = NodeStatus.COMPLETED
        ready = dag.get_ready_nodes()
        assert "join" in ready

    def test_root_nodes_identification(self):
        dag = DAG()
        for nid in ("a", "b", "c"):
            dag.add_node(DAGNode(id=nid, role="r", description="d"))
        dag.add_edge("a", "b")
        dag.add_edge("a", "c")
        roots = dag.get_root_nodes()
        assert roots == ["a"]


class TestDAGProperties:
    def test_is_complete_all_done(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.nodes["a"].status = NodeStatus.COMPLETED
        dag.nodes["b"].status = NodeStatus.COMPLETED
        assert dag.is_complete is True

    def test_is_complete_mixed_terminal(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.nodes["a"].status = NodeStatus.COMPLETED
        dag.nodes["b"].status = NodeStatus.FAILED
        assert dag.is_complete is True

    def test_is_complete_pending(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        assert dag.is_complete is False

    def test_stats_returns_correct_counts(self):
        dag = DAG("test")
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.add_edge("a", "b")
        stats = dag.stats
        assert stats["name"] == "test"
        assert stats["nodes"] == 2
        assert stats["edges"] == 1


class TestDAGExecutor:
    def test_execute_simple_dag(self):
        dag = DAG()
        dag.add_node(DAGNode(id="n1", role="worker", description="test"))
        ctx = ExecutionContext(query="test")
        executor = DAGExecutor()
        result = executor.execute(dag, ctx)
        assert dag.nodes["n1"].status == NodeStatus.COMPLETED
        assert "n1" in result.node_outputs

    def test_execute_with_handler(self):
        def my_handler(context):
            return {"result": "computed"}

        dag = DAG()
        dag.add_node(DAGNode(id="n1", role="worker", description="test",
                             handler=my_handler))
        ctx = ExecutionContext(query="test")
        executor = DAGExecutor()
        result = executor.execute(dag, ctx)
        assert result.node_outputs["n1"]["result"] == "computed"

    def test_execute_failing_handler(self):
        def failing_handler(context):
            raise ValueError("intentional failure")

        dag = DAG()
        dag.add_node(DAGNode(id="n1", role="worker", description="test",
                             handler=failing_handler, max_retries=0))
        ctx = ExecutionContext(query="test")
        executor = DAGExecutor()
        result = executor.execute(dag, ctx)
        assert dag.nodes["n1"].status == NodeStatus.FAILED
        assert len(result.errors) > 0

    def test_conditional_edge_skip(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d"))
        dag.add_node(DAGNode(id="b", role="r", description="d"))
        dag.add_edge("a", "b", EdgeType.CONDITIONAL,
                     condition=lambda output: False)  # always skip
        ctx = ExecutionContext(query="test")
        executor = DAGExecutor()
        executor.execute(dag, ctx)
        assert dag.nodes["b"].status == NodeStatus.SKIPPED

    def test_execution_order_preserved(self):
        call_order = []

        def make_handler(name):
            def handler(ctx):
                call_order.append(name)
                return name
            return handler

        dag = DAG()
        dag.add_node(DAGNode(id="a", role="r", description="d",
                             handler=make_handler("a")))
        dag.add_node(DAGNode(id="b", role="r", description="d",
                             handler=make_handler("b")))
        dag.add_edge("a", "b")
        ctx = ExecutionContext(query="test")
        DAGExecutor().execute(dag, ctx)
        assert call_order == ["a", "b"]


# ============================================================
# DIFFICULTY ROUTER TESTS
# ============================================================

from core.difficulty_router import (
    DifficultyEstimator,
    OperatorAllocator,
    CostAwareRouter,
    DAAORouter,
    QueryDifficulty,
    OperatorType,
    DifficultySignal,
    WorkflowPlan,
)


class TestDifficultyEstimator:
    def setup_method(self):
        self.estimator = DifficultyEstimator()

    def test_trivial_query(self):
        signal = self.estimator.estimate("hello")
        assert signal.level == QueryDifficulty.TRIVIAL
        assert signal.complexity_score < 0.15

    def test_simple_query(self):
        signal = self.estimator.estimate("What is a Python function?")
        assert signal.level in (QueryDifficulty.TRIVIAL, QueryDifficulty.SIMPLE)

    def test_moderate_query(self):
        signal = self.estimator.estimate(
            "Analyze the code structure and compare different design patterns "
            "for implementing the observer pattern in the codebase."
        )
        assert signal.level.value >= QueryDifficulty.SIMPLE.value

    def test_complex_query(self):
        signal = self.estimator.estimate(
            "Step by step, analyze the market performance of tech stocks, "
            "compare financial statements across multiple quarters, then "
            "write a Python class that must compute risk-adjusted returns "
            "given these exact constraints. After that, based on the result, "
            "install and configure a monitoring dashboard."
        )
        assert signal.level.value >= QueryDifficulty.MODERATE.value

    def test_adversarial_query(self):
        signal = self.estimator.estimate(
            "Step by step, compare and analyze the stock market performance "
            "of exactly 10 companies, given their financial statements determine "
            "risk metrics, then write a Python function that must compute "
            "risk-adjusted returns. After that configure the network monitoring "
            "system and install browser automation. Compare all approaches and "
            "evaluate trade-offs across multiple dimensions with mandatory constraints. "
            "The analysis shall cover treasury valuation and portfolio optimization."
        )
        assert signal.level.value >= QueryDifficulty.COMPLEX.value

    def test_domain_detection_code(self):
        signal = self.estimator.estimate("Write a Python function with import statements")
        assert "code" in signal.domain_tags

    def test_domain_detection_web(self):
        signal = self.estimator.estimate("Open the website in a browser and take a screenshot")
        assert "web" in signal.domain_tags

    def test_domain_detection_finance(self):
        signal = self.estimator.estimate("Analyze stock market performance and treasury valuation")
        assert "finance" in signal.domain_tags

    def test_requires_tools_for_code(self):
        signal = self.estimator.estimate("Write a Python function using import and class definitions")
        assert signal.requires_tools is True

    def test_confidence_range(self):
        signal = self.estimator.estimate("test query")
        assert 0.5 <= signal.confidence <= 1.0

    def test_estimated_tokens_set(self):
        signal = self.estimator.estimate("hello")
        assert signal.estimated_tokens > 0

    def test_history_blending(self):
        q = "analyze code patterns"
        sig1 = self.estimator.estimate(q)
        score1 = sig1.complexity_score
        # Second estimation should blend with history
        sig2 = self.estimator.estimate(q)
        # History should be stored
        qhash = hashlib.md5(q.lower().encode()).hexdigest()[:8]
        assert qhash in self.estimator._history

    def test_feedback_updates_history(self):
        q = "test feedback query"
        self.estimator.estimate(q)
        self.estimator.update_from_feedback(q, QueryDifficulty.COMPLEX, True)
        qhash = hashlib.md5(q.lower().encode()).hexdigest()[:8]
        assert qhash in self.estimator._history
        updated = self.estimator._history[qhash]
        assert updated.level == QueryDifficulty.COMPLEX

    def test_empty_query(self):
        signal = self.estimator.estimate("")
        assert signal.level == QueryDifficulty.TRIVIAL


class TestOperatorAllocator:
    def setup_method(self):
        self.allocator = OperatorAllocator()

    def test_trivial_maps_to_direct(self):
        signal = DifficultySignal(
            level=QueryDifficulty.TRIVIAL, confidence=0.9,
            complexity_score=0.1, domain_tags=[], reasoning_depth=0,
            requires_tools=False, requires_debate=False, estimated_tokens=256
        )
        op, roles, cost = self.allocator.allocate(signal)
        assert op == OperatorType.DIRECT
        assert cost == 0.1

    def test_complex_maps_to_multi_agent(self):
        signal = DifficultySignal(
            level=QueryDifficulty.COMPLEX, confidence=0.8,
            complexity_score=0.7, domain_tags=["code"], reasoning_depth=3,
            requires_tools=True, requires_debate=False, estimated_tokens=2048
        )
        op, roles, cost = self.allocator.allocate(signal)
        # tools required escalates at minimum to TOOL_AUGMENTED
        assert op.value in ("tool_augmented", "multi_agent", "debate")

    def test_debate_escalation(self):
        signal = DifficultySignal(
            level=QueryDifficulty.SIMPLE, confidence=0.7,
            complexity_score=0.2, domain_tags=[], reasoning_depth=1,
            requires_tools=False, requires_debate=True, estimated_tokens=512
        )
        op, roles, cost = self.allocator.allocate(signal)
        assert op == OperatorType.DEBATE

    def test_tool_escalation(self):
        signal = DifficultySignal(
            level=QueryDifficulty.TRIVIAL, confidence=0.9,
            complexity_score=0.1, domain_tags=["code"], reasoning_depth=0,
            requires_tools=True, requires_debate=False, estimated_tokens=256
        )
        op, roles, cost = self.allocator.allocate(signal)
        assert op == OperatorType.TOOL_AUGMENTED

    def test_domain_specialists_added(self):
        signal = DifficultySignal(
            level=QueryDifficulty.MODERATE, confidence=0.8,
            complexity_score=0.4, domain_tags=["code", "web"], reasoning_depth=2,
            requires_tools=True, requires_debate=False, estimated_tokens=1024
        )
        op, roles, cost = self.allocator.allocate(signal)
        assert "code_specialist" in roles
        assert "web_specialist" in roles


class TestCostAwareRouter:
    def test_default_backends(self):
        router = CostAwareRouter()
        assert len(router.backends) > 0

    def test_budget_filtering(self):
        router = CostAwareRouter({
            "cheap": {"type": "llm", "speed": "fast", "cost": 0.1,
                     "capabilities": ["reasoning"], "max_tokens": 512},
            "expensive": {"type": "llm", "speed": "medium", "cost": 0.9,
                         "capabilities": ["reasoning", "code"], "max_tokens": 8192},
        })
        signal = DifficultySignal(
            level=QueryDifficulty.TRIVIAL, confidence=0.9,
            complexity_score=0.1, domain_tags=[], reasoning_depth=0,
            requires_tools=False, requires_debate=False, estimated_tokens=256
        )
        result = router.route(OperatorType.DIRECT, signal, budget=0.2)
        assert result == "cheap"

    def test_capability_matching(self):
        router = CostAwareRouter({
            "no_code": {"type": "llm", "speed": "fast", "cost": 0.1,
                       "capabilities": ["vision"], "max_tokens": 512},
            "with_code": {"type": "llm", "speed": "medium", "cost": 0.4,
                         "capabilities": ["reasoning", "code"], "max_tokens": 8192},
        })
        signal = DifficultySignal(
            level=QueryDifficulty.MODERATE, confidence=0.8,
            complexity_score=0.4, domain_tags=["code"], reasoning_depth=2,
            requires_tools=True, requires_debate=False, estimated_tokens=1024
        )
        result = router.route(OperatorType.TOOL_AUGMENTED, signal, budget=1.0)
        assert result == "with_code"

    def test_fallback_to_cheapest(self):
        router = CostAwareRouter({
            "only": {"type": "llm", "speed": "fast", "cost": 5.0,
                    "capabilities": [], "max_tokens": 512},
        })
        signal = DifficultySignal(
            level=QueryDifficulty.TRIVIAL, confidence=0.9,
            complexity_score=0.1, domain_tags=[], reasoning_depth=0,
            requires_tools=False, requires_debate=False, estimated_tokens=256
        )
        result = router.route(OperatorType.DIRECT, signal, budget=0.1)
        assert result == "only"  # fallback


class TestDAAORouter:
    def test_route_returns_workflow_plan(self):
        router = DAAORouter()
        plan = router.route("hello world")
        assert isinstance(plan, WorkflowPlan)
        assert plan.query == "hello world"

    def test_route_difficulty_matches(self):
        router = DAAORouter()
        plan = router.route("hello")
        assert plan.difficulty.level == QueryDifficulty.TRIVIAL

    def test_route_stores_history(self):
        router = DAAORouter()
        router.route("test query 1")
        router.route("test query 2")
        assert len(router._plan_history) == 2

    def test_stats_empty(self):
        router = DAAORouter()
        assert router.stats["total_plans"] == 0

    def test_stats_after_routes(self):
        router = DAAORouter()
        router.route("hello")
        router.route("analyze complex code with multiple steps")
        stats = router.stats
        assert stats["total_plans"] == 2
        assert "difficulty_distribution" in stats

    def test_feedback_method(self):
        router = DAAORouter()
        router.route("test")
        router.feedback("test", QueryDifficulty.MODERATE, True)
        # Should not raise

    def test_tool_selection(self):
        router = DAAORouter()
        plan = router.route("Write a Python function to process data")
        # Code domain should trigger code tools
        if "code" in plan.difficulty.domain_tags:
            assert any("code" in t or "file" in t for t in plan.tool_bindings)


# ============================================================
# DATABASE TESTS
# ============================================================

from core.database import ScreenMemoryDB, ScreenRecord


class TestDatabaseCRUD:
    @pytest.fixture
    def db(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        db_inst = ScreenMemoryDB(db_path)
        yield db_inst
        db_inst.close()

    def test_insert_and_retrieve(self, db):
        record = ScreenRecord(
            timestamp=time.time(),
            monitor_index=0,
            width=1920, height=1080,
            dhash="abc123",
            active_window_title="Test Window",
            active_process="test.exe",
            analysis_text="Test analysis",
            ocr_text="Test OCR text",
        )
        rid = db.insert_capture(record)
        assert rid > 0
        assert record.id == rid

    def test_get_recent(self, db):
        for i in range(5):
            db.insert_capture(ScreenRecord(
                timestamp=time.time() + i,
                active_window_title=f"Window {i}",
                active_process="test.exe",
            ))
        recent = db.get_recent(limit=3)
        assert len(recent) == 3

    def test_get_by_process(self, db):
        db.insert_capture(ScreenRecord(
            timestamp=time.time(),
            active_process="chrome.exe",
            active_window_title="Chrome",
        ))
        db.insert_capture(ScreenRecord(
            timestamp=time.time(),
            active_process="code.exe",
            active_window_title="VS Code",
        ))
        results = db.get_by_process("chrome.exe")
        assert len(results) >= 1

    def test_get_by_timerange(self, db):
        now = time.time()
        db.insert_capture(ScreenRecord(timestamp=now - 100, active_process="a"))
        db.insert_capture(ScreenRecord(timestamp=now, active_process="b"))
        db.insert_capture(ScreenRecord(timestamp=now + 100, active_process="c"))
        results = db.get_by_timerange(now - 50, now + 50)
        assert len(results) == 1

    def test_get_stats(self, db):
        db.insert_capture(ScreenRecord(timestamp=time.time(), active_process="test"))
        stats = db.get_stats()
        assert stats["total_captures"] >= 1
        assert "db_size_mb" in stats

    def test_context_manager(self, tmp_path):
        db_path = str(tmp_path / "ctx_test.db")
        with ScreenMemoryDB(db_path) as db:
            db.insert_capture(ScreenRecord(timestamp=time.time(), active_process="test"))


class TestDatabaseSearch:
    @pytest.fixture
    def populated_db(self, tmp_path):
        db_path = str(tmp_path / "search_test.db")
        db = ScreenMemoryDB(db_path)
        db.insert_capture(ScreenRecord(
            timestamp=time.time(),
            active_process="chrome.exe",
            active_window_title="Google Chrome",
            analysis_text="User browsing email inbox on Gmail",
            ocr_text="From: sender@example.com Subject: Meeting",
        ))
        db.insert_capture(ScreenRecord(
            timestamp=time.time(),
            active_process="code.exe",
            active_window_title="VS Code - main.py",
            analysis_text="Developer editing Python code in VS Code",
            ocr_text="def process_data(input): return result",
        ))
        yield db
        db.close()

    def test_text_search(self, populated_db):
        results = populated_db.search_text("Python")
        assert len(results) >= 1

    def test_text_search_empty(self, populated_db):
        results = populated_db.search_text("nonexistent_xyzzy_term")
        assert len(results) == 0

    def test_text_search_special_chars(self, populated_db):
        # Should not crash on special chars
        results = populated_db.search_text("test@#$%")
        assert isinstance(results, list)

    def test_semantic_search_without_vec(self, populated_db):
        # Without sqlite-vec, should return empty
        if not populated_db._vec_available:
            results = populated_db.search_semantic(b"\x00" * 768)
            assert results == []

    def test_hybrid_search(self, populated_db):
        results = populated_db.search_hybrid("Python code")
        assert isinstance(results, list)


class TestDatabaseMaintenance:
    def test_cleanup_old(self, tmp_path):
        db_path = str(tmp_path / "cleanup_test.db")
        db = ScreenMemoryDB(db_path)
        old_ts = time.time() - (200 * 86400)  # 200 days ago
        db.insert_capture(ScreenRecord(timestamp=old_ts, active_process="old"))
        db.insert_capture(ScreenRecord(timestamp=time.time(), active_process="new"))
        deleted = db.cleanup_old(retention_days=90)
        assert deleted >= 1
        remaining = db.get_recent(limit=100)
        assert len(remaining) == 1
        db.close()


class TestDatabaseConcurrency:
    def test_wal_mode_enabled(self, tmp_path):
        db_path = str(tmp_path / "wal_test.db")
        db = ScreenMemoryDB(db_path)
        mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        db.close()

    def test_multiple_inserts(self, tmp_path):
        db_path = str(tmp_path / "multi_test.db")
        db = ScreenMemoryDB(db_path)
        for i in range(50):
            db.insert_capture(ScreenRecord(
                timestamp=time.time() + i * 0.001,
                active_process=f"proc_{i}",
            ))
        stats = db.get_stats()
        assert stats["total_captures"] == 50
        db.close()


# ============================================================
# DAG BUILDER TESTS
# ============================================================

class TestDAGBuilder:
    def setup_method(self):
        self.builder = DAGBuilder()

    def _make_plan(self, operator, roles=None, tools=None, domain_tags=None):
        signal = DifficultySignal(
            level=QueryDifficulty.MODERATE, confidence=0.8,
            complexity_score=0.5, domain_tags=domain_tags or [],
            reasoning_depth=2, requires_tools=False,
            requires_debate=False, estimated_tokens=1024
        )
        return WorkflowPlan(
            query="test",
            difficulty=signal,
            operator=operator,
            backend="test-model",
            agent_roles=roles or [],
            tool_bindings=tools or [],
            max_iterations=3,
            token_budget=1024,
            cost_estimate=0.5,
            timestamp=time.time(),
        )

    def test_direct_operator(self):
        plan = self._make_plan(OperatorType.DIRECT)
        dag = self.builder.from_workflow_plan(plan)
        assert len(dag.nodes) >= 1

    def test_chain_of_thought(self):
        plan = self._make_plan(OperatorType.CHAIN_OF_THOUGHT, roles=["reasoner"])
        dag = self.builder.from_workflow_plan(plan)
        assert len(dag.nodes) >= 2
        assert len(dag.edges) >= 1

    def test_multi_agent_has_specialists(self):
        plan = self._make_plan(
            OperatorType.MULTI_AGENT,
            roles=["planner", "specialist", "validator"]
        )
        dag = self.builder.from_workflow_plan(plan)
        assert len(dag.nodes) >= 3

    def test_debate_has_feedback_loop(self):
        plan = self._make_plan(
            OperatorType.DEBATE,
            roles=["proposer", "critic", "judge"]
        )
        dag = self.builder.from_workflow_plan(plan)
        feedback_edges = [e for e in dag.edges if e.edge_type == EdgeType.FEEDBACK]
        assert len(feedback_edges) >= 1


# ============================================================
# ORCHESTRATOR TESTS (with mocks)
# ============================================================

class TestOrchestrator:
    @pytest.fixture
    def orch(self):
        with patch.dict('sys.modules', {
            'core.hybrid_retrieval': MagicMock(),
            'core.agent_factory': MagicMock(),
        }):
            from core.orchestrator import Orchestrator
            o = Orchestrator.__new__(Orchestrator)
            o.router = MagicMock()
            o.factory = MagicMock()
            o.dag_builder = MagicMock()
            o.executor = MagicMock()
            o.retriever = MagicMock()
            o.retriever.bm25 = MagicMock()
            o.retriever.bm25.size = 0
            o.guard = MagicMock()
            o._planner = None
            o._reflexion = None
            o._episodic_memory = None
            o._lance_store = None
            o._memory = None
            o._guardian = None
            o._history = []
            o._total_queries = 0
            o._total_blocked = 0
            o._sop_registry = {}
            o._init_time = time.time()
            return o

    def test_process_blocked_query(self, orch):
        scan_result = MagicMock()
        scan_result.blocked = True
        scan_result.score = 0.95
        scan_result.triggers = ["injection"]
        scan_result.sanitized_input = ""
        orch.guard.scan.return_value = scan_result

        result = orch.process("DROP TABLE users; --")
        assert result["status"] == "blocked"
        assert orch._total_blocked == 1

    def test_process_successful_query(self, orch):
        # Setup guard to pass
        scan_result = MagicMock()
        scan_result.blocked = False
        scan_result.sanitized_input = "test query"
        orch.guard.scan.return_value = scan_result

        # Setup router
        plan = MagicMock()
        plan.difficulty = MagicMock()
        plan.difficulty.level = MagicMock()
        plan.difficulty.level.name = "SIMPLE"
        plan.difficulty.level.value = 2
        plan.operator = MagicMock()
        plan.operator.value = "direct"
        plan.agent_roles = ["worker"]
        plan.backend = "test"
        plan.tool_bindings = []
        orch.router.route.return_value = plan

        # Setup retriever
        orch.retriever.search.return_value = []

        # Setup factory
        team = MagicMock()
        orch.factory.create_team.return_value = team

        # Setup DAG
        dag = MagicMock()
        dag.stats = {"nodes": 1}
        orch.dag_builder.from_workflow_plan.return_value = dag

        # Setup executor
        exec_ctx = MagicMock()
        exec_ctx.errors = []
        exec_ctx.node_outputs = {"n1": {"output": "test result"}}
        orch.executor.execute.return_value = exec_ctx

        result = orch.process("test query")
        assert result["status"] in ("success", "partial", "processing")
        assert orch._total_queries == 1

    def test_sop_matching(self, orch):
        orch._sop_registry["abc123"] = {
            "name": "test pattern",
            "keywords": ["analyze", "code", "patterns", "structure"],
            "operator": "cot",
        }
        match = orch._find_matching_sop("analyze the code patterns and structure")
        assert match is not None

    def test_sop_no_match(self, orch):
        orch._sop_registry["abc123"] = {
            "name": "test pattern",
            "keywords": ["analyze", "code", "patterns", "structure"],
            "operator": "cot",
        }
        match = orch._find_matching_sop("hello world")
        assert match is None

    def test_stats_property(self, orch):
        stats = orch.stats
        assert "total_queries" in stats
        assert "total_blocked" in stats


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
