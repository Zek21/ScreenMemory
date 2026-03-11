"""
Graph of Thoughts (GoT) Reasoning Engine.

Non-linear reasoning topology that models information as an arbitrary graph
where vertices represent discrete thought units and edges represent logical
dependencies. Enables:
- Multi-path exploration (explore multiple reasoning branches simultaneously)
- Thought aggregation (merge findings from disparate branches)
- Thought refinement (iteratively improve a thought based on new evidence)
- Backtracking (abandon failed reasoning paths without losing other progress)

This replaces linear chain-of-thought (CoT) with a graph structure that
mirrors human lateral thinking.

Reference: "Graph of Thoughts: Solving Elaborate Problems with Large Language
Models" (Besta et al., 2024)

Architecture:
    ┌─────┐     ┌─────┐     ┌─────┐
    │ T_1 │────▶│ T_2 │────▶│ T_4 │──┐
    └─────┘     └──┬──┘     └─────┘  │
                   │                  ▼
                   │              ┌─────┐
                   │              │MERGE │──▶ Final Thought
                   │              └─────┘
                   ▼                  ▲
                ┌─────┐     ┌─────┐  │
                │ T_3 │────▶│ T_5 │──┘
                └─────┘     └─────┘

Graph Operations:
    GENERATE: Create new thought vertices from existing ones
    AGGREGATE: Merge multiple thought vertices into one (lossy compression)
    REFINE: Improve a single thought vertex using new context
    SCORE: Evaluate the quality/utility of a thought vertex
    PRUNE: Remove low-scoring branches
"""
import time
import json
import hashlib
import logging
from typing import List, Optional, Dict, Set, Tuple, Callable, Any
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class ThoughtStatus(Enum):
    ACTIVE = "active"
    REFINED = "refined"
    MERGED = "merged"
    PRUNED = "pruned"
    FINAL = "final"


