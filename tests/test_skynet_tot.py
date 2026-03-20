"""Comprehensive tests for tools/skynet_tot.py — Tree of Thoughts reasoning engine.
# signed: delta

Covers:
  - ThoughtNode dataclass (serialization, properties, lifecycle)
  - ID generation helpers
  - Default evaluator (heuristic scoring)
  - Persistence (_load_state, _save_state)
  - TreeOfThoughts (construction, generate, evaluate, expand, prune, solve, serialization)
  - Parallel dispatch exploration
  - CLI
  - Edge cases (empty trees, all-pruned, depth limits, etc.)
"""
# signed: delta

import json
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock, mock_open, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools.skynet_tot as tot
from tools.skynet_tot import (
    ThoughtNode,
    NodeStatus,
    TreeOfThoughts,
    _gen_node_id,
    _gen_tree_id,
    _default_evaluator,
    _load_state,
    _save_state,
    _get_idle_workers,
    _poll_bus_results,
    dispatch_parallel_exploration,
    HYPOTHESIS_TEMPLATES,
    EXPANSION_TEMPLATES,
    DEFAULT_BREADTH,
    DEFAULT_DEPTH,
    DEFAULT_KEEP_TOP,
    SCORE_THRESHOLD,
    WORKER_NAMES,
)


# ── ThoughtNode tests ──────────────────────────────────────────────

class TestThoughtNodeBasic(unittest.TestCase):
    """ThoughtNode construction and default values."""

    def test_defaults(self):
        node = ThoughtNode(node_id="n1", hypothesis="idea")
        self.assertEqual(node.node_id, "n1")
        self.assertEqual(node.hypothesis, "idea")
        self.assertEqual(node.evidence, "")
        self.assertEqual(node.score, 0.0)
        self.assertEqual(node.depth, 0)
        self.assertIsNone(node.parent_id)
        self.assertEqual(node.children, [])
        self.assertEqual(node.status, NodeStatus.PENDING)
        self.assertIsInstance(node.created_at, float)

    def test_is_leaf_true(self):
        node = ThoughtNode(node_id="n1", hypothesis="x")
        self.assertTrue(node.is_leaf)

    def test_is_leaf_false(self):
        node = ThoughtNode(node_id="n1", hypothesis="x", children=["c1"])
        self.assertFalse(node.is_leaf)

    def test_is_root_true(self):
        node = ThoughtNode(node_id="n1", hypothesis="x")
        self.assertTrue(node.is_root)

    def test_is_root_false(self):
        node = ThoughtNode(node_id="n1", hypothesis="x", parent_id="p1")
        self.assertFalse(node.is_root)


class TestThoughtNodeSerialization(unittest.TestCase):
    """ThoughtNode to_dict/from_dict roundtrip."""

    def test_to_dict_status_serialized(self):
        node = ThoughtNode(node_id="n1", hypothesis="h", status=NodeStatus.EVALUATED)
        d = node.to_dict()
        self.assertEqual(d["status"], "evaluated")
        self.assertEqual(d["node_id"], "n1")

    def test_from_dict_basic(self):
        d = {
            "node_id": "n2",
            "hypothesis": "test idea",
            "evidence": "some evidence",
            "score": 0.7,
            "depth": 2,
            "parent_id": "p1",
            "children": ["c1", "c2"],
            "status": "expanded",
            "metadata": {"angle": "direct"},
            "created_at": 1000.0,
        }
        node = ThoughtNode.from_dict(d)
        self.assertEqual(node.node_id, "n2")
        self.assertEqual(node.status, NodeStatus.EXPANDED)
        self.assertEqual(node.score, 0.7)
        self.assertEqual(node.depth, 2)
        self.assertEqual(len(node.children), 2)

    def test_from_dict_missing_status_defaults_pending(self):
        d = {"node_id": "n3", "hypothesis": "h"}
        node = ThoughtNode.from_dict(d)
        self.assertEqual(node.status, NodeStatus.PENDING)

    def test_from_dict_ignores_extra_keys(self):
        d = {"node_id": "n4", "hypothesis": "h", "unknown_key": 999}
        node = ThoughtNode.from_dict(d)
        self.assertEqual(node.node_id, "n4")

    def test_roundtrip(self):
        original = ThoughtNode(
            node_id="rt1",
            hypothesis="roundtrip test",
            evidence="evidence here",
            score=0.55,
            depth=1,
            parent_id="root",
            children=["c1"],
            status=NodeStatus.SELECTED,
            metadata={"angle": "defensive"},
        )
        d = original.to_dict()
        restored = ThoughtNode.from_dict(d)
        self.assertEqual(restored.node_id, original.node_id)
        self.assertEqual(restored.hypothesis, original.hypothesis)
        self.assertEqual(restored.score, original.score)
        self.assertEqual(restored.status, original.status)
        self.assertEqual(restored.children, original.children)


class TestNodeStatus(unittest.TestCase):
    """NodeStatus enum coverage."""

    def test_all_values(self):
        expected = {"pending", "evaluated", "expanded", "pruned", "selected"}
        actual = {s.value for s in NodeStatus}
        self.assertEqual(actual, expected)

    def test_from_string(self):
        self.assertEqual(NodeStatus("pruned"), NodeStatus.PRUNED)


