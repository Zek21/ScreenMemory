#!/usr/bin/env python3
"""
skynet_got_router.py -- Graph-of-Thoughts decomposition router for Skynet Brain.

Replaces linear COMPLEX/ADVERSARIAL task templates with branching thought
exploration via GraphOfThoughts from core/cognitive/graph_of_thoughts.py.

For COMPLEX tasks: parallel exploration branches that research, design,
implement, and validate simultaneously -- then merge into a unified plan.

For ADVERSARIAL tasks: debate-format graphs where competing proposals are
generated, critiqued, and synthesized into a consensus solution.

Usage:
    python tools/skynet_got_router.py --think "build a REST API with auth"
    python tools/skynet_got_router.py --visualize "redesign the dispatch system"
    python tools/skynet_got_router.py --decompose "audit security across all modules"

API:
    from tools.skynet_got_router import got_decompose
    subtasks = got_decompose("goal text", "COMPLEX", ["alpha", "beta", "gamma", "delta"])
"""
# signed: beta

import argparse
import json
import re
import sys
import time
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Dict, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)

# Timeout for GoT operations (seconds)
GOT_TIMEOUT = 5.0

WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

# ─── Action Verb Categories (for branch generation) ──────────
# signed: beta

_BUILD_VERBS = frozenset([
    "build", "create", "implement", "add", "write", "design",
    "setup", "configure", "install", "integrate", "deploy",
])
_FIX_VERBS = frozenset([
    "fix", "debug", "repair", "patch", "resolve", "correct",
])
_ANALYSIS_VERBS = frozenset([
    "audit", "review", "analyze", "scan", "check", "verify",
    "inspect", "profile", "benchmark", "assess", "evaluate",
])
_REFACTOR_VERBS = frozenset([
    "refactor", "redesign", "optimize", "migrate", "upgrade",
    "modernize", "restructure", "clean",
])
_TEST_VERBS = frozenset([
    "test", "validate", "verify", "check", "ensure", "confirm",
])


@dataclass
class GoTSubtask:
    """A subtask derived from GoT decomposition."""
    task_text: str
    assigned_worker: str
    context: str = ""
    dependencies: List[str] = field(default_factory=list)
    index: int = 0
    thought_id: str = ""
    branch_type: str = ""  # e.g., "research", "design", "implement", "validate", "propose", "critique"
    score: float = 0.5
    # signed: beta


def _classify_goal(goal: str) -> List[str]:
    """Classify goal into domain categories based on verb analysis."""
    # signed: beta
    gl = goal.lower()
    categories = []
    if any(v in gl for v in _BUILD_VERBS):
        categories.append("build")
    if any(v in gl for v in _FIX_VERBS):
        categories.append("fix")
    if any(v in gl for v in _ANALYSIS_VERBS):
        categories.append("analysis")
    if any(v in gl for v in _REFACTOR_VERBS):
        categories.append("refactor")
    if any(v in gl for v in _TEST_VERBS):
        categories.append("test")
    return categories or ["general"]


def _extract_key_nouns(goal: str) -> List[str]:
    """Extract key nouns/topics from goal for branch specialization."""
    # signed: beta
    words = re.findall(r'\b[a-zA-Z_]{3,}\b', goal)
    stop = {"the", "and", "for", "with", "from", "into", "that", "this",
            "all", "each", "every", "across", "should", "must", "will",
            "can", "has", "have", "are", "was", "were", "been", "being"}
    return [w for w in words if w.lower() not in stop][:8]


# ─── COMPLEX Decomposition (Parallel Exploration) ────────────
# signed: beta

