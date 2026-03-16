"""Tree of Thoughts (ToT) reasoning for Skynet task solving.
# signed: alpha

Structured tree search over solution hypotheses.  Unlike Graph-of-Thoughts
(which allows arbitrary merges), ToT maintains a strict parent→child tree
where each branch represents a distinct hypothesis about how to solve a
problem.  Branches are scored, pruned, and the best surviving path becomes
the solution.

Architecture:
    ThoughtNode   — single hypothesis with evidence, score, children
    TreeOfThoughts — BFS/DFS exploration engine with generate/evaluate/expand/prune
    dispatch_parallel_exploration — sends hypotheses to workers for investigation

Usage:
    python tools/skynet_tot.py solve "How should we redesign the bus persistence layer?"
    python tools/skynet_tot.py solve "Fix auth middleware race condition" --depth 3 --breadth 4
    python tools/skynet_tot.py dispatch "Optimize SSE stream" --n 3
    python tools/skynet_tot.py show <tree_id>
    python tools/skynet_tot.py history
"""
# signed: alpha

import json
import os
import sys
import time
import hashlib
import argparse
import threading
from collections import deque
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DATA_DIR = REPO_ROOT / "data"
TOT_STATE_PATH = DATA_DIR / "tot_state.json"
BUS_URL = "http://localhost:8420"

WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

# ── Defaults ────────────────────────────────────────────────────────
DEFAULT_BREADTH = 3      # hypotheses generated per node
DEFAULT_DEPTH = 3        # max tree depth
DEFAULT_KEEP_TOP = 2     # branches kept after pruning
SCORE_THRESHOLD = 0.3    # minimum score to survive pruning
# signed: alpha


class NodeStatus(Enum):
    """Lifecycle state of a thought node."""
    PENDING = "pending"        # created, not yet evaluated
    EVALUATED = "evaluated"    # scored by evaluator
    EXPANDED = "expanded"      # children generated
    PRUNED = "pruned"          # removed by pruning
    SELECTED = "selected"      # chosen as best path
    # signed: alpha


@dataclass
class ThoughtNode:
    """A single node in the Tree of Thoughts.

    Represents one hypothesis about how to solve (or partially solve)
    a problem.  Nodes form a strict tree: each node has exactly one
    parent (except root) and zero or more children.

    Attributes:
        node_id:    Unique identifier.
        hypothesis: The proposed idea or approach.
        evidence:   Supporting evidence or reasoning collected so far.
        score:      Quality score (0.0–1.0), set by evaluate().
        depth:      Distance from root (root = 0).
        parent_id:  ID of parent node (None for root).
        children:   List of child node IDs.
        status:     Lifecycle state.
        metadata:   Arbitrary extra data (worker assignment, timing, etc).
        created_at: Unix timestamp.
    """
    node_id: str
    hypothesis: str
    evidence: str = ""
    score: float = 0.0
    depth: int = 0
    parent_id: Optional[str] = None
    children: List[str] = field(default_factory=list)
    status: NodeStatus = NodeStatus.PENDING
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def is_leaf(self) -> bool:
        return len(self.children) == 0

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ThoughtNode":
        d = dict(d)
        d["status"] = NodeStatus(d.get("status", "pending"))
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})
    # signed: alpha


def _gen_node_id(text: str) -> str:
    """Generate short deterministic node ID."""
    raw = f"{text}:{time.time()}"
    return "tn_" + hashlib.sha256(raw.encode()).hexdigest()[:10]


def _gen_tree_id(problem: str) -> str:
    """Generate unique tree session ID."""
    raw = f"tot:{problem}:{time.time()}"
    return "tot_" + hashlib.sha256(raw.encode()).hexdigest()[:10]


# ────────────────────────────────────────────────────────────────────
# Persistence
# ────────────────────────────────────────────────────────────────────
_lock = threading.Lock()


def _load_state() -> dict:
    if TOT_STATE_PATH.exists():
        try:
            with open(TOT_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"trees": {}, "history": [], "version": 1}


def _save_state(state: dict) -> None:
    TOT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(TOT_STATE_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(TOT_STATE_PATH))
    # signed: alpha


