"""Skynet Prompt Evolution via Genetic Algorithm (P3.11).

Evolves prompt templates using genetic programming techniques.
A population of prompt templates is maintained; each template's fitness
is evaluated by task success rate.  High-fitness parents produce offspring
via crossover and mutation; low-fitness templates are culled each generation.

Operators
---------
* **Crossover** — swap segments between two high-fitness parents.
* **Mutation**  — synonym replacement, instruction reordering, emphasis addition.
* **Selection** — tournament selection (k=3 by default).

Seeds
-----
10 base prompt templates covering different Skynet task types (code review,
implementation, testing, security, refactoring, documentation, architecture,
debugging, monitoring, research).

CLI
---
    python tools/skynet_prompt_evolution.py evolve   [--generations N]
    python tools/skynet_prompt_evolution.py best      [--category CAT]
    python tools/skynet_prompt_evolution.py population
    python tools/skynet_prompt_evolution.py history   [--limit N]
"""
# signed: gamma
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Paths & constants ────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_PATH = DATA_DIR / "prompt_evolution.json"

POPULATION_SIZE = 20
TOURNAMENT_K = 3
CROSSOVER_RATE = 0.7
MUTATION_RATE = 0.3
ELITE_COUNT = 2          # best N survive unchanged
MAX_GENERATIONS = 100
MAX_HISTORY = 200

TASK_CATEGORIES = [
    "code_review", "implementation", "testing", "security",
    "refactoring", "documentation", "architecture", "debugging",
    "monitoring", "research",
]

logger = logging.getLogger("skynet.prompt_evolution")

# ── Synonym bank for mutation ────────────────────────────────────
# signed: gamma
SYNONYMS: Dict[str, List[str]] = {
    "analyze": ["examine", "inspect", "evaluate", "audit", "review"],
    "fix": ["repair", "resolve", "correct", "patch", "remedy"],
    "implement": ["build", "create", "develop", "construct", "write"],
    "check": ["verify", "validate", "confirm", "ensure", "test"],
    "improve": ["enhance", "optimize", "upgrade", "refine", "strengthen"],
    "review": ["inspect", "examine", "assess", "evaluate", "audit"],
    "find": ["locate", "identify", "discover", "detect", "uncover"],
    "report": ["document", "summarize", "describe", "detail", "outline"],
    "scan": ["search", "sweep", "probe", "survey", "inspect"],
    "test": ["verify", "validate", "check", "exercise", "prove"],
}

EMPHASIS_PHRASES = [
    "CRITICAL: ",
    "IMPORTANT: ",
    "Pay special attention to ",
    "Focus on ",
    "Prioritize ",
    "Thoroughly ",
    "Carefully ",
    "Rigorously ",
]

# ── Data classes ─────────────────────────────────────────────────

@dataclass
class PromptTemplate:
    """A single prompt template in the evolving population."""
    template_id: str
    category: str
    segments: List[str]       # ordered list of prompt segments
    fitness: float = 0.0
    uses: int = 0
    successes: int = 0
    failures: int = 0
    generation: int = 0
    parent_ids: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    # signed: gamma

    @property
    def success_rate(self) -> float:
        if self.uses == 0:
            return 0.5  # neutral prior for untested templates
        return self.successes / self.uses

    @property
    def text(self) -> str:
        return " ".join(self.segments)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "template_id": self.template_id,
            "category": self.category,
            "segments": self.segments,
            "fitness": self.fitness,
            "uses": self.uses,
            "successes": self.successes,
            "failures": self.failures,
            "generation": self.generation,
            "parent_ids": self.parent_ids,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PromptTemplate":
        return cls(
            template_id=d["template_id"],
            category=d.get("category", "implementation"),
            segments=d.get("segments", []),
            fitness=d.get("fitness", 0.0),
            uses=d.get("uses", 0),
            successes=d.get("successes", 0),
            failures=d.get("failures", 0),
            generation=d.get("generation", 0),
            parent_ids=d.get("parent_ids", []),
            created_at=d.get("created_at", time.time()),
        )


@dataclass
class GenerationRecord:
    """Record of a single generation's evolution."""
    generation: int
    best_fitness: float
    avg_fitness: float
    population_size: int
    mutations: int
    crossovers: int
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generation": self.generation,
            "best_fitness": round(self.best_fitness, 4),
            "avg_fitness": round(self.avg_fitness, 4),
            "population_size": self.population_size,
            "mutations": self.mutations,
            "crossovers": self.crossovers,
            "timestamp": self.timestamp,
        }


# ── Seed templates ───────────────────────────────────────────────
# signed: gamma