def _build_complex_graph(goal: str, got, categories: List[str]) -> Dict[str, any]:
    """Build a COMPLEX task graph with parallel exploration branches.

    Structure:
        Root (goal)
        ├── Research branch: analyze requirements, constraints, prior art
        ├── Design branch: architecture, interfaces, data flow
        ├── Implementation branch: core logic, edge cases, integration
        ├── Validation branch: test strategy, risk mitigation, verification
        └── Synthesis (aggregate all branches)
    """
    # signed: beta
    root = got.add_thought(f"GOAL: {goal}", score=0.5)
    branches = {}

    # Research branch -- always present
    research = got.generate(
        root.id,
        f"Research and analyze requirements, constraints, and prior art for: {goal}",
        score=0.6,
        metadata={"branch_type": "research"},
    )
    branches["research"] = research

    # Design branch -- especially for build/refactor tasks
    if any(c in categories for c in ["build", "refactor", "general"]):
        design = got.generate(
            root.id,
            f"Design solution architecture, interfaces, and data flow for: {goal}",
            score=0.65,
            metadata={"branch_type": "design"},
        )
        branches["design"] = design

    # Implementation branch
    impl = got.generate(
        root.id,
        f"Implement core logic, handle edge cases, and integrate for: {goal}",
        score=0.7,
        metadata={"branch_type": "implement"},
    )
    branches["implement"] = impl

    # Fix-specific: root cause analysis branch
    if "fix" in categories:
        rca = got.generate(
            root.id,
            f"Identify root cause, reproduce issue, and trace failure path for: {goal}",
            score=0.7,
            metadata={"branch_type": "root_cause"},
        )
        branches["root_cause"] = rca

    # Analysis-specific: deep audit branch
    if "analysis" in categories:
        audit = got.generate(
            root.id,
            f"Deep audit: scan all relevant files, identify patterns, and catalog findings for: {goal}",
            score=0.7,
            metadata={"branch_type": "audit"},
        )
        branches["audit"] = audit

    # Validation branch -- always present
    validate = got.generate(
        root.id,
        f"Design validation strategy, test plan, and risk mitigation for: {goal}",
        score=0.55,
        metadata={"branch_type": "validate"},
    )
    branches["validate"] = validate

    # Score all and aggregate
    got.score_all()

    # Aggregate best branches into synthesis
    branch_ids = [b.id for b in branches.values()]
    if len(branch_ids) >= 2:
        synthesis = got.aggregate(
            branch_ids,
            f"Synthesize parallel research into unified execution plan for: {goal}",
            score=0.8,
        )
        branches["synthesis"] = synthesis

    return branches


def _complex_to_subtasks(
    goal: str, branches: Dict, workers: List[str], got
) -> List[GoTSubtask]:
    """Convert COMPLEX graph branches to ordered subtasks with dependencies."""
    # signed: beta
    subtasks = []
    w = (workers * 2)[:4]  # Ensure at least 4 worker slots

    # Map branch types to worker assignments and ordering
    # Research and design are independent (parallel), impl depends on both,
    # validate depends on impl
    branch_order = ["research", "design", "root_cause", "audit", "implement", "validate"]
    active_branches = [b for b in branch_order if b in branches and b != "synthesis"]

    for i, branch_name in enumerate(active_branches):
        branch = branches[branch_name]
        worker = w[i % len(w)]

        # Determine dependencies
        deps = []
        if branch_name == "implement":
            # Implementation depends on research and design
            for dep_name in ["research", "design", "root_cause", "audit"]:
                if dep_name in branches:
                    dep_idx = active_branches.index(dep_name)
                    deps.append(f"subtask_{dep_idx}")
        elif branch_name == "validate":
            # Validation depends on implementation
            if "implement" in active_branches:
                impl_idx = active_branches.index("implement")
                deps.append(f"subtask_{impl_idx}")

        subtasks.append(GoTSubtask(
            task_text=branch.content,
            assigned_worker=worker,
            context=f"GoT branch: {branch_name.upper()} | Graph node: {branch.id} | Score: {branch.score:.2f}",
            dependencies=deps,
            index=i,
            thought_id=branch.id,
            branch_type=branch_name,
            score=branch.score,
        ))

    return subtasks


# ─── ADVERSARIAL Decomposition (Debate Format) ──────────────
# signed: beta

