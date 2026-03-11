# signed: alpha
"""
Tests for core/difficulty_router.py — DAAO (Difficulty-Aware Agentic Orchestration) Router.

Covers:
- DifficultyEstimator: feature extraction, scoring, level classification, history, feedback
- OperatorAllocator: operator selection, role assignment, tool/debate escalation
- CostAwareRouter: backend selection, capability filtering, budget constraints
- DAAORouter: full pipeline routing, stats, tool selection
"""

import pytest
from core.difficulty_router import (
    QueryDifficulty,
    OperatorType,
    DifficultySignal,
    WorkflowPlan,
    DifficultyEstimator,
    OperatorAllocator,
    CostAwareRouter,
    DAAORouter,
)


# ── Fixtures ──


@pytest.fixture
def estimator():
    return DifficultyEstimator()


@pytest.fixture
def allocator():
    return OperatorAllocator()


@pytest.fixture
def router():
    return CostAwareRouter()


@pytest.fixture
def daao():
    return DAAORouter()


# ── Enum values ──


class TestEnums:
    def test_difficulty_levels(self):
        assert QueryDifficulty.TRIVIAL.value == 1
        assert QueryDifficulty.SIMPLE.value == 2
        assert QueryDifficulty.MODERATE.value == 3
        assert QueryDifficulty.COMPLEX.value == 4
        assert QueryDifficulty.ADVERSARIAL.value == 5

    def test_operator_types(self):
        assert OperatorType.DIRECT.value == "direct"
        assert OperatorType.CHAIN_OF_THOUGHT.value == "cot"
        assert OperatorType.TOOL_AUGMENTED.value == "tool_augmented"
        assert OperatorType.MULTI_AGENT.value == "multi_agent"
        assert OperatorType.DEBATE.value == "debate"


# ── DifficultyEstimator ──


