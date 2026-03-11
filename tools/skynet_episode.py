"""Canonical episode logging for Skynet task execution.

Each episode captures a task dispatch, its result, outcome classification,
and metadata. Episodes are persisted as individual JSON files under
data/episodes/ for post-hoc analysis and learning-store ingestion.
"""

import json
import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

EPISODES_DIR = Path(__file__).resolve().parent.parent / "data" / "episodes"


class Outcome(str, Enum):
    """Canonical outcome labels for episode classification."""
    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


def log_episode(
    task: str,
    result: str,
    outcome: str | Outcome,
    strategy_id: Optional[str] = None,
    worker: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """Write an episode record to data/episodes/.

    Args:
        task: The task description that was dispatched.
        result: The raw result text returned by the worker.
        outcome: One of success, failure, unknown.
        strategy_id: Optional identifier linking to the strategy used.
        worker: Name of the worker that executed the task.
        metadata: Arbitrary extra fields.

    Returns:
        The episode dict that was persisted (includes ``filepath``).
    """
    if isinstance(outcome, Outcome):
        outcome_val = outcome.value
    else:
        outcome_val = Outcome(outcome).value

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%d_%H-%M-%S")
    worker_tag = worker or "unknown"

    episode = {
        "timestamp": now.isoformat(),
        "task": task,
        "result": result,
        "outcome": outcome_val,
        "strategy_id": strategy_id,
        "worker": worker_tag,
        "metadata": metadata or {},
    }

    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{ts}_{worker_tag}.json"
    filepath = EPISODES_DIR / filename

    # Avoid collisions by appending a counter
    counter = 1
    while filepath.exists():
        filename = f"{ts}_{worker_tag}_{counter}.json"
        filepath = EPISODES_DIR / filename
        counter += 1

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(episode, f, indent=2, ensure_ascii=False)

    episode["filepath"] = str(filepath)
    return episode


def load_episode(filepath: str) -> dict:
    """Load a single episode from disk."""
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def list_episodes(worker: Optional[str] = None, limit: int = 50) -> list[dict]:
    """List recent episodes, optionally filtered by worker.

    Returns episodes sorted newest-first, up to ``limit``.
    """
    if not EPISODES_DIR.exists():
        return []

    files = sorted(EPISODES_DIR.glob("*.json"), reverse=True)
    episodes = []
    for fp in files:
        if len(episodes) >= limit:
            break
        try:
            ep = load_episode(str(fp))
            if worker and ep.get("worker") != worker:
                continue
            ep["filepath"] = str(fp)
            episodes.append(ep)
        except (json.JSONDecodeError, OSError):
            continue
    return episodes


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "list":
        worker_filter = sys.argv[2] if len(sys.argv) > 2 else None
        for ep in list_episodes(worker=worker_filter, limit=20):
            print(f"[{ep['outcome']}] {ep['worker']}: {ep['task'][:80]}")
    else:
        print("Usage: python skynet_episode.py list [worker]")