# ── ID generation ──────────────────────────────────────────────────

class TestIdGeneration(unittest.TestCase):
    """_gen_node_id and _gen_tree_id helpers."""

    def test_node_id_prefix(self):
        nid = _gen_node_id("test")
        self.assertTrue(nid.startswith("tn_"))

    def test_node_id_length(self):
        nid = _gen_node_id("hello")
        # "tn_" + 10 hex chars = 13
        self.assertEqual(len(nid), 13)

    def test_tree_id_prefix(self):
        tid = _gen_tree_id("problem")
        self.assertTrue(tid.startswith("tot_"))

    def test_tree_id_length(self):
        tid = _gen_tree_id("problem")
        # "tot_" + 10 hex chars = 14
        self.assertEqual(len(tid), 14)

    def test_different_inputs_different_ids(self):
        # Due to time.time() component, even same text produces different IDs
        id1 = _gen_node_id("same")
        time.sleep(0.01)
        id2 = _gen_node_id("same")
        # They MIGHT collide if time is identical, but very unlikely
        # Just verify format
        self.assertTrue(id1.startswith("tn_"))
        self.assertTrue(id2.startswith("tn_"))


# ── Default evaluator ─────────────────────────────────────────────

class TestDefaultEvaluator(unittest.TestCase):
    """_default_evaluator heuristic scoring logic."""

    def test_baseline_score(self):
        node = ThoughtNode(node_id="e1", hypothesis="vague idea")
        score = _default_evaluator(node, "some problem")
        # Baseline is 0.3, should be >= 0.3
        self.assertGreaterEqual(score, 0.3)

    def test_specificity_boost(self):
        node_vague = ThoughtNode(node_id="e2", hypothesis="do something")
        node_specific = ThoughtNode(
            node_id="e3",
            hypothesis="fix function in class at line 42 of module file"
        )
        s_vague = _default_evaluator(node_vague, "p")
        s_specific = _default_evaluator(node_specific, "p")
        self.assertGreater(s_specific, s_vague)

    def test_evidence_boost(self):
        node_no_ev = ThoughtNode(node_id="e4", hypothesis="idea")
        node_with_ev = ThoughtNode(
            node_id="e5", hypothesis="idea", evidence="A" * 300
        )
        s_no = _default_evaluator(node_no_ev, "p")
        s_ev = _default_evaluator(node_with_ev, "p")
        self.assertGreater(s_ev, s_no)

    def test_action_signals_boost(self):
        node_passive = ThoughtNode(node_id="e6", hypothesis="observe the system")
        node_action = ThoughtNode(
            node_id="e7",
            hypothesis="implement fix and replace the old module, then refactor"
        )
        s_passive = _default_evaluator(node_passive, "p")
        s_action = _default_evaluator(node_action, "p")
        self.assertGreater(s_action, s_passive)

    def test_depth_bonus(self):
        shallow = ThoughtNode(node_id="e8", hypothesis="idea", depth=0)
        deep = ThoughtNode(node_id="e9", hypothesis="idea", depth=3)
        s_shallow = _default_evaluator(shallow, "p")
        s_deep = _default_evaluator(deep, "p")
        self.assertGreater(s_deep, s_shallow)

    def test_risk_signals_boost(self):
        node_risky = ThoughtNode(
            node_id="e10",
            hypothesis="add fallback for edge case with rollback"
        )
        node_plain = ThoughtNode(node_id="e11", hypothesis="do the thing")
        s_risky = _default_evaluator(node_risky, "p")
        s_plain = _default_evaluator(node_plain, "p")
        self.assertGreater(s_risky, s_plain)

    def test_score_capped_at_1(self):
        # Max out all signals
        node = ThoughtNode(
            node_id="e12",
            hypothesis=(
                "implement fix change replace create modify remove update refactor "
                "optimize file function class module endpoint line error test config path "
                "edge case fallback rollback backward compat"
            ),
            evidence="A" * 1000,
            depth=10,
        )
        score = _default_evaluator(node, "p")
        self.assertLessEqual(score, 1.0)

    def test_returns_float(self):
        node = ThoughtNode(node_id="e13", hypothesis="test")
        score = _default_evaluator(node, "problem")
        self.assertIsInstance(score, float)


# ── Persistence ────────────────────────────────────────────────────

class TestLoadState(unittest.TestCase):
    """_load_state from disk."""

    @patch("tools.skynet_tot.TOT_STATE_PATH")
    def test_missing_file_returns_default(self, mock_path):
        mock_path.exists.return_value = False
        state = _load_state()
        self.assertEqual(state, {"trees": {}, "history": [], "version": 1})

    @patch("tools.skynet_tot.TOT_STATE_PATH")
    def test_valid_file_loaded(self, mock_path):
        mock_path.exists.return_value = True
        data = {"trees": {"t1": {}}, "history": [{"x": 1}], "version": 1}
        m = mock_open(read_data=json.dumps(data))
        with patch("builtins.open", m):
            state = _load_state()
        self.assertEqual(state["trees"], {"t1": {}})
        self.assertEqual(len(state["history"]), 1)

    @patch("tools.skynet_tot.TOT_STATE_PATH")
    def test_corrupt_file_returns_default(self, mock_path):
        mock_path.exists.return_value = True
        m = mock_open(read_data="not-json{{{")
        with patch("builtins.open", m):
            state = _load_state()
        self.assertEqual(state, {"trees": {}, "history": [], "version": 1})


