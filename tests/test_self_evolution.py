#!/usr/bin/env python3
"""Tests for core/self_evolution.py — genetic algorithm strategy evolution.

Tests cover: StrategyGene dataclass, PerformanceTracker (record, rates,
fitness), EvolutionEngine (mutate, crossover, select, evolve), SelfReflector
(failures, bottlenecks, proposals), EvolutionDashboard, SelfEvolutionSystem.

# signed: alpha
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── StrategyGene Tests ───────────────────────────────────────────

class TestStrategyGene:
    """Test StrategyGene dataclass."""

    def test_creation(self):
        from core.self_evolution import StrategyGene
        g = StrategyGene(
            strategy_id="s1", name="test", category="code",
            parameters={"cot_depth": 3, "temperature": 0.5}
        )
        assert g.strategy_id == "s1"
        assert g.category == "code"
        assert g.fitness_score == 0.0
        assert g.generation == 0
        assert g.task_count == 0

    def test_to_dict(self):
        from core.self_evolution import StrategyGene
        g = StrategyGene(
            strategy_id="s1", name="test", category="code",
            parameters={"cot_depth": 3}
        )
        d = g.to_dict()
        assert d["strategy_id"] == "s1"
        assert "parameters" in d

    def test_from_dict_roundtrip(self):
        from core.self_evolution import StrategyGene
        g = StrategyGene(
            strategy_id="s1", name="test", category="research",
            parameters={"temperature": 0.7}, fitness_score=0.85,
            generation=3, task_count=10
        )
        d = g.to_dict()
        g2 = StrategyGene.from_dict(d)
        assert g2.strategy_id == g.strategy_id
        assert g2.category == g.category
        assert g2.fitness_score == g.fitness_score
        assert g2.generation == g.generation


# ── PerformanceTracker Tests ─────────────────────────────────────

class TestPerformanceTracker:
    """Test PerformanceTracker SQLite recording and queries."""

    def _make_tracker(self, tmp_path):
        from core.self_evolution import PerformanceTracker
        db = str(tmp_path / "test_evolution.db")
        return PerformanceTracker(db)

    def _make_metrics(self, **overrides):
        from core.self_evolution import TaskMetrics
        defaults = dict(
            task_id="t1", category="code", strategy_id="s1",
            success=True, latency_ms=500.0, quality_score=0.8,
            tokens_used=100, memory_hits=5, memory_queries=10
        )
        defaults.update(overrides)
        return TaskMetrics(**defaults)

    def test_record_and_success_rate(self, tmp_path):
        tracker = self._make_tracker(tmp_path)
        for i in range(10):
            tracker.record(self._make_metrics(
                task_id=f"t{i}", success=(i < 7)  # 70% success
            ))
        rate = tracker.get_success_rate("code", window=10)
        assert abs(rate - 0.7) < 0.01

    def test_success_rate_no_data(self, tmp_path):
        tracker = self._make_tracker(tmp_path)
        rate = tracker.get_success_rate("code")
        assert rate == 0.0

    def test_latency_percentiles(self, tmp_path):
        tracker = self._make_tracker(tmp_path)
        for i in range(20):
            tracker.record(self._make_metrics(
                task_id=f"t{i}", latency_ms=float(i * 100)
            ))
        p = tracker.get_latency_percentiles("code", window=20)
        assert "p50" in p
        assert "p95" in p
        assert "p99" in p
        assert p["p50"] < p["p95"]

    def test_memory_hit_rate(self, tmp_path):
        tracker = self._make_tracker(tmp_path)
        tracker.record(self._make_metrics(memory_hits=8, memory_queries=10))
        rate = tracker.get_memory_hit_rate("code", window=10)
        assert abs(rate - 0.8) < 0.01

    def test_memory_hit_rate_zero_queries(self, tmp_path):
        tracker = self._make_tracker(tmp_path)
        tracker.record(self._make_metrics(memory_hits=0, memory_queries=0))
        rate = tracker.get_memory_hit_rate("code", window=10)
        assert rate == 0.0

    def test_strategy_fitness(self, tmp_path):
        tracker = self._make_tracker(tmp_path)
        for i in range(5):
            tracker.record(self._make_metrics(
                task_id=f"t{i}", strategy_id="s1",
                success=True, quality_score=0.9, latency_ms=1000.0
            ))
        fitness = tracker.get_strategy_fitness("s1", window=5)
        assert 0.0 <= fitness <= 1.0
        assert fitness > 0.5  # All success + good quality

    def test_strategy_fitness_no_data(self, tmp_path):
        tracker = self._make_tracker(tmp_path)
        fitness = tracker.get_strategy_fitness("nonexistent")
        assert fitness == 0.5  # Default

    def test_recent_failures(self, tmp_path):
        tracker = self._make_tracker(tmp_path)
        tracker.record(self._make_metrics(task_id="t1", success=False, error_type="timeout"))
        tracker.record(self._make_metrics(task_id="t2", success=True))
        failures = tracker.get_recent_failures(limit=10)
        assert len(failures) == 1
        assert failures[0]["task_id"] == "t1"


# ── EvolutionEngine Tests ────────────────────────────────────────

class TestEvolutionEngine:
    """Test EvolutionEngine mutation, crossover, selection."""

    def _make_engine(self, tmp_path):
        from core.self_evolution import PerformanceTracker, EvolutionEngine
        db = str(tmp_path / "test_evo.db")
        tracker = PerformanceTracker(db)
        engine = EvolutionEngine(db, tracker)
        return engine, tracker

    def test_mutate_preserves_category(self, tmp_path):
        engine, _ = self._make_engine(tmp_path)
        from core.self_evolution import StrategyGene
        parent = StrategyGene(
            strategy_id="p1", name="parent", category="code",
            parameters={"cot_depth": 3, "temperature": 0.5},
            generation=1
        )
        child = engine.mutate_strategy(parent)
        assert child.category == "code"
        assert child.generation == 2
        assert child.parent_id == "p1"

    def test_mutate_clamps_to_ranges(self, tmp_path):
        engine, _ = self._make_engine(tmp_path)
        from core.self_evolution import StrategyGene, EvolutionEngine
        parent = StrategyGene(
            strategy_id="p1", name="parent", category="code",
            parameters={k: v[1] for k, v in EvolutionEngine.PARAM_RANGES.items()},
            generation=0
        )
        child = engine.mutate_strategy(parent)
        for k, (lo, hi) in EvolutionEngine.PARAM_RANGES.items():
            if k in child.parameters:
                assert lo <= child.parameters[k] <= hi, f"{k}={child.parameters[k]} out of [{lo},{hi}]"

    def test_crossover_produces_child(self, tmp_path):
        engine, _ = self._make_engine(tmp_path)
        from core.self_evolution import StrategyGene
        a = StrategyGene("a", "alpha", "code", {"cot_depth": 2, "temperature": 0.3}, generation=1)
        b = StrategyGene("b", "beta", "code", {"cot_depth": 5, "temperature": 0.8}, generation=2)
        child = engine.crossover(a, b)
        assert child.generation == 3  # max(1,2)+1
        assert "a" in child.parent_id and "b" in child.parent_id
        # Child params come from either parent
        assert child.parameters["cot_depth"] in (2, 5)
        assert child.parameters["temperature"] in (0.3, 0.8)

    def test_select_best_returns_strategy(self, tmp_path):
        engine, _ = self._make_engine(tmp_path)
        best = engine.select_best("code")
        assert best is not None
        assert best.category == "code"

    def test_get_optimal_config(self, tmp_path):
        engine, _ = self._make_engine(tmp_path)
        config = engine.get_optimal_config("code")
        assert isinstance(config, dict)
        # Should have some strategy parameters
        assert len(config) > 0

    def test_evolve_generation_runs(self, tmp_path):
        engine, _ = self._make_engine(tmp_path)
        # Should not raise
        engine.evolve_generation("code")


# ── SelfReflector Tests ──────────────────────────────────────────

class TestSelfReflector:
    """Test SelfReflector failure analysis and bottleneck detection."""

    def _make_reflector(self, tmp_path):
        from core.self_evolution import PerformanceTracker, SelfReflector, TaskMetrics
        db = str(tmp_path / "test_reflect.db")
        tracker = PerformanceTracker(db)
        # Add some data
        for i in range(10):
            tracker.record(TaskMetrics(
                task_id=f"t{i}", category="code", strategy_id="s1",
                success=(i >= 5), latency_ms=float(i * 500),
                quality_score=0.5, tokens_used=100,
                memory_hits=3, memory_queries=10,
                error_type="timeout" if i < 3 else None
            ))
        return SelfReflector(tracker), tracker

    def test_reflect_on_failures(self, tmp_path):
        reflector, _ = self._make_reflector(tmp_path)
        hypotheses = reflector.reflect_on_failures(recent_n=10)
        assert isinstance(hypotheses, list)
        # Should have some hypotheses since we have failures
        assert len(hypotheses) >= 1

    def test_reflect_no_failures(self, tmp_path):
        from core.self_evolution import PerformanceTracker, SelfReflector
        db = str(tmp_path / "test_reflect_ok.db")
        tracker = PerformanceTracker(db)
        reflector = SelfReflector(tracker)
        hypotheses = reflector.reflect_on_failures()
        assert isinstance(hypotheses, list)

    def test_identify_bottlenecks(self, tmp_path):
        reflector, _ = self._make_reflector(tmp_path)
        bottlenecks = reflector.identify_bottlenecks()
        assert isinstance(bottlenecks, list)

    def test_propose_improvements(self, tmp_path):
        reflector, _ = self._make_reflector(tmp_path)
        proposals = reflector.propose_improvements()
        assert isinstance(proposals, list)


# ── SelfEvolutionSystem Facade Tests ─────────────────────────────

class TestSelfEvolutionSystem:
    """Test SelfEvolutionSystem facade."""

    def test_init_and_status(self, tmp_path):
        from core.self_evolution import SelfEvolutionSystem
        sys_evo = SelfEvolutionSystem(data_dir=str(tmp_path))
        status = sys_evo.get_status()
        assert isinstance(status, dict)

    def test_record_task(self, tmp_path):
        from core.self_evolution import SelfEvolutionSystem
        sys_evo = SelfEvolutionSystem(data_dir=str(tmp_path))
        # Get a valid strategy first
        config = sys_evo.get_strategy_for_task("code")
        result = sys_evo.record_task({
            "task_id": "test1", "category": "code",
            "strategy_id": "random_s", "success": True,
            "latency_ms": 500, "quality_score": 0.9,
            "tokens_used": 100, "memory_hits": 5, "memory_queries": 10
        })
        assert isinstance(result, float)

    def test_evolve_all_categories(self, tmp_path):
        from core.self_evolution import SelfEvolutionSystem
        sys_evo = SelfEvolutionSystem(data_dir=str(tmp_path))
        # Should not raise
        sys_evo.evolve_all_categories()

    def test_get_strategy_for_task(self, tmp_path):
        from core.self_evolution import SelfEvolutionSystem
        sys_evo = SelfEvolutionSystem(data_dir=str(tmp_path))
        config = sys_evo.get_strategy_for_task("research")
        assert isinstance(config, dict)