# ────────────────────────────────────────────────────────────────────
# Heuristic evaluator
# ────────────────────────────────────────────────────────────────────
def _default_evaluator(node: ThoughtNode, problem: str) -> float:
    """Score a hypothesis using keyword heuristics.

    In production, this would call an LLM or a domain-specific evaluator.
    The heuristic version scores based on specificity, evidence presence,
    and actionability signals.

    Returns:
        Float score in [0.0, 1.0].
    """
    score = 0.3  # baseline

    text = (node.hypothesis + " " + node.evidence).lower()

    # Specificity: mentions concrete artifacts
    specificity_signals = [
        "file", "function", "class", "module", "endpoint",
        "line", "error", "test", "config", "path",
    ]
    hits = sum(1 for s in specificity_signals if s in text)
    score += min(0.2, hits * 0.04)

    # Evidence quality: has supporting data
    if node.evidence:
        evidence_len = len(node.evidence)
        score += min(0.2, evidence_len / 500.0)

    # Actionability: proposes concrete steps
    action_signals = [
        "implement", "add", "fix", "change", "replace", "create",
        "modify", "remove", "update", "refactor", "optimize",
    ]
    action_hits = sum(1 for s in action_signals if s in text)
    score += min(0.15, action_hits * 0.05)

    # Depth bonus: deeper nodes that survived are likely refined
    score += min(0.1, node.depth * 0.03)

    # Risk signals: mentions risk mitigation
    risk_signals = ["edge case", "fallback", "rollback", "backward", "compat"]
    risk_hits = sum(1 for s in risk_signals if s in text)
    score += min(0.05, risk_hits * 0.025)

    return round(min(1.0, score), 4)
    # signed: alpha


# ────────────────────────────────────────────────────────────────────
# Hypothesis generators
# ────────────────────────────────────────────────────────────────────
# Strategy templates for generating diverse initial hypotheses.
# Each template frames the problem from a different angle.

HYPOTHESIS_TEMPLATES = [
    {
        "angle": "direct",
        "template": "Direct approach: {problem}. Implement the most straightforward "
                    "solution that solves the core requirement with minimal changes.",
    },
    {
        "angle": "defensive",
        "template": "Defensive approach: {problem}. Focus on error handling, edge "
                    "cases, input validation, and graceful degradation first.",
    },
    {
        "angle": "architectural",
        "template": "Architectural approach: {problem}. Redesign the relevant "
                    "interfaces and abstractions for long-term maintainability.",
    },
    {
        "angle": "performance",
        "template": "Performance approach: {problem}. Optimize for speed, memory "
                    "usage, and scalability. Profile before and after.",
    },
    {
        "angle": "minimal",
        "template": "Minimal approach: {problem}. Find the smallest possible change "
                    "that fixes the issue. One-line fix preferred.",
    },
    {
        "angle": "test-driven",
        "template": "Test-driven approach: {problem}. Write the failing test first, "
                    "then implement the minimum code to make it pass.",
    },
]

# Sub-hypothesis templates for expanding nodes
EXPANSION_TEMPLATES = [
    "Break down '{hypothesis}' into concrete implementation steps.",
    "Identify risks and mitigations for '{hypothesis}'.",
    "Define the validation criteria for '{hypothesis}' — how do we know it works?",
]
# signed: alpha