def _build_adversarial_graph(goal: str, got, categories: List[str]) -> Dict[str, any]:
    """Build an ADVERSARIAL task graph with debate-format structure.

    Structure:
        Root (goal)
        ├── Proposal A: conservative/incremental approach
        ├── Proposal B: radical/redesign approach
        ├── Critique: analyze both proposals, find flaws
        └── Synthesis: merge best elements into final plan
    """
    # signed: beta
    root = got.add_thought(f"DEBATE: {goal}", score=0.5)

    # Proposal A -- conservative approach
    proposal_a = got.generate(
        root.id,
        f"PROPOSAL A (Conservative): Design an incremental, low-risk solution for: {goal}. "
        f"Focus on backward compatibility, minimal changes, proven patterns.",
        score=0.6,
        metadata={"branch_type": "propose_a", "stance": "conservative"},
    )

    # Proposal B -- aggressive approach
    proposal_b = got.generate(
        root.id,
        f"PROPOSAL B (Aggressive): Design a bold, high-impact solution for: {goal}. "
        f"Focus on optimal architecture, future-proofing, performance.",
        score=0.6,
        metadata={"branch_type": "propose_b", "stance": "aggressive"},
    )

    # Refine proposals with domain-specific context
    key_nouns = _extract_key_nouns(goal)
    if key_nouns:
        noun_context = ", ".join(key_nouns[:4])
        got.refine(
            proposal_a.id,
            f"Refined Proposal A: conservative approach targeting {noun_context} for: {goal}",
            score_delta=0.1,
        )
        got.refine(
            proposal_b.id,
            f"Refined Proposal B: aggressive approach targeting {noun_context} for: {goal}",
            score_delta=0.1,
        )

    # Critique -- compare and find flaws in both
    critique = got.generate(
        root.id,
        f"CRITIQUE: Analyze both proposals for: {goal}. "
        f"Compare trade-offs, identify risks, find flaws in each approach. "
        f"Which handles edge cases better? Which is more maintainable?",
        score=0.7,
        metadata={"branch_type": "critique"},
    )

    # Score all
    got.score_all()

    # Synthesize -- merge best elements
    synthesis = got.aggregate(
        [proposal_a.id, proposal_b.id, critique.id],
        f"SYNTHESIS: Combine the best elements of both proposals for: {goal}. "
        f"Take conservative elements where risk is high, aggressive elements where gain is high. "
        f"Produce the final implementation plan.",
        score=0.85,
    )

    return {
        "proposal_a": proposal_a,
        "proposal_b": proposal_b,
        "critique": critique,
        "synthesis": synthesis,
    }


def _adversarial_to_subtasks(
    goal: str, branches: Dict, workers: List[str], got
) -> List[GoTSubtask]:
    """Convert ADVERSARIAL graph to ordered debate subtasks."""
    # signed: beta
    w = (workers * 2)[:4]

    subtasks = [
        GoTSubtask(
            task_text=branches["proposal_a"].content,
            assigned_worker=w[0],
            context=f"GoT debate: PROPOSAL A | Node: {branches['proposal_a'].id} | "
                    f"Score: {branches['proposal_a'].score:.2f}",
            dependencies=[],
            index=0,
            thought_id=branches["proposal_a"].id,
            branch_type="propose_a",
            score=branches["proposal_a"].score,
        ),
        GoTSubtask(
            task_text=branches["proposal_b"].content,
            assigned_worker=w[1],
            context=f"GoT debate: PROPOSAL B | Node: {branches['proposal_b'].id} | "
                    f"Score: {branches['proposal_b'].score:.2f}",
            dependencies=[],
            index=1,
            thought_id=branches["proposal_b"].id,
            branch_type="propose_b",
            score=branches["proposal_b"].score,
        ),
        GoTSubtask(
            task_text=branches["critique"].content,
            assigned_worker=w[2],
            context=f"GoT debate: CRITIQUE | Node: {branches['critique'].id} | "
                    f"Score: {branches['critique'].score:.2f}",
            dependencies=["subtask_0", "subtask_1"],
            index=2,
            thought_id=branches["critique"].id,
            branch_type="critique",
            score=branches["critique"].score,
        ),
        GoTSubtask(
            task_text=branches["synthesis"].content,
            assigned_worker=w[3],
            context=f"GoT debate: SYNTHESIS | Node: {branches['synthesis'].id} | "
                    f"Score: {branches['synthesis'].score:.2f}",
            dependencies=["subtask_0", "subtask_1", "subtask_2"],
            index=3,
            thought_id=branches["synthesis"].id,
            branch_type="synthesis",
            score=branches["synthesis"].score,
        ),
    ]
    return subtasks


# ─── Public API ──────────────────────────────────────────────
# signed: beta

def got_decompose(
    goal: str,
    difficulty: str,
    workers: Optional[List[str]] = None,
    timeout: float = GOT_TIMEOUT,
) -> Optional[List[GoTSubtask]]:
    """Decompose a goal using Graph-of-Thoughts for COMPLEX/ADVERSARIAL tasks.

    Args:
        goal: The task goal text.
        difficulty: One of COMPLEX or ADVERSARIAL.
        workers: List of available worker names.
        timeout: Max seconds for GoT computation.

    Returns:
        List of GoTSubtask objects if successful, None if GoT fails (caller
        should fall back to linear decomposition).
    """
    # signed: beta
    if difficulty not in ("COMPLEX", "ADVERSARIAL"):
        return None

    workers = workers or list(WORKER_NAMES)
    start = time.monotonic()

    try:
        from core.cognitive.graph_of_thoughts import GraphOfThoughts
    except ImportError:
        logger.warning("[GoT-Router] GraphOfThoughts unavailable -- falling back")
        return None

    try:
        got = GraphOfThoughts(max_depth=5, max_branches=5, prune_threshold=0.2)
        categories = _classify_goal(goal)

        if difficulty == "COMPLEX":
            branches = _build_complex_graph(goal, got, categories)
            subtasks = _complex_to_subtasks(goal, branches, workers, got)
        else:  # ADVERSARIAL
            branches = _build_adversarial_graph(goal, got, categories)
            subtasks = _adversarial_to_subtasks(goal, branches, workers, got)

        elapsed = time.monotonic() - start
        if elapsed > timeout:
            logger.warning(f"[GoT-Router] Timed out after {elapsed:.1f}s (limit {timeout}s)")
            return None

        logger.info(
            f"[GoT-Router] {difficulty} decomposition: {len(subtasks)} subtasks, "
            f"{len(got._thoughts)} thoughts, {elapsed:.2f}s"
        )
        return subtasks

    except Exception as e:
        elapsed = time.monotonic() - start
        logger.warning(f"[GoT-Router] Failed after {elapsed:.2f}s: {e}")
        return None