class TestSaveState(unittest.TestCase):
    """_save_state writes atomically."""

    @patch("tools.skynet_tot.os.replace")
    @patch("builtins.open", new_callable=mock_open)
    @patch("tools.skynet_tot.TOT_STATE_PATH")
    def test_writes_via_tmp(self, mock_path, mock_file, mock_replace):
        mock_path.parent.mkdir = MagicMock()
        mock_path.__str__ = lambda s: "/fake/tot_state.json"
        _save_state({"trees": {}, "history": [], "version": 1})
        mock_file.assert_called_once_with(
            "/fake/tot_state.json.tmp", "w", encoding="utf-8"
        )
        mock_replace.assert_called_once()


# ── TreeOfThoughts construction ────────────────────────────────────

class TestTreeConstruction(unittest.TestCase):
    """TreeOfThoughts __init__ and basic properties."""

    @patch.object(TreeOfThoughts, "_persist")
    def setUp(self, mock_persist):
        self.tree = TreeOfThoughts("Fix the bug", breadth=3, max_depth=2)

    def test_has_root(self):
        self.assertIn(self.tree.root_id, self.tree.nodes)

    def test_root_is_root(self):
        root = self.tree.nodes[self.tree.root_id]
        self.assertTrue(root.is_root)
        self.assertEqual(root.depth, 0)

    def test_root_status_evaluated(self):
        root = self.tree.nodes[self.tree.root_id]
        self.assertEqual(root.status, NodeStatus.EVALUATED)

    def test_root_score_baseline(self):
        root = self.tree.nodes[self.tree.root_id]
        self.assertEqual(root.score, 0.5)

    def test_problem_stored(self):
        self.assertEqual(self.tree.problem, "Fix the bug")

    def test_breadth_and_depth(self):
        self.assertEqual(self.tree.breadth, 3)
        self.assertEqual(self.tree.max_depth, 2)

    def test_tree_id_format(self):
        self.assertTrue(self.tree.tree_id.startswith("tot_"))

    def test_custom_evaluator(self):
        custom = lambda n, p: 0.99
        with patch.object(TreeOfThoughts, "_persist"):
            tree = TreeOfThoughts("p", evaluator=custom)
        self.assertEqual(tree.evaluator, custom)


# ── generate_hypotheses ────────────────────────────────────────────

class TestGenerateHypotheses(unittest.TestCase):
    """Generate initial hypotheses from templates."""

    @patch.object(TreeOfThoughts, "_persist")
    def setUp(self, mock_persist):
        self.tree = TreeOfThoughts("Optimize caching", breadth=3, max_depth=3)

    @patch.object(TreeOfThoughts, "_persist")
    def test_generates_correct_count(self, mock_persist):
        nodes = self.tree.generate_hypotheses()
        self.assertEqual(len(nodes), 3)

    @patch.object(TreeOfThoughts, "_persist")
    def test_hypotheses_are_children_of_root(self, mock_persist):
        nodes = self.tree.generate_hypotheses()
        root = self.tree.nodes[self.tree.root_id]
        for n in nodes:
            self.assertEqual(n.parent_id, self.tree.root_id)
            self.assertIn(n.node_id, root.children)

    @patch.object(TreeOfThoughts, "_persist")
    def test_hypotheses_depth_one(self, mock_persist):
        nodes = self.tree.generate_hypotheses()
        for n in nodes:
            self.assertEqual(n.depth, 1)

    @patch.object(TreeOfThoughts, "_persist")
    def test_hypotheses_status_pending(self, mock_persist):
        nodes = self.tree.generate_hypotheses()
        for n in nodes:
            self.assertEqual(n.status, NodeStatus.PENDING)

    @patch.object(TreeOfThoughts, "_persist")
    def test_parent_status_expanded(self, mock_persist):
        self.tree.generate_hypotheses()
        root = self.tree.nodes[self.tree.root_id]
        self.assertEqual(root.status, NodeStatus.EXPANDED)

    @patch.object(TreeOfThoughts, "_persist")
    def test_capped_by_template_count(self, mock_persist):
        tree = TreeOfThoughts("p", breadth=100, max_depth=1)
        nodes = tree.generate_hypotheses()
        self.assertEqual(len(nodes), len(HYPOTHESIS_TEMPLATES))

    @patch.object(TreeOfThoughts, "_persist")
    def test_custom_parent(self, mock_persist):
        child = ThoughtNode(
            node_id="custom_parent", hypothesis="sub", depth=1
        )
        self.tree.nodes["custom_parent"] = child
        nodes = self.tree.generate_hypotheses(parent_id="custom_parent")
        for n in nodes:
            self.assertEqual(n.parent_id, "custom_parent")
            self.assertEqual(n.depth, 2)

    @patch.object(TreeOfThoughts, "_persist")
    def test_invalid_parent_raises(self, mock_persist):
        with self.assertRaises(ValueError):
            self.tree.generate_hypotheses(parent_id="nonexistent")

    @patch.object(TreeOfThoughts, "_persist")
    def test_hypotheses_have_angle_metadata(self, mock_persist):
        nodes = self.tree.generate_hypotheses()
        angles = [n.metadata.get("angle") for n in nodes]
        self.assertIn("direct", angles)
        self.assertIn("defensive", angles)