@dataclass
class Thought:
    """A single vertex in the Graph of Thoughts."""
    id: str
    content: str
    score: float = 0.5
    status: ThoughtStatus = ThoughtStatus.ACTIVE
    depth: int = 0
    parent_ids: List[str] = field(default_factory=list)
    child_ids: List[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    refinement_count: int = 0

    @property
    def is_leaf(self) -> bool:
        return len(self.child_ids) == 0

    @property
    def is_root(self) -> bool:
        return len(self.parent_ids) == 0


class GraphOfThoughts:
    """
    Non-linear reasoning engine that explores multiple solution paths
    simultaneously and merges the best findings.

    Usage:
        got = GraphOfThoughts()
        root = got.add_thought("Research AI agent papers on arxiv")
        
        # Branch into parallel reasoning paths
        t1 = got.generate(root.id, "Search for visual grounding papers")
        t2 = got.generate(root.id, "Search for MCTS navigation papers")
        t3 = got.generate(root.id, "Search for memory architecture papers")
        
        # Refine based on findings
        t1_refined = got.refine(t1.id, "Found UGround paper - 20% improvement")
        
        # Merge parallel paths into synthesis
        merged = got.aggregate([t1.id, t2.id, t3.id],
            "Three key advances: UGround for vision, R-MCTS for search, "
            "tripartite memory for long-horizon")
        
        # Score and select best path
        got.score_all()
        best = got.get_best_path()

    LOG FORMAT:
        [GOT] generate   -- depth=1, parent=root, id=t_001
        [GOT] aggregate  -- merged 3 thoughts -> t_004
        [GOT] refine     -- t_001 refined (score 0.5 -> 0.8)
        [GOT] prune      -- removed 2 low-scoring branches
        [GOT] resolve    -- best path: root -> t_001 -> t_004 (score=0.82)
    """

    def __init__(self, max_depth: int = 10, max_branches: int = 5,
                 prune_threshold: float = 0.2, scorer: Optional[Callable] = None):
        """
        Args:
            max_depth: Maximum depth of reasoning chains
            max_branches: Maximum branches per vertex
            prune_threshold: Minimum score to survive pruning
            scorer: Optional function to evaluate thought quality
        """
        self.max_depth = max_depth
        self.max_branches = max_branches
        self.prune_threshold = prune_threshold
        self.scorer = scorer or self._default_scorer

        self._thoughts: Dict[str, Thought] = {}
        self._counter = 0
        self._root_id: Optional[str] = None

    def _next_id(self) -> str:
        self._counter += 1
        return f"t_{self._counter:04d}"

    # ── Core Operations ──

    def add_thought(self, content: str, score: float = 0.5,
                    metadata: dict = None) -> Thought:
        """Add the root thought (initial problem/goal)."""
        thought = Thought(
            id=self._next_id(),
            content=content,
            score=score,
            depth=0,
            metadata=metadata or {},
        )
        self._thoughts[thought.id] = thought
        if self._root_id is None:
            self._root_id = thought.id

        logger.info(f"[GoT] root: {content[:80]}")
        return thought

    def generate(self, parent_id: str, content: str,
                 score: float = 0.5, metadata: dict = None) -> Thought:
        """
        GENERATE operation: Create a new thought branching from a parent.
        This represents exploring a new reasoning direction.
        """
        parent = self._thoughts.get(parent_id)
        if not parent:
            raise ValueError(f"Parent thought {parent_id} not found")

        if parent.depth >= self.max_depth:
            logger.warning(f"[GoT] max depth {self.max_depth} reached at {parent_id}")
            return parent

        if len(parent.child_ids) >= self.max_branches:
            logger.warning(f"[GoT] max branches {self.max_branches} reached at {parent_id}")
            return parent

        thought = Thought(
            id=self._next_id(),
            content=content,
            score=score,
            depth=parent.depth + 1,
            parent_ids=[parent_id],
            metadata=metadata or {},
        )

        self._thoughts[thought.id] = thought
        parent.child_ids.append(thought.id)

        logger.info(f"[GoT] generate: depth={thought.depth}, parent={parent_id}, id={thought.id}")
        return thought

    def aggregate(self, thought_ids: List[str], merged_content: str,
                  score: float = None) -> Thought:
        """
        AGGREGATE operation: Merge multiple thought vertices into one.
        This represents synthesizing findings from parallel exploration.
        The merged thought becomes a child of all source thoughts.
        """
        sources = [self._thoughts[tid] for tid in thought_ids if tid in self._thoughts]
        if not sources:
            raise ValueError("No valid source thoughts to aggregate")

        # Compute merged score as weighted average if not provided
        if score is None:
            score = sum(t.score for t in sources) / len(sources)

        max_depth = max(t.depth for t in sources)

        merged = Thought(
            id=self._next_id(),
            content=merged_content,
            score=score,
            depth=max_depth + 1,
            parent_ids=thought_ids,
            metadata={"operation": "aggregate", "source_count": len(sources)},
        )

        self._thoughts[merged.id] = merged

        # Link parents to merged child
        for src in sources:
            src.child_ids.append(merged.id)
            src.status = ThoughtStatus.MERGED

        logger.info(f"[GoT] aggregate: merged {len(sources)} thoughts -> {merged.id} (score={score:.2f})")
        return merged

    def refine(self, thought_id: str, new_content: str,
               score_delta: float = 0.1) -> Thought:
        """
        REFINE operation: Improve an existing thought with new information.
        Creates a new refined version while keeping the original for history.
        """
        original = self._thoughts.get(thought_id)
        if not original:
            raise ValueError(f"Thought {thought_id} not found")

        refined = Thought(
            id=self._next_id(),
            content=new_content,
            score=min(1.0, original.score + score_delta),
            depth=original.depth,
            parent_ids=[thought_id],
            metadata={"operation": "refine", "original_id": thought_id},
            refinement_count=original.refinement_count + 1,
        )

        self._thoughts[refined.id] = refined
        original.child_ids.append(refined.id)
        original.status = ThoughtStatus.REFINED

        logger.info(f"[GoT] refine: {thought_id} -> {refined.id} (score {original.score:.2f} -> {refined.score:.2f})")
        return refined

    def score_thought(self, thought_id: str, new_score: float) -> None:
        """Manually set a thought's score."""
        if thought_id in self._thoughts:
            old = self._thoughts[thought_id].score
            self._thoughts[thought_id].score = max(0.0, min(1.0, new_score))
            logger.debug(f"[GoT] score: {thought_id} {old:.2f} -> {new_score:.2f}")

    def score_all(self) -> None:
        """Score all thoughts using the scorer function."""
        for thought in self._thoughts.values():
            if thought.status == ThoughtStatus.ACTIVE:
                thought.score = self.scorer(thought)

    def prune(self, threshold: float = None) -> int:
        """
        PRUNE operation: Remove low-scoring branches.
        Returns count of pruned thoughts.
        """
        threshold = threshold or self.prune_threshold
        pruned = 0

        for thought in list(self._thoughts.values()):
            if thought.status == ThoughtStatus.ACTIVE and thought.score < threshold:
                if thought.id != self._root_id:  # Never prune root
                    thought.status = ThoughtStatus.PRUNED
                    pruned += 1

        if pruned > 0:
            logger.info(f"[GoT] prune: removed {pruned} branches below threshold {threshold}")
        return pruned

    # ── Query Operations ──

    def get_leaves(self) -> List[Thought]:
        """Get all active leaf thoughts (endpoints of reasoning)."""
        return [t for t in self._thoughts.values()
                if t.is_leaf and t.status in (ThoughtStatus.ACTIVE, ThoughtStatus.FINAL)]

    def get_best_thought(self) -> Optional[Thought]:
        """Get the highest-scoring active thought."""
        active = [t for t in self._thoughts.values()
                  if t.status in (ThoughtStatus.ACTIVE, ThoughtStatus.FINAL)]
        if not active:
            return None
        return max(active, key=lambda t: t.score)

    def get_best_path(self) -> List[Thought]:
        """
        Trace the highest-scoring path from root to best leaf.
        Returns ordered list of thoughts representing the optimal reasoning chain.
        """
        best_leaf = None
        best_score = -1

        for thought in self.get_leaves():
            if thought.score > best_score:
                best_score = thought.score
                best_leaf = thought

        if not best_leaf:
            return []

        # Trace back to root
        path = [best_leaf]
        current = best_leaf
        visited = {current.id}

        while current.parent_ids:
            # Pick highest-scoring parent
            parents = [self._thoughts[pid] for pid in current.parent_ids
                       if pid in self._thoughts and pid not in visited]
            if not parents:
                break
            best_parent = max(parents, key=lambda t: t.score)
            path.append(best_parent)
            visited.add(best_parent.id)
            current = best_parent

        path.reverse()
        return path

    def get_all_paths(self) -> List[List[Thought]]:
        """Get all paths from root to leaves."""
        paths = []
        for leaf in self.get_leaves():
            path = [leaf]
            current = leaf
            visited = {current.id}
            while current.parent_ids:
                parents = [self._thoughts[pid] for pid in current.parent_ids
                           if pid in self._thoughts and pid not in visited]
                if not parents:
                    break
                parent = parents[0]
                path.append(parent)
                visited.add(parent.id)
                current = parent
            path.reverse()
            paths.append(path)
        return paths

    # ── Utility ──

    def _default_scorer(self, thought: Thought) -> float:
        """
        Default scoring heuristic based on:
        - Content length (more substantive = higher score)
        - Depth (deeper reasoning = higher score up to a point)
        - Number of connections (more connected = more validated)
        """
        length_score = min(1.0, len(thought.content) / 200)
        depth_score = min(1.0, thought.depth / max(self.max_depth / 2, 1))
        connection_score = min(1.0, (len(thought.parent_ids) + len(thought.child_ids)) / 4)

        return (length_score * 0.3 + depth_score * 0.3 +
                connection_score * 0.2 + thought.score * 0.2)

    @property
    def stats(self) -> dict:
        """Graph statistics."""
        statuses = {}
        for t in self._thoughts.values():
            statuses[t.status.value] = statuses.get(t.status.value, 0) + 1

        depths = [t.depth for t in self._thoughts.values()]

        return {
            "total_thoughts": len(self._thoughts),
            "statuses": statuses,
            "max_depth": max(depths) if depths else 0,
            "leaf_count": len(self.get_leaves()),
            "best_score": max((t.score for t in self._thoughts.values()), default=0),
        }

    def to_text(self) -> str:
        """Render graph as human-readable text for debugging."""
        lines = ["=== Graph of Thoughts ==="]

        if self._root_id:
            self._render_node(self._root_id, lines, indent=0, visited=set())

        lines.append(f"\nStats: {json.dumps(self.stats)}")

        best_path = self.get_best_path()
        if best_path:
            lines.append(f"\nBest path ({len(best_path)} steps, score={best_path[-1].score:.2f}):")
            for i, t in enumerate(best_path):
                lines.append(f"  {i+1}. [{t.score:.2f}] {t.content[:80]}")

        return "\n".join(lines)

    def _render_node(self, node_id: str, lines: list, indent: int, visited: set):
        """Recursively render graph nodes."""
        if node_id in visited:
            return
        visited.add(node_id)

        thought = self._thoughts.get(node_id)
        if not thought:
            return

        prefix = "  " * indent
        status_icon = {
            ThoughtStatus.ACTIVE: "[+]",
            ThoughtStatus.REFINED: "[~]",
            ThoughtStatus.MERGED: "[*]",
            ThoughtStatus.PRUNED: "[x]",
            ThoughtStatus.FINAL: "[!]",
        }.get(thought.status, "[?]")

        lines.append(f"{prefix}{status_icon} {thought.id} (s={thought.score:.2f}): {thought.content[:70]}")

        for child_id in thought.child_ids:
            self._render_node(child_id, lines, indent + 1, visited)

    def resolve(self) -> str:
        """
        Resolve the graph into a final answer by tracing the best path
        and concatenating the reasoning chain.
        """
        best_path = self.get_best_path()
        if not best_path:
            return "No reasoning path found."

        # Mark final thought
        best_path[-1].status = ThoughtStatus.FINAL

        # Build resolution string
        parts = []
        for i, t in enumerate(best_path):
            if i == 0:
                parts.append(f"Goal: {t.content}")
            elif i == len(best_path) - 1:
                parts.append(f"Conclusion: {t.content}")
            else:
                parts.append(f"Step {i}: {t.content}")

        logger.info(f"[GoT] resolve: best path {len(best_path)} steps, score={best_path[-1].score:.2f}")
        return "\n".join(parts)


class GoTReasoner:
    """
    High-level reasoning interface that uses Graph of Thoughts
    to solve complex multi-faceted problems.

    Integrates with VLM for scoring and Episodic Memory for context.
    """

    def __init__(self, vlm_analyzer=None, memory=None):
        self.vlm = vlm_analyzer
        self.memory = memory

    def reason(self, problem: str, perspectives: List[str] = None,
               max_depth: int = 5) -> Tuple[str, GraphOfThoughts]:
        """Solve a problem using graph-based reasoning."""
        got = GraphOfThoughts(max_depth=max_depth)
        root = got.add_thought(problem, score=0.3)

        if perspectives:
            self._explore_perspectives(got, root, perspectives)

        got.score_all()
        got.prune()
        resolution = got.resolve()

        if self.memory:
            self.memory.store_episodic(
                f"GoT reasoning: {problem[:60]} -> {got.stats['total_thoughts']} thoughts",
                tags=["reasoning", "got"], source_action="got_reason", importance=0.7,
            )
        return resolution, got

    def _explore_perspectives(self, got: GraphOfThoughts, root, perspectives: List[str]):
        """Create parallel branches, refine via VLM if available, then aggregate."""
        branches = [got.generate(root.id, p, score=0.5) for p in perspectives]

        if self.vlm and self.vlm.is_available:
            for branch in branches:
                got.refine(branch.id, f"[VLM-refined] {branch.content}", score_delta=0.15)

        active = [b.id for b in branches if b.score >= 0.3]
        if len(active) >= 2:
            synthesis = f"Synthesis of {len(active)} perspectives on: {root.content[:60]}"
            got.aggregate(active, synthesis, score=0.8)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=== Graph of Thoughts Test ===\n")

    got = GraphOfThoughts(max_depth=5, prune_threshold=0.15)

    # Root problem
    root = got.add_thought("Research optimal architecture for autonomous web agent", score=0.3)

    # Parallel exploration branches
    t1 = got.generate(root.id, "Visual grounding: UGround achieves 20% improvement over SoM", score=0.7)
    t2 = got.generate(root.id, "Cognitive: R-MCTS enables 30% improvement on VisualWebArena", score=0.8)
    t3 = got.generate(root.id, "Memory: Tripartite architecture prevents contextual degradation", score=0.6)
    t4 = got.generate(root.id, "Code gen: Dynamic script generation bypasses GUI bottlenecks", score=0.5)

    # Refine based on deeper analysis
    t1r = got.refine(t1.id, "UGround trained on 10M GUI elements, works across web/desktop/mobile", score_delta=0.15)
    t2r = got.refine(t2.id, "R-MCTS uses contrastive reflection + multi-agent debate for state eval", score_delta=0.1)

    # Sub-branches
    t3a = got.generate(t3.id, "Working memory: 7 items, flushed per subtask", score=0.6)
    t3b = got.generate(t3.id, "Episodic: vector-backed, intelligent decay, knowledge distillation", score=0.7)
    t3c = got.generate(t3.id, "Semantic: knowledge graph, permanent, multi-hop reasoning", score=0.65)

    # Low-quality branch (will be pruned)
    t5 = got.generate(root.id, "Maybe just use Selenium?", score=0.1)

    # Aggregate best findings
    merged = got.aggregate(
        [t1r.id, t2r.id, t3.id, t4.id],
        "Optimal architecture: UGround visual grounding + R-MCTS navigation + "
        "tripartite memory + dynamic code gen. Agent-E two-tier design with "
        "Planner LLM + Navigator LLM separation.",
        score=0.9,
    )

    # Prune low-scoring branches
    pruned = got.prune()

    # Output
    print(got.to_text())
    print(f"\n--- Resolution ---")
    print(got.resolve())