def _make_id() -> str:
    return "pt_" + uuid.uuid4().hex[:10]


SEED_TEMPLATES: List[PromptTemplate] = [
    PromptTemplate(
        template_id=_make_id(), category="code_review",
        segments=[
            "Review the following code for bugs, security issues, and style problems.",
            "Check error handling paths and edge cases.",
            "Verify input validation is present and correct.",
            "Report findings with severity levels.",
        ],
    ),
    PromptTemplate(
        template_id=_make_id(), category="implementation",
        segments=[
            "Implement the requested feature following existing code patterns.",
            "Add proper error handling and logging.",
            "Include type hints and docstrings.",
            "Validate with py_compile after changes.",
        ],
    ),
    PromptTemplate(
        template_id=_make_id(), category="testing",
        segments=[
            "Write comprehensive tests for the target module.",
            "Cover both happy path and edge cases.",
            "Test error conditions and boundary values.",
            "Verify all assertions pass cleanly.",
        ],
    ),
    PromptTemplate(
        template_id=_make_id(), category="security",
        segments=[
            "Audit the code for security vulnerabilities.",
            "Check for injection risks, path traversal, and unsafe deserialization.",
            "Verify authentication and authorization checks.",
            "Report critical findings with remediation steps.",
        ],
    ),
    PromptTemplate(
        template_id=_make_id(), category="refactoring",
        segments=[
            "Refactor the target code to improve clarity and maintainability.",
            "Extract duplicated logic into shared functions.",
            "Preserve existing behavior and interfaces.",
            "Run validation after changes to ensure nothing broke.",
        ],
    ),
    PromptTemplate(
        template_id=_make_id(), category="documentation",
        segments=[
            "Document the module architecture and public API.",
            "Add clear examples for common usage patterns.",
            "Explain design decisions and trade-offs.",
            "Keep documentation concise and actionable.",
        ],
    ),
    PromptTemplate(
        template_id=_make_id(), category="architecture",
        segments=[
            "Analyze the system architecture for scalability and maintainability.",
            "Identify coupling between components.",
            "Propose improvements with clear rationale.",
            "Consider backward compatibility in recommendations.",
        ],
    ),
    PromptTemplate(
        template_id=_make_id(), category="debugging",
        segments=[
            "Investigate the reported issue and identify the root cause.",
            "Trace the execution path to find where behavior diverges.",
            "Apply a targeted fix that addresses the root cause.",
            "Verify the fix resolves the issue without side effects.",
        ],
    ),
    PromptTemplate(
        template_id=_make_id(), category="monitoring",
        segments=[
            "Check system health across all monitored components.",
            "Identify any degraded or failed services.",
            "Verify metrics are being collected accurately.",
            "Report status with actionable recommendations.",
        ],
    ),
    PromptTemplate(
        template_id=_make_id(), category="research",
        segments=[
            "Research the topic thoroughly using available sources.",
            "Compare different approaches with pros and cons.",
            "Synthesize findings into actionable recommendations.",
            "Cite specific evidence for each conclusion.",
        ],
    ),
]


# ── Genetic operators ────────────────────────────────────────────
# signed: gamma

def _tournament_select(
    population: List[PromptTemplate], k: int = TOURNAMENT_K,
) -> PromptTemplate:
    """Tournament selection: pick k random individuals, return the fittest."""
    if len(population) <= k:
        candidates = list(population)
    else:
        candidates = random.sample(population, k)
    return max(candidates, key=lambda t: t.fitness)


def _crossover(
    parent_a: PromptTemplate, parent_b: PromptTemplate,
) -> Tuple[PromptTemplate, PromptTemplate]:
    """Single-point crossover on segment lists.

    Swaps segments after a random crossover point between two parents.
    Children inherit the category of their first parent.
    """
    seg_a = list(parent_a.segments)
    seg_b = list(parent_b.segments)

    min_len = min(len(seg_a), len(seg_b))
    if min_len < 2:
        # Can't crossover single-segment templates
        return (
            PromptTemplate(
                template_id=_make_id(), category=parent_a.category,
                segments=seg_a, parent_ids=[parent_a.template_id, parent_b.template_id],
            ),
            PromptTemplate(
                template_id=_make_id(), category=parent_b.category,
                segments=seg_b, parent_ids=[parent_a.template_id, parent_b.template_id],
            ),
        )

    point = random.randint(1, min_len - 1)
    child_a_segs = seg_a[:point] + seg_b[point:]
    child_b_segs = seg_b[:point] + seg_a[point:]

    child_a = PromptTemplate(
        template_id=_make_id(), category=parent_a.category,
        segments=child_a_segs,
        parent_ids=[parent_a.template_id, parent_b.template_id],
    )
    child_b = PromptTemplate(
        template_id=_make_id(), category=parent_b.category,
        segments=child_b_segs,
        parent_ids=[parent_a.template_id, parent_b.template_id],
    )
    return child_a, child_b