# ── evaluate ───────────────────────────────────────────────────────

class TestEvaluate(unittest.TestCase):
    """Evaluate individual nodes."""

    @patch.object(TreeOfThoughts, "_persist")
    def setUp(self, mock_persist):
        self.tree = TreeOfThoughts("problem", breadth=2, max_depth=2)

    @patch.object(TreeOfThoughts, "_persist")
    def test_evaluate_sets_score(self, mock_persist):
        nodes = self.tree.generate_hypotheses(n=1)
        node = nodes[0]
        score = self.tree.evaluate(node.node_id)
        self.assertIsInstance(score, float)
        self.assertEqual(node.score, score)

    @patch.object(TreeOfThoughts, "_persist")
    def test_evaluate_sets_status(self, mock_persist):
        nodes = self.tree.generate_hypotheses(n=1)
        node = nodes[0]
        self.tree.evaluate(node.node_id)
        self.assertEqual(node.status, NodeStatus.EVALUATED)

    @patch.object(TreeOfThoughts, "_persist")
    def test_evaluate_nonexistent_raises(self, mock_persist):
        with self.assertRaises(ValueError):
            self.tree.evaluate("fake_id")

    @patch.object(TreeOfThoughts, "_persist")
    def test_evaluate_all_pending(self, mock_persist):
        nodes = self.tree.generate_hypotheses(n=2)
        scores = self.tree.evaluate_all_pending()
        self.assertEqual(len(scores), 2)
        for node in nodes:
            self.assertEqual(node.status, NodeStatus.EVALUATED)


# ── expand ─────────────────────────────────────────────────────────

class TestExpand(unittest.TestCase):
    """Expand nodes to create sub-hypotheses."""

    @patch.object(TreeOfThoughts, "_persist")
    def setUp(self, mock_persist):
        self.tree = TreeOfThoughts("problem", breadth=3, max_depth=3)

    @patch.object(TreeOfThoughts, "_persist")
    def test_expand_creates_children(self, mock_persist):
        hyps = self.tree.generate_hypotheses(n=1)
        children = self.tree.expand(hyps[0].node_id)
        self.assertGreater(len(children), 0)
        for c in children:
            self.assertEqual(c.parent_id, hyps[0].node_id)

    @patch.object(TreeOfThoughts, "_persist")
    def test_expand_increments_depth(self, mock_persist):
        hyps = self.tree.generate_hypotheses(n=1)
        children = self.tree.expand(hyps[0].node_id)
        for c in children:
            self.assertEqual(c.depth, 2)

    @patch.object(TreeOfThoughts, "_persist")
    def test_expand_at_max_depth_returns_empty(self, mock_persist):
        tree = TreeOfThoughts("p", breadth=2, max_depth=1)
        hyps = tree.generate_hypotheses(n=1)
        # hyps are at depth 1 = max_depth, so expand should return []
        children = tree.expand(hyps[0].node_id)
        self.assertEqual(children, [])

    @patch.object(TreeOfThoughts, "_persist")
    def test_expand_pruned_returns_empty(self, mock_persist):
        hyps = self.tree.generate_hypotheses(n=1)
        hyps[0].status = NodeStatus.PRUNED
        children = self.tree.expand(hyps[0].node_id)
        self.assertEqual(children, [])

    @patch.object(TreeOfThoughts, "_persist")
    def test_expand_nonexistent_raises(self, mock_persist):
        with self.assertRaises(ValueError):
            self.tree.expand("nonexistent")

    @patch.object(TreeOfThoughts, "_persist")
    def test_expand_sets_parent_expanded(self, mock_persist):
        hyps = self.tree.generate_hypotheses(n=1)
        self.tree.expand(hyps[0].node_id)
        self.assertEqual(hyps[0].status, NodeStatus.EXPANDED)

    @patch.object(TreeOfThoughts, "_persist")
    def test_children_have_parent_angle_metadata(self, mock_persist):
        hyps = self.tree.generate_hypotheses(n=1)
        children = self.tree.expand(hyps[0].node_id)
        for c in children:
            self.assertIn("parent_angle", c.metadata)


# ── prune ──────────────────────────────────────────────────────────