# ────────────────────────────────────────────────────────────────────
# TreeOfThoughts
# ────────────────────────────────────────────────────────────────────
class TreeOfThoughts:
    """BFS/DFS exploration engine over hypothesis trees.

    The tree starts with a problem statement at the root. Initial hypotheses
    are generated as root children. Each hypothesis can be evaluated (scored),
    expanded (sub-hypotheses generated), and pruned (low-scoring branches
    removed). The best surviving leaf is the solution.

    Attributes:
        tree_id:   Unique session identifier.
        problem:   The problem being solved.
        nodes:     Dict mapping node_id -> ThoughtNode.
        root_id:   ID of the root node.
        breadth:   Number of hypotheses per expansion.
        max_depth: Maximum tree depth.
        evaluator: Callable(node, problem) -> float score.
    # signed: alpha
    """

    def __init__(
        self,
        problem: str,
        breadth: int = DEFAULT_BREADTH,
        max_depth: int = DEFAULT_DEPTH,
        evaluator: Optional[Callable] = None,
        tree_id: Optional[str] = None,
    ):
        self.tree_id = tree_id or _gen_tree_id(problem)
        self.problem = problem
        self.breadth = breadth
        self.max_depth = max_depth
        self.evaluator = evaluator or _default_evaluator
        self.nodes: Dict[str, ThoughtNode] = {}
        self.created_at = time.strftime("%Y-%m-%dT%H:%M:%S")

        # Create root node
        root = ThoughtNode(
            node_id=_gen_node_id("root"),
            hypothesis=problem,
            depth=0,
            status=NodeStatus.EVALUATED,
            score=0.5,
        )
        self.root_id = root.node_id
        self.nodes[root.node_id] = root

    def generate_hypotheses(
        self,
        parent_id: Optional[str] = None,
        n: Optional[int] = None,
    ) -> List[ThoughtNode]:
        """Generate N initial hypotheses as children of parent (default: root).

        Uses HYPOTHESIS_TEMPLATES to create diverse approaches. Each hypothesis
        frames the problem from a different angle (direct, defensive,
        architectural, performance, minimal, test-driven).

        Args:
            parent_id: Node to attach hypotheses to (default: root).
            n: Number of hypotheses (default: self.breadth, capped by templates).

        Returns:
            List of newly created ThoughtNode objects.
        """
        parent_id = parent_id or self.root_id
        parent = self.nodes.get(parent_id)
        if not parent:
            raise ValueError(f"Parent node '{parent_id}' not found")

        n = n or self.breadth
        n = min(n, len(HYPOTHESIS_TEMPLATES))
        templates = HYPOTHESIS_TEMPLATES[:n]

        new_nodes = []
        for tmpl in templates:
            hypothesis = tmpl["template"].format(problem=self.problem)
            node = ThoughtNode(
                node_id=_gen_node_id(tmpl["angle"]),
                hypothesis=hypothesis,
                depth=parent.depth + 1,
                parent_id=parent_id,
                status=NodeStatus.PENDING,
                metadata={"angle": tmpl["angle"]},
            )
            self.nodes[node.node_id] = node
            parent.children.append(node.node_id)
            new_nodes.append(node)

        parent.status = NodeStatus.EXPANDED
        self._persist()
        return new_nodes
        # signed: alpha

    def evaluate(self, node_id: str) -> float:
        """Score a hypothesis using the configured evaluator.

        Args:
            node_id: ID of the node to evaluate.

        Returns:
            The computed score (0.0–1.0).

        Raises:
            ValueError: If node not found.
        """
        node = self.nodes.get(node_id)
        if not node:
            raise ValueError(f"Node '{node_id}' not found")

        score = self.evaluator(node, self.problem)
        node.score = score
        node.status = NodeStatus.EVALUATED
        self._persist()
        return score
        # signed: alpha

    def evaluate_all_pending(self) -> Dict[str, float]:
        """Evaluate all PENDING nodes in the tree.

        Returns:
            Dict mapping node_id -> score for each evaluated node.
        """
        scores = {}
        for nid, node in self.nodes.items():
            if node.status == NodeStatus.PENDING:
                scores[nid] = self.evaluate(nid)
        return scores

    def expand(self, node_id: str) -> List[ThoughtNode]:
        """Generate sub-hypotheses for a node, deepening the tree.

        Uses EXPANSION_TEMPLATES to create refinement branches.
        Respects max_depth — returns empty list if depth limit reached.

        Args:
            node_id: ID of the node to expand.

        Returns:
            List of newly created child ThoughtNodes.
        """
        node = self.nodes.get(node_id)
        if not node:
            raise ValueError(f"Node '{node_id}' not found")

        if node.depth >= self.max_depth:
            return []  # depth limit reached

        if node.status == NodeStatus.PRUNED:
            return []  # don't expand pruned nodes

        new_nodes = []
        templates = EXPANSION_TEMPLATES[:self.breadth]

        for tmpl in templates:
            sub_hypothesis = tmpl.format(hypothesis=node.hypothesis[:200])
            child = ThoughtNode(
                node_id=_gen_node_id(sub_hypothesis[:30]),
                hypothesis=sub_hypothesis,
                depth=node.depth + 1,
                parent_id=node_id,
                status=NodeStatus.PENDING,
                metadata={"parent_angle": node.metadata.get("angle", "unknown")},
            )
            self.nodes[child.node_id] = child
            node.children.append(child.node_id)
            new_nodes.append(child)

        node.status = NodeStatus.EXPANDED
        self._persist()
        return new_nodes
        # signed: alpha

    def prune(self, keep_top: int = DEFAULT_KEEP_TOP) -> int:
        """Remove low-scoring branches, keeping only the top-K at each depth.

        For each depth level, ranks siblings by score and prunes all but the
        top keep_top nodes. Pruned nodes and all their descendants are marked
        PRUNED.

        Args:
            keep_top: Number of branches to keep at each depth level.

        Returns:
            Number of nodes pruned.
        """
        pruned_count = 0

        # Group non-pruned nodes by (depth, parent_id)
        sibling_groups: Dict[tuple, List[ThoughtNode]] = {}
        for node in self.nodes.values():
            if node.status == NodeStatus.PRUNED or node.is_root:
                continue
            key = (node.depth, node.parent_id)
            sibling_groups.setdefault(key, []).append(node)

        for _key, siblings in sibling_groups.items():
            if len(siblings) <= keep_top:
                continue

            # Sort by score descending
            siblings.sort(key=lambda n: n.score, reverse=True)

            # Prune everything after keep_top
            for node in siblings[keep_top:]:
                if node.score < SCORE_THRESHOLD or True:
                    pruned_count += self._prune_subtree(node.node_id)

        self._persist()
        return pruned_count
        # signed: alpha

    def _prune_subtree(self, node_id: str) -> int:
        """Recursively mark a node and all descendants as PRUNED."""
        node = self.nodes.get(node_id)
        if not node or node.status == NodeStatus.PRUNED:
            return 0

        count = 1
        node.status = NodeStatus.PRUNED

        for child_id in node.children:
            count += self._prune_subtree(child_id)

        return count

    def solve(self, strategy: str = "bfs") -> ThoughtNode:
        """Run tree exploration to find the best solution.

        BFS strategy (default):
            1. Generate initial hypotheses (breadth-first at depth 1)
            2. Evaluate all pending nodes
            3. Prune low-scoring branches
            4. Expand surviving nodes to next depth
            5. Repeat until max_depth or no expandable nodes
            6. Return highest-scoring leaf

        DFS strategy:
            1. Generate initial hypotheses
            2. For each hypothesis (depth-first):
               a. Evaluate
               b. If score > threshold, expand and recurse
               c. Else prune
            3. Return highest-scoring leaf

        Args:
            strategy: "bfs" (breadth-first) or "dfs" (depth-first).

        Returns:
            The ThoughtNode with the highest score (the solution).
        """
        if strategy == "bfs":
            return self._solve_bfs()
        elif strategy == "dfs":
            return self._solve_dfs()
        else:
            raise ValueError(f"Unknown strategy '{strategy}'; use 'bfs' or 'dfs'")
        # signed: alpha

    def _solve_bfs(self) -> ThoughtNode:
        """Breadth-first tree exploration."""
        # Phase 1: generate initial hypotheses
        initial = self.generate_hypotheses()

        for depth in range(1, self.max_depth + 1):
            # Evaluate all pending
            self.evaluate_all_pending()

            # Prune weak branches
            self.prune(keep_top=DEFAULT_KEEP_TOP)

            # Expand surviving leaves at current depth
            expandable = [
                n for n in self.nodes.values()
                if n.depth == depth
                and n.status == NodeStatus.EVALUATED
                and n.depth < self.max_depth
            ]
            if not expandable:
                break

            for node in expandable:
                self.expand(node.node_id)

        # Evaluate any final pending nodes
        self.evaluate_all_pending()

        # Select best leaf
        return self.get_best_leaf()

    def _solve_dfs(self) -> ThoughtNode:
        """Depth-first tree exploration."""
        initial = self.generate_hypotheses()
        self.evaluate_all_pending()

        # DFS stack: explore highest-scoring branches first
        stack = sorted(initial, key=lambda n: n.score, reverse=True)

        while stack:
            node = stack.pop()
            if node.status == NodeStatus.PRUNED:
                continue
            if node.depth >= self.max_depth:
                continue
            if node.score < SCORE_THRESHOLD:
                self._prune_subtree(node.node_id)
                continue

            children = self.expand(node.node_id)
            for child in children:
                self.evaluate(child.node_id)

            # Push high-scoring children onto stack (highest first → explored first)
            surviving = [c for c in children if c.score >= SCORE_THRESHOLD]
            surviving.sort(key=lambda n: n.score, reverse=True)
            stack.extend(surviving)

            # Prune low-scoring children
            for child in children:
                if child.score < SCORE_THRESHOLD:
                    self._prune_subtree(child.node_id)

        self._persist()
        return self.get_best_leaf()

    def get_best_leaf(self) -> ThoughtNode:
        """Return the highest-scoring non-pruned leaf node."""
        leaves = [
            n for n in self.nodes.values()
            if n.is_leaf and n.status != NodeStatus.PRUNED
        ]
        if not leaves:
            # Fallback: return root if everything was pruned
            return self.nodes[self.root_id]
        return max(leaves, key=lambda n: n.score)

    def get_best_path(self) -> List[ThoughtNode]:
        """Return the path from root to the best leaf."""
        best = self.get_best_leaf()
        path = [best]
        current = best
        while current.parent_id:
            current = self.nodes[current.parent_id]
            path.append(current)
        path.reverse()
        return path

    def get_stats(self) -> Dict[str, Any]:
        """Return tree statistics."""
        total = len(self.nodes)
        pruned = sum(1 for n in self.nodes.values()
                     if n.status == NodeStatus.PRUNED)
        evaluated = sum(1 for n in self.nodes.values()
                        if n.status in (NodeStatus.EVALUATED, NodeStatus.EXPANDED,
                                        NodeStatus.SELECTED))
        max_depth = max((n.depth for n in self.nodes.values()), default=0)
        best = self.get_best_leaf()
        return {
            "tree_id": self.tree_id,
            "problem": self.problem[:100],
            "total_nodes": total,
            "pruned_nodes": pruned,
            "active_nodes": total - pruned,
            "evaluated_nodes": evaluated,
            "max_depth_reached": max_depth,
            "best_score": best.score,
            "best_hypothesis": best.hypothesis[:120],
            "created_at": self.created_at,
        }

    # ── Serialization ───────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "tree_id": self.tree_id,
            "problem": self.problem,
            "breadth": self.breadth,
            "max_depth": self.max_depth,
            "root_id": self.root_id,
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TreeOfThoughts":
        tree = cls.__new__(cls)
        tree.tree_id = d["tree_id"]
        tree.problem = d["problem"]
        tree.breadth = d.get("breadth", DEFAULT_BREADTH)
        tree.max_depth = d.get("max_depth", DEFAULT_DEPTH)
        tree.root_id = d["root_id"]
        tree.evaluator = _default_evaluator
        tree.created_at = d.get("created_at", "")
        tree.nodes = {
            nid: ThoughtNode.from_dict(nd) for nid, nd in d["nodes"].items()
        }
        return tree

    def _persist(self) -> None:
        """Save tree to shared state file."""
        with _lock:
            state = _load_state()
            state["trees"][self.tree_id] = self.to_dict()
            _save_state(state)
    # signed: alpha