def _mutate(template: PromptTemplate) -> PromptTemplate:
    """Apply one random mutation to a template.

    Mutation types:
      1. Synonym replacement — swap a keyword with a synonym
      2. Instruction reordering — shuffle segment order
      3. Emphasis addition — prepend emphasis phrase to a segment
    """
    mutant = PromptTemplate(
        template_id=_make_id(),
        category=template.category,
        segments=list(template.segments),
        parent_ids=[template.template_id],
    )

    if not mutant.segments:
        return mutant

    mutation_type = random.choice(["synonym", "reorder", "emphasis"])

    if mutation_type == "synonym":
        _apply_synonym_mutation(mutant)
    elif mutation_type == "reorder":
        _apply_reorder_mutation(mutant)
    else:
        _apply_emphasis_mutation(mutant)

    return mutant
# signed: gamma


def _apply_synonym_mutation(template: PromptTemplate) -> None:
    """Replace a random keyword with a synonym in a random segment."""
    idx = random.randrange(len(template.segments))
    seg = template.segments[idx]
    words = seg.split()
    replaceable = [
        (i, w.lower().rstrip(".,;:!?"))
        for i, w in enumerate(words)
        if w.lower().rstrip(".,;:!?") in SYNONYMS
    ]
    if replaceable:
        wi, word = random.choice(replaceable)
        synonym = random.choice(SYNONYMS[word])
        # Preserve original capitalization
        if words[wi][0].isupper():
            synonym = synonym.capitalize()
        # Preserve trailing punctuation
        trail = ""
        while words[wi] and words[wi][-1] in ".,;:!?":
            trail = words[wi][-1] + trail
            words[wi] = words[wi][:-1]
        words[wi] = synonym + trail
        template.segments[idx] = " ".join(words)


def _apply_reorder_mutation(template: PromptTemplate) -> None:
    """Swap two random segments."""
    if len(template.segments) < 2:
        return
    i, j = random.sample(range(len(template.segments)), 2)
    template.segments[i], template.segments[j] = (
        template.segments[j], template.segments[i]
    )


def _apply_emphasis_mutation(template: PromptTemplate) -> None:
    """Add an emphasis phrase to a random segment."""
    idx = random.randrange(len(template.segments))
    seg = template.segments[idx]
    # Don't double-emphasize
    if any(seg.startswith(e) for e in EMPHASIS_PHRASES):
        return
    emphasis = random.choice(EMPHASIS_PHRASES)
    # Lowercase the first char of the segment when prepending
    if seg and seg[0].isupper():
        seg = seg[0].lower() + seg[1:]
    template.segments[idx] = emphasis + seg


# ── Fitness evaluation ───────────────────────────────────────────
# signed: gamma

def evaluate_fitness(template: PromptTemplate) -> float:
    """Compute fitness from success rate with Bayesian smoothing.

    Formula:
        fitness = (successes + prior_a) / (uses + prior_a + prior_b)

    where prior_a=1, prior_b=1 give a Beta(1,1) uniform prior.
    This prevents untested templates from dominating (they score 0.5)
    and converges to the true success rate with more data.

    Bonus: slight diversity bonus for segment variety (0-0.05).
    """
    prior_a, prior_b = 1.0, 1.0
    bayesian = (template.successes + prior_a) / (template.uses + prior_a + prior_b)

    # Diversity bonus: unique word count / total word count
    all_words = template.text.lower().split()
    if all_words:
        diversity = len(set(all_words)) / len(all_words)
    else:
        diversity = 0.0
    diversity_bonus = diversity * 0.05

    return round(bayesian + diversity_bonus, 4)


def record_outcome(
    template: PromptTemplate, success: bool,
) -> None:
    """Record a task outcome for a template."""
    template.uses += 1
    if success:
        template.successes += 1
    else:
        template.failures += 1
    template.fitness = evaluate_fitness(template)


# ── PromptEvolver ────────────────────────────────────────────────
# signed: gamma