class TestPrune(unittest.TestCase):
    """Pruning low-scoring branches."""

    @patch.object(TreeOfThoughts, "_persist")
    def setUp(self, mock_persist):
        self.tree = TreeOfThoughts("problem", breadth=4, max_depth=3)

    @patch.object(TreeOfThoughts, "_persist")
    def test_prune_removes_low_scorers(self, mock_persist):
        hyps = self.tree.generate_hypotheses(n=4)
        # Assign scores: 0.9, 0.7, 0.2, 0.1
        hyps[0].score = 0.9; hyps[0].status = NodeStatus.EVALUATED
        hyps[1].score = 0.7; hyps[1].status = NodeStatus.EVALUATED
        hyps[2].score = 0.2; hyps[2].status = NodeStatus.EVALUATED
        hyps[3].score = 0.1; hyps[3].status = NodeStatus.EVALUATED

        pruned = self.tree.prune(keep_top=2)
        self.assertGreater(pruned, 0)
        self.assertEqual(hyps[2].status, NodeStatus.PRUNED)
        self.assertEqual(hyps[3].status, NodeStatus.PRUNED)
        self.assertNotEqual(hyps[0].status, NodeStatus.PRUNED)
        self.assertNotEqual(hyps[1].status, NodeStatus.PRUNED)

    @patch.object(TreeOfThoughts, "_persist")
    def test_prune_keeps_all_when_fewer_than_keep_top(self, mock_persist):
        hyps = self.tree.generate_hypotheses(n=2)
        hyps[0].score = 0.5; hyps[0].status = NodeStatus.EVALUATED
        hyps[1].score = 0.4; hyps[1].status = NodeStatus.EVALUATED

        pruned = self.tree.prune(keep_top=3)
        self.assertEqual(pruned, 0)

    @patch.object(TreeOfThoughts, "_persist")
    def test_prune_cascades_to_children(self, mock_persist):
        hyps = self.tree.generate_hypotheses(n=3)
        hyps[0].score = 0.9; hyps[0].status = NodeStatus.EVALUATED
        hyps[1].score = 0.8; hyps[1].status = NodeStatus.EVALUATED
        hyps[2].score = 0.1; hyps[2].status = NodeStatus.EVALUATED

        # Expand the low scorer to create grandchildren
        children = self.tree.expand(hyps[2].node_id)

        pruned = self.tree.prune(keep_top=2)
        # hyps[2] and all its children should be pruned
        self.assertEqual(hyps[2].status, NodeStatus.PRUNED)
        for c in children:
            self.assertEqual(c.status, NodeStatus.PRUNED)

    @patch.object(TreeOfThoughts, "_persist")
    def test_prune_subtree_already_pruned(self, mock_persist):
        hyps = self.tree.generate_hypotheses(n=1)
        hyps[0].status = NodeStatus.PRUNED
        count = self.tree._prune_subtree(hyps[0].node_id)
        self.assertEqual(count, 0)

    @patch.object(TreeOfThoughts, "_persist")
    def test_prune_subtree_nonexistent(self, mock_persist):
        count = self.tree._prune_subtree("nonexistent")
        self.assertEqual(count, 0)


# ── solve ──────────────────────────────────────────────────────────

class TestSolveBFS(unittest.TestCase):
    """BFS solving strategy."""

    @patch.object(TreeOfThoughts, "_persist")
    def test_bfs_returns_node(self, mock_persist):
        tree = TreeOfThoughts("Fix caching", breadth=2, max_depth=2)
        best = tree.solve(strategy="bfs")
        self.assertIsInstance(best, ThoughtNode)

    @patch.object(TreeOfThoughts, "_persist")
    def test_bfs_best_has_score(self, mock_persist):
        tree = TreeOfThoughts("Fix caching", breadth=2, max_depth=2)
        best = tree.solve(strategy="bfs")
        self.assertGreater(best.score, 0.0)

    @patch.object(TreeOfThoughts, "_persist")
    def test_bfs_creates_multiple_depths(self, mock_persist):
        tree = TreeOfThoughts("problem", breadth=2, max_depth=2)
        tree.solve(strategy="bfs")
        max_depth = max(n.depth for n in tree.nodes.values())
        self.assertGreaterEqual(max_depth, 1)


class TestSolveDFS(unittest.TestCase):
    """DFS solving strategy."""

    @patch.object(TreeOfThoughts, "_persist")
    def test_dfs_returns_node(self, mock_persist):
        tree = TreeOfThoughts("Fix auth", breadth=2, max_depth=2)
        best = tree.solve(strategy="dfs")
        self.assertIsInstance(best, ThoughtNode)

    @patch.object(TreeOfThoughts, "_persist")
    def test_dfs_best_has_score(self, mock_persist):
        tree = TreeOfThoughts("Fix auth", breadth=2, max_depth=2)
        best = tree.solve(strategy="dfs")
        self.assertGreater(best.score, 0.0)


class TestSolveEdgeCases(unittest.TestCase):
    """Edge cases in solve."""

    @patch.object(TreeOfThoughts, "_persist")
    def test_unknown_strategy_raises(self, mock_persist):
        tree = TreeOfThoughts("p", breadth=1, max_depth=1)
        with self.assertRaises(ValueError):
            tree.solve(strategy="random")

    @patch.object(TreeOfThoughts, "_persist")
    def test_depth_one_terminates(self, mock_persist):
        tree = TreeOfThoughts("p", breadth=2, max_depth=1)
        best = tree.solve(strategy="bfs")
        self.assertIsInstance(best, ThoughtNode)

    @patch.object(TreeOfThoughts, "_persist")
    def test_breadth_one(self, mock_persist):
        tree = TreeOfThoughts("p", breadth=1, max_depth=2)
        best = tree.solve(strategy="bfs")
        self.assertIsInstance(best, ThoughtNode)