# ────────────────────────────────────────────────────────────────────
# Parallel worker dispatch
# ────────────────────────────────────────────────────────────────────
def dispatch_parallel_exploration(
    problem: str,
    n_workers: int = 3,
    timeout: float = 120.0,
) -> Dict[str, Any]:
    """Send each hypothesis to a different worker for investigation.

    Creates a ToT tree, generates initial hypotheses, and dispatches each
    to a separate worker.  Workers investigate the hypothesis and report
    evidence quality back via the bus.  The best branch is selected based
    on worker-reported evidence.

    Args:
        problem: The problem to solve.
        n_workers: Number of workers to use (1-4).
        timeout: Seconds to wait for worker responses.

    Returns:
        Dict with tree_id, assignments, and best_hypothesis.
    """
    n_workers = max(1, min(n_workers, len(WORKER_NAMES)))

    # Build tree and generate hypotheses
    tree = TreeOfThoughts(problem, breadth=n_workers)
    hypotheses = tree.generate_hypotheses(n=n_workers)

    # Get idle workers
    idle = _get_idle_workers()
    available = idle[:n_workers] if len(idle) >= n_workers else WORKER_NAMES[:n_workers]

    # Assign hypotheses to workers
    assignments = []
    for i, (hyp, worker) in enumerate(zip(hypotheses, available)):
        hyp.metadata["assigned_worker"] = worker
        assignments.append({
            "worker": worker,
            "node_id": hyp.node_id,
            "angle": hyp.metadata.get("angle", "unknown"),
            "hypothesis": hyp.hypothesis[:200],
        })

    # Dispatch to workers
    dispatched = []
    try:
        from tools.skynet_dispatch import dispatch_to_worker

        for assignment in assignments:
            prompt = (
                f"[ToT Exploration — Tree {tree.tree_id}]\n"
                f"HYPOTHESIS ({assignment['angle']}): {assignment['hypothesis']}\n\n"
                f"TASK: Investigate this hypothesis for the problem:\n"
                f"  {problem}\n\n"
                f"Provide:\n"
                f"1. Evidence supporting or refuting this approach\n"
                f"2. Concrete implementation details if viable\n"
                f"3. Risk assessment (what could go wrong)\n"
                f"4. Quality rating: STRONG / MODERATE / WEAK\n\n"
                f"Include '{tree.tree_id}' in your bus result content.\n"
                f"signed:{{your_name}}"
            )
            try:
                dispatch_to_worker(assignment["worker"], prompt)
                dispatched.append(assignment["worker"])
                time.sleep(1.5)  # clipboard cooldown
            except Exception as e:
                assignment["error"] = str(e)
    except ImportError:
        pass  # dispatch not available

    # Collect results from bus
    results = _poll_bus_results(tree.tree_id, dispatched, timeout)

    # Score hypotheses based on worker feedback
    for assignment in assignments:
        worker = assignment["worker"]
        node = tree.nodes.get(assignment["node_id"])
        if not node:
            continue
        if worker in results:
            node.evidence = results[worker][:2000]
            # Boost score based on evidence quality keywords
            content_lower = results[worker].lower()
            if "strong" in content_lower:
                node.score = 0.85
            elif "moderate" in content_lower:
                node.score = 0.60
            elif "weak" in content_lower:
                node.score = 0.30
            else:
                node.score = _default_evaluator(node, problem)
            node.status = NodeStatus.EVALUATED
        else:
            # No response — evaluate heuristically
            node.score = _default_evaluator(node, problem)
            node.status = NodeStatus.EVALUATED

    tree._persist()

    # Select best
    best = tree.get_best_leaf()
    best.status = NodeStatus.SELECTED
    best_path = tree.get_best_path()
    tree._persist()

    # Archive to history
    with _lock:
        state = _load_state()
        state["history"].append({
            "tree_id": tree.tree_id,
            "problem": problem[:120],
            "n_workers": len(dispatched),
            "responses": len(results),
            "best_worker": best.metadata.get("assigned_worker", "unknown"),
            "best_score": best.score,
            "best_angle": best.metadata.get("angle", "unknown"),
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        if len(state["history"]) > 100:
            state["history"] = state["history"][-100:]
        _save_state(state)

    return {
        "tree_id": tree.tree_id,
        "problem": problem,
        "assignments": assignments,
        "dispatched": dispatched,
        "responses_received": len(results),
        "best_hypothesis": best.hypothesis[:200],
        "best_score": best.score,
        "best_angle": best.metadata.get("angle", "unknown"),
        "best_worker": best.metadata.get("assigned_worker", "unknown"),
        "best_path": [
            {"depth": n.depth, "hypothesis": n.hypothesis[:100], "score": n.score}
            for n in best_path
        ],
        "stats": tree.get_stats(),
    }
    # signed: alpha


def _get_idle_workers() -> List[str]:
    """Get idle workers from realtime.json or /status."""
    rt_path = DATA_DIR / "realtime.json"
    if rt_path.exists():
        try:
            with open(rt_path, "r", encoding="utf-8") as f:
                rt = json.load(f)
            return [
                name for name in WORKER_NAMES
                if rt.get("workers", {}).get(name, {}).get("status", "IDLE").upper() == "IDLE"
            ]
        except (json.JSONDecodeError, OSError):
            pass
    try:
        import urllib.request
        resp = urllib.request.urlopen(f"{BUS_URL}/status", timeout=3)
        data = json.loads(resp.read().decode())
        resp.close()
        return [
            a["name"].lower() for a in data.get("agents", [])
            if a.get("status", "IDLE").upper() == "IDLE"
            and a.get("name", "").lower() in WORKER_NAMES
        ]
    except Exception:
        return list(WORKER_NAMES)


def _poll_bus_results(
    tree_id: str, expected: List[str], timeout: float
) -> Dict[str, str]:
    """Poll bus for worker results containing the tree_id."""
    import urllib.request
    results: Dict[str, str] = {}
    deadline = time.time() + timeout
    while time.time() < deadline and len(results) < len(expected):
        try:
            resp = urllib.request.urlopen(
                f"{BUS_URL}/bus/messages?limit=50", timeout=5
            )
            messages = json.loads(resp.read().decode())
            resp.close()
            for msg in messages:
                sender = msg.get("sender", "").lower()
                content = msg.get("content", "")
                if (sender in expected and sender not in results
                        and msg.get("type") == "result"
                        and tree_id in content):
                    results[sender] = content
        except Exception:
            pass
        if len(results) < len(expected):
            time.sleep(2.0)
    return results


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────
def _cli():
    parser = argparse.ArgumentParser(
        description="Skynet Tree of Thoughts (ToT) reasoning engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python tools/skynet_tot.py solve "How to fix the auth race condition?"
    python tools/skynet_tot.py solve "Optimize SSE" --depth 4 --breadth 4 --strategy dfs
    python tools/skynet_tot.py dispatch "Redesign bus persistence" --n 3
    python tools/skynet_tot.py show <tree_id>
    python tools/skynet_tot.py history
""",
    )
    sub = parser.add_subparsers(dest="command")

    # solve
    solve_p = sub.add_parser("solve", help="Run ToT exploration locally")
    solve_p.add_argument("problem", help="Problem to solve")
    solve_p.add_argument("--breadth", type=int, default=DEFAULT_BREADTH)
    solve_p.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    solve_p.add_argument("--strategy", choices=["bfs", "dfs"], default="bfs")

    # dispatch
    disp_p = sub.add_parser("dispatch", help="Dispatch hypotheses to workers")
    disp_p.add_argument("problem", help="Problem to solve")
    disp_p.add_argument("--n", type=int, default=3)
    disp_p.add_argument("--timeout", type=float, default=120.0)

    # show
    show_p = sub.add_parser("show", help="Show a tree")
    show_p.add_argument("tree_id", help="Tree ID to display")

    # history
    sub.add_parser("history", help="Show ToT history")

    args = parser.parse_args()

    if args.command == "solve":
        tree = TreeOfThoughts(
            args.problem, breadth=args.breadth, max_depth=args.depth
        )
        best = tree.solve(strategy=args.strategy)
        stats = tree.get_stats()

        print(f"Tree: {tree.tree_id}")
        print(f"Nodes: {stats['total_nodes']} (pruned: {stats['pruned_nodes']})")
        print(f"Max depth: {stats['max_depth_reached']}")
        print(f"\nBest solution (score={best.score:.4f}):")
        print(f"  {best.hypothesis}")
        if best.evidence:
            print(f"  Evidence: {best.evidence[:200]}")

        print("\nBest path:")
        for node in tree.get_best_path():
            indent = "  " * node.depth
            status = node.status.value
            print(f"  {indent}[{status}] score={node.score:.3f}: "
                  f"{node.hypothesis[:80]}")

    elif args.command == "dispatch":
        result = dispatch_parallel_exploration(
            args.problem, n_workers=args.n, timeout=args.timeout
        )
        print(f"Tree: {result['tree_id']}")
        print(f"Dispatched: {len(result['dispatched'])} workers")
        print(f"Responses: {result['responses_received']}")
        print(f"\nBest hypothesis (score={result['best_score']:.4f}):")
        print(f"  Angle: {result['best_angle']}")
        print(f"  Worker: {result['best_worker']}")
        print(f"  {result['best_hypothesis']}")

    elif args.command == "show":
        state = _load_state()
        tree_data = state.get("trees", {}).get(args.tree_id)
        if not tree_data:
            print(f"Tree '{args.tree_id}' not found.")
            return
        tree = TreeOfThoughts.from_dict(tree_data)
        stats = tree.get_stats()
        print(json.dumps(stats, indent=2))

    elif args.command == "history":
        state = _load_state()
        history = state.get("history", [])
        if not history:
            print("No ToT history.")
            return
        for h in history[-20:]:
            print(
                f"  {h.get('completed_at', '?')} | {h.get('n_workers', 0)} workers "
                f"| best={h.get('best_angle', '?')} "
                f"(score={h.get('best_score', 0):.3f}) "
                f"| {h.get('problem', '?')[:50]}"
            )

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: alpha