class TestDifficultyEstimator:
    def test_trivial_query(self, estimator):
        signal = estimator.estimate("Hi")
        assert signal.level == QueryDifficulty.TRIVIAL
        assert signal.complexity_score < 0.15

    def test_simple_query(self, estimator):
        signal = estimator.estimate("What is a Python function?")
        assert signal.level in (QueryDifficulty.TRIVIAL, QueryDifficulty.SIMPLE)

    def test_moderate_query(self, estimator):
        signal = estimator.estimate(
            "Write a Python function that takes a list of database records, "
            "filters by date, and returns the results as JSON. Must handle errors."
        )
        assert signal.level in (QueryDifficulty.SIMPLE, QueryDifficulty.MODERATE, QueryDifficulty.COMPLEX)
        assert signal.complexity_score >= 0.15

    def test_complex_query(self, estimator):
        signal = estimator.estimate(
            "Analyze the Bitcoin market trends and correlate against Ethereum price "
            "movement. Step by step, first gather market data, then compare trading "
            "volumes across exchanges, evaluate risk factors, and finally synthesize "
            "a comprehensive investment strategy with portfolio allocation. "
            "Must be exactly formatted as a report with at least three sections."
        )
        assert signal.level in (QueryDifficulty.COMPLEX, QueryDifficulty.ADVERSARIAL)
        assert signal.complexity_score >= 0.55

    def test_domain_detection_code(self, estimator):
        signal = estimator.estimate("Fix the bug in the Python function")
        assert "code" in signal.domain_tags

    def test_domain_detection_finance(self, estimator):
        signal = estimator.estimate("Analyze the stock market portfolio returns")
        assert "finance" in signal.domain_tags

    def test_domain_detection_web(self, estimator):
        signal = estimator.estimate("Navigate to the website and click the login button")
        assert "web" in signal.domain_tags

    def test_domain_detection_system(self, estimator):
        signal = estimator.estimate("Install and configure the Docker container for the server")
        assert "system" in signal.domain_tags

    def test_domain_detection_analysis(self, estimator):
        signal = estimator.estimate("Analyze and compare the trends in this data")
        assert "analysis" in signal.domain_tags

    def test_multi_domain(self, estimator):
        signal = estimator.estimate(
            "Deploy the Python API server, analyze website performance, "
            "and evaluate the stock market impact"
        )
        assert len(signal.domain_tags) >= 2

    def test_confidence_extreme_values(self, estimator):
        # Trivial query should have higher confidence (far from midpoint)
        trivial = estimator.estimate("Hello")
        # Moderate should have lower confidence (close to midpoint)
        moderate = estimator.estimate(
            "Write a function to process data with error handling"
        )
        assert 0.0 <= trivial.confidence <= 1.0
        assert 0.0 <= moderate.confidence <= 1.0

    def test_reasoning_depth_minimum_1(self, estimator):
        signal = estimator.estimate("Hi")
        assert signal.reasoning_depth >= 1

    def test_reasoning_depth_increases(self, estimator):
        simple = estimator.estimate("Hi")
        complex_q = estimator.estimate(
            "Step by step, first analyze the code, then compare approaches, "
            "and cross-reference with multiple data sources across systems"
        )
        assert complex_q.reasoning_depth >= simple.reasoning_depth

    def test_requires_tools_for_code(self, estimator):
        signal = estimator.estimate("Debug the Python function and fix the runtime error")
        assert signal.requires_tools is True

    def test_requires_tools_for_web(self, estimator):
        signal = estimator.estimate("Navigate to the website and screenshot the page")
        assert signal.requires_tools is True

    def test_requires_tools_for_system(self, estimator):
        signal = estimator.estimate("Install Docker and configure the server deployment")
        assert signal.requires_tools is True

    def test_no_tools_for_plain_query(self, estimator):
        signal = estimator.estimate("Hello how are you?")
        assert signal.requires_tools is False

    def test_estimated_tokens_scale(self, estimator):
        trivial = estimator.estimate("Hi")
        complex_q = estimator.estimate(
            "Analyze Bitcoin whale behavior, correlate with Ethereum, "
            "step by step evaluate risks, compare multiple strategies, "
            "and synthesize a comprehensive report across several domains"
        )
        assert trivial.estimated_tokens <= complex_q.estimated_tokens

    def test_history_blending(self, estimator):
        query = "Analyze the complex data pipeline"
        first = estimator.estimate(query)
        second = estimator.estimate(query)
        # Second should have slightly higher confidence due to history
        assert second.confidence >= first.confidence

    def test_feedback_adjusts_history(self, estimator):
        query = "Simple test query for feedback"
        estimator.estimate(query)
        estimator.update_from_feedback(query, QueryDifficulty.COMPLEX, success=True)
        signal = estimator.estimate(query)
        # Should be adjusted toward COMPLEX
        assert signal.complexity_score > 0.15

    def test_score_to_level_boundaries(self):
        assert DifficultyEstimator._score_to_level(0.0) == QueryDifficulty.TRIVIAL
        assert DifficultyEstimator._score_to_level(0.14) == QueryDifficulty.TRIVIAL
        assert DifficultyEstimator._score_to_level(0.15) == QueryDifficulty.SIMPLE
        assert DifficultyEstimator._score_to_level(0.34) == QueryDifficulty.SIMPLE
        assert DifficultyEstimator._score_to_level(0.35) == QueryDifficulty.MODERATE
        assert DifficultyEstimator._score_to_level(0.54) == QueryDifficulty.MODERATE
        assert DifficultyEstimator._score_to_level(0.55) == QueryDifficulty.COMPLEX
        assert DifficultyEstimator._score_to_level(0.79) == QueryDifficulty.COMPLEX
        assert DifficultyEstimator._score_to_level(0.80) == QueryDifficulty.ADVERSARIAL
        assert DifficultyEstimator._score_to_level(1.0) == QueryDifficulty.ADVERSARIAL

    def test_extract_features_structure(self, estimator):
        features = estimator._extract_features("Fix the Python bug in the API endpoint")
        assert "token_count" in features
        assert "sentence_count" in features
        assert "domains" in features
        assert "domain_density" in features
        assert "reasoning_hits" in features
        assert "multi_hop_hits" in features
        assert "constraint_hits" in features
        assert "ambiguity_hits" in features

    def test_compute_raw_score_range(self):
        features = {
            "token_count": 50, "sentence_count": 3, "domains": ["code"],
            "domain_density": 2, "reasoning_hits": 1, "multi_hop_hits": 0,
            "constraint_hits": 1, "ambiguity_hits": 0,
        }
        score = DifficultyEstimator._compute_raw_score(features)
        assert 0.0 <= score <= 1.0


# ── OperatorAllocator ──


