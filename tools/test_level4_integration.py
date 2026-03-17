#!/usr/bin/env python3
"""
test_level4_integration.py -- Level 4 Cognition Integration Tests.

Validates that all Level 4 cognitive engines are importable, instantiable,
and integrated into the Skynet pipeline. Also checks version consistency
across all configuration surfaces.

Usage:
    python tools/test_level4_integration.py          # Run all tests
    python tools/test_level4_integration.py -v        # Verbose output

# signed: delta
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


# ═══════════════════════════════════════════════════════════════
#  TEST 1: ReflexionEngine Integration
# ═══════════════════════════════════════════════════════════════

class TestReflexionIntegration(unittest.TestCase):
    """Verify ReflexionEngine from core/cognitive/reflexion.py is importable
    and functional with mock data."""
    # signed: delta

    def test_import_reflexion_module(self):
        """core.cognitive.reflexion imports without error."""
        from core.cognitive import reflexion
        self.assertTrue(hasattr(reflexion, "ReflexionEngine"))
        self.assertTrue(hasattr(reflexion, "FailureContext"))
        self.assertTrue(hasattr(reflexion, "Reflection"))

    def test_reflexion_engine_instantiable(self):
        """ReflexionEngine can be created without external dependencies."""
        from core.cognitive.reflexion import ReflexionEngine
        engine = ReflexionEngine()
        self.assertIsNotNone(engine)
        self.assertIsInstance(engine._reflections, list)
        self.assertEqual(engine._counter, 0)

    def test_reflexion_engine_with_memory(self):
        """ReflexionEngine works with EpisodicMemory attached."""
        from core.cognitive.reflexion import ReflexionEngine
        from core.cognitive.memory import EpisodicMemory
        memory = EpisodicMemory(working_capacity=7, episodic_capacity=100)
        engine = ReflexionEngine(memory=memory)
        self.assertIs(engine.memory, memory)

    def test_on_failure_produces_reflection(self):
        """on_failure() generates a Reflection from a FailureContext."""
        from core.cognitive.reflexion import ReflexionEngine, FailureContext
        engine = ReflexionEngine()
        ctx = FailureContext(
            action_type="code_edit",
            action_target="tools/skynet_dispatch.py",
            action_value="fix ghost_type",
            error_message="FileNotFoundError: dispatch.py not found",
            error_type="FileNotFoundError",
        )
        reflection = engine.on_failure(ctx)
        self.assertIsNotNone(reflection)
        self.assertTrue(len(reflection.critique) > 0)
        self.assertTrue(len(reflection.lesson) > 0)
        self.assertEqual(len(engine._reflections), 1)

    def test_failure_pattern_tracking(self):
        """Repeated failures of same type are tracked in _failure_patterns."""
        from core.cognitive.reflexion import ReflexionEngine, FailureContext
        engine = ReflexionEngine()
        for i in range(3):
            ctx = FailureContext(
                action_type="click",
                action_target=f"button_{i}",
                error_message="Element not found",
                error_type="ElementNotFound",
            )
            engine.on_failure(ctx)
        self.assertEqual(len(engine._reflections), 3)
        # Patterns are tracked by action_type:error_type composite key
        self.assertIn("click:ElementNotFound", engine._failure_patterns)


# ═══════════════════════════════════════════════════════════════
#  TEST 2: Graph of Thoughts Integration
# ═══════════════════════════════════════════════════════════════

class TestGraphOfThoughtsIntegration(unittest.TestCase):
    """Verify GraphOfThoughts from core/cognitive/graph_of_thoughts.py."""
    # signed: delta

    def test_import_got_module(self):
        """core.cognitive.graph_of_thoughts imports without error."""
        from core.cognitive import graph_of_thoughts
        self.assertTrue(hasattr(graph_of_thoughts, "GraphOfThoughts"))
        self.assertTrue(hasattr(graph_of_thoughts, "Thought"))
        self.assertTrue(hasattr(graph_of_thoughts, "ThoughtStatus"))
        self.assertTrue(hasattr(graph_of_thoughts, "GoTReasoner"))

    def test_got_instantiable(self):
        """GraphOfThoughts can be created with default params."""
        from core.cognitive.graph_of_thoughts import GraphOfThoughts
        got = GraphOfThoughts()
        self.assertEqual(got.max_depth, 10)
        self.assertEqual(got.max_branches, 5)
        self.assertAlmostEqual(got.prune_threshold, 0.2)
        self.assertEqual(len(got._thoughts), 0)

    def test_got_add_thought(self):
        """add_thought() creates a root thought vertex."""
        from core.cognitive.graph_of_thoughts import GraphOfThoughts
        got = GraphOfThoughts()
        root = got.add_thought("Solve complex problem X")
        self.assertIsNotNone(root)
        self.assertEqual(root.content, "Solve complex problem X")
        self.assertEqual(root.depth, 0)
        self.assertEqual(got._root_id, root.id)

    def test_got_generate_branches(self):
        """generate() creates child thoughts from a parent."""
        from core.cognitive.graph_of_thoughts import GraphOfThoughts
        got = GraphOfThoughts()
        root = got.add_thought("Root problem")
        b1 = got.generate(root.id, "Approach A", score=0.6)
        b2 = got.generate(root.id, "Approach B", score=0.7)
        self.assertEqual(b1.depth, 1)
        self.assertEqual(b2.depth, 1)
        self.assertIn(b1.id, root.child_ids)
        self.assertIn(b2.id, root.child_ids)
        self.assertEqual(len(got._thoughts), 3)  # root + 2 branches

    def test_got_aggregate(self):
        """aggregate() merges multiple thoughts into one."""
        from core.cognitive.graph_of_thoughts import GraphOfThoughts
        got = GraphOfThoughts()
        root = got.add_thought("Problem")
        b1 = got.generate(root.id, "Path A", score=0.6)
        b2 = got.generate(root.id, "Path B", score=0.7)
        merged = got.aggregate([b1.id, b2.id], "Merged synthesis", score=0.8)
        self.assertIsNotNone(merged)
        self.assertEqual(merged.score, 0.8)
        self.assertIn("Merged synthesis", merged.content)

    def test_got_score_and_prune(self):
        """score_all() and prune() work without crashing."""
        from core.cognitive.graph_of_thoughts import GraphOfThoughts
        got = GraphOfThoughts(prune_threshold=0.3)
        root = got.add_thought("Problem", score=0.5)
        got.generate(root.id, "Good path", score=0.8)
        got.generate(root.id, "Bad path", score=0.1)
        got.score_all()
        got.prune()
        # Low-scoring branch should have been pruned
        remaining_scores = [t.score for t in got._thoughts.values()
                           if t.status.value == "active"]
        for s in remaining_scores:
            self.assertGreaterEqual(s, 0.0)

    def test_got_resolve(self):
        """resolve() returns a string resolution."""
        from core.cognitive.graph_of_thoughts import GraphOfThoughts
        got = GraphOfThoughts()
        root = got.add_thought("Build feature X", score=0.5)
        got.generate(root.id, "Design approach", score=0.7)
        got.score_all()
        result = got.resolve()
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_got_reasoner(self):
        """GoTReasoner.reason() returns (resolution, graph) tuple."""
        from core.cognitive.graph_of_thoughts import GoTReasoner
        reasoner = GoTReasoner()
        resolution, graph = reasoner.reason(
            "Build feature X",
            perspectives=["Performance view", "Security view"],
            max_depth=3,
        )
        self.assertIsInstance(resolution, str)
        self.assertGreater(len(graph._thoughts), 0)


# ═══════════════════════════════════════════════════════════════
#  TEST 3: Knowledge Distillation Integration
# ═══════════════════════════════════════════════════════════════

class TestKnowledgeDistillIntegration(unittest.TestCase):
    """Verify KnowledgeDistiller and distill_hook pipeline."""
    # signed: delta

    def test_import_knowledge_distill(self):
        """core.cognitive.knowledge_distill imports without error."""
        from core.cognitive import knowledge_distill
        self.assertTrue(hasattr(knowledge_distill, "KnowledgeDistiller"))

    def test_import_distill_hook(self):
        """tools.skynet_distill_hook imports and has key functions."""
        from tools import skynet_distill_hook
        self.assertTrue(callable(getattr(skynet_distill_hook, "distill_result", None)))
        self.assertTrue(callable(getattr(skynet_distill_hook, "distill_scan_bus", None)))
        self.assertTrue(callable(getattr(skynet_distill_hook, "get_distill_stats", None)))

    def test_distiller_instantiable(self):
        """KnowledgeDistiller can be created with EpisodicMemory."""
        from core.cognitive.knowledge_distill import KnowledgeDistiller
        from core.cognitive.memory import EpisodicMemory
        memory = EpisodicMemory(working_capacity=7, episodic_capacity=100)
        distiller = KnowledgeDistiller(
            memory=memory,
            decay_threshold=0.3,
            min_cluster_size=2,
        )
        self.assertIsNotNone(distiller)
        self.assertEqual(distiller.decay_threshold, 0.3)
        self.assertEqual(distiller.min_cluster_size, 2)

    def test_distill_empty_memory(self):
        """distill() on empty memory returns gracefully."""
        from core.cognitive.knowledge_distill import KnowledgeDistiller
        from core.cognitive.memory import EpisodicMemory
        memory = EpisodicMemory(working_capacity=7, episodic_capacity=100)
        distiller = KnowledgeDistiller(memory=memory)
        result = distiller.distill()
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("distilled", 0), 0)

    def test_distill_result_mock(self):
        """distill_result() does not crash with mock data."""
        from tools.skynet_distill_hook import distill_result
        result = distill_result(
            worker="alpha",
            task_text="Fix CORS header in auth.py",
            result_text="Fixed X-Frame-Options and Access-Control headers. All tests pass.",
            success=True,
        )
        self.assertIsInstance(result, dict)
        self.assertIn("episodic_stored", result)
        self.assertIn("patterns_extracted", result)
        self.assertIn("insights", result)
        self.assertIsInstance(result["insights"], list)

    def test_extract_tags(self):
        """_extract_tags() produces relevant domain tags."""
        from tools.skynet_distill_hook import _extract_tags
        tags = _extract_tags(
            "Fix dashboard CSS",
            "Updated god_console.html styles",
            "beta",
        )
        self.assertIn("beta", tags)
        self.assertIn("dashboard", tags)


# ═══════════════════════════════════════════════════════════════
#  TEST 4: Memory Architecture
# ═══════════════════════════════════════════════════════════════

class TestMemoryArchitecture(unittest.TestCase):
    """Verify the 3-tier memory system (working/episodic/semantic)."""
    # signed: delta

    def test_import_memory_module(self):
        """core.cognitive.memory imports with all key classes."""
        from core.cognitive.memory import EpisodicMemory, MemoryEntry, MemoryType
        self.assertIsNotNone(EpisodicMemory)
        self.assertIsNotNone(MemoryEntry)
        self.assertIsNotNone(MemoryType)

    def test_memory_3_tier_init(self):
        """EpisodicMemory initializes with 3 stores: working, episodic, semantic."""
        from core.cognitive.memory import EpisodicMemory
        mem = EpisodicMemory(working_capacity=7, episodic_capacity=500)
        self.assertEqual(mem.working_capacity, 7)
        self.assertEqual(mem.episodic_capacity, 500)
        self.assertIsInstance(mem._working, list)
        self.assertIsInstance(mem._episodic, list)
        self.assertIsInstance(mem._semantic, list)

    def test_store_and_retrieve_episodic(self):
        """Can store and retrieve episodic memories."""
        from core.cognitive.memory import EpisodicMemory
        mem = EpisodicMemory(working_capacity=7, episodic_capacity=100)
        mem.store_episodic(
            content="Opened Chrome and searched for AI agents",
            tags=["chrome", "search"],
            source_action="browser_open",
            importance=0.6,
        )
        self.assertEqual(len(mem._episodic), 1)
        results = mem.retrieve("chrome search", limit=5)
        self.assertGreater(len(results), 0)


# ═══════════════════════════════════════════════════════════════
#  TEST 5: Version Consistency
# ═══════════════════════════════════════════════════════════════

class TestVersionConsistency(unittest.TestCase):
    """Verify version 4.0 is set consistently across all config surfaces."""
    # signed: delta

    def test_skynet_self_version(self):
        """skynet_self.py SkynetIdentity reports version 4.0."""
        from tools.skynet_self import SkynetIdentity
        identity = SkynetIdentity()
        self.assertEqual(identity.version, "4.0")
        self.assertEqual(identity.level, 4)

    def test_skynet_version_constants(self):
        """skynet_version.py constants report 4.0."""
        from tools.skynet_version import CURRENT_VERSION, CURRENT_LEVEL, CURRENT_CODENAME
        self.assertEqual(CURRENT_VERSION, "4.0")
        self.assertEqual(CURRENT_LEVEL, 4)
        self.assertEqual(CURRENT_CODENAME, "Cognition")

    def test_brain_config_level(self):
        """data/brain_config.json level is 4.0."""
        config_path = ROOT / "data" / "brain_config.json"
        self.assertTrue(config_path.exists(), "brain_config.json not found")
        config = json.loads(config_path.read_text())
        self.assertEqual(config.get("level"), "4.0")

    def test_version_history_has_level4(self):
        """data/version_history.json contains a Level 4 entry."""
        vh_path = ROOT / "data" / "version_history.json"
        self.assertTrue(vh_path.exists(), "version_history.json not found")
        history = json.loads(vh_path.read_text())
        self.assertIsInstance(history, list)
        self.assertGreater(len(history), 0)
        level4_entries = [e for e in history if e.get("level") == 4]
        self.assertGreater(len(level4_entries), 0,
                          "No Level 4 entry in version_history.json")
        latest_l4 = level4_entries[-1]
        self.assertEqual(latest_l4.get("version"), "4.0")
        self.assertEqual(latest_l4.get("codename"), "Cognition")

    def test_level4_architecture_doc_exists(self):
        """data/level4_architecture.md exists and has substantial content."""
        arch_path = ROOT / "data" / "level4_architecture.md"
        self.assertTrue(arch_path.exists(), "level4_architecture.md not found")
        content = arch_path.read_text(encoding="utf-8")
        self.assertGreater(len(content), 5000,
                          "Architecture doc too short")
        self.assertIn("Cognition", content)
        self.assertIn("ReflexionEngine", content)
        self.assertIn("GraphOfThoughts", content)
        self.assertIn("KnowledgeDistiller", content)

    def test_version_history_progression(self):
        """Version history covers all levels: Genesis through Cognition."""
        vh_path = ROOT / "data" / "version_history.json"
        history = json.loads(vh_path.read_text())
        levels_present = {e.get("level") for e in history}
        self.assertIn(1, levels_present, "Missing Level 1 (Genesis)")
        self.assertIn(2, levels_present, "Missing Level 2 (Awakening)")
        self.assertIn(3, levels_present, "Missing Level 3 (Production)")
        self.assertIn(4, levels_present, "Missing Level 4 (Cognition)")


# ═══════════════════════════════════════════════════════════════
#  TEST 6: MCTS Module (Future Integration)
# ═══════════════════════════════════════════════════════════════

class TestMCTSModule(unittest.TestCase):
    """Verify MCTS module is importable (not yet wired into pipeline)."""
    # signed: delta

    def test_import_mcts(self):
        """core.cognitive.mcts imports without error."""
        from core.cognitive import mcts
        self.assertTrue(hasattr(mcts, "ReflectiveMCTS"))
        self.assertTrue(hasattr(mcts, "NavigationState"))
        self.assertTrue(hasattr(mcts, "DualOptimizationMCTS"))

    def test_mcts_instantiable(self):
        """ReflectiveMCTS can be created with defaults."""
        from core.cognitive.mcts import ReflectiveMCTS
        nav = ReflectiveMCTS()
        self.assertIsNotNone(nav)


if __name__ == "__main__":
    unittest.main(verbosity=2)