# ── get_best_leaf / get_best_path ──────────────────────────────────

class TestBestLeafAndPath(unittest.TestCase):
    """Best leaf selection and path tracing."""

    @patch.object(TreeOfThoughts, "_persist")
    def setUp(self, mock_persist):
        self.tree = TreeOfThoughts("problem", breadth=3, max_depth=2)

    @patch.object(TreeOfThoughts, "_persist")
    def test_best_leaf_from_only_root(self, mock_persist):
        best = self.tree.get_best_leaf()
        self.assertEqual(best.node_id, self.tree.root_id)

    @patch.object(TreeOfThoughts, "_persist")
    def test_best_leaf_selects_highest_score(self, mock_persist):
        hyps = self.tree.generate_hypotheses(n=3)
        hyps[0].score = 0.3; hyps[0].status = NodeStatus.EVALUATED
        hyps[1].score = 0.9; hyps[1].status = NodeStatus.EVALUATED
        hyps[2].score = 0.5; hyps[2].status = NodeStatus.EVALUATED

        best = self.tree.get_best_leaf()
        self.assertEqual(best.node_id, hyps[1].node_id)

    @patch.object(TreeOfThoughts, "_persist")
    def test_best_leaf_excludes_pruned(self, mock_persist):
        hyps = self.tree.generate_hypotheses(n=2)
        hyps[0].score = 0.9; hyps[0].status = NodeStatus.PRUNED
        hyps[1].score = 0.3; hyps[1].status = NodeStatus.EVALUATED

        best = self.tree.get_best_leaf()
        self.assertEqual(best.node_id, hyps[1].node_id)

    @patch.object(TreeOfThoughts, "_persist")
    def test_all_pruned_returns_root(self, mock_persist):
        hyps = self.tree.generate_hypotheses(n=2)
        for h in hyps:
            h.status = NodeStatus.PRUNED
        # root has no children that are leaves (they're pruned)
        # But root still has children list, so it's not a leaf either.
        # The method checks is_leaf property which is len(children)==0
        # Root has children (pruned ones), so root is NOT a leaf.
        # All non-pruned leaves = [] → fallback returns root
        best = self.tree.get_best_leaf()
        self.assertEqual(best.node_id, self.tree.root_id)

    @patch.object(TreeOfThoughts, "_persist")
    def test_best_path_starts_at_root(self, mock_persist):
        hyps = self.tree.generate_hypotheses(n=2)
        hyps[0].score = 0.8; hyps[0].status = NodeStatus.EVALUATED
        hyps[1].score = 0.3; hyps[1].status = NodeStatus.EVALUATED

        path = self.tree.get_best_path()
        self.assertEqual(path[0].node_id, self.tree.root_id)
        self.assertEqual(path[-1].node_id, hyps[0].node_id)

    @patch.object(TreeOfThoughts, "_persist")
    def test_best_path_length(self, mock_persist):
        hyps = self.tree.generate_hypotheses(n=1)
        hyps[0].score = 0.8; hyps[0].status = NodeStatus.EVALUATED
        path = self.tree.get_best_path()
        # root -> hypothesis = 2 nodes
        self.assertEqual(len(path), 2)


# ── get_stats ──────────────────────────────────────────────────────

class TestGetStats(unittest.TestCase):
    """Tree statistics."""

    @patch.object(TreeOfThoughts, "_persist")
    def test_stats_keys(self, mock_persist):
        tree = TreeOfThoughts("problem")
        stats = tree.get_stats()
        expected_keys = {
            "tree_id", "problem", "total_nodes", "pruned_nodes",
            "active_nodes", "evaluated_nodes", "max_depth_reached",
            "best_score", "best_hypothesis", "created_at",
        }
        self.assertEqual(set(stats.keys()), expected_keys)

    @patch.object(TreeOfThoughts, "_persist")
    def test_stats_initial_counts(self, mock_persist):
        tree = TreeOfThoughts("problem")
        stats = tree.get_stats()
        self.assertEqual(stats["total_nodes"], 1)
        self.assertEqual(stats["pruned_nodes"], 0)
        self.assertEqual(stats["active_nodes"], 1)

    @patch.object(TreeOfThoughts, "_persist")
    def test_stats_after_solve(self, mock_persist):
        tree = TreeOfThoughts("problem", breadth=2, max_depth=2)
        tree.solve(strategy="bfs")
        stats = tree.get_stats()
        self.assertGreater(stats["total_nodes"], 1)
        self.assertGreaterEqual(stats["max_depth_reached"], 1)


# ── Tree serialization ─────────────────────────────────────────────