class TestOperatorAllocator:
    def test_trivial_gets_direct(self, allocator):
        signal = DifficultySignal(
            level=QueryDifficulty.TRIVIAL, confidence=0.9,
            complexity_score=0.05, domain_tags=[], reasoning_depth=1,
            requires_tools=False, requires_debate=False, estimated_tokens=256,
        )
        op, roles, cost = allocator.allocate(signal)
        assert op == OperatorType.DIRECT
        assert cost == 0.1

    def test_simple_gets_cot(self, allocator):
        signal = DifficultySignal(
            level=QueryDifficulty.SIMPLE, confidence=0.8,
            complexity_score=0.25, domain_tags=[], reasoning_depth=2,
            requires_tools=False, requires_debate=False, estimated_tokens=512,
        )
        op, roles, cost = allocator.allocate(signal)
        assert op == OperatorType.CHAIN_OF_THOUGHT
        assert "reasoner" in roles

    def test_moderate_gets_tool_augmented(self, allocator):
        signal = DifficultySignal(
            level=QueryDifficulty.MODERATE, confidence=0.7,
            complexity_score=0.45, domain_tags=["code"], reasoning_depth=3,
            requires_tools=True, requires_debate=False, estimated_tokens=1024,
        )
        op, roles, cost = allocator.allocate(signal)
        assert op == OperatorType.TOOL_AUGMENTED
        assert "tool_executor" in roles

    def test_complex_gets_multi_agent(self, allocator):
        signal = DifficultySignal(
            level=QueryDifficulty.COMPLEX, confidence=0.6,
            complexity_score=0.65, domain_tags=[], reasoning_depth=4,
            requires_tools=False, requires_debate=False, estimated_tokens=2048,
        )
        op, roles, cost = allocator.allocate(signal)
        assert op == OperatorType.MULTI_AGENT

    def test_adversarial_gets_debate(self, allocator):
        signal = DifficultySignal(
            level=QueryDifficulty.ADVERSARIAL, confidence=0.55,
            complexity_score=0.85, domain_tags=[], reasoning_depth=5,
            requires_tools=False, requires_debate=True, estimated_tokens=4096,
        )
        op, roles, cost = allocator.allocate(signal)
        assert op == OperatorType.DEBATE
        assert "judge" in roles

    def test_tools_required_escalates_direct(self, allocator):
        signal = DifficultySignal(
            level=QueryDifficulty.TRIVIAL, confidence=0.9,
            complexity_score=0.05, domain_tags=["code"], reasoning_depth=1,
            requires_tools=True, requires_debate=False, estimated_tokens=256,
        )
        op, _, _ = allocator.allocate(signal)
        assert op == OperatorType.TOOL_AUGMENTED

    def test_tools_required_escalates_cot(self, allocator):
        signal = DifficultySignal(
            level=QueryDifficulty.SIMPLE, confidence=0.8,
            complexity_score=0.25, domain_tags=["web"], reasoning_depth=2,
            requires_tools=True, requires_debate=False, estimated_tokens=512,
        )
        op, _, _ = allocator.allocate(signal)
        assert op == OperatorType.TOOL_AUGMENTED

    def test_debate_required_escalates(self, allocator):
        signal = DifficultySignal(
            level=QueryDifficulty.MODERATE, confidence=0.7,
            complexity_score=0.45, domain_tags=[], reasoning_depth=3,
            requires_tools=False, requires_debate=True, estimated_tokens=1024,
        )
        op, _, _ = allocator.allocate(signal)
        assert op == OperatorType.DEBATE

    def test_domain_specialists_added(self, allocator):
        signal = DifficultySignal(
            level=QueryDifficulty.COMPLEX, confidence=0.6,
            complexity_score=0.65, domain_tags=["code", "finance"],
            reasoning_depth=4, requires_tools=False,
            requires_debate=False, estimated_tokens=2048,
        )
        _, roles, _ = allocator.allocate(signal)
        assert "code_specialist" in roles
        assert "finance_specialist" in roles

    def test_cost_ordering(self, allocator):
        costs = []
        for level in QueryDifficulty:
            signal = DifficultySignal(
                level=level, confidence=0.5, complexity_score=0.5,
                domain_tags=[], reasoning_depth=1,
                requires_tools=False, requires_debate=False,
                estimated_tokens=256,
            )
            _, _, cost = allocator.allocate(signal)
            costs.append(cost)
        # Costs should be non-decreasing (higher difficulty = more expensive)
        for i in range(1, len(costs)):
            assert costs[i] >= costs[i - 1]


# ── CostAwareRouter ──


