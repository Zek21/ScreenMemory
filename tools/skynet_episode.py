"""Canonical episode logging for Skynet task execution.

Each episode captures a task dispatch, its result, outcome classification,
and metadata. Episodes are persisted as individual JSON files under
data/episodes/ for post-hoc analysis and learning-store ingestion.

Deduplication: Every episode gets a SHA256 fingerprint derived from
worker + task + outcome + result content. Before writing, the logger
checks for an existing episode with the same fingerprint. Retried
deliveries that produce identical content are silently deduplicated.
"""

import hashlib
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


def compute_fingerprint(worker: str, task: str, outcome: str, result: str) -> str:
    """Generate a SHA256 fingerprint from episode content fields.

    The fingerprint uniquely identifies the semantic content of an episode,
    regardless of timestamp or filename. Two episodes with identical
    worker+task+outcome+result will produce the same fingerprint.
    """
    blob = f"{worker or ''}|{task or ''}|{outcome or ''}|{result or ''}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _find_existing_by_fingerprint(fingerprint: str) -> Optional[Path]:
    """Scan data/episodes/ for an episode matching the given fingerprint.

    Returns the Path of the first match, or None if no duplicate exists.
    """
    if not EPISODES_DIR.exists():
        return None
    for fp in EPISODES_DIR.glob("*.json"):
        try:
            ep = json.loads(fp.read_text(encoding="utf-8"))
            if ep.get("fingerprint") == fingerprint:
                return fp
        except (json.JSONDecodeError, OSError):
            continue
    return None


def _check_dedup(fingerprint: str) -> dict | None:
    """Check if an episode with this fingerprint already exists. Returns episode dict or None."""
    existing = _find_existing_by_fingerprint(fingerprint)
    if existing is not None:
        try:
            ep = json.loads(existing.read_text(encoding="utf-8"))
            ep["filepath"] = str(existing)
            ep["deduplicated"] = True
            return ep
        except Exception:
            pass
    return None


def _write_episode(episode: dict, worker_tag: str, ts: str) -> str:
    """Write episode to disk, handling filename collisions. Returns filepath."""
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{ts}_{worker_tag}.json"
    filepath = EPISODES_DIR / filename
    counter = 1
    while filepath.exists():
        filename = f"{ts}_{worker_tag}_{counter}.json"
        filepath = EPISODES_DIR / filename
        counter += 1
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(episode, f, indent=2, ensure_ascii=False)
    return str(filepath)


def log_episode(
    task: str,
    result: str,
    outcome: str | Outcome,
    strategy_id: Optional[str] = None,
    worker: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """Write an episode record to data/episodes/ with deduplication."""
    outcome_val = outcome.value if isinstance(outcome, Outcome) else Outcome(outcome).value
    worker_tag = worker or "unknown"
    fingerprint = compute_fingerprint(worker_tag, task, outcome_val, result)

    dedup = _check_dedup(fingerprint)
    if dedup is not None:
        return dedup

    now = datetime.now(timezone.utc)
    episode = {
        "timestamp": now.isoformat(), "task": task, "result": result,
        "outcome": outcome_val, "strategy_id": strategy_id,
        "worker": worker_tag, "fingerprint": fingerprint,
        "metadata": metadata or {},
    }

    episode["filepath"] = _write_episode(episode, worker_tag, now.strftime("%Y-%m-%d_%H-%M-%S"))
    return episode


def load_episode(filepath: str) -> dict:
    """Load a single episode from disk."""
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def query_by_fingerprint(fingerprint: str) -> Optional[dict]:
    """Find an episode by its fingerprint hash.

    Returns the episode dict with ``filepath`` set, or None if not found.
    """
    match = _find_existing_by_fingerprint(fingerprint)
    if match is not None:
        try:
            ep = json.loads(match.read_text(encoding="utf-8"))
            ep["filepath"] = str(match)
            return ep
        except Exception:
            pass
    return None


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
    elif len(sys.argv) > 1 and sys.argv[1] == "query-fp":
        if len(sys.argv) < 3:
            print("Usage: python skynet_episode.py query-fp <fingerprint>")
        else:
            ep = query_by_fingerprint(sys.argv[2])
            if ep:
                print(json.dumps(ep, indent=2))
            else:
                print("No episode found with that fingerprint.")
    else:
        print("Usage: python skynet_episode.py list [worker]")
        print("       python skynet_episode.py query-fp <fingerprint>")
