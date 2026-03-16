"""Emergent specialization tracking for Skynet workers.

Tracks per-worker success rates across task categories, enabling skill-based
routing. Workers naturally develop specializations through reinforcement:
successful task completions in a category increase that worker's affinity,
while unused skills decay slowly to prevent over-specialization.

Usage:
    python tools/skynet_specialization.py record <worker> <category> <success> [--duration <seconds>]
    python tools/skynet_specialization.py specialization <worker>
    python tools/skynet_specialization.py recommend <category>
    python tools/skynet_specialization.py leaderboard
    python tools/skynet_specialization.py decay [--factor <float>]
"""
# signed: gamma

import json
import os
import sys
import time
import argparse
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILES_PATH = REPO_ROOT / "data" / "agent_profiles.json"

# Standard task categories tracked by the specialization system
TASK_CATEGORIES = [
    "security",
    "testing",
    "refactoring",
    "documentation",
    "infrastructure",
    "frontend",
    "backend",
    "performance",
    "debugging",
    "architecture",
    "code_review",
    "deployment",
    "monitoring",
    "wiring",
    "research",
]  # signed: gamma

# Workers eligible for specialization tracking
WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

# Learning rate for skill vector updates: S_new = S_old + eta * (1 - S_old) on success
SKILL_LEARNING_RATE = 0.05  # signed: gamma

# Decay factor applied to unused skills per decay cycle (weekly recommended)
SKILL_DECAY_FACTOR = 0.98  # signed: gamma

# Minimum tasks in a category before it counts toward specialization ranking
MIN_TASKS_FOR_RANKING = 2  # signed: gamma