class PromptEvolver:
    """Manages a population of prompt templates and evolves them.

    The evolver maintains a population of POPULATION_SIZE templates,
    evaluates fitness based on task outcomes, and produces new generations
    via tournament selection, crossover, and mutation.
    """

    def __init__(self) -> None:
        self.population: List[PromptTemplate] = []
        self.generation: int = 0
        self.history: List[Dict[str, Any]] = []
        self._load()

    # ── State management ─────────────────────────────────────────

    def _load(self) -> None:
        """Load population and history from disk."""
        if not STATE_PATH.exists():
            self._seed_population()
            return
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            self._seed_population()
            return

        self.generation = state.get("generation", 0)
        self.history = state.get("history", [])[-MAX_HISTORY:]
        pop_raw = state.get("population", [])
        self.population = [PromptTemplate.from_dict(d) for d in pop_raw]

        if not self.population:
            self._seed_population()
    # signed: gamma

    def _save(self) -> None:
        """Persist state to disk (atomic write)."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        state = {
            "generation": self.generation,
            "population": [t.to_dict() for t in self.population],
            "history": self.history[-MAX_HISTORY:],
            "saved_at": time.time(),
        }
        tmp = STATE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        tmp.replace(STATE_PATH)

    def _seed_population(self) -> None:
        """Initialize population with seed templates + random variants."""
        self.population = [copy.deepcopy(t) for t in SEED_TEMPLATES]
        # Fill remaining slots with mutations of seeds
        while len(self.population) < POPULATION_SIZE:
            parent = random.choice(SEED_TEMPLATES)
            mutant = _mutate(copy.deepcopy(parent))
            self.population.append(mutant)
        # Evaluate initial fitness
        for t in self.population:
            t.fitness = evaluate_fitness(t)
        self._save()

    # ── Evolution ────────────────────────────────────────────────

    def evolve_generation(self) -> GenerationRecord:
        """Run one generation of evolution.

        Steps:
          1. Sort by fitness, preserve elite
          2. Tournament select parents
          3. Crossover with probability CROSSOVER_RATE
          4. Mutate with probability MUTATION_RATE
          5. Evaluate fitness for new individuals
          6. Cull to POPULATION_SIZE
        """
        self.generation += 1
        mutations_count = 0
        crossovers_count = 0

        # Sort by fitness (descending)
        self.population.sort(key=lambda t: t.fitness, reverse=True)

        # Preserve elite
        elite = [copy.deepcopy(t) for t in self.population[:ELITE_COUNT]]
        offspring: List[PromptTemplate] = []

        # Generate offspring
        target = POPULATION_SIZE - ELITE_COUNT
        while len(offspring) < target:
            parent_a = _tournament_select(self.population)
            parent_b = _tournament_select(self.population)

            if random.random() < CROSSOVER_RATE and parent_a.template_id != parent_b.template_id:
                child_a, child_b = _crossover(parent_a, parent_b)
                child_a.generation = self.generation
                child_b.generation = self.generation
                offspring.extend([child_a, child_b])
                crossovers_count += 1
            else:
                # Clone parent
                child = copy.deepcopy(parent_a)
                child.template_id = _make_id()
                child.generation = self.generation
                offspring.append(child)

            # Mutate
            if offspring and random.random() < MUTATION_RATE:
                idx = len(offspring) - 1
                offspring[idx] = _mutate(offspring[idx])
                offspring[idx].generation = self.generation
                mutations_count += 1

        # Trim to target size
        offspring = offspring[:target]

        # New population = elite + offspring
        self.population = elite + offspring

        # Evaluate fitness for all
        for t in self.population:
            t.fitness = evaluate_fitness(t)

        # Record generation
        fitnesses = [t.fitness for t in self.population]
        record = GenerationRecord(
            generation=self.generation,
            best_fitness=max(fitnesses) if fitnesses else 0.0,
            avg_fitness=sum(fitnesses) / len(fitnesses) if fitnesses else 0.0,
            population_size=len(self.population),
            mutations=mutations_count,
            crossovers=crossovers_count,
        )
        self.history.append(record.to_dict())
        self._save()

        return record
    # signed: gamma

    def evolve(self, generations: int = 5) -> List[GenerationRecord]:
        """Run multiple generations of evolution."""
        records = []
        for _ in range(min(generations, MAX_GENERATIONS)):
            rec = self.evolve_generation()
            records.append(rec)
        return records

    # ── Queries ──────────────────────────────────────────────────

    def best_template(self, category: Optional[str] = None) -> Optional[PromptTemplate]:
        """Return the highest-fitness template, optionally filtered by category."""
        candidates = self.population
        if category:
            candidates = [t for t in candidates if t.category == category]
        if not candidates:
            return None
        return max(candidates, key=lambda t: t.fitness)

    def get_template(self, template_id: str) -> Optional[PromptTemplate]:
        """Find a template by ID."""
        for t in self.population:
            if t.template_id == template_id:
                return t
        return None

    def record_outcome(self, template_id: str, success: bool) -> bool:
        """Record a task outcome for a specific template."""
        template = self.get_template(template_id)
        if not template:
            return False
        record_outcome(template, success)
        self._save()
        return True

    def population_summary(self) -> Dict[str, Any]:
        """Summary of current population."""
        by_cat: Dict[str, List[float]] = {}
        for t in self.population:
            by_cat.setdefault(t.category, []).append(t.fitness)

        cat_stats = {}
        for cat, fits in by_cat.items():
            cat_stats[cat] = {
                "count": len(fits),
                "best": round(max(fits), 4),
                "avg": round(sum(fits) / len(fits), 4),
            }

        fitnesses = [t.fitness for t in self.population]
        return {
            "generation": self.generation,
            "population_size": len(self.population),
            "best_fitness": round(max(fitnesses), 4) if fitnesses else 0.0,
            "avg_fitness": round(sum(fitnesses) / len(fitnesses), 4) if fitnesses else 0.0,
            "categories": cat_stats,
            "total_uses": sum(t.uses for t in self.population),
            "total_successes": sum(t.successes for t in self.population),
        }

    def fitness_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return recent generation records."""
        return self.history[-limit:]


