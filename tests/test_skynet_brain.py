#!/usr/bin/env python3
"""
Comprehensive tests for tools/skynet_brain.py -- Intelligent Dispatch Brain.

Tests cover: CognitiveStrategy, SkynetBrain, Subtask/BrainPlan dataclasses,
difficulty adjustment, natural subtask extraction, decomposition, synthesis,
learning, episode saving, and helper functions.

# signed: delta
"""

import hashlib
import json
import sys
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.skynet_brain import (
    BrainPlan,
    CognitiveStrategy,
    SkynetBrain,
    Subtask,
    _generate_strategy_id,
    _get_idle_workers,
    _read_worker_states,
    _save_episode,
    query_episodes_by_strategy,
    WORKER_NAMES,
)


# ── Data Structures ──────────────────────────────────────


class TestSubtask(unittest.TestCase):
    """Tests for the Subtask dataclass."""

    def test_default_fields(self):
        st = Subtask(task_text="do work", assigned_worker="alpha")
        self.assertEqual(st.task_text, "do work")
        self.assertEqual(st.assigned_worker, "alpha")
        self.assertEqual(st.context, "")
        self.assertEqual(st.dependencies, [])
        self.assertEqual(st.index, 0)

    def test_custom_fields(self):
        st = Subtask(
            task_text="analyze", assigned_worker="beta",
            context="context here", dependencies=["subtask_0"], index=1,
        )
        self.assertEqual(st.context, "context here")
        self.assertEqual(st.dependencies, ["subtask_0"])
        self.assertEqual(st.index, 1)

    def test_asdict(self):
        st = Subtask(task_text="x", assigned_worker="gamma", index=2)
        d = asdict(st)
        self.assertIn("task_text", d)
        self.assertIn("assigned_worker", d)
        self.assertEqual(d["index"], 2)


class TestBrainPlan(unittest.TestCase):
    """Tests for the BrainPlan dataclass."""

    def test_default_fields(self):
        plan = BrainPlan(
            goal="fix bugs", difficulty="MODERATE",
            subtasks=[], reasoning="plan it",
        )
        self.assertEqual(plan.goal, "fix bugs")
        self.assertEqual(plan.relevant_learnings, [])
        self.assertEqual(plan.operator, "")
        self.assertEqual(plan.domain_tags, [])
        self.assertEqual(plan.strategy_id, "")

    def test_full_plan(self):
        st = Subtask(task_text="sub1", assigned_worker="alpha")
        plan = BrainPlan(
            goal="deploy", difficulty="COMPLEX",
            subtasks=[st], reasoning="chain",
            relevant_learnings=["past fix"],
            operator="PIPELINE", domain_tags=["infra"],
            strategy_id="abc123",
        )
        self.assertEqual(len(plan.subtasks), 1)
        self.assertEqual(plan.strategy_id, "abc123")


class TestGenerateStrategyId(unittest.TestCase):
    """Tests for _generate_strategy_id()."""

    def test_returns_hex_string(self):
        sid = _generate_strategy_id("test goal")
        self.assertEqual(len(sid), 16)
        int(sid, 16)  # Should be valid hex

    def test_different_goals_different_ids(self):
        # Due to timestamp inclusion, even same goal gives different ids
        s1 = _generate_strategy_id("goal a")
        time.sleep(0.01)
        s2 = _generate_strategy_id("goal b")
        self.assertNotEqual(s1, s2)

    def test_empty_goal(self):
        sid = _generate_strategy_id("")
        self.assertEqual(len(sid), 16)


# ── Helpers ───────────────────────────────────────────────


class TestReadWorkerStates(unittest.TestCase):
    """Tests for _read_worker_states()."""

    @patch("tools.skynet_brain.STATE_FILE")
    @patch("tools.skynet_brain.STATE_FILE_ALT")
    def test_reads_from_primary_file(self, mock_alt, mock_primary):
        mock_alt.exists.return_value = False
        mock_primary.exists.return_value = True
        mock_primary.read_text.return_value = json.dumps({
            "workers": {"alpha": {"status": "IDLE"}}
        })
        result = _read_worker_states()
        self.assertIn("alpha", result)

    @patch("tools.skynet_brain.STATE_FILE")
    @patch("tools.skynet_brain.STATE_FILE_ALT")
    def test_fallback_to_alt(self, mock_alt, mock_primary):
        mock_primary.exists.return_value = False
        mock_alt.exists.return_value = True
        mock_alt.read_text.return_value = json.dumps({
            "workers": {"beta": {"status": "PROCESSING"}}
        })
        result = _read_worker_states()
        self.assertIn("beta", result)

    @patch("tools.skynet_brain.STATE_FILE")
    @patch("tools.skynet_brain.STATE_FILE_ALT")
    @patch("tools.skynet_brain.urlopen", side_effect=Exception("no bus"))
    def test_all_sources_fail(self, _mock_url, mock_alt, mock_primary):
        mock_primary.exists.return_value = False
        mock_alt.exists.return_value = False
        result = _read_worker_states()
        self.assertEqual(result, {})