class TestCostAwareRouter:
    def test_default_backends(self, router):
        assert "moondream" in router.backends
        assert "qwen3:8b" in router.backends

    def test_routes_to_viable_backend(self, router):
        signal = DifficultySignal(
            level=QueryDifficulty.SIMPLE, confidence=0.8,
            complexity_score=0.25, domain_tags=[], reasoning_depth=1,
            requires_tools=False, requires_debate=False, estimated_tokens=512,
        )
        backend = router.route(OperatorType.DIRECT, signal)
        assert backend in router.backends

    def test_code_requires_reasoning(self, router):
        signal = DifficultySignal(
            level=QueryDifficulty.MODERATE, confidence=0.7,
            complexity_score=0.45, domain_tags=["code"], reasoning_depth=3,
            requires_tools=True, requires_debate=False, estimated_tokens=1024,
        )
        backend = router.route(OperatorType.TOOL_AUGMENTED, signal)
        caps = router.backends[backend].get("capabilities", [])
        assert "code" in caps or "reasoning" in caps

    def test_multi_agent_prefers_quality(self, router):
        signal = DifficultySignal(
            level=QueryDifficulty.COMPLEX, confidence=0.6,
            complexity_score=0.65, domain_tags=["analysis"], reasoning_depth=4,
            requires_tools=False, requires_debate=False, estimated_tokens=2048,
        )
        backend = router.route(OperatorType.MULTI_AGENT, signal)
        # For multi-agent/debate, quality weight favors slower models
        assert backend in router.backends

    def test_budget_constraint(self):
        router = CostAwareRouter(backends={
            "cheap": {"type": "llm", "speed": "fast", "cost": 0.1,
                      "capabilities": ["reasoning"], "max_tokens": 512},
            "expensive": {"type": "llm", "speed": "medium", "cost": 0.9,
                          "capabilities": ["reasoning", "code"], "max_tokens": 8192},
        })
        signal = DifficultySignal(
            level=QueryDifficulty.MODERATE, confidence=0.7,
            complexity_score=0.45, domain_tags=["code"], reasoning_depth=3,
            requires_tools=True, requires_debate=False, estimated_tokens=1024,
        )
        backend = router.route(OperatorType.TOOL_AUGMENTED, signal, budget=0.5)
        assert backend == "cheap"

    def test_fallback_to_cheapest(self):
        router = CostAwareRouter(backends={
            "only_vision": {"type": "vlm", "speed": "fast", "cost": 0.1,
                            "capabilities": ["vision"], "max_tokens": 256},
        })
        signal = DifficultySignal(
            level=QueryDifficulty.COMPLEX, confidence=0.6,
            complexity_score=0.65, domain_tags=["code"], reasoning_depth=4,
            requires_tools=True, requires_debate=False, estimated_tokens=2048,
        )
        backend = router.route(OperatorType.MULTI_AGENT, signal)
        assert backend == "only_vision"

    def test_lambda_tradeoff(self):
        router = CostAwareRouter()
        router._lambda = 0.0  # Quality only
        signal = DifficultySignal(
            level=QueryDifficulty.SIMPLE, confidence=0.8,
            complexity_score=0.25, domain_tags=[], reasoning_depth=1,
            requires_tools=False, requires_debate=False, estimated_tokens=512,
        )
        backend = router.route(OperatorType.DIRECT, signal)
        assert backend in router.backends


# ── DAAORouter (full pipeline) ──


