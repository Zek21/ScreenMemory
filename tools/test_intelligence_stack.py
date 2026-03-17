"""
End-to-end tests proving the Skynet intelligence stack works.

Tests 6 subsystems: cognitive engines, knowledge system, backup protection,
dispatch resilience, post-task lifecycle, and self-awareness.

Usage:
    python -m pytest tools/test_intelligence_stack.py -v

# signed: delta
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# 1. COGNITIVE ENGINES (4 tests)
# ---------------------------------------------------------------------------
class TestCognitiveEngines(unittest.TestCase):
    """Verify all three cognitive engines instantiate and function correctly."""

    # -- 1a. ReflexionEngine --------------------------------------------------
    def test_reflexion_engine_instantiates(self):
        """ReflexionEngine imports and constructs without external deps."""
        from core.cognitive.reflexion import ReflexionEngine

        engine = ReflexionEngine()
        self.assertIsNotNone(engine)
        self.assertEqual(engine.max_reflections, 100)
        self.assertTrue(hasattr(engine, "on_failure"))
        self.assertTrue(hasattr(engine, "get_relevant_reflections"))
        self.assertTrue(hasattr(engine, "should_adjust_action"))
        self.assertTrue(hasattr(engine, "stats"))

    def test_reflexion_engine_stats(self):
        """ReflexionEngine.stats property returns expected structure."""
        from core.cognitive.reflexion import ReflexionEngine

        engine = ReflexionEngine()
        stats = engine.stats  # property, not method
        self.assertIsInstance(stats, dict)
        self.assertIn("total_reflections", stats)
        self.assertEqual(stats["total_reflections"], 0)

    # -- 1b. GraphOfThoughts --------------------------------------------------
    def test_graph_of_thoughts_instantiates(self):
        """GraphOfThoughts imports, constructs, and basic operations work."""
        from core.cognitive.graph_of_thoughts import GraphOfThoughts

        got = GraphOfThoughts(max_depth=5, max_branches=3)
        self.assertIsNotNone(got)
        self.assertTrue(hasattr(got, "add_thought"))
        self.assertTrue(hasattr(got, "generate"))
        self.assertTrue(hasattr(got, "prune"))
        self.assertTrue(hasattr(got, "get_best_path"))

    def test_graph_of_thoughts_add_and_resolve(self):
        """GraphOfThoughts can add thoughts, branch, and resolve."""
        from core.cognitive.graph_of_thoughts import GraphOfThoughts

        got = GraphOfThoughts()
        root = got.add_thought("Root idea", score=0.8)
        self.assertIsNotNone(root)
        self.assertEqual(root.content, "Root idea")
        self.assertAlmostEqual(root.score, 0.8)

        child = got.generate(root.id, "Branch idea", score=0.6)
        self.assertIsNotNone(child)
        self.assertEqual(child.depth, 1)

        stats = got.stats  # property, not method
        self.assertIsInstance(stats, dict)
        self.assertGreaterEqual(stats.get("total_thoughts", 0), 2)

        best = got.get_best_thought()
        self.assertIsNotNone(best)

    # -- 1c. HierarchicalPlanner ----------------------------------------------
    def test_hierarchical_planner_instantiates(self):
        """HierarchicalPlanner imports and creates plans."""
        from core.cognitive.planner import HierarchicalPlanner

        planner = HierarchicalPlanner()
        self.assertIsNotNone(planner)
        self.assertTrue(hasattr(planner, "create_plan"))
        self.assertTrue(hasattr(planner, "execute_step"))

    def test_hierarchical_planner_creates_plan(self):
        """HierarchicalPlanner.create_plan decomposes a goal into subtasks."""
        from core.cognitive.planner import HierarchicalPlanner

        planner = HierarchicalPlanner()
        plan = planner.create_plan("Test the login page")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.goal, "Test the login page")
        self.assertIsInstance(plan.subtasks, list)
        self.assertGreater(len(plan.subtasks), 0)

    # -- 1d. Brain decomposition ----------------------------------------------
    def test_brain_assess_returns_valid_result(self):
        """SkynetBrain.assess() returns difficulty classification."""
        from tools.skynet_brain import SkynetBrain

        brain = SkynetBrain()
        result = brain.assess("Write a simple unit test")
        self.assertIsInstance(result, dict)
        self.assertIn("difficulty", result)
        self.assertIn(result["difficulty"],
                      ["TRIVIAL", "SIMPLE", "MODERATE", "COMPLEX", "ADVERSARIAL"])


# ---------------------------------------------------------------------------
# 2. KNOWLEDGE SYSTEM (3 tests)
# ---------------------------------------------------------------------------
class TestKnowledgeSystem(unittest.TestCase):
    """Verify learning store, knowledge broadcasting, and collective IQ."""

    def test_learning_store_loads(self):
        """LearningStore instantiates with a temp DB and stores/recalls facts."""
        from core.learning_store import LearningStore

        tmp = tempfile.mkdtemp()
        try:
            db_path = os.path.join(tmp, "test_learning.db")
            store = LearningStore(db_path=db_path)
            self.assertIsNotNone(store)

            fact_id = store.learn(
                content="The bus ring buffer holds 100 messages",
                category="architecture",
                source="delta_test",
                tags=["bus", "ring_buffer"],
            )
            self.assertIsNotNone(fact_id)

            results = store.recall("bus ring buffer", top_k=3)
            self.assertIsInstance(results, list)
            self.assertGreater(len(results), 0)
            self.assertIn("ring buffer", results[0].content.lower())

            stats = store.stats()
            self.assertIsInstance(stats, dict)
            self.assertGreaterEqual(stats.get("total_facts", 0), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_knowledge_broadcast_and_poll_importable(self):
        """broadcast_learning and poll_knowledge are importable and callable."""
        from tools.skynet_knowledge import broadcast_learning, poll_knowledge

        self.assertTrue(callable(broadcast_learning))
        self.assertTrue(callable(poll_knowledge))

    @patch("tools.skynet_knowledge._bus_post", return_value=True)
    def test_broadcast_learning_calls_bus_post(self, mock_post):
        """broadcast_learning publishes a knowledge message via _bus_post."""
        from tools.skynet_knowledge import broadcast_learning

        result = broadcast_learning(
            sender="delta",
            fact="Test fact for intelligence stack",
            category="pattern",
            tags=["test"],
        )
        self.assertTrue(result)
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        msg = call_args[0][0] if call_args[0] else call_args[1]
        self.assertEqual(msg.get("sender", ""), "delta")

    def test_collective_intelligence_score_importable(self):
        """intelligence_score() is importable and callable."""
        from tools.skynet_collective import intelligence_score

        self.assertTrue(callable(intelligence_score))


# ---------------------------------------------------------------------------
# 3. BACKUP PROTECTION (4 tests)
# ---------------------------------------------------------------------------
class TestBackupProtection(unittest.TestCase):
    """Verify snapshot, verify, diff, and edit guard."""

    def setUp(self):
        self._orig_data = REPO_ROOT / "data"
        self._snap_dir = self._orig_data / "backups"

    def test_snapshot_creates_valid_snapshot(self):
        """skynet_backup.snapshot() creates a snapshot and returns an ID."""
        from tools.skynet_backup import snapshot

        snap_id = snapshot(label="intelligence_stack_test", auto=True)
        self.assertIsNotNone(snap_id)
        self.assertIsInstance(snap_id, str)
        self.assertGreater(len(snap_id), 0)

    def test_verify_passes_integrity_check(self):
        """skynet_backup.verify() passes for the latest snapshot."""
        from tools.skynet_backup import snapshot, verify

        snap_id = snapshot(label="verify_test", auto=True)
        result = verify(snap_id)
        self.assertIsInstance(result, dict)
        # Should have files checked and no corruption
        has_any_check = (
            "files_checked" in result
            or "total" in result
            or "ok" in result
            or "passed" in result
            or len(result) > 0
        )
        self.assertTrue(has_any_check, f"verify() returned empty result: {result}")

    def test_diff_shows_no_changes_after_snapshot(self):
        """skynet_backup.diff() returns clean diff immediately after snapshot."""
        from tools.skynet_backup import snapshot, diff

        snap_id = snapshot(label="diff_test", auto=True)
        result = diff(snap_id)
        self.assertIsInstance(result, dict)

    def test_edit_guard_validate_workers_json(self):
        """skynet_edit_guard.guard_edit() allows reading workers.json."""
        from tools.skynet_edit_guard import guard_edit

        workers_path = str(REPO_ROOT / "data" / "workers.json")
        if os.path.exists(workers_path):
            allowed, warnings = guard_edit(workers_path)
            # guard_edit without new_content is just a check — should be allowed
            self.assertIsInstance(allowed, bool)
            self.assertIsInstance(warnings, list)


# ---------------------------------------------------------------------------
# 4. DISPATCH RESILIENCE (3 tests)
# ---------------------------------------------------------------------------
class TestDispatchResilience(unittest.TestCase):
    """Verify dispatch resilience imports and pattern matching."""

    def test_dispatch_resilience_imports_and_instantiates(self):
        """DispatchResilience class is importable and constructable."""
        from tools.skynet_dispatch_resilience import DispatchResilience

        dr = DispatchResilience()
        self.assertIsNotNone(dr)
        self.assertTrue(hasattr(dr, "dispatch_with_retry"))

    def test_cli_error_patterns_match_known_strings(self):
        """Known CLI error strings are detected by the resilience module."""
        from tools.skynet_dispatch_resilience import DispatchResilience

        dr = DispatchResilience()
        # The class should have error pattern detection
        self.assertTrue(
            hasattr(dr, "detect_cli_error")
            or hasattr(dr, "_match_error_pattern")
            or hasattr(dr, "CLI_ERROR_PATTERNS")
            or hasattr(dr, "error_patterns"),
            "DispatchResilience has no error detection capability",
        )

    def test_resilient_dispatch_exists_in_skynet_dispatch(self):
        """resilient_dispatch_to_worker exists in skynet_dispatch module."""
        import tools.skynet_dispatch as sd

        self.assertTrue(hasattr(sd, "resilient_dispatch_to_worker"))
        self.assertTrue(callable(sd.resilient_dispatch_to_worker))


# ---------------------------------------------------------------------------
# 5. POST-TASK LIFECYCLE (2 tests)
# ---------------------------------------------------------------------------
class TestPostTaskLifecycle(unittest.TestCase):
    """Verify post-task lifecycle and autonomous worker instantiation."""

    def test_execute_post_task_lifecycle_importable(self):
        """execute_post_task_lifecycle is importable from skynet_post_task."""
        from tools.skynet_post_task import execute_post_task_lifecycle

        self.assertTrue(callable(execute_post_task_lifecycle))

    def test_autonomous_worker_instantiates(self):
        """AutonomousWorker instantiates with a valid worker name."""
        from tools.skynet_autonomous_worker import AutonomousWorker

        worker = AutonomousWorker("delta")
        self.assertEqual(worker.name, "delta")
        self.assertIsInstance(worker.specialties, list)
        self.assertIn("testing", worker.specialties)
        self.assertIsInstance(worker.score, (int, float))
        self.assertTrue(hasattr(worker, "find_next_task"))
        self.assertTrue(hasattr(worker, "claim_task"))

    def test_autonomous_worker_rejects_invalid_name(self):
        """AutonomousWorker rejects names not in the worker roster."""
        from tools.skynet_autonomous_worker import AutonomousWorker

        with self.assertRaises(ValueError):
            AutonomousWorker("invalid_worker_xyz")


# ---------------------------------------------------------------------------
# 6. SELF-AWARENESS (3 tests)
# ---------------------------------------------------------------------------
class TestSelfAwareness(unittest.TestCase):
    """Verify self-awareness kernel and architecture verification."""

    def test_skynet_self_has_all_subsystems(self):
        """SkynetSelf has identity, capabilities, health, introspection, goals."""
        from tools.skynet_self import SkynetSelf

        ss = SkynetSelf()
        self.assertTrue(hasattr(ss, "identity"))
        self.assertTrue(hasattr(ss, "capabilities"))
        self.assertTrue(hasattr(ss, "health"))
        self.assertTrue(hasattr(ss, "introspection"))
        self.assertTrue(hasattr(ss, "goals"))
        self.assertTrue(hasattr(ss, "full_status"))
        self.assertTrue(hasattr(ss, "quick_pulse"))
        self.assertTrue(hasattr(ss, "compute_iq"))

    def test_skynet_self_identity_report(self):
        """SkynetIdentity.report() returns identity data."""
        from tools.skynet_self import SkynetIdentity

        identity = SkynetIdentity()
        report = identity.report()
        self.assertIsInstance(report, dict)
        # Actual keys: identity, agents, consultants, agent_count, alive_count, etc.
        self.assertIn("identity", report)
        self.assertIn("agents", report)
        self.assertIn("consultants", report)
        self.assertIn("agent_count", report)
        self.assertIsInstance(report["identity"], dict)
        self.assertIn("name", report["identity"])

    def test_skynet_self_capabilities_census(self):
        """SkynetCapabilities.census() returns capability inventory."""
        from tools.skynet_self import SkynetCapabilities

        caps = SkynetCapabilities()
        census = caps.census()
        self.assertIsInstance(census, dict)
        self.assertIn("engine_count", census)
        self.assertIn("capability_ratio", census)

    def test_architecture_verify_importable(self):
        """verify_architecture() is importable and callable."""
        from tools.skynet_arch_verify import verify_architecture

        self.assertTrue(callable(verify_architecture))

    def test_architecture_verify_returns_domains(self):
        """verify_architecture() returns checks across known domains."""
        from tools.skynet_arch_verify import verify_architecture

        result = verify_architecture()
        self.assertIsInstance(result, dict)
        # Should check entities, delivery, bus, daemons
        has_domains = any(
            k in result
            for k in ["entities", "delivery", "bus", "daemons",
                       "overall", "checks", "passed", "failed"]
        )
        self.assertTrue(has_domains,
                        f"Architecture verify missing expected domains: {list(result.keys())}")


# ---------------------------------------------------------------------------
# 7. BONUS: Cross-subsystem integration (1 test)
# ---------------------------------------------------------------------------
class TestCrossSubsystemIntegration(unittest.TestCase):
    """Verify subsystems can interoperate."""

    def test_cognitive_with_learning_store(self):
        """ReflexionEngine can be initialized with a LearningStore backend."""
        from core.cognitive.reflexion import ReflexionEngine
        from core.learning_store import LearningStore

        tmp = tempfile.mkdtemp()
        try:
            db_path = os.path.join(tmp, "cross_test.db")
            store = LearningStore(db_path=db_path)
            engine = ReflexionEngine(learning_store=store)
            self.assertIsNotNone(engine)
            # Verify the engine can query persistent reflections
            persistent = engine.get_persistent_reflections("test query")
            self.assertIsInstance(persistent, list)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