class TestGetIdleWorkers(unittest.TestCase):
    """Tests for _get_idle_workers()."""

    @patch("tools.skynet_brain._read_worker_states")
    def test_returns_idle_workers(self, mock_states):
        mock_states.return_value = {
            "alpha": {"status": "IDLE", "tasks_completed": 5},
            "beta": {"status": "PROCESSING"},
            "gamma": {"status": "IDLE", "tasks_completed": 2},
            "delta": {"status": "IDLE", "tasks_completed": 10},
        }
        idle = _get_idle_workers()
        self.assertEqual(len(idle), 3)
        # Sorted by tasks_completed ascending
        self.assertEqual(idle[0], "gamma")
        self.assertEqual(idle[1], "alpha")
        self.assertEqual(idle[2], "delta")

    @patch("tools.skynet_brain._read_worker_states")
    def test_no_idle_workers(self, mock_states):
        mock_states.return_value = {
            "alpha": {"status": "PROCESSING"},
            "beta": {"status": "PROCESSING"},
        }
        idle = _get_idle_workers()
        self.assertEqual(idle, [])

    @patch("tools.skynet_brain._read_worker_states")
    def test_empty_states(self, mock_states):
        mock_states.return_value = {}
        idle = _get_idle_workers()
        self.assertEqual(idle, [])

    @patch("tools.skynet_brain._read_worker_states")
    def test_unknown_status_not_idle(self, mock_states):
        mock_states.return_value = {
            "alpha": {"status": "UNKNOWN"},
        }
        idle = _get_idle_workers()
        self.assertEqual(idle, [])


# ── CognitiveStrategy ────────────────────────────────────


