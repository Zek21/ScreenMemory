#!/usr/bin/env python3
"""
skynet_brain.py -- The Intelligent Dispatch Brain for Skynet.

Replaces dumb text-pasting with AI-powered task intelligence:
  Goal in -> Intelligence pipeline -> Context-enriched subtasks
  -> Workers execute -> Results synthesized -> Learning stored

Usage:
    python tools/skynet_brain.py think "review the auth module"
    python tools/skynet_brain.py execute "fix all failing tests"
    python tools/skynet_brain.py assess "build a REST API"
"""

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List  # signed: gamma (removed unused Any, Dict, Optional)
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "chrome_bridge"))

BUS_URL = "http://localhost:8420"
STATE_FILE = ROOT / "data" / "realtime_sse.json"
STATE_FILE_ALT = ROOT / "data" / "realtime.json"
EPISODES_DIR = ROOT / "data" / "episodes"
EPISODES_INDEX = ROOT / "data" / "learning_episodes.json"
WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]


# ─── Data Structures ──────────────────────────────────

@dataclass
class Subtask:
    task_text: str
    assigned_worker: str
    context: str = ""
    dependencies: List[str] = field(default_factory=list)
    index: int = 0


def _generate_strategy_id(goal: str) -> str:
    """Generate a unique strategy_id from goal text + current timestamp."""
    raw = f"{goal}:{time.time()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class BrainPlan:
    goal: str
    difficulty: str
    subtasks: List[Subtask]
    reasoning: str
    relevant_learnings: List[str] = field(default_factory=list)
    operator: str = ""
    domain_tags: List[str] = field(default_factory=list)
    strategy_id: str = ""


# ─── Helpers ───────────────────────────────────────────

def _read_worker_states() -> dict:
    """Read worker states from realtime JSON files. No import dependency."""
    for path in [STATE_FILE, STATE_FILE_ALT]:
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return data.get("workers", {})
        except Exception:
            continue
    # Fallback: query Skynet directly
    try:
        with urlopen(f"{BUS_URL}/status", timeout=5) as r:
            data = json.loads(r.read())
            return data.get("agents", {})
    except Exception:
        return {}


def _bus_post(message: dict) -> bool:
    """POST a message to the Skynet bus. Returns True on success."""
    from tools.shared.bus import bus_post
    return bus_post(message)


def _save_episode(plan, results: dict, success: bool):
    """Persist an episode JSON file in data/episodes/ and update the index.

    Uses atomic writes to prevent corruption from concurrent brain executions.
    """  # signed: gamma
    strategy_id = getattr(plan, "strategy_id", "") or _generate_strategy_id(plan.goal)
    ts = time.time()

    # Build workers list from dataclass or dict subtasks
    workers = []
    for st in plan.subtasks:
        if hasattr(st, "assigned_worker"):
            workers.append(st.assigned_worker)
        elif isinstance(st, dict):
            workers.append(st.get("worker", "?"))

    episode = {
        "id": f"ep_{int(ts * 1000)}_{strategy_id[:8]}",
        "strategy_id": strategy_id,
        "goal": plan.goal[:300],
        "difficulty": plan.difficulty,
        "worker_count": len(plan.subtasks),
        "workers": workers,
        "outcome": "success" if success else "failure",
        "timestamp": ts,
        "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
    }

    # Write individual episode file (atomic to prevent partial writes)
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    ep_file = EPISODES_DIR / f"{episode['id']}.json"
    try:
        from tools.skynet_atomic import atomic_write_json
        atomic_write_json(ep_file, episode)
    except ImportError:
        try:
            ep_file.write_text(json.dumps(episode, indent=2), encoding="utf-8")
        except Exception:
            pass

    # Update index atomically (read-modify-write under lock prevents corruption
    # when multiple brain processes or threads call _save_episode concurrently)
    try:
        from tools.skynet_atomic import atomic_update_json

        def _append_episode(index):
            if not isinstance(index, list):
                index = []
            index.append(episode)
            if len(index) > 500:
                index = index[-500:]
            return index

        atomic_update_json(EPISODES_INDEX, _append_episode, default=[])
    except ImportError:
        try:
            if EPISODES_INDEX.exists():
                index = json.loads(EPISODES_INDEX.read_text(encoding="utf-8"))
            else:
                index = []
            index.append(episode)
            if len(index) > 500:
                index = index[-500:]
            EPISODES_INDEX.write_text(json.dumps(index, indent=2), encoding="utf-8")
        except Exception:
            pass


