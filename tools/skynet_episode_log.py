"""Canonical episode logging for Skynet workers.

Records task episodes with explicit outcomes (success/failure/unknown).
No inferred outcomes -- only what is explicitly reported.

Storage: data/learning_episodes.json
"""

import json
import time
import os
import sys
import argparse
from pathlib import Path
from typing import Optional, List, Dict, Any

REPO_ROOT = Path(__file__).resolve().parent.parent
EPISODES_FILE = REPO_ROOT / "data" / "learning_episodes.json"
VALID_OUTCOMES = {"success", "failure", "unknown"}


def _load_episodes() -> List[Dict[str, Any]]:
    if not EPISODES_FILE.exists():
        return []
    try:
        with open(EPISODES_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def _save_episodes(episodes: List[Dict[str, Any]]) -> None:
    EPISODES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(EPISODES_FILE, "w") as f:
        json.dump(episodes, f, indent=2)


def log_episode(
    worker: str,
    task: str,
    strategy: str,
    outcome: str,
    evidence: str = "",
) -> Dict[str, Any]:
    """Log a task episode with an explicit outcome.

    Args:
        worker: Worker name (alpha/beta/gamma/delta).
        task: Description of the task executed.
        strategy: Strategy or approach used.
        outcome: Must be 'success', 'failure', or 'unknown'.
        evidence: Supporting evidence for the outcome.

    Returns:
        The episode record that was stored.
    """
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"outcome must be one of {VALID_OUTCOMES}, got '{outcome}'")

    episode = {
        "id": f"ep_{int(time.time() * 1000)}_{worker}",
        "worker": worker,
        "task": task,
        "strategy": strategy,
        "outcome": outcome,
        "evidence": evidence,
        "timestamp": time.time(),
        "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    episodes = _load_episodes()
    episodes.append(episode)
    _save_episodes(episodes)
    return episode


def get_episodes(
    worker: Optional[str] = None, limit: int = 50
) -> List[Dict[str, Any]]:
    """Retrieve episodes, optionally filtered by worker.

    Args:
        worker: Filter by worker name, or None for all.
        limit: Maximum number of episodes to return (most recent first).

    Returns:
        List of episode records.
    """
    episodes = _load_episodes()
    if worker:
        episodes = [e for e in episodes if e.get("worker") == worker]
    return episodes[-limit:]


def get_stats() -> Dict[str, Any]:
    """Get aggregate episode statistics.

    Returns:
        Dict with total counts and per-worker breakdowns.
    """
    episodes = _load_episodes()
    stats: Dict[str, Any] = {
        "total": len(episodes),
        "by_outcome": {"success": 0, "failure": 0, "unknown": 0},
        "by_worker": {},
    }

    for ep in episodes:
        outcome = ep.get("outcome", "unknown")
        stats["by_outcome"][outcome] = stats["by_outcome"].get(outcome, 0) + 1

        w = ep.get("worker", "unknown")
        if w not in stats["by_worker"]:
            stats["by_worker"][w] = {"success": 0, "failure": 0, "unknown": 0, "total": 0}
        stats["by_worker"][w][outcome] = stats["by_worker"][w].get(outcome, 0) + 1
        stats["by_worker"][w]["total"] += 1

    if stats["total"] > 0:
        stats["success_rate"] = round(
            stats["by_outcome"]["success"] / stats["total"], 3
        )
    else:
        stats["success_rate"] = 0.0

    return stats


def main():
    parser = argparse.ArgumentParser(description="Skynet Episode Logger")
    sub = parser.add_subparsers(dest="cmd")

    log_p = sub.add_parser("log", help="Log an episode")
    log_p.add_argument("--worker", required=True)
    log_p.add_argument("--task", required=True)
    log_p.add_argument("--strategy", required=True)
    log_p.add_argument("--outcome", required=True, choices=VALID_OUTCOMES)
    log_p.add_argument("--evidence", default="")

    sub.add_parser("stats", help="Show episode statistics")

    list_p = sub.add_parser("list", help="List episodes")
    list_p.add_argument("--worker", default=None)
    list_p.add_argument("--limit", type=int, default=50)

    args = parser.parse_args()

    if args.cmd == "log":
        ep = log_episode(args.worker, args.task, args.strategy, args.outcome, args.evidence)
        print(json.dumps(ep, indent=2))
    elif args.cmd == "stats":
        print(json.dumps(get_stats(), indent=2))
    elif args.cmd == "list":
        eps = get_episodes(worker=args.worker, limit=args.limit)
        print(json.dumps(eps, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