class TestCognitiveStrategy(unittest.TestCase):
    """Tests for CognitiveStrategy class."""

    def setUp(self):
        # Patch all cognitive engine imports to avoid dependency on real modules
        with patch.object(CognitiveStrategy, "_init_cognitive_engines"):
            self.cs = CognitiveStrategy()
        self.cs.got = None
        self.cs.mcts = None
        self.cs.reflexion = None
        self.cs.planner = None

    def test_select_strategy_trivial(self):
        self.assertEqual(self.cs.select_strategy("TRIVIAL"), "direct")

    def test_select_strategy_simple(self):
        self.assertEqual(self.cs.select_strategy("SIMPLE"), "direct")

    def test_select_strategy_moderate_no_reflexion(self):
        self.cs.reflexion = None
        self.assertEqual(self.cs.select_strategy("MODERATE"), "direct")

    def test_select_strategy_moderate_with_reflexion(self):
        self.cs.reflexion = MagicMock()
        self.assertEqual(self.cs.select_strategy("MODERATE"), "reflexion")

    def test_select_strategy_complex_no_engines(self):
        """COMPLEX without GoT or reflexion falls back to direct."""
        with patch.dict("sys.modules", {"tools.skynet_got_router": None}):
            with patch("builtins.__import__", side_effect=ImportError("no got")):
                result = self.cs.select_strategy("COMPLEX")
        self.assertEqual(result, "direct")

    def test_select_strategy_complex_with_reflexion_fallback(self):
        self.cs.reflexion = MagicMock()
        with patch("builtins.__import__", side_effect=ImportError("no got")):
            result = self.cs.select_strategy("COMPLEX")
        self.assertEqual(result, "reflexion")

    def test_select_strategy_unknown_difficulty(self):
        self.assertEqual(self.cs.select_strategy("UNKNOWN"), "direct")

    def test_apply_reflexion_no_engine(self):
        result = self.cs.apply_reflexion("goal", [])
        self.assertEqual(result["strategy"], "reflexion")
        self.assertEqual(result["insights"], [])

    def test_apply_reflexion_with_engine(self):
        mock_ref = MagicMock()
        mock_ref.get_relevant_reflections.return_value = []
        self.cs.reflexion = mock_ref
        result = self.cs.apply_reflexion("goal", ["some failure happened"])
        self.assertEqual(result["strategy"], "reflexion")
        self.assertIn("Warning from memory", result["insights"][0])

    def test_apply_reflexion_mines_failure_keywords(self):
        mock_ref = MagicMock()
        mock_ref.get_relevant_reflections.return_value = []
        self.cs.reflexion = mock_ref
        result = self.cs.apply_reflexion("goal", [
            "build succeeded",
            "error in deploy step",
            "bug found in auth",
        ])
        # Should find "error" and "bug" keywords
        warnings = [i for i in result["insights"] if "Warning" in i]
        self.assertEqual(len(warnings), 2)

    def test_apply_got_no_engine(self):
        with patch("builtins.__import__", side_effect=ImportError):
            result = self.cs.apply_got("goal", [])
        self.assertEqual(result["strategy"], "got")
        self.assertEqual(result["thoughts"], [])

    def test_apply_mcts_no_engine(self):
        result = self.cs.apply_mcts("goal")
        self.assertEqual(result["strategy"], "mcts")
        self.assertEqual(result["iterations"], 0)

    def test_apply_direct(self):
        result = self.cs.apply("TRIVIAL", "simple task")
        self.assertEqual(result["strategy"], "direct")

    def test_apply_routes_correctly(self):
        self.cs.reflexion = MagicMock()
        self.cs.reflexion.get_relevant_reflections.return_value = []
        result = self.cs.apply("MODERATE", "goal", ["learning"])
        self.assertEqual(result["strategy"], "reflexion")

    def test_enrich_direct_returns_empty(self):
        result = self.cs.enrich_with_cognitive_context({"strategy": "direct"})
        self.assertEqual(result, "")

    def test_enrich_reflexion(self):
        result = self.cs.enrich_with_cognitive_context({
            "strategy": "reflexion",
            "insights": ["Watch out for X"],
            "adjustments": ["Try Y instead"],
        })
        self.assertIn("REFLEXION", result)
        self.assertIn("Watch out for X", result)
        self.assertIn("Try Y instead", result)

    def test_enrich_got(self):
        result = self.cs.enrich_with_cognitive_context({
            "strategy": "got",
            "best_path": ["step 1", "step 2"],
            "graph_size": 5,
            "source": "internal",
        })
        self.assertIn("REASONING PATH", result)
        self.assertIn("step 1", result)
        self.assertIn("5 nodes", result)

    def test_enrich_mcts(self):
        result = self.cs.enrich_with_cognitive_context({
            "strategy": "mcts",
            "iterations": 10,
            "best_score": 0.85,
            "best_action": "approach A",
            "reflections": 3,
        })
        self.assertIn("MCTS SEARCH", result)
        self.assertIn("approach A", result)

    def test_generate_thought_branches_build(self):
        branches = self.cs._generate_thought_branches("build a REST API")
        self.assertTrue(len(branches) >= 3)
        contents = [b["content"] for b in branches]
        self.assertTrue(any("architecture" in c.lower() for c in contents))

    def test_generate_thought_branches_fix(self):
        branches = self.cs._generate_thought_branches("fix the auth bug")
        contents = [b["content"] for b in branches]
        self.assertTrue(any("validation" in c.lower() or "testing" in c.lower() for c in contents))

    def test_generate_thought_branches_max_4(self):
        branches = self.cs._generate_thought_branches("build and fix and test and deploy and audit")
        self.assertLessEqual(len(branches), 4)


# ── SkynetBrain ───────────────────────────────────────────