def got_visualize(goal: str, difficulty: str = "COMPLEX") -> str:
    """Generate an ASCII visualization of the GoT graph for a goal.

    Returns human-readable text showing the thought graph structure.
    """
    # signed: beta
    try:
        from core.cognitive.graph_of_thoughts import GraphOfThoughts
    except ImportError:
        return "[GoT-Router] GraphOfThoughts not available"

    got = GraphOfThoughts(max_depth=5, max_branches=5, prune_threshold=0.2)
    categories = _classify_goal(goal)

    if difficulty == "ADVERSARIAL":
        _build_adversarial_graph(goal, got, categories)
    else:
        _build_complex_graph(goal, got, categories)

    lines = []
    lines.append(f"+== GoT Decomposition ({difficulty}) ==+")
    lines.append(f"| Goal: {goal[:60]}{'...' if len(goal) > 60 else ''}")
    lines.append(f"| Categories: {', '.join(categories)}")
    lines.append(f"+== Thought Graph ==+")
    lines.append(got.to_text())
    lines.append(f"+== Statistics ==+")
    stats = got.stats
    lines.append(f"| Thoughts: {stats['total_thoughts']}")
    lines.append(f"| Max depth: {stats['max_depth']}")
    lines.append(f"| Leaves: {stats['leaf_count']}")
    lines.append(f"| Best score: {stats['best_score']:.2f}")
    lines.append(f"+{'=' * 40}+")
    return "\n".join(lines)


def got_think(goal: str, difficulty: str = "COMPLEX") -> str:
    """Show GoT decomposition as structured JSON output."""
    # signed: beta
    workers = list(WORKER_NAMES)
    subtasks = got_decompose(goal, difficulty, workers)

    if subtasks is None:
        return json.dumps({
            "status": "fallback",
            "reason": "GoT decomposition failed or unavailable",
            "difficulty": difficulty,
            "goal": goal,
        }, indent=2)

    result = {
        "status": "success",
        "difficulty": difficulty,
        "goal": goal,
        "subtask_count": len(subtasks),
        "subtasks": [asdict(st) for st in subtasks],
        "graph_metadata": {
            "categories": _classify_goal(goal),
            "key_nouns": _extract_key_nouns(goal),
        },
    }
    return json.dumps(result, indent=2, default=str)


# ─── CLI ─────────────────────────────────────────────────────
# signed: beta

def main():
    parser = argparse.ArgumentParser(
        description="Skynet GoT Router -- Graph-of-Thoughts task decomposition"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--think", metavar="GOAL",
                       help="Show GoT decomposition as JSON")
    group.add_argument("--visualize", metavar="GOAL",
                       help="Show ASCII tree of thought graph")
    group.add_argument("--decompose", metavar="GOAL",
                       help="Decompose and print subtask assignments")
    parser.add_argument("--difficulty", choices=["COMPLEX", "ADVERSARIAL"],
                        default="COMPLEX", help="Difficulty level (default: COMPLEX)")
    args = parser.parse_args()

    if args.think:
        print(got_think(args.think, args.difficulty))
    elif args.visualize:
        print(got_visualize(args.visualize, args.difficulty))
    elif args.decompose:
        subtasks = got_decompose(args.decompose, args.difficulty)
        if subtasks:
            for st in subtasks:
                deps = f" [depends: {', '.join(st.dependencies)}]" if st.dependencies else ""
                print(f"  [{st.assigned_worker}] ({st.branch_type}) {st.task_text[:80]}{deps}")
        else:
            print("[GoT-Router] Decomposition failed -- would fall back to linear")


if __name__ == "__main__":
    main()