def _load_profiles() -> dict:
    """Load agent_profiles.json with utf-8 encoding."""
    with open(PROFILES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_profiles(profiles: dict) -> None:
    """Atomically save agent_profiles.json."""
    tmp = str(PROFILES_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(PROFILES_PATH))  # signed: gamma


def _ensure_perf_field(profile: dict) -> dict:
    """Ensure performance_by_category exists with correct schema.

    Schema per category:
        {
            "tasks_completed": int,
            "tasks_succeeded": int,
            "total_duration_s": float,
            "success_rate": float,      # tasks_succeeded / tasks_completed
            "avg_duration_s": float,     # total_duration_s / tasks_completed
            "skill_score": float         # 0.0–1.0 emergent skill affinity
        }
    """
    if "performance_by_category" not in profile:
        profile["performance_by_category"] = {}
    return profile  # signed: gamma


def _ensure_category(perf: dict, category: str) -> dict:
    """Initialize a category entry if missing."""
    if category not in perf:
        perf[category] = {
            "tasks_completed": 0,
            "tasks_succeeded": 0,
            "total_duration_s": 0.0,
            "success_rate": 0.0,
            "avg_duration_s": 0.0,
            "skill_score": 0.0,
        }
    return perf[category]  # signed: gamma


def record_task_outcome(
    worker: str,
    category: str,
    success: bool,
    duration_s: Optional[float] = None,
) -> dict:
    """Record a task outcome and update the worker's specialization profile.

    Args:
        worker: Worker name (alpha, beta, gamma, delta).
        category: Task category from TASK_CATEGORIES.
        success: Whether the task succeeded.
        duration_s: Optional task duration in seconds.

    Returns:
        Updated category stats dict for the worker.

    Raises:
        ValueError: If worker or category is invalid.
    """
    worker = worker.lower().strip()
    category = category.lower().strip()

    if worker not in WORKER_NAMES:
        raise ValueError(
            f"Unknown worker '{worker}'. Valid: {WORKER_NAMES}"
        )
    if category not in TASK_CATEGORIES:
        raise ValueError(
            f"Unknown category '{category}'. Valid: {TASK_CATEGORIES}"
        )

    profiles = _load_profiles()
    profile = profiles.get(worker, {})
    _ensure_perf_field(profile)
    perf = profile["performance_by_category"]
    cat_stats = _ensure_category(perf, category)

    # Update counters
    cat_stats["tasks_completed"] += 1
    if success:
        cat_stats["tasks_succeeded"] += 1

    if duration_s is not None and duration_s > 0:
        cat_stats["total_duration_s"] += duration_s

    # Recompute derived metrics
    completed = cat_stats["tasks_completed"]
    cat_stats["success_rate"] = round(
        cat_stats["tasks_succeeded"] / completed, 4
    )
    if cat_stats["total_duration_s"] > 0:
        cat_stats["avg_duration_s"] = round(
            cat_stats["total_duration_s"] / completed, 2
        )

    # Update emergent skill score using reinforcement learning rate
    old_score = cat_stats["skill_score"]
    if success:
        # Approach 1.0 asymptotically on success
        cat_stats["skill_score"] = round(
            old_score + SKILL_LEARNING_RATE * (1.0 - old_score), 4
        )
    else:
        # Small penalty on failure (half the learning rate)
        cat_stats["skill_score"] = round(
            max(0.0, old_score - SKILL_LEARNING_RATE * 0.5 * old_score), 4
        )

    # Write back
    perf[category] = cat_stats
    profile["performance_by_category"] = perf
    profiles[worker] = profile
    profiles["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    profiles["updated_by"] = "skynet_specialization"
    _save_profiles(profiles)

    return cat_stats  # signed: gamma


def get_specialization(worker: str, top_n: int = 3) -> list[dict]:
    """Return a worker's top N specialization categories by skill_score.

    Args:
        worker: Worker name.
        top_n: Number of top categories to return (default 3).

    Returns:
        List of dicts: [{category, skill_score, success_rate, tasks_completed}]
        sorted by skill_score descending. Only categories with >= MIN_TASKS_FOR_RANKING
        tasks are included.
    """
    worker = worker.lower().strip()
    if worker not in WORKER_NAMES:
        raise ValueError(f"Unknown worker '{worker}'. Valid: {WORKER_NAMES}")

    profiles = _load_profiles()
    profile = profiles.get(worker, {})
    perf = profile.get("performance_by_category", {})

    ranked = []
    for cat, stats in perf.items():
        if stats.get("tasks_completed", 0) >= MIN_TASKS_FOR_RANKING:
            ranked.append(
                {
                    "category": cat,
                    "skill_score": stats.get("skill_score", 0.0),
                    "success_rate": stats.get("success_rate", 0.0),
                    "tasks_completed": stats.get("tasks_completed", 0),
                    "avg_duration_s": stats.get("avg_duration_s", 0.0),
                }
            )

    # Sort by skill_score desc, then success_rate desc as tiebreaker
    ranked.sort(key=lambda x: (x["skill_score"], x["success_rate"]), reverse=True)
    return ranked[:top_n]  # signed: gamma


def recommend_worker(category: str) -> list[dict]:
    """Recommend the best worker(s) for a task category based on historical performance.

    Args:
        category: Task category to find the best worker for.

    Returns:
        List of all workers ranked by suitability for this category.
        Each entry: {worker, skill_score, success_rate, tasks_completed, rank}.
        Workers with no history in the category appear last with score 0.
    """
    category = category.lower().strip()
    if category not in TASK_CATEGORIES:
        raise ValueError(
            f"Unknown category '{category}'. Valid: {TASK_CATEGORIES}"
        )

    profiles = _load_profiles()
    candidates = []

    for worker_name in WORKER_NAMES:
        profile = profiles.get(worker_name, {})
        perf = profile.get("performance_by_category", {})
        cat_stats = perf.get(category, {})

        skill = cat_stats.get("skill_score", 0.0)
        rate = cat_stats.get("success_rate", 0.0)
        completed = cat_stats.get("tasks_completed", 0)

        # Composite score: 70% skill affinity + 30% raw success rate
        # Workers with zero history get 0.0
        composite = round(0.7 * skill + 0.3 * rate, 4) if completed > 0 else 0.0

        candidates.append(
            {
                "worker": worker_name,
                "skill_score": skill,
                "success_rate": rate,
                "tasks_completed": completed,
                "composite_score": composite,
            }
        )

    # Sort by composite desc, then tasks_completed desc as tiebreaker
    candidates.sort(
        key=lambda x: (x["composite_score"], x["tasks_completed"]),
        reverse=True,
    )

    # Add rank
    for i, c in enumerate(candidates):
        c["rank"] = i + 1

    return candidates  # signed: gamma


def apply_decay(decay_factor: Optional[float] = None) -> dict:
    """Apply time-based decay to all skill scores to prevent over-specialization.

    Unused skills decay toward zero, encouraging workers to maintain breadth.
    Should be called periodically (e.g., weekly).

    Args:
        decay_factor: Multiplier applied to all skill_scores (default SKILL_DECAY_FACTOR).

    Returns:
        Summary of decayed categories per worker.
    """
    factor = decay_factor if decay_factor is not None else SKILL_DECAY_FACTOR
    profiles = _load_profiles()
    summary = {}

    for worker_name in WORKER_NAMES:
        profile = profiles.get(worker_name, {})
        perf = profile.get("performance_by_category", {})
        decayed_cats = []

        for cat, stats in perf.items():
            old = stats.get("skill_score", 0.0)
            if old > 0.001:  # Only decay non-zero scores
                stats["skill_score"] = round(old * factor, 4)
                decayed_cats.append(cat)

        if decayed_cats:
            profile["performance_by_category"] = perf
            profiles[worker_name] = profile
            summary[worker_name] = decayed_cats

    if summary:
        profiles["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        profiles["updated_by"] = "skynet_specialization_decay"
        _save_profiles(profiles)

    return summary  # signed: gamma


def get_leaderboard() -> dict:
    """Return a category-by-category leaderboard of all workers.

    Returns:
        Dict mapping each category to the ranked list of workers with stats.
    """
    profiles = _load_profiles()
    board = {}

    for category in TASK_CATEGORIES:
        entries = []
        for worker_name in WORKER_NAMES:
            profile = profiles.get(worker_name, {})
            perf = profile.get("performance_by_category", {})
            cat_stats = perf.get(category, {})
            completed = cat_stats.get("tasks_completed", 0)
            if completed > 0:
                entries.append(
                    {
                        "worker": worker_name,
                        "skill_score": cat_stats.get("skill_score", 0.0),
                        "success_rate": cat_stats.get("success_rate", 0.0),
                        "tasks_completed": completed,
                    }
                )
        if entries:
            entries.sort(key=lambda x: x["skill_score"], reverse=True)
            board[category] = entries

    return board  # signed: gamma


def _cli():
    """CLI entry point for skynet_specialization."""
    parser = argparse.ArgumentParser(
        description="Skynet emergent specialization tracker"
    )
    sub = parser.add_subparsers(dest="command")

    # record
    rec = sub.add_parser("record", help="Record a task outcome")
    rec.add_argument("worker", help="Worker name")
    rec.add_argument("category", help="Task category")
    rec.add_argument(
        "success", help="true/false or 1/0",
    )
    rec.add_argument(
        "--duration", type=float, default=None, help="Duration in seconds"
    )

    # specialization
    spec = sub.add_parser("specialization", help="Get worker specialization")
    spec.add_argument("worker", help="Worker name")
    spec.add_argument("--top", type=int, default=3, help="Top N categories")

    # recommend
    rcmd = sub.add_parser("recommend", help="Recommend worker for category")
    rcmd.add_argument("category", help="Task category")

    # leaderboard
    sub.add_parser("leaderboard", help="Show full leaderboard")

    # decay
    dec = sub.add_parser("decay", help="Apply skill decay")
    dec.add_argument(
        "--factor", type=float, default=None, help="Decay factor (0-1)"
    )

    args = parser.parse_args()

    if args.command == "record":
        success_val = args.success.lower() in ("true", "1", "yes", "pass")
        result = record_task_outcome(
            args.worker, args.category, success_val, args.duration
        )
        print(json.dumps(result, indent=2))

    elif args.command == "specialization":
        specs = get_specialization(args.worker, args.top)
        if not specs:
            print(f"{args.worker}: No specialization data yet (need >= {MIN_TASKS_FOR_RANKING} tasks per category)")
        else:
            print(f"Top {args.top} specializations for {args.worker}:")
            for i, s in enumerate(specs, 1):
                print(
                    f"  {i}. {s['category']}: skill={s['skill_score']:.3f} "
                    f"success={s['success_rate']:.1%} "
                    f"({s['tasks_completed']} tasks, avg {s['avg_duration_s']:.0f}s)"
                )

    elif args.command == "recommend":
        candidates = recommend_worker(args.category)
        print(f"Best workers for '{args.category}':")
        for c in candidates:
            marker = " <-- BEST" if c["rank"] == 1 and c["composite_score"] > 0 else ""
            print(
                f"  #{c['rank']} {c['worker']}: composite={c['composite_score']:.3f} "
                f"skill={c['skill_score']:.3f} success={c['success_rate']:.1%} "
                f"({c['tasks_completed']} tasks){marker}"
            )

    elif args.command == "leaderboard":
        board = get_leaderboard()
        if not board:
            print("No specialization data recorded yet.")
        else:
            for cat, entries in sorted(board.items()):
                leader = entries[0]
                print(
                    f"  {cat}: {leader['worker']} "
                    f"(skill={leader['skill_score']:.3f}, "
                    f"{leader['tasks_completed']} tasks)"
                )

    elif args.command == "decay":
        summary = apply_decay(args.factor)
        if not summary:
            print("No skills to decay.")
        else:
            for w, cats in summary.items():
                print(f"  {w}: decayed {len(cats)} categories")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: gamma