# ── CLI ──────────────────────────────────────────────────────────
# signed: gamma

def _cli() -> None:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Skynet Prompt Evolution via Genetic Algorithm")
    sub = parser.add_subparsers(dest="command")

    # evolve
    p_evolve = sub.add_parser("evolve", help="Run evolution generations")
    p_evolve.add_argument("--generations", type=int, default=5,
                          help="Number of generations to run")

    # best
    p_best = sub.add_parser("best", help="Show best template")
    p_best.add_argument("--category", default=None,
                        help="Filter by task category")

    # population
    sub.add_parser("population", help="Show population summary")

    # history
    p_hist = sub.add_parser("history", help="Show fitness history")
    p_hist.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    evolver = PromptEvolver()

    if args.command == "evolve":
        records = evolver.evolve(args.generations)
        print(f"Evolved {len(records)} generations:")
        for r in records:
            print(f"  Gen {r.generation}: best={r.best_fitness:.4f} "
                  f"avg={r.avg_fitness:.4f} "
                  f"pop={r.population_size} "
                  f"cross={r.crossovers} mut={r.mutations}")
        best = evolver.best_template()
        if best:
            print(f"\nBest template ({best.category}):")
            print(f"  Fitness: {best.fitness:.4f}")
            print(f"  Text: {best.text[:200]}")

    elif args.command == "best":
        best = evolver.best_template(category=args.category)
        if not best:
            cat_msg = f" for category '{args.category}'" if args.category else ""
            print(f"No templates found{cat_msg}.")
            return
        print(f"Best Template ({best.category})")
        print(f"  ID:      {best.template_id}")
        print(f"  Fitness: {best.fitness:.4f}")
        print(f"  Uses:    {best.uses} ({best.successes}S/{best.failures}F)")
        print(f"  Gen:     {best.generation}")
        print(f"  Parents: {best.parent_ids or 'seed'}")
        print(f"\nSegments:")
        for i, seg in enumerate(best.segments, 1):
            print(f"  {i}. {seg}")

    elif args.command == "population":
        summary = evolver.population_summary()
        print(f"Population Summary (Gen {summary['generation']})")
        print(f"  Size: {summary['population_size']}")
        print(f"  Best: {summary['best_fitness']:.4f}")
        print(f"  Avg:  {summary['avg_fitness']:.4f}")
        print(f"  Uses: {summary['total_uses']} "
              f"({summary['total_successes']} successes)")
        print(f"\nBy Category:")
        for cat, stats in sorted(summary["categories"].items()):
            print(f"  {cat:16s}  n={stats['count']}  "
                  f"best={stats['best']:.4f}  avg={stats['avg']:.4f}")

    elif args.command == "history":
        records = evolver.fitness_history(args.limit)
        if not records:
            print("No evolution history yet.")
            return
        print(f"Fitness History (last {len(records)} generations):")
        for r in records:
            print(f"  Gen {r['generation']:3d}: "
                  f"best={r['best_fitness']:.4f} "
                  f"avg={r['avg_fitness']:.4f} "
                  f"pop={r['population_size']} "
                  f"cross={r['crossovers']} mut={r['mutations']}")


if __name__ == "__main__":
    _cli()