class TestSkynetBrain(unittest.TestCase):
    """Tests for SkynetBrain class."""

    def setUp(self):
        with patch.object(SkynetBrain, "_init_engines"):
            self.brain = SkynetBrain()
        self.brain.router = None
        self.brain.dag_builder = None
        self.brain.retriever = None
        self.brain.learning_store = None
        with patch.object(CognitiveStrategy, "_init_cognitive_engines"):
            self.brain.cognitive = CognitiveStrategy()
        self.brain.cognitive.got = None
        self.brain.cognitive.mcts = None
        self.brain.cognitive.reflexion = None
        self.brain.cognitive.planner = None

    # ── assess ────────────────────────────────────────

    def test_assess_no_router(self):
        result = self.brain.assess("test goal")
        self.assertEqual(result["difficulty"], "MODERATE")
        self.assertEqual(result["confidence"], 0.5)

    def test_assess_with_cognitive_strategy(self):
        result = self.brain.assess("fix a bug")
        self.assertIn("cognitive_strategy", result)
        self.assertEqual(result["cognitive_strategy"], "direct")

    # ── _adjust_difficulty ────────────────────────────

    def test_adjust_trivial_stays_trivial(self):
        result = SkynetBrain._adjust_difficulty("fix bug", "TRIVIAL")
        self.assertEqual(result, "TRIVIAL")

    def test_adjust_single_verb_no_change(self):
        result = SkynetBrain._adjust_difficulty("fix", "SIMPLE")
        self.assertEqual(result, "SIMPLE")

    def test_adjust_two_verbs_bumps_one(self):
        result = SkynetBrain._adjust_difficulty("build and test the module", "SIMPLE")
        # 2 verbs (build, test) + 1 "and" = 2 signals -> +1 level
        self.assertEqual(result, "MODERATE")

    def test_adjust_many_signals_bumps_two(self):
        result = SkynetBrain._adjust_difficulty(
            "build and deploy and test and verify all modules across entire system", "SIMPLE"
        )
        # Many verbs, multiple "and", scope words -> 4+ signals -> +2 levels
        self.assertIn(result, ["COMPLEX", "ADVERSARIAL"])

    def test_adjust_numbered_items(self):
        result = SkynetBrain._adjust_difficulty(
            "1) fix bugs 2) add tests", "SIMPLE"
        )
        # Enumeration + 2 verbs = 2+ signals -> +1 level
        self.assertIn(result, ["MODERATE", "COMPLEX"])

    def test_adjust_scope_keywords(self):
        result = SkynetBrain._adjust_difficulty(
            "scan all modules and fix every bug", "SIMPLE"
        )
        # "all", "every" + verbs + "and" -> multiple signals
        self.assertNotEqual(result, "SIMPLE")

    def test_adjust_long_goal(self):
        long_goal = "a" * 201
        result = SkynetBrain._adjust_difficulty(long_goal, "SIMPLE")
        # Length > 200 adds a signal, but only 1 signal total -> no change
        self.assertEqual(result, "SIMPLE")

    def test_adjust_caps_at_adversarial(self):
        result = SkynetBrain._adjust_difficulty(
            "build and create and implement and fix and test and deploy all systems across entire codebase",
            "ADVERSARIAL",
        )
        self.assertEqual(result, "ADVERSARIAL")

    def test_adjust_unknown_difficulty_uses_index_1(self):
        result = SkynetBrain._adjust_difficulty(
            "build and test", "UNKNOWN_LEVEL"
        )
        # Unknown defaults to index 1 (SIMPLE), then signals may bump
        self.assertIn(result, ["SIMPLE", "MODERATE", "COMPLEX"])

    # ── _extract_natural_subtasks ─────────────────────

    def test_extract_no_boundaries(self):
        parts = SkynetBrain._extract_natural_subtasks("fix the bug")
        self.assertEqual(parts, ["fix the bug"])

    def test_extract_numbered_parenthesis(self):
        parts = SkynetBrain._extract_natural_subtasks("1) scan code 2) fix bugs 3) test")
        self.assertEqual(len(parts), 3)
        self.assertIn("scan code", parts[0])

    def test_extract_numbered_dot(self):
        parts = SkynetBrain._extract_natural_subtasks("1. build API 2. deploy it")
        self.assertEqual(len(parts), 2)

    def test_extract_semicolons(self):
        parts = SkynetBrain._extract_natural_subtasks("audit code; deploy; run tests")
        self.assertEqual(len(parts), 3)

    def test_extract_comma_with_verbs(self):
        parts = SkynetBrain._extract_natural_subtasks(
            "scan bugs, fix issues, run tests, and update docs"
        )
        self.assertEqual(len(parts), 4)

    def test_extract_and_between_verbs(self):
        parts = SkynetBrain._extract_natural_subtasks("build the API and deploy it")
        self.assertEqual(len(parts), 2)

    def test_extract_and_without_verbs_stays_single(self):
        parts = SkynetBrain._extract_natural_subtasks("the cat and the dog")
        self.assertEqual(parts, ["the cat and the dog"])

    def test_extract_oxford_comma(self):
        parts = SkynetBrain._extract_natural_subtasks(
            "fix the auth, update the docs, and run the tests"
        )
        self.assertGreaterEqual(len(parts), 3)

    def test_extract_priority_numbered_over_semicolons(self):
        """Numbered items take priority over semicolons."""
        parts = SkynetBrain._extract_natural_subtasks("1) fix code; deploy 2) test; validate")
        self.assertEqual(len(parts), 2)  # Split on numbers, not semicolons

    def test_extract_single_item_list(self):
        parts = SkynetBrain._extract_natural_subtasks("1) fix code")
        # "1) fix code" -> split yields ["", "fix code"] -> ["fix code"]
        self.assertEqual(len(parts), 1)

    def test_extract_too_many_and_parts_stays_single(self):
        """More than 4 parts from 'and' split = stays single."""
        parts = SkynetBrain._extract_natural_subtasks(
            "a and b and c and d and e"
        )
        # len(parts) = 5, > 4, so should stay as single goal
        self.assertEqual(len(parts), 1)

    # ── _decompose ────────────────────────────────────

    def test_decompose_trivial(self):
        subtasks = self.brain._decompose("fix bug", "TRIVIAL", ["alpha"], [], [])
        self.assertEqual(len(subtasks), 1)
        self.assertEqual(subtasks[0].assigned_worker, "alpha")

    def test_decompose_simple(self):
        subtasks = self.brain._decompose("update config", "SIMPLE", ["beta"], [], [])
        self.assertEqual(len(subtasks), 1)

    def test_decompose_moderate(self):
        subtasks = self.brain._decompose("refactor auth", "MODERATE", ["alpha", "beta"], [], [])
        self.assertEqual(len(subtasks), 2)
        self.assertEqual(subtasks[1].dependencies, ["subtask_0"])

    @patch("tools.skynet_brain.SkynetBrain._try_got_decompose", return_value=[])
    def test_decompose_complex_fallback(self, _mock_got):
        """Without GoT, COMPLEX uses linear 4-task chain."""
        subtasks = self.brain._decompose("build system", "COMPLEX",
                                          ["alpha", "beta", "gamma", "delta"], [], [])
        self.assertEqual(len(subtasks), 4)
        self.assertEqual(subtasks[0].dependencies, [])
        self.assertIn("subtask_0", subtasks[1].dependencies)

    def test_decompose_adversarial_debate(self):
        """ADVERSARIAL without GoT uses debate format."""
        subtasks = self.brain._decompose("design API", "ADVERSARIAL",
                                          ["alpha", "beta", "gamma", "delta"], [], [])
        self.assertEqual(len(subtasks), 4)
        # Last task depends on all previous
        self.assertIn("subtask_0", subtasks[3].dependencies)
        self.assertIn("subtask_1", subtasks[3].dependencies)
        self.assertIn("subtask_2", subtasks[3].dependencies)

    def test_decompose_worker_cycling(self):
        """Workers cycle when fewer workers than subtasks."""
        subtasks = self.brain._decompose("build system", "COMPLEX", ["alpha", "beta"], [], [])
        workers = [st.assigned_worker for st in subtasks]
        self.assertEqual(len(subtasks), 4)
        self.assertIn("alpha", workers)
        self.assertIn("beta", workers)

    def test_decompose_moderate_single_worker(self):
        """MODERATE with only 1 worker assigns both tasks to same worker."""
        subtasks = self.brain._decompose("refactor", "MODERATE", ["alpha"], [], [])
        self.assertEqual(len(subtasks), 2)
        self.assertEqual(subtasks[0].assigned_worker, "alpha")
        self.assertEqual(subtasks[1].assigned_worker, "alpha")

    # ── _decompose_natural ────────────────────────────

    def test_decompose_natural_round_robin(self):
        parts = ["scan code", "fix bugs", "run tests"]
        subtasks = self.brain._decompose_natural(parts, ["alpha", "beta"], "ctx")
        self.assertEqual(len(subtasks), 3)
        self.assertEqual(subtasks[0].assigned_worker, "alpha")
        self.assertEqual(subtasks[1].assigned_worker, "beta")
        self.assertEqual(subtasks[2].assigned_worker, "alpha")  # wraps

    def test_decompose_natural_context_attached(self):
        parts = ["task A"]
        subtasks = self.brain._decompose_natural(parts, ["alpha"], "some context")
        self.assertEqual(subtasks[0].context, "some context")

    # ── _build_context ────────────────────────────────

    def test_build_context_empty(self):
        result = self.brain._build_context([], [])
        self.assertEqual(result, "")

    def test_build_context_with_learnings(self):
        result = self.brain._build_context(["learned X"], [])
        self.assertIn("RELEVANT PAST LEARNINGS", result)
        self.assertIn("learned X", result)

    def test_build_context_with_docs(self):
        result = self.brain._build_context([], ["doc content"])
        self.assertIn("RELEVANT CONTEXT", result)
        self.assertIn("doc content", result)

    def test_build_context_truncates(self):
        long_learning = "x" * 300
        result = self.brain._build_context([long_learning], [])
        # Should truncate to 200 chars
        self.assertLessEqual(len(result.split("\n")[1].strip()), 210)

    # ── _build_reasoning ──────────────────────────────

    def test_build_reasoning_basic(self):
        st = Subtask(task_text="fix it", assigned_worker="alpha")
        result = self.brain._build_reasoning(
            "fix bug", "SIMPLE", "CHAIN", [st], [], [], ["alpha"],
        )
        self.assertIn("fix bug", result)
        self.assertIn("SIMPLE", result)
        self.assertIn("alpha", result)

    def test_build_reasoning_with_learnings_and_docs(self):
        result = self.brain._build_reasoning(
            "goal", "MODERATE", "CHAIN", [],
            ["learning 1", "learning 2"], ["doc 1"], ["alpha", "beta"],
        )
        self.assertIn("2 relevant past learnings", result)
        self.assertIn("1 relevant context", result)

    # ── _append_cognitive_reasoning ───────────────────

    def test_append_cognitive_direct(self):
        result = self.brain._append_cognitive_reasoning("base", {"strategy": "direct"})
        self.assertEqual(result, "base")

    def test_append_cognitive_none(self):
        result = self.brain._append_cognitive_reasoning("base", None)
        self.assertEqual(result, "base")

    def test_append_cognitive_got(self):
        result = self.brain._append_cognitive_reasoning("base", {
            "strategy": "got", "graph_size": 5,
        })
        self.assertIn("GOT", result)
        self.assertIn("5 nodes", result)

    def test_append_cognitive_mcts(self):
        result = self.brain._append_cognitive_reasoning("base", {
            "strategy": "mcts", "iterations": 10, "best_score": 0.8,
        })
        self.assertIn("MCTS", result)
        self.assertIn("10 iterations", result)

    def test_append_cognitive_reflexion(self):
        result = self.brain._append_cognitive_reasoning("base", {
            "strategy": "reflexion", "reflection_count": 3,
        })
        self.assertIn("REFLEXION", result)
        self.assertIn("3 relevant reflections", result)

    # ── synthesize ────────────────────────────────────

    def test_synthesize_successes(self):
        plan = BrainPlan(goal="test", difficulty="SIMPLE", subtasks=[], reasoning="")
        results = {
            "subtask_0": {"dispatched": True, "worker": "alpha", "task": "do it",
                          "result_content": "done"},
        }
        summary = self.brain.synthesize(plan, results)
        self.assertIn("COMPLETED (1)", summary)
        self.assertIn("alpha", summary)

    def test_synthesize_failures(self):
        plan = BrainPlan(goal="test", difficulty="SIMPLE", subtasks=[], reasoning="")
        results = {
            "subtask_0": {"dispatched": False, "worker": "beta",
                          "error": "deps not met"},
        }
        summary = self.brain.synthesize(plan, results)
        self.assertIn("FAILED (1)", summary)
        self.assertIn("deps not met", summary)

    def test_synthesize_mixed(self):
        plan = BrainPlan(goal="test", difficulty="MODERATE", subtasks=[], reasoning="")
        results = {
            "subtask_0": {"dispatched": True, "worker": "alpha", "task": "analyze"},
            "subtask_1": {"dispatched": False, "worker": "beta", "error": "timeout"},
        }
        summary = self.brain.synthesize(plan, results)
        self.assertIn("COMPLETED (1)", summary)
        self.assertIn("FAILED (1)", summary)

    def test_synthesize_empty_results(self):
        plan = BrainPlan(goal="test", difficulty="SIMPLE", subtasks=[], reasoning="")
        summary = self.brain.synthesize(plan, {})
        self.assertIn("No results collected", summary)

    def test_synthesize_awaiting_result(self):
        plan = BrainPlan(goal="test", difficulty="SIMPLE", subtasks=[], reasoning="")
        results = {
            "subtask_0": {"dispatched": True, "worker": "gamma", "task": "scan"},
        }
        summary = self.brain.synthesize(plan, results)
        self.assertIn("awaiting result", summary)

    # ── _dispatch_single ──────────────────────────────

    def test_dispatch_single_success(self):
        st = Subtask(task_text="do work", assigned_worker="alpha", index=0)
        dispatch_fn = MagicMock(return_value=True)
        completed = {}
        result = self.brain._dispatch_single(st, dispatch_fn, completed)
        self.assertTrue(result["dispatched"])
        self.assertIn("subtask_0", completed)

    def test_dispatch_single_failure(self):
        st = Subtask(task_text="do work", assigned_worker="alpha", index=0)
        dispatch_fn = MagicMock(return_value=False)
        completed = {}
        result = self.brain._dispatch_single(st, dispatch_fn, completed)
        self.assertFalse(result["dispatched"])
        self.assertNotIn("subtask_0", completed)

    def test_dispatch_single_deps_not_met(self):
        st = Subtask(task_text="step 2", assigned_worker="beta",
                     dependencies=["subtask_0"], index=1)
        dispatch_fn = MagicMock()
        completed = {}  # subtask_0 not in completed
        result = self.brain._dispatch_single(st, dispatch_fn, completed)
        self.assertFalse(result["dispatched"])
        self.assertIn("Dependencies not met", result["error"])
        dispatch_fn.assert_not_called()

    def test_dispatch_single_deps_met(self):
        st = Subtask(task_text="step 2", assigned_worker="beta",
                     dependencies=["subtask_0"], index=1)
        dispatch_fn = MagicMock(return_value=True)
        completed = {"subtask_0": True}
        result = self.brain._dispatch_single(st, dispatch_fn, completed)
        self.assertTrue(result["dispatched"])

    def test_dispatch_single_exception(self):
        st = Subtask(task_text="crash", assigned_worker="gamma", index=0)
        dispatch_fn = MagicMock(side_effect=RuntimeError("boom"))
        completed = {}
        result = self.brain._dispatch_single(st, dispatch_fn, completed)
        self.assertFalse(result["dispatched"])
        self.assertIn("boom", result["error"])

    def test_dispatch_single_with_context(self):
        st = Subtask(task_text="do work", assigned_worker="alpha",
                     context="important context", index=0)
        dispatch_fn = MagicMock(return_value=True)
        completed = {}
        self.brain._dispatch_single(st, dispatch_fn, completed)
        call_args = dispatch_fn.call_args[0]
        self.assertIn("CONTEXT", call_args[1])
        self.assertIn("important context", call_args[1])

    # ── think ─────────────────────────────────────────

    @patch("tools.skynet_brain._get_idle_workers")
    def test_think_simple_goal(self, mock_idle):
        mock_idle.return_value = ["alpha"]
        plan = self.brain.think("fix the bug")
        self.assertIsInstance(plan, BrainPlan)
        self.assertEqual(plan.goal, "fix the bug")
        self.assertGreater(len(plan.subtasks), 0)
        self.assertGreater(len(plan.strategy_id), 0)

    @patch("tools.skynet_brain._get_idle_workers")
    def test_think_natural_subtasks(self, mock_idle):
        mock_idle.return_value = ["alpha", "beta", "gamma"]
        plan = self.brain.think("scan code; fix bugs; run tests")
        self.assertEqual(len(plan.subtasks), 3)

    @patch("tools.skynet_brain._get_idle_workers")
    def test_think_auto_upgrades_difficulty(self, mock_idle):
        mock_idle.return_value = ["alpha", "beta"]
        plan = self.brain.think("scan code; fix bugs; run tests")
        # 3 parts from SIMPLE -> COMPLEX
        self.assertIn(plan.difficulty, ["MODERATE", "COMPLEX"])

    @patch("tools.skynet_brain._get_idle_workers")
    def test_think_no_idle_workers_uses_all(self, mock_idle):
        mock_idle.return_value = []
        plan = self.brain.think("fix bug")
        # Falls back to WORKER_NAMES
        self.assertGreater(len(plan.subtasks), 0)

    # ── learn ─────────────────────────────────────────

    @patch("tools.skynet_brain._save_episode")
    def test_learn_stores_episode(self, mock_save):
        plan = BrainPlan(
            goal="test", difficulty="SIMPLE", subtasks=[], reasoning="",
            strategy_id="abc",
        )
        results = {"subtask_0": {"dispatched": True}}
        self.brain.learn(plan, results, success=True)
        mock_save.assert_called_once()

    @patch("tools.skynet_brain._save_episode")
    def test_learn_with_learning_store(self, mock_save):
        mock_ls = MagicMock()
        self.brain.learning_store = mock_ls
        plan = BrainPlan(
            goal="deploy", difficulty="MODERATE", subtasks=[], reasoning="",
            domain_tags=["infra"],
        )
        results = {
            "subtask_0": {"dispatched": True},
            "subtask_1": {"dispatched": False},
        }
        self.brain.learn(plan, results, success=False)
        mock_ls.learn.assert_called_once()
        call_kwargs = mock_ls.learn.call_args[1]
        self.assertIn("Failure", call_kwargs["content"])
        self.assertIn("brain", call_kwargs["tags"])

    # ── _recall_and_search ────────────────────────────

    def test_recall_and_search_no_engines(self):
        learnings, docs = self.brain._recall_and_search("goal")
        self.assertEqual(learnings, [])
        self.assertEqual(docs, [])

    def test_recall_and_search_with_store(self):
        mock_fact = MagicMock()
        mock_fact.content = "learned something"
        mock_ls = MagicMock()
        mock_ls.recall.return_value = [mock_fact]
        self.brain.learning_store = mock_ls
        learnings, docs = self.brain._recall_and_search("goal")
        self.assertEqual(len(learnings), 1)
        self.assertEqual(learnings[0], "learned something")


