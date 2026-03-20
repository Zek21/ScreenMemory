#!/usr/bin/env python3
"""Core tests for skynet_brain.py — difficulty assessment, decomposition, natural splitting.

Tests the SkynetBrain intelligence pipeline WITHOUT requiring live Skynet backend,
DAAORouter, or cognitive engines. All external dependencies are mocked.
# signed: gamma
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def brain():
    """Create a SkynetBrain with all engines mocked to None (pure logic testing)."""
    with patch("tools.skynet_brain.SkynetBrain._init_engines"):
        from tools.skynet_brain import SkynetBrain
        b = SkynetBrain()
        b.router = None
        b.dag_builder = None
        b.retriever = None
        b.learning_store = None
        b.cognitive = None
        return b


# ---------------------------------------------------------------------------
# 1. _extract_natural_subtasks() tests
# ---------------------------------------------------------------------------

class TestExtractNaturalSubtasks:
    """Test natural language task splitting."""

    def test_numbered_items(self, brain):
        """Numbered items like '1) scan 2) fix 3) test' split correctly."""
        result = brain._extract_natural_subtasks("1) scan code 2) fix bugs 3) run tests")
        assert len(result) == 3
        assert "scan code" in result[0]
        assert "fix bugs" in result[1]
        assert "run tests" in result[2]
        # signed: gamma

    def test_semicolon_split(self, brain):
        """Semicolons split into separate subtasks."""
        result = brain._extract_natural_subtasks("audit code; deploy app; run tests")
        assert len(result) == 3
        # signed: gamma

    def test_comma_with_and(self, brain):
        """Comma-separated list with 'and' splits correctly."""
        result = brain._extract_natural_subtasks(
            "scan bugs, fix issues, run tests, and update docs"
        )
        assert len(result) >= 3
        # signed: gamma

    def test_and_between_verbs(self, brain):
        """'and' between verb phrases splits into subtasks."""
        result = brain._extract_natural_subtasks("build the API and deploy it")
        assert len(result) == 2
        # signed: gamma

    def test_single_task_no_split(self, brain):
        """A single task without separators returns as-is."""
        result = brain._extract_natural_subtasks("fix the typo in README")
        assert len(result) == 1
        assert result[0] == "fix the typo in README"
        # signed: gamma

    def test_dot_numbered_items(self, brain):
        """Dot-numbered items like '1. scan 2. fix' split correctly."""
        result = brain._extract_natural_subtasks("1. scan code 2. fix bugs")
        assert len(result) == 2
        # signed: gamma


# ---------------------------------------------------------------------------
# 2. _adjust_difficulty() tests
# ---------------------------------------------------------------------------

class TestAdjustDifficulty:
    """Test difficulty adjustment based on text signals."""

    def test_simple_task_stays_simple(self, brain):
        """A single-verb task should not be upgraded."""
        result = brain._adjust_difficulty("fix a typo", "SIMPLE")
        assert result == "SIMPLE"
        # signed: gamma

    def test_multi_verb_upgrades(self, brain):
        """Multiple action verbs should upgrade difficulty."""
        result = brain._adjust_difficulty(
            "build the API and deploy it and test all endpoints", "SIMPLE"
        )
        # 3 verbs (build, deploy, test) + 2 "and" = signals >= 4 → upgrade by 2
        assert result in ("MODERATE", "COMPLEX", "ADVERSARIAL")
        # signed: gamma

    def test_scope_words_upgrade(self, brain):
        """Scope words like 'all', 'entire' should contribute to upgrade."""
        result = brain._adjust_difficulty(
            "audit all security issues across the entire system and fix them", "SIMPLE"
        )
        # scope words + multiple verbs + and
        assert result != "SIMPLE"
        # signed: gamma

    def test_long_goal_adds_signal(self, brain):
        """Goals longer than 200 chars get a signal boost."""
        long_goal = "implement a comprehensive " + "feature " * 30 + " and test it"
        result = brain._adjust_difficulty(long_goal, "SIMPLE")
        # len > 200 + "and" + multiple verbs
        assert result != "SIMPLE"
        # signed: gamma

    def test_adversarial_cap(self, brain):
        """Difficulty should never exceed ADVERSARIAL."""
        result = brain._adjust_difficulty(
            "build and create and implement and fix and audit and review and redesign everything across all systems",
            "COMPLEX"
        )
        assert result == "ADVERSARIAL"
        # signed: gamma

    def test_enumeration_detection(self, brain):
        """Explicit enumerations like '1)' or '2.' should add signals."""
        result = brain._adjust_difficulty(
            "1) scan code 2) fix bugs and update docs", "TRIVIAL"
        )
        # enumeration + "and" + multiple verbs
        assert result != "TRIVIAL"
        # signed: gamma


# ---------------------------------------------------------------------------
# 3. assess() tests
# ---------------------------------------------------------------------------

class TestAssess:
    """Test SkynetBrain.assess() difficulty assessment."""

    def test_assess_without_router_defaults_moderate(self, brain):
        """Without DAAORouter, assess() defaults to MODERATE."""
        result = brain.assess("fix a bug")
        assert result["difficulty"] == "MODERATE"
        assert result["confidence"] == 0.5
        # signed: gamma

    def test_assess_returns_dict(self, brain):
        """assess() always returns a dict with required keys."""
        result = brain.assess("build something")
        assert isinstance(result, dict)
        assert "difficulty" in result
        assert "confidence" in result
        # signed: gamma


# ---------------------------------------------------------------------------
# 4. Subtask and BrainPlan dataclass tests
# ---------------------------------------------------------------------------

class TestDataStructures:
    """Test Subtask and BrainPlan dataclass behavior."""

    def test_subtask_defaults(self):
        """Subtask should have sensible defaults."""
        from tools.skynet_brain import Subtask
        st = Subtask(task_text="test", assigned_worker="alpha")
        assert st.context == ""
        assert st.dependencies == []
        assert st.index == 0
        # signed: gamma

    def test_brain_plan_creation(self):
        """BrainPlan should hold all fields correctly."""
        from tools.skynet_brain import BrainPlan, Subtask
        plan = BrainPlan(
            goal="test goal",
            difficulty="MODERATE",
            subtasks=[Subtask(task_text="sub1", assigned_worker="alpha")],
            reasoning="test reasoning",
        )
        assert plan.goal == "test goal"
        assert plan.difficulty == "MODERATE"
        assert len(plan.subtasks) == 1
        assert plan.strategy_id == ""
        # signed: gamma


# ---------------------------------------------------------------------------
# 5. _generate_strategy_id() tests
# ---------------------------------------------------------------------------

class TestStrategyId:
    """Test strategy ID generation."""

    def test_strategy_id_is_hex_string(self):
        """Strategy ID should be a 16-char hex string."""
        from tools.skynet_brain import _generate_strategy_id
        sid = _generate_strategy_id("test goal")
        assert len(sid) == 16
        assert all(c in "0123456789abcdef" for c in sid)
        # signed: gamma

    def test_strategy_id_differs_per_call(self):
        """Consecutive calls should produce different IDs (time-based)."""
        from tools.skynet_brain import _generate_strategy_id
        id1 = _generate_strategy_id("same goal")
        time.sleep(0.01)
        id2 = _generate_strategy_id("same goal")
        assert id1 != id2
        # signed: gamma


# ---------------------------------------------------------------------------
# 6. CognitiveStrategy.select_strategy() tests
# ---------------------------------------------------------------------------

class TestCognitiveStrategy:
    """Test cognitive strategy selection."""

    def test_trivial_maps_to_direct(self):
        """TRIVIAL difficulty should use 'direct' strategy."""
        from tools.skynet_brain import CognitiveStrategy
        with patch.object(CognitiveStrategy, "_init_cognitive_engines"):
            cs = CognitiveStrategy()
            cs.reflexion = None
            cs.got = None
            cs.mcts = None
            cs.planner = None
        result = cs.select_strategy("TRIVIAL")
        assert result == "direct"
        # signed: gamma

    def test_moderate_downgrade_without_reflexion(self):
        """MODERATE without reflexion engine should downgrade to direct."""
        from tools.skynet_brain import CognitiveStrategy
        with patch.object(CognitiveStrategy, "_init_cognitive_engines"):
            cs = CognitiveStrategy()
            cs.reflexion = None
            cs.got = None
            cs.mcts = None
            cs.planner = None
        result = cs.select_strategy("MODERATE")
        assert result == "direct"
        # signed: gamma

    def test_moderate_with_reflexion(self):
        """MODERATE with reflexion engine should use 'reflexion'."""
        from tools.skynet_brain import CognitiveStrategy
        with patch.object(CognitiveStrategy, "_init_cognitive_engines"):
            cs = CognitiveStrategy()
            cs.reflexion = MagicMock()
            cs.got = None
            cs.mcts = None
            cs.planner = None
        result = cs.select_strategy("MODERATE")
        assert result == "reflexion"
        # signed: gamma