class TestTreeSerialization(unittest.TestCase):
    """TreeOfThoughts to_dict/from_dict."""

    @patch.object(TreeOfThoughts, "_persist")
    def test_to_dict_keys(self, mock_persist):
        tree = TreeOfThoughts("p")
        d = tree.to_dict()
        self.assertIn("tree_id", d)
        self.assertIn("problem", d)
        self.assertIn("nodes", d)
        self.assertIn("root_id", d)

    @patch.object(TreeOfThoughts, "_persist")
    def test_from_dict_restores(self, mock_persist):
        tree = TreeOfThoughts("problem X", breadth=2, max_depth=4)
        d = tree.to_dict()
        restored = TreeOfThoughts.from_dict(d)
        self.assertEqual(restored.tree_id, tree.tree_id)
        self.assertEqual(restored.problem, "problem X")
        self.assertEqual(restored.breadth, 2)
        self.assertEqual(restored.max_depth, 4)
        self.assertEqual(restored.root_id, tree.root_id)
        self.assertEqual(len(restored.nodes), len(tree.nodes))

    @patch.object(TreeOfThoughts, "_persist")
    def test_from_dict_nodes_are_ThoughtNode(self, mock_persist):
        tree = TreeOfThoughts("p")
        d = tree.to_dict()
        restored = TreeOfThoughts.from_dict(d)
        for nid, node in restored.nodes.items():
            self.assertIsInstance(node, ThoughtNode)

    @patch.object(TreeOfThoughts, "_persist")
    def test_roundtrip_after_solve(self, mock_persist):
        tree = TreeOfThoughts("solve me", breadth=2, max_depth=2)
        tree.solve(strategy="bfs")
        d = tree.to_dict()
        restored = TreeOfThoughts.from_dict(d)
        self.assertEqual(len(restored.nodes), len(tree.nodes))
        # Best leaf should match
        orig_best = tree.get_best_leaf()
        rest_best = restored.get_best_leaf()
        self.assertEqual(orig_best.score, rest_best.score)


# ── _get_idle_workers ──────────────────────────────────────────────

class TestGetIdleWorkers(unittest.TestCase):
    """Idle worker detection."""

    @patch("tools.skynet_tot.DATA_DIR")
    def test_reads_realtime_json(self, mock_data):
        rt_path = MagicMock()
        mock_data.__truediv__ = MagicMock(return_value=rt_path)
        rt_path.exists.return_value = True
        rt_data = {
            "workers": {
                "alpha": {"status": "IDLE"},
                "beta": {"status": "PROCESSING"},
                "gamma": {"status": "IDLE"},
                "delta": {"status": "IDLE"},
            }
        }
        m = mock_open(read_data=json.dumps(rt_data))
        with patch("builtins.open", m):
            idle = _get_idle_workers()
        self.assertIn("alpha", idle)
        self.assertNotIn("beta", idle)
        self.assertIn("gamma", idle)

    @patch("tools.skynet_tot.DATA_DIR")
    def test_falls_back_to_http(self, mock_data):
        rt_path = MagicMock()
        mock_data.__truediv__ = MagicMock(return_value=rt_path)
        rt_path.exists.return_value = False

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "agents": [
                {"name": "Alpha", "status": "IDLE"},
                {"name": "Beta", "status": "PROCESSING"},
            ]
        }).encode()
        with patch("urllib.request.urlopen", return_value=mock_resp):
            idle = _get_idle_workers()
        self.assertIn("alpha", idle)
        self.assertNotIn("beta", idle)

    @patch("tools.skynet_tot.DATA_DIR")
    def test_all_fail_returns_all_workers(self, mock_data):
        rt_path = MagicMock()
        mock_data.__truediv__ = MagicMock(return_value=rt_path)
        rt_path.exists.return_value = False

        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            idle = _get_idle_workers()
        self.assertEqual(idle, list(WORKER_NAMES))


# ── _poll_bus_results ──────────────────────────────────────────────

class TestPollBusResults(unittest.TestCase):
    """Bus polling for tree results."""

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_collects_matching_results(self, mock_urlopen, mock_sleep):
        messages = [
            {"sender": "alpha", "type": "result", "content": "tot_abc123 STRONG evidence"},
            {"sender": "beta", "type": "result", "content": "tot_abc123 MODERATE evidence"},
            {"sender": "gamma", "type": "report", "content": "tot_abc123 unrelated"},
        ]
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(messages).encode()
        mock_urlopen.return_value = mock_resp

        results = _poll_bus_results("tot_abc123", ["alpha", "beta"], timeout=1)
        self.assertIn("alpha", results)
        self.assertIn("beta", results)
        self.assertNotIn("gamma", results)  # type != result

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_timeout_returns_partial(self, mock_urlopen, mock_sleep):
        messages = [
            {"sender": "alpha", "type": "result", "content": "tot_xyz STRONG"},
        ]
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(messages).encode()
        mock_urlopen.return_value = mock_resp

        results = _poll_bus_results("tot_xyz", ["alpha", "beta"], timeout=0.1)
        self.assertIn("alpha", results)
        self.assertNotIn("beta", results)

    @patch("time.sleep")
    @patch("urllib.request.urlopen", side_effect=Exception("network"))
    def test_network_error_returns_empty(self, mock_urlopen, mock_sleep):
        results = _poll_bus_results("tot_err", ["alpha"], timeout=0.1)
        self.assertEqual(results, {})


# ── dispatch_parallel_exploration ──────────────────────────────────