def query_episodes_by_strategy(strategy_id: str) -> List[dict]:
    """Return all episodes that match a given strategy_id."""
    results = []
    # Check index first
    try:
        if EPISODES_INDEX.exists():
            index = json.loads(EPISODES_INDEX.read_text(encoding="utf-8"))
            results = [ep for ep in index if ep.get("strategy_id") == strategy_id]
    except Exception:
        pass
    # Fallback: scan individual files
    if not results and EPISODES_DIR.exists():
        for f in EPISODES_DIR.glob("*.json"):
            try:
                ep = json.loads(f.read_text(encoding="utf-8"))
                if ep.get("strategy_id") == strategy_id:
                    results.append(ep)
            except Exception:
                continue
    return results


def _get_idle_workers() -> List[str]:
    """Return list of idle worker names, sorted by load (fewest tasks first)."""
    states = _read_worker_states()
    idle = []
    for name in WORKER_NAMES:
        w = states.get(name, {})
        status = w.get("status", "UNKNOWN").upper()
        if status == "IDLE":
            tasks_done = w.get("tasks_completed", 0)
            idle.append((name, tasks_done))
    idle.sort(key=lambda x: x[1])
    return [name for name, _ in idle]


# ─── SkynetBrain ───────────────────────────────────────

class CognitiveStrategy:
    """Selects and applies cognitive reasoning strategies based on task difficulty.

    Strategy mapping:
        TRIVIAL/SIMPLE  -> direct (no cognitive layer)
        MODERATE        -> Reflexion (reflect on past failures before executing)
        COMPLEX         -> GoT (graph-of-thought decomposition before dispatch)
        ADVERSARIAL     -> MCTS (monte carlo search over solution space)
    """

    def __init__(self):
        self.got = None
        self.mcts = None
        self.reflexion = None
        self.planner = None
        self._init_cognitive_engines()

    def _init_cognitive_engines(self):
        """Fault-tolerant loading of cognitive modules."""
        try:
            from core.cognitive.graph_of_thoughts import GraphOfThoughts
            self.got = GraphOfThoughts(max_depth=5, max_branches=4, prune_threshold=0.3)
            print("[brain] Cognitive: GoT loaded")
        except Exception as e:
            print(f"[brain] WARN: GoT unavailable: {e}")

        try:
            from core.cognitive.mcts import ReflectiveMCTS, NavigationState
            self.mcts = ReflectiveMCTS(max_depth=5, max_iterations=20)
            self._NavigationState = NavigationState
            print("[brain] Cognitive: R-MCTS loaded")
        except Exception as e:
            print(f"[brain] WARN: R-MCTS unavailable: {e}")

        try:
            from core.cognitive.reflexion import ReflexionEngine
            self.reflexion = ReflexionEngine(max_reflections=50)
            print("[brain] Cognitive: Reflexion loaded")
        except Exception as e:
            print(f"[brain] WARN: Reflexion unavailable: {e}")

        try:
            from core.cognitive.planner import HierarchicalPlanner
            self.planner = HierarchicalPlanner()
            print("[brain] Cognitive: Planner loaded")
        except Exception as e:
            print(f"[brain] WARN: Planner unavailable: {e}")

    def select_strategy(self, difficulty: str) -> str:
        """Select the cognitive strategy based on task difficulty."""
        strategy_map = {
            "TRIVIAL": "direct",
            "SIMPLE": "direct",
            "MODERATE": "reflexion",
            "COMPLEX": "got",
            "ADVERSARIAL": "mcts",
        }
        selected = strategy_map.get(difficulty, "direct")
        # Downgrade if engine unavailable
        if selected == "reflexion" and not self.reflexion:
            selected = "direct"
        if selected == "got" and not self.got:
            selected = "reflexion" if self.reflexion else "direct"
        if selected == "mcts" and not self.mcts:
            selected = "got" if self.got else "direct"
        return selected

    def apply_reflexion(self, goal: str, learnings: list) -> dict:
        """Apply Reflexion: analyze past failures relevant to this goal before execution."""
        if not self.reflexion:
            return {"strategy": "reflexion", "insights": [], "adjustments": []}

        insights = []
        adjustments = []

        # Check for relevant past reflections
        relevant = self.reflexion.get_relevant_reflections(
            action_type="task_execution",
            target=goal[:50],
            context=goal,
            limit=5,
        )
        for ref in relevant:
            insights.append(f"Past lesson: {ref.lesson}")
            if ref.action_adjustment:
                adjustments.append(ref.action_adjustment)

        # Also mine learnings for failure patterns
        for learning in learnings:
            if any(kw in learning.lower() for kw in ["fail", "error", "broke", "wrong", "bug"]):
                insights.append(f"Warning from memory: {learning[:150]}")

        return {
            "strategy": "reflexion",
            "insights": insights,
            "adjustments": adjustments,
            "reflection_count": len(relevant),
        }

    def apply_got(self, goal: str, learnings: list) -> dict:
        """Apply Graph-of-Thought: decompose into multi-path reasoning graph."""
        if not self.got:
            return {"strategy": "got", "thoughts": [], "best_path": []}

        # Create reasoning graph
        root = self.got.add_thought(goal, score=0.5)

        # Generate exploration branches based on goal analysis
        branches = self._generate_thought_branches(goal)
        thought_nodes = []
        for branch in branches:
            t = self.got.generate(root.id, branch["content"], score=branch.get("score", 0.5))
            thought_nodes.append({"id": t.id, "content": t.content, "score": t.score})

        # Score all thoughts
        self.got.score_all()

        # Get best reasoning path
        best_path = self.got.get_best_path()
        path_contents = [t.content for t in best_path]

        # Aggregate findings
        if len(thought_nodes) > 1:
            agg_content = f"Synthesize approaches for: {goal}"
            merged = self.got.aggregate(
                [t["id"] for t in thought_nodes],
                agg_content,
            )
            thought_nodes.append({"id": merged.id, "content": merged.content, "score": merged.score})

        return {
            "strategy": "got",
            "thoughts": thought_nodes,
            "best_path": path_contents,
            "graph_size": len(self.got._thoughts),
        }

    def apply_mcts(self, goal: str) -> dict:
        """Apply MCTS: search solution space with contrastive reflection."""
        if not self.mcts:
            return {"strategy": "mcts", "iterations": 0, "best_score": 0}

        # Create root state representing the problem
        root_state = self._NavigationState(
            id="root",
            description=goal[:200],
            active_app="skynet",
        )
        self.mcts.create_root(root_state)

        # Run search iterations
        scores = []
        for _ in range(min(10, self.mcts.max_iterations)):
            try:
                score = self.mcts.iterate()
                scores.append(score)
            except Exception:
                break

        best_action = None
        try:
            best_action = self.mcts.get_best_action()
        except Exception:
            pass

        return {
            "strategy": "mcts",
            "iterations": len(scores),
            "best_score": max(scores) if scores else 0,
            "avg_score": sum(scores) / len(scores) if scores else 0,
            "best_action": str(best_action) if best_action else None,
            "reflections": len(self.mcts._reflections),
        }

    def apply(self, difficulty: str, goal: str, learnings: list = None) -> dict:
        """Apply the appropriate cognitive strategy for the difficulty level."""
        strategy = self.select_strategy(difficulty)
        learnings = learnings or []

        if strategy == "direct":
            return {"strategy": "direct", "message": "No cognitive layer needed"}

        if strategy == "reflexion":
            return self.apply_reflexion(goal, learnings)

        if strategy == "got":
            return self.apply_got(goal, learnings)

        if strategy == "mcts":
            return self.apply_mcts(goal)

        return {"strategy": "direct", "message": "Fallback to direct"}

    def _generate_thought_branches(self, goal: str) -> list:
        """Generate thought branches from goal analysis."""
        gl = goal.lower()
        branches = []

        # Approach analysis branch
        branches.append({
            "content": f"Analyze requirements and constraints for: {goal}",
            "score": 0.6,
        })

        # Risk analysis branch
        branches.append({
            "content": f"Identify risks, edge cases, and failure modes for: {goal}",
            "score": 0.5,
        })

        # Implementation strategy branch
        if any(kw in gl for kw in ["build", "create", "implement", "add", "write"]):
            branches.append({
                "content": f"Design architecture and implementation plan for: {goal}",
                "score": 0.7,
            })

        # Testing/validation branch
        if any(kw in gl for kw in ["fix", "debug", "test", "audit", "review"]):
            branches.append({
                "content": f"Design validation and testing strategy for: {goal}",
                "score": 0.6,
            })

        # Existing knowledge branch
        branches.append({
            "content": f"Recall relevant past solutions and patterns for: {goal}",
            "score": 0.4,
        })

        return branches[:4]  # Max 4 branches

    def enrich_with_cognitive_context(self, result: dict) -> str:
        """Convert cognitive strategy output to context string for workers."""
        strategy = result.get("strategy", "direct")

        if strategy == "direct":
            return ""

        parts = [f"COGNITIVE STRATEGY: {strategy.upper()}"]

        if strategy == "reflexion":
            if result.get("insights"):
                parts.append("REFLEXION INSIGHTS:")
                for insight in result["insights"][:5]:
                    parts.append(f"  - {insight[:200]}")
            if result.get("adjustments"):
                parts.append("SUGGESTED ADJUSTMENTS:")
                for adj in result["adjustments"][:3]:
                    parts.append(f"  - {adj[:200]}")

        elif strategy == "got":
            if result.get("best_path"):
                parts.append("REASONING PATH (Graph of Thought):")
                for i, step in enumerate(result["best_path"][:5], 1):
                    parts.append(f"  {i}. {step[:200]}")
            parts.append(f"  Graph size: {result.get('graph_size', 0)} nodes")

        elif strategy == "mcts":
            parts.append(f"MCTS SEARCH: {result.get('iterations', 0)} iterations, "
                         f"best_score={result.get('best_score', 0):.2f}")
            if result.get("best_action"):
                parts.append(f"  Recommended action: {result['best_action']}")
            if result.get("reflections"):
                parts.append(f"  Contrastive reflections: {result['reflections']}")

        return "\n".join(parts)