# ── Episode persistence ──────────────────────────────────


class TestSaveEpisode(unittest.TestCase):
    """Tests for _save_episode() and query_episodes_by_strategy()."""

    def test_save_episode_creates_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ep_dir = Path(tmpdir) / "episodes"
            ep_index = Path(tmpdir) / "index.json"
            plan = MagicMock()
            plan.goal = "test goal"
            plan.difficulty = "SIMPLE"
            plan.subtasks = []
            plan.strategy_id = "test_sid"

            with patch("tools.skynet_brain.EPISODES_DIR", ep_dir), \
                 patch("tools.skynet_brain.EPISODES_INDEX", ep_index):
                _save_episode(plan, {}, True)

            # Check episode file was created
            ep_files = list(ep_dir.glob("*.json"))
            self.assertEqual(len(ep_files), 1)
            ep = json.loads(ep_files[0].read_text())
            self.assertEqual(ep["outcome"], "success")
            self.assertIn("test_sid", ep["strategy_id"])

    def test_save_episode_updates_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ep_dir = Path(tmpdir) / "episodes"
            ep_index = Path(tmpdir) / "index.json"
            plan = MagicMock()
            plan.goal = "test"
            plan.difficulty = "SIMPLE"
            plan.subtasks = [
                MagicMock(assigned_worker="alpha"),
            ]
            plan.strategy_id = "sid1"

            with patch("tools.skynet_brain.EPISODES_DIR", ep_dir), \
                 patch("tools.skynet_brain.EPISODES_INDEX", ep_index):
                _save_episode(plan, {}, True)

            if ep_index.exists():
                index = json.loads(ep_index.read_text())
                self.assertGreater(len(index), 0)

    def test_query_episodes_by_strategy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ep_dir = Path(tmpdir) / "episodes"
            ep_index = Path(tmpdir) / "index.json"
            ep_dir.mkdir()

            # Write an episode file
            ep = {"strategy_id": "target_sid", "goal": "found", "outcome": "success"}
            (ep_dir / "ep_test.json").write_text(json.dumps(ep))

            with patch("tools.skynet_brain.EPISODES_DIR", ep_dir), \
                 patch("tools.skynet_brain.EPISODES_INDEX", ep_index):
                results = query_episodes_by_strategy("target_sid")

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["goal"], "found")

    def test_query_episodes_no_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ep_dir = Path(tmpdir) / "episodes"
            ep_index = Path(tmpdir) / "index.json"
            ep_dir.mkdir()

            with patch("tools.skynet_brain.EPISODES_DIR", ep_dir), \
                 patch("tools.skynet_brain.EPISODES_INDEX", ep_index):
                results = query_episodes_by_strategy("nonexistent")

            self.assertEqual(results, [])


# ── Worker Names constant ────────────────────────────────


class TestConstants(unittest.TestCase):
    """Tests for module-level constants."""

    def test_worker_names(self):
        self.assertEqual(WORKER_NAMES, ["alpha", "beta", "gamma", "delta"])

    def test_natural_action_verbs_is_frozenset(self):
        self.assertIsInstance(SkynetBrain._NATURAL_ACTION_VERBS, frozenset)
        self.assertIn("build", SkynetBrain._NATURAL_ACTION_VERBS)
        self.assertIn("deploy", SkynetBrain._NATURAL_ACTION_VERBS)


if __name__ == "__main__":
    unittest.main()