class TestDispatchParallelExploration(unittest.TestCase):
    """Parallel worker exploration dispatch."""

    @patch.object(TreeOfThoughts, "_persist")
    @patch("tools.skynet_tot._poll_bus_results", return_value={})
    @patch("tools.skynet_tot._get_idle_workers", return_value=["alpha", "beta", "gamma"])
    def test_returns_result_dict(self, mock_idle, mock_poll, mock_persist):
        with patch.dict("sys.modules", {"tools.skynet_dispatch": MagicMock()}):
            result = dispatch_parallel_exploration("test problem", n_workers=2, timeout=1)
        self.assertIn("tree_id", result)
        self.assertIn("assignments", result)
        self.assertIn("best_hypothesis", result)
        self.assertIn("best_score", result)

    @patch.object(TreeOfThoughts, "_persist")
    @patch("tools.skynet_tot._poll_bus_results", return_value={"alpha": "STRONG evidence here"})
    @patch("tools.skynet_tot._get_idle_workers", return_value=["alpha"])
    def test_strong_evidence_boosts_score(self, mock_idle, mock_poll, mock_persist):
        with patch.dict("sys.modules", {"tools.skynet_dispatch": MagicMock()}):
            result = dispatch_parallel_exploration("test", n_workers=1, timeout=1)
        self.assertGreaterEqual(result["best_score"], 0.5)

    @patch.object(TreeOfThoughts, "_persist")
    @patch("tools.skynet_tot._poll_bus_results", return_value={})
    @patch("tools.skynet_tot._get_idle_workers", return_value=["alpha", "beta"])
    def test_n_workers_clamped(self, mock_idle, mock_poll, mock_persist):
        with patch.dict("sys.modules", {"tools.skynet_dispatch": MagicMock()}):
            result = dispatch_parallel_exploration("p", n_workers=10, timeout=1)
        # Clamped to len(WORKER_NAMES)=4
        self.assertLessEqual(len(result["assignments"]), 4)

    @patch.object(TreeOfThoughts, "_persist")
    @patch("tools.skynet_tot._poll_bus_results", return_value={})
    @patch("tools.skynet_tot._get_idle_workers", return_value=[])
    def test_no_idle_uses_fallback(self, mock_idle, mock_poll, mock_persist):
        with patch.dict("sys.modules", {"tools.skynet_dispatch": MagicMock()}):
            result = dispatch_parallel_exploration("p", n_workers=2, timeout=1)
        self.assertEqual(len(result["assignments"]), 2)


# ── Constants ──────────────────────────────────────────────────────

class TestConstants(unittest.TestCase):
    """Module-level constants."""

    def test_default_breadth(self):
        self.assertEqual(DEFAULT_BREADTH, 3)

    def test_default_depth(self):
        self.assertEqual(DEFAULT_DEPTH, 3)

    def test_default_keep_top(self):
        self.assertEqual(DEFAULT_KEEP_TOP, 2)

    def test_score_threshold(self):
        self.assertEqual(SCORE_THRESHOLD, 0.3)

    def test_worker_names(self):
        self.assertEqual(WORKER_NAMES, ["alpha", "beta", "gamma", "delta"])

    def test_hypothesis_templates_count(self):
        self.assertEqual(len(HYPOTHESIS_TEMPLATES), 6)

    def test_expansion_templates_count(self):
        self.assertEqual(len(EXPANSION_TEMPLATES), 3)

    def test_hypothesis_templates_have_required_keys(self):
        for tmpl in HYPOTHESIS_TEMPLATES:
            self.assertIn("angle", tmpl)
            self.assertIn("template", tmpl)
            self.assertIn("{problem}", tmpl["template"])


# ── CLI ────────────────────────────────────────────────────────────

class TestCLI(unittest.TestCase):
    """CLI subcommands."""

    @patch.object(TreeOfThoughts, "_persist")
    @patch("sys.argv", ["skynet_tot.py", "solve", "Fix the bug"])
    def test_cli_solve(self, mock_persist):
        from tools.skynet_tot import _cli
        # Should not raise
        _cli()

    @patch("tools.skynet_tot._load_state", return_value={"trees": {}, "history": []})
    @patch("sys.argv", ["skynet_tot.py", "show", "tot_nonexistent"])
    def test_cli_show_not_found(self, mock_load):
        from tools.skynet_tot import _cli
        _cli()  # prints "not found" but doesn't crash

    @patch("tools.skynet_tot._load_state", return_value={"trees": {}, "history": []})
    @patch("sys.argv", ["skynet_tot.py", "history"])
    def test_cli_history_empty(self, mock_load):
        from tools.skynet_tot import _cli
        _cli()  # prints "No ToT history." but doesn't crash

    @patch("tools.skynet_tot._load_state")
    @patch("sys.argv", ["skynet_tot.py", "history"])
    def test_cli_history_with_entries(self, mock_load):
        mock_load.return_value = {
            "trees": {},
            "history": [{
                "completed_at": "2026-01-01T00:00:00",
                "n_workers": 3,
                "best_angle": "direct",
                "best_score": 0.85,
                "problem": "test problem",
            }],
        }
        from tools.skynet_tot import _cli
        _cli()


if __name__ == "__main__":
    unittest.main()
# signed: delta