class SkynetBrain:
    """Intelligent dispatch brain for Skynet multi-agent system."""

    def __init__(self):
        self.router = None
        self.dag_builder = None
        self.retriever = None
        self.learning_store = None
        self.cognitive = None
        self._init_engines()

    def _init_engines(self):
        """Fault-tolerant engine instantiation."""
        # DAAORouter
        try:
            from core.difficulty_router import DAAORouter
            self.router = DAAORouter()
        except Exception as e:
            print(f"[brain] WARN: DAAORouter unavailable: {e}")

        # DAGBuilder
        try:
            from core.dag_engine import DAGBuilder
            self.dag_builder = DAGBuilder
        except Exception as e:
            print(f"[brain] WARN: DAGBuilder unavailable: {e}")

        # HybridRetriever
        try:
            from core.hybrid_retrieval import HybridRetriever
            self.retriever = HybridRetriever()
        except Exception as e:
            print(f"[brain] WARN: HybridRetriever unavailable: {e}")

        # LearningStore
        try:
            from core.learning_store import LearningStore
            db_path = str(ROOT / "data" / "learning.db")
            self.learning_store = LearningStore(db_path)
        except Exception as e:
            print(f"[brain] WARN: LearningStore unavailable: {e}")

        # Cognitive Strategy Engine (Level 4)
        try:
            self.cognitive = CognitiveStrategy()
        except Exception as e:
            print(f"[brain] WARN: CognitiveStrategy unavailable: {e}")

    # ─── ASSESS ────────────────────────────────────────

    def assess(self, goal: str) -> dict:
        """Assess difficulty of a goal. Returns difficulty info dict."""
        if not self.router:
            result = {"difficulty": "MODERATE", "confidence": 0.5,
                    "reason": "Router unavailable, defaulting to MODERATE"}
        else:
            plan = self.router.route(goal)
            result = {
                "difficulty": plan.difficulty.level.name,
                "confidence": plan.difficulty.confidence,
                "complexity_score": plan.difficulty.complexity_score,
                "operator": plan.operator.name,
                "domain_tags": plan.difficulty.domain_tags,
                "agent_roles": plan.agent_roles,
            }

        # Add cognitive strategy selection
        if self.cognitive:
            difficulty = result.get("difficulty", "MODERATE")
            adjusted = self._adjust_difficulty(goal, difficulty)
            strategy = self.cognitive.select_strategy(adjusted)
            result["cognitive_strategy"] = strategy
            result["adjusted_difficulty"] = adjusted

        return result

    # ─── THINK ─────────────────────────────────────────

    def _recall_and_search(self, goal: str) -> tuple:
        """Recall past learnings and search for relevant context. Returns (learnings, context_docs)."""
        learnings = []
        if self.learning_store:
            try:
                facts = self.learning_store.recall(goal, top_k=5)
                learnings = [f.content for f in facts if hasattr(f, "content")]
            except Exception:
                pass

        context_docs = []
        if self.retriever:
            try:
                results = self.retriever.search(goal, limit=5)
                context_docs = [r.content for r in results if hasattr(r, "content")]
            except Exception:
                pass

        return learnings, context_docs

    def _apply_cognitive_strategy(self, goal: str, difficulty: str, learnings: list) -> tuple:
        """Apply cognitive strategy and return (cognitive_context, cognitive_result)."""
        if not self.cognitive:
            return "", None
        try:
            result = self.cognitive.apply(difficulty, goal, learnings)
            context = self.cognitive.enrich_with_cognitive_context(result)
            if result.get("strategy", "direct") != "direct":
                print(f"[brain] Cognitive strategy: {result['strategy'].upper()} applied")
            return context, result
        except Exception as e:
            print(f"[brain] WARN: Cognitive strategy failed: {e}")
            return "", None

    def _append_cognitive_reasoning(self, reasoning: str, cognitive_result: dict) -> str:
        """Append cognitive strategy info to reasoning string."""
        if not cognitive_result or cognitive_result.get("strategy") == "direct":
            return reasoning
        strategy = cognitive_result["strategy"]
        reasoning += f"\nCognitive strategy: {strategy.upper()}"
        if strategy == "got":
            reasoning += f" (graph: {cognitive_result.get('graph_size', 0)} nodes)"
        elif strategy == "mcts":
            reasoning += f" ({cognitive_result.get('iterations', 0)} iterations, best={cognitive_result.get('best_score', 0):.2f})"
        elif strategy == "reflexion":
            reasoning += f" ({cognitive_result.get('reflection_count', 0)} relevant reflections)"
        return reasoning

    def think(self, goal: str) -> BrainPlan:
        """Given a goal, produce an intelligent execution plan."""
        assessment = self.assess(goal)
        difficulty = self._adjust_difficulty(goal, assessment.get("difficulty", "MODERATE"))
        operator = assessment.get("operator", "CHAIN_OF_THOUGHT")
        domain_tags = assessment.get("domain_tags", [])

        learnings, context_docs = self._recall_and_search(goal)
        cognitive_context, cognitive_result = self._apply_cognitive_strategy(goal, difficulty, learnings)

        idle_workers = _get_idle_workers() or list(WORKER_NAMES)

        natural_parts = self._extract_natural_subtasks(goal)
        if len(natural_parts) > 1 and difficulty in ("TRIVIAL", "SIMPLE"):
            difficulty = "MODERATE" if len(natural_parts) <= 2 else "COMPLEX"

        base_context = self._build_context(learnings, context_docs)
        full_context = f"{base_context}\n\n{cognitive_context}" if cognitive_context and base_context else (cognitive_context or base_context)

        if len(natural_parts) > 1:
            subtasks = self._decompose_natural(natural_parts, idle_workers, full_context)
        else:
            subtasks = self._decompose(goal, difficulty, idle_workers, learnings, context_docs)
            if cognitive_context:
                for st in subtasks:
                    st.context = f"{st.context}\n\n{cognitive_context}" if st.context else cognitive_context

        reasoning = self._build_reasoning(goal, difficulty, operator, subtasks,
                                          learnings, context_docs, idle_workers)
        reasoning = self._append_cognitive_reasoning(reasoning, cognitive_result)

        return BrainPlan(
            goal=goal, difficulty=difficulty, subtasks=subtasks,
            reasoning=reasoning, relevant_learnings=learnings[:5],
            operator=operator, domain_tags=domain_tags,
            strategy_id=_generate_strategy_id(goal),
        )

    @staticmethod
    def _adjust_difficulty(goal: str, router_difficulty: str) -> str:
        """Override router difficulty using text signals.

        The DAAORouter uses heuristic keyword scoring which often underestimates.
        This applies structural analysis: multi-verb goals, explicit enumerations,
        and scope keywords indicate higher complexity.
        """
        gl = goal.lower()
        signals = 0

        # Multiple action verbs = multiple tasks
        action_verbs = ["build", "create", "implement", "fix", "audit", "review",
                        "redesign", "add", "remove", "refactor", "test", "deploy",
                        "analyze", "scan", "check", "verify", "update", "enhance",
                        "write", "design", "optimize", "integrate", "migrate"]
        verb_count = sum(1 for v in action_verbs if v in gl)
        if verb_count >= 3:
            signals += 2
        elif verb_count >= 2:
            signals += 1

        # Explicit enumerations  # signed: gamma
        if re.search(r'\d\)', gl) or re.search(r'\d\.', gl):
            signals += 1
        # "and" joining distinct clauses
        and_count = gl.count(" and ")
        if and_count >= 2:
            signals += 2
        elif and_count >= 1:
            signals += 1

        # Scope keywords
        scope_words = ["all", "every", "entire", "complete", "full", "whole",
                       "across", "throughout", "system-wide"]
        if any(w in gl for w in scope_words):
            signals += 1

        # Size keywords
        if len(goal) > 200:
            signals += 1

        # Apply override
        levels = ["TRIVIAL", "SIMPLE", "MODERATE", "COMPLEX", "ADVERSARIAL"]
        current_idx = levels.index(router_difficulty) if router_difficulty in levels else 1
        if signals >= 4:
            return levels[min(current_idx + 2, 4)]
        elif signals >= 2:
            return levels[min(current_idx + 1, 4)]
        return router_difficulty

    _NATURAL_ACTION_VERBS = frozenset([
        "build", "create", "implement", "fix", "audit", "review",
        "redesign", "add", "remove", "refactor", "test", "deploy",
        "analyze", "scan", "check", "verify", "update", "enhance",
        "write", "design", "optimize", "integrate", "migrate",
        "count", "list", "report", "find", "search", "delete",
        "install", "configure", "setup", "clean", "document",
        "debug", "profile", "benchmark", "monitor", "restart",
        "run", "execute", "start", "stop", "upgrade", "downgrade",
    ])

    @staticmethod
    def _extract_natural_subtasks(goal: str) -> List[str]:
        """Extract natural subtask boundaries from goal text."""  # signed: gamma

        # Try numbered items: "1) ... 2) ..." or "1. ... 2. ..."
        numbered = re.split(r'\d+[.)]\s+', goal)
        numbered = [p.strip().rstrip(",;.") for p in numbered if p.strip()]
        if len(numbered) > 1:
            return numbered

        # Try semicolons
        if ";" in goal:
            parts = [p.strip() for p in goal.split(";") if p.strip()]
            if len(parts) > 1:
                return parts

        verbs = SkynetBrain._NATURAL_ACTION_VERBS

        # Try comma-separated list with optional final "and" (Oxford comma pattern)
        if "," in goal:
            normalized = re.sub(r',\s+and\s+', ', ', goal)
            parts = [re.sub(r'^and\s+', '', p).strip() for p in normalized.split(",")]
            parts = [p for p in parts if p]
            if sum(1 for p in parts if any(v in p.lower() for v in verbs)) >= 2:
                return parts

        # Split on " and " between independent clauses
        if " and " in goal:
            parts = [p.strip() for p in goal.split(" and ")]
            verb_parts = [p for p in parts if any(v in p.lower() for v in verbs)]
            if len(verb_parts) >= 2 and len(parts) <= 4:
                return parts

        return [goal]

    def _decompose_natural(self, parts: List[str], idle_workers: List[str],
                           context_str: str) -> List[Subtask]:
        """Create subtasks from naturally extracted goal parts — one per worker."""
        subtasks = []
        for i, part in enumerate(parts):
            worker = idle_workers[i % len(idle_workers)]
            subtasks.append(Subtask(
                task_text=part.strip(),
                assigned_worker=worker,
                context=context_str,
                index=i,
            ))
        return subtasks

    @staticmethod
    def _make_subtask_chain(specs: list, context_str: str) -> List[Subtask]:
        """Build a list of Subtasks from (task_text, worker, deps_list) specs."""
        return [
            Subtask(task_text=text, assigned_worker=worker, context=context_str,
                    dependencies=deps, index=i)
            for i, (text, worker, deps) in enumerate(specs)
        ]

    def _decompose(self, goal: str, difficulty: str, idle_workers: List[str],
                   learnings: List[str], context_docs: List[str]) -> List[Subtask]:
        """Decompose goal into subtasks based on difficulty level."""
        context_str = self._build_context(learnings, context_docs)

        if difficulty in ("TRIVIAL", "SIMPLE"):
            return [Subtask(task_text=goal, assigned_worker=idle_workers[0],
                           context=context_str, index=0)]

        w = (idle_workers * 2)[:4]

        if difficulty == "MODERATE":
            return self._make_subtask_chain([
                (f"Analyze and plan approach for: {goal}", w[0], []),
                (f"Implement and verify: {goal}", w[1] if len(idle_workers) > 1 else w[0], ["subtask_0"]),
            ], context_str)

        if difficulty == "COMPLEX":
            return self._make_subtask_chain([
                (f"Research and analyze requirements for: {goal}", w[0], []),
                (f"Design solution architecture for: {goal}", w[1], ["subtask_0"]),
                (f"Implement core logic for: {goal}", w[2], ["subtask_1"]),
                (f"Validate and test implementation for: {goal}", w[3], ["subtask_2"]),
            ], context_str)

        # ADVERSARIAL / DEBATE
        return self._make_subtask_chain([
            (f"Propose solution approach A for: {goal}", w[0], []),
            (f"Propose alternative approach B for: {goal}", w[1], []),
            (f"Critique both approaches for: {goal}. Identify flaws and risks.", w[2], ["subtask_0", "subtask_1"]),
            (f"Synthesize final solution from debate for: {goal}", w[3], ["subtask_0", "subtask_1", "subtask_2"]),
        ], context_str)

    def _build_context(self, learnings: List[str], docs: List[str]) -> str:
        """Build context string from learnings and retrieved documents."""
        parts = []
        if learnings:
            parts.append("RELEVANT PAST LEARNINGS:")
            for i, l in enumerate(learnings[:3], 1):
                parts.append(f"  {i}. {l[:200]}")
        if docs:
            parts.append("RELEVANT CONTEXT:")
            for i, d in enumerate(docs[:3], 1):
                parts.append(f"  {i}. {d[:200]}")
        return "\n".join(parts) if parts else ""

    def _build_reasoning(self, goal: str, difficulty: str, operator: str,
                         subtasks: List[Subtask], learnings: List[str],
                         docs: List[str], idle_workers: List[str]) -> str:
        """Build human-readable reasoning for the plan."""
        lines = [
            f"Goal: {goal}",
            f"Assessed difficulty: {difficulty} (operator: {operator})",
            f"Available workers: {', '.join(idle_workers)}",
            f"Decomposed into {len(subtasks)} subtask(s)",
        ]
        if learnings:
            lines.append(f"Found {len(learnings)} relevant past learnings")
        if docs:
            lines.append(f"Retrieved {len(docs)} relevant context documents")

        for st in subtasks:
            deps = f" (depends on: {', '.join(st.dependencies)})" if st.dependencies else ""
            lines.append(f"  -> [{st.assigned_worker}] {st.task_text[:80]}{deps}")

        return "\n".join(lines)

    # ─── DISPATCH ──────────────────────────────────────

    def _dispatch_single(self, st: Subtask, dispatch_fn, completed: dict) -> dict:
        """Dispatch one subtask and return its result entry."""
        if st.dependencies and not all(d in completed for d in st.dependencies):
            return {"worker": st.assigned_worker, "dispatched": False,
                    "error": f"Dependencies not met: {st.dependencies}"}

        enriched = f"{st.task_text}\n\nCONTEXT:\n{st.context}" if st.context else st.task_text
        try:
            ok = dispatch_fn(st.assigned_worker, enriched)
            if ok:
                completed[f"subtask_{st.index}"] = True
            return {"worker": st.assigned_worker, "dispatched": ok, "task": st.task_text[:100]}
        except Exception as e:
            return {"worker": st.assigned_worker, "dispatched": False, "error": str(e)}

    def dispatch(self, plan: BrainPlan) -> dict:
        """Execute the plan by dispatching subtasks to workers."""
        from tools.skynet_dispatch import dispatch_to_worker

        completed = {}
        results = {}

        independent = [st for st in plan.subtasks if not st.dependencies]
        dependent = [st for st in plan.subtasks if st.dependencies]

        for st in independent:
            results[f"subtask_{st.index}"] = self._dispatch_single(st, dispatch_to_worker, completed)

        for st in dependent:
            results[f"subtask_{st.index}"] = self._dispatch_single(st, dispatch_to_worker, completed)

        return results

    # ─── SYNTHESIZE ────────────────────────────────────

    def synthesize(self, plan: BrainPlan, results: dict) -> str:
        """Merge worker results into a coherent summary."""
        lines = [f"SYNTHESIS for: {plan.goal}", f"Difficulty: {plan.difficulty}", ""]

        successes = []
        failures = []

        for key, r in results.items():
            worker = r.get("worker", "?")
            task = r.get("task", r.get("error", "?"))
            if r.get("dispatched"):
                content = r.get("result_content", "awaiting result")
                successes.append(f"  [{worker}] {task}: {content}")
            else:
                error = r.get("error", "dispatch failed")
                failures.append(f"  [{worker}] {task}: FAILED - {error}")

        if successes:
            lines.append(f"COMPLETED ({len(successes)}):")
            lines.extend(successes)
        if failures:
            lines.append(f"\nFAILED ({len(failures)}):")
            lines.extend(failures)

        if not successes and not failures:
            lines.append("No results collected yet.")

        return "\n".join(lines)

    # ─── LEARN ─────────────────────────────────────────

    def learn(self, plan: BrainPlan, results: dict, success: bool):
        """Store learnings from completed task execution."""
        strategy_id = getattr(plan, "strategy_id", "") or ""

        # Store in LearningStore
        if self.learning_store:
            try:
                summary = f"Task '{plan.goal}' ({plan.difficulty}): "
                dispatched = sum(1 for r in results.values() if r.get("dispatched"))
                failed = sum(1 for r in results.values() if not r.get("dispatched"))
                summary += f"{dispatched} dispatched, {failed} failed. "
                summary += f"{'Success' if success else 'Failure'}."

                self.learning_store.learn(
                    content=summary,
                    category=plan.domain_tags[0] if plan.domain_tags else "general",
                    source="skynet_brain",
                    tags=["brain", plan.difficulty.lower()] + plan.domain_tags
                         + ([f"strategy:{strategy_id}"] if strategy_id else []),
                )
            except Exception:
                pass

        # Save episode to data/episodes/
        _save_episode(plan, results, success)

        # Feed back to router
        if self.router and success is not None:
            try:
                from core.difficulty_router import QueryDifficulty
                diff_map = {
                    "TRIVIAL": QueryDifficulty.TRIVIAL,
                    "SIMPLE": QueryDifficulty.SIMPLE,
                    "MODERATE": QueryDifficulty.MODERATE,
                    "COMPLEX": QueryDifficulty.COMPLEX,
                    "ADVERSARIAL": QueryDifficulty.ADVERSARIAL,
                }
                actual = diff_map.get(plan.difficulty, QueryDifficulty.MODERATE)
                self.router.feedback(plan.goal, actual, success)
            except Exception:
                pass

        # Broadcast via knowledge system
        try:
            from tools.skynet_knowledge import broadcast_learning
            fact = f"Brain executed '{plan.goal}' at {plan.difficulty} level: {'success' if success else 'failure'}"
            broadcast_learning("brain", fact, "execution", ["brain", plan.difficulty.lower()])
        except Exception:
            pass

    # ─── EXECUTE (all-in-one) ──────────────────────────

    def execute(self, goal: str, wait_timeout: float = 90.0) -> dict:
        """Full pipeline: think -> dispatch -> wait -> synthesize -> learn."""
        # Think
        plan = self.think(goal)
        print(f"\n[brain] Plan ready: {plan.difficulty}, {len(plan.subtasks)} subtasks")
        print(f"[brain] Reasoning:\n{plan.reasoning}\n")

        # Dispatch
        dispatch_results = self.dispatch(plan)
        dispatched_count = sum(1 for r in dispatch_results.values() if r.get("dispatched"))
        print(f"[brain] Dispatched {dispatched_count}/{len(plan.subtasks)} subtasks")

        # Wait for results (poll state file)
        if dispatched_count > 0:
            print(f"[brain] Waiting up to {wait_timeout}s for results...")
            try:
                from tools.skynet_sse_daemon import wait_for_result
                for key in dispatch_results:
                    r = dispatch_results[key]
                    if r.get("dispatched"):
                        worker = r.get("worker", "")
                        result = wait_for_result(worker, timeout=wait_timeout)
                        if result:
                            r["result_content"] = result.get("content", "")[:500]
            except ImportError:
                # Fallback: just wait
                time.sleep(min(10, wait_timeout))

        # Synthesize
        synthesis = self.synthesize(plan, dispatch_results)
        print(f"\n[brain] Synthesis:\n{synthesis}")

        # Learn
        success = all(r.get("dispatched") for r in dispatch_results.values())
        self.learn(plan, dispatch_results, success)

        return {
            "goal": goal,
            "plan": asdict(plan),
            "dispatch_results": dispatch_results,
            "synthesis": synthesis,
            "success": success,
        }


# ─── CLI ───────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Skynet Brain -- Intelligent Dispatch")
    parser.add_argument("command", choices=["think", "execute", "assess"],
                        help="Command: think (plan only), execute (full pipeline), assess (difficulty only)")
    parser.add_argument("goal", type=str, help="The goal/task to process")
    parser.add_argument("--timeout", type=float, default=90.0, help="Wait timeout for execute mode")
    args = parser.parse_args()

    brain = SkynetBrain()

    if args.command == "assess":
        result = brain.assess(args.goal)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "think":
        plan = brain.think(args.goal)
        print(json.dumps(asdict(plan), indent=2, default=str))

    elif args.command == "execute":
        result = brain.execute(args.goal, wait_timeout=args.timeout)
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