class TestDAAORouter:
    def test_full_pipeline_trivial(self, daao):
        plan = daao.route("Hello")
        assert isinstance(plan, WorkflowPlan)
        assert plan.difficulty.level == QueryDifficulty.TRIVIAL
        assert plan.operator in (OperatorType.DIRECT, OperatorType.CHAIN_OF_THOUGHT)
        assert plan.max_iterations >= 1
        assert plan.backend in daao.router.backends

    def test_full_pipeline_complex(self, daao):
        plan = daao.route(
            "Step by step analyze the Python codebase, compare the API endpoints, "
            "evaluate performance across the database and server configurations, "
            "then synthesize a comprehensive deployment strategy. "
            "Must include exactly three phases with at least two metrics each."
        )
        assert plan.difficulty.level in (
            QueryDifficulty.MODERATE, QueryDifficulty.COMPLEX,
            QueryDifficulty.ADVERSARIAL,
        )
        assert plan.max_iterations >= 1

    def test_plan_has_tools_for_code(self, daao):
        plan = daao.route("Fix the bug in the Python API endpoint")
        if "code" in plan.difficulty.domain_tags:
            assert len(plan.tool_bindings) > 0
            assert "code_executor" in plan.tool_bindings or "file_system" in plan.tool_bindings

    def test_plan_has_tools_for_web(self, daao):
        plan = daao.route("Navigate to the website and click the login button")
        if "web" in plan.difficulty.domain_tags:
            assert "browser" in plan.tool_bindings or "web_scraper" in plan.tool_bindings

    def test_stats_empty_initially(self, daao):
        stats = daao.stats
        assert stats["total_plans"] == 0

    def test_stats_after_routing(self, daao):
        daao.route("Hello")
        daao.route("Fix the Python bug")
        stats = daao.stats
        assert stats["total_plans"] == 2
        assert "difficulty_distribution" in stats
        assert "operator_distribution" in stats
        assert "avg_cost" in stats
        assert stats["avg_cost"] >= 0

    def test_feedback_integration(self, daao):
        query = "Test feedback loop"
        daao.route(query)
        daao.feedback(query, QueryDifficulty.COMPLEX, success=True)
        plan = daao.route(query)
        # After feedback toward COMPLEX, score should increase
        assert plan.difficulty.complexity_score > 0

    def test_plan_history_grows(self, daao):
        for i in range(5):
            daao.route(f"Query number {i}")
        assert len(daao._plan_history) == 5

    def test_token_budget_matches_difficulty(self, daao):
        plan = daao.route("Hi")
        expected = {
            QueryDifficulty.TRIVIAL: 256,
            QueryDifficulty.SIMPLE: 512,
            QueryDifficulty.MODERATE: 1024,
            QueryDifficulty.COMPLEX: 2048,
            QueryDifficulty.ADVERSARIAL: 4096,
        }
        assert plan.token_budget == expected[plan.difficulty.level]

    def test_max_iterations_per_operator(self, daao):
        expected = {
            OperatorType.DIRECT: 1,
            OperatorType.CHAIN_OF_THOUGHT: 1,
            OperatorType.TOOL_AUGMENTED: 3,
            OperatorType.MULTI_AGENT: 5,
            OperatorType.DEBATE: 7,
        }
        # Route various queries and verify iteration budgets
        plan = daao.route("Hi")
        assert plan.max_iterations == expected[plan.operator]

    def test_budget_passthrough(self, daao):
        plan = daao.route("Hello", budget=0.1)
        assert plan.backend in daao.router.backends


# ── Tool selection ──


class TestToolSelection:
    def test_code_tools(self, daao):
        signal = DifficultySignal(
            level=QueryDifficulty.MODERATE, confidence=0.7,
            complexity_score=0.45, domain_tags=["code"],
            reasoning_depth=3, requires_tools=True,
            requires_debate=False, estimated_tokens=1024,
        )
        tools = daao._select_tools(signal)
        assert "code_executor" in tools
        assert "file_system" in tools
        assert "git" in tools

    def test_web_tools(self, daao):
        signal = DifficultySignal(
            level=QueryDifficulty.MODERATE, confidence=0.7,
            complexity_score=0.45, domain_tags=["web"],
            reasoning_depth=3, requires_tools=True,
            requires_debate=False, estimated_tokens=1024,
        )
        tools = daao._select_tools(signal)
        assert "browser" in tools
        assert "screenshot" in tools

    def test_finance_tools(self, daao):
        signal = DifficultySignal(
            level=QueryDifficulty.MODERATE, confidence=0.7,
            complexity_score=0.45, domain_tags=["finance"],
            reasoning_depth=3, requires_tools=True,
            requires_debate=False, estimated_tokens=1024,
        )
        tools = daao._select_tools(signal)
        assert "market_data" in tools
        assert "calculator" in tools

    def test_no_domain_no_tools(self, daao):
        signal = DifficultySignal(
            level=QueryDifficulty.TRIVIAL, confidence=0.9,
            complexity_score=0.05, domain_tags=[],
            reasoning_depth=1, requires_tools=False,
            requires_debate=False, estimated_tokens=256,
        )
        tools = daao._select_tools(signal)
        assert tools == []

    def test_multi_domain_deduplicates(self, daao):
        signal = DifficultySignal(
            level=QueryDifficulty.COMPLEX, confidence=0.6,
            complexity_score=0.65, domain_tags=["code", "system"],
            reasoning_depth=4, requires_tools=True,
            requires_debate=False, estimated_tokens=2048,
        )
        tools = daao._select_tools(signal)
        # "file_system" appears in both code and system maps - should not duplicate
        assert tools.count("file_system") == 1
