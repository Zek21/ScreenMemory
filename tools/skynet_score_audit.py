#!/usr/bin/env python3
"""Skynet Score Audit Tool — detects unscored completed tasks and scoring gaps.

Usage:
    python tools/skynet_score_audit.py                    # Full audit report
    python tools/skynet_score_audit.py --fix              # Award missing points (dry-run)
    python tools/skynet_score_audit.py --fix --apply      # Award missing points (live)
    python tools/skynet_score_audit.py --worker beta      # Audit specific worker
    python tools/skynet_score_audit.py --summary          # One-line summary per worker
"""
# signed: delta

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
DISPATCH_LOG = DATA / "dispatch_log.json"
SCORES_FILE = DATA / "worker_scores.json"

# Task summaries that are NOT real work (identity acks, announcements, etc.)
NOISE_PATTERNS = [
    "confirm your identity",
    "acknowledge with:",
    "announce yourself",
    "hwnd=",
    "post to bus: sender=",
    "you are now self-invoked",
    "acknowledge: post sender=",
]

WORKER_NAMES = {"alpha", "beta", "gamma", "delta"}


def _is_noise_task(summary: str) -> bool:
    """Check if a task is just identity/ack noise, not real work."""
    lower = summary.lower()
    return any(p in lower for p in NOISE_PATTERNS)
    # signed: delta


def _load_dispatch_log() -> list:
    if not DISPATCH_LOG.exists():
        return []
    try:
        data = json.loads(DISPATCH_LOG.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []
    # signed: delta


def _load_scores() -> dict:
    if not SCORES_FILE.exists():
        return {"scores": {}, "history": []}
    try:
        return json.loads(SCORES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"scores": {}, "history": []}
    # signed: delta


def _scored_task_ids(history: list, worker: str) -> set:
    """Get task_ids that already have awards in history for a worker."""
    ids = set()
    for rec in history:
        if rec.get("worker") == worker and rec.get("action") == "award":
            ids.add(rec.get("task_id", ""))
    return ids
    # signed: delta


def audit_worker(worker: str, dispatch_log: list, scores: dict) -> dict:
    """Audit a single worker's scoring gaps."""
    history = scores.get("history", [])
    scored_ids = _scored_task_ids(history, worker)
    entry = scores.get("scores", {}).get(worker, {})

    tasks = [e for e in dispatch_log if e.get("worker") == worker]
    completed = [t for t in tasks if t.get("result_received")]
    real_completed = [t for t in completed if not _is_noise_task(t.get("task_summary", ""))]
    unscored = []

    for task in real_completed:
        summary = task.get("task_summary", "")[:50]
        ts = task.get("timestamp", "")
        # Check if this task has a corresponding award
        if not any(tid in summary or summary in tid for tid in scored_ids):
            unscored.append({
                "summary": task.get("task_summary", "")[:100],
                "timestamp": ts,
                "result_at": task.get("result_received_at", task.get("received_at", "?")),
            })

    return {
        "worker": worker,
        "total_dispatched": len(tasks),
        "total_completed": len(completed),
        "real_completed": len(real_completed),
        "noise_filtered": len(completed) - len(real_completed),
        "already_scored": len(scored_ids),
        "unscored_tasks": len(unscored),
        "unscored_details": unscored,
        "current_score": entry.get("total", 0.0),
        "current_awards": entry.get("awards", 0),
        "current_deductions": entry.get("deductions", 0),
    }
    # signed: delta


def full_audit(target_worker: str | None = None) -> list:
    """Run full audit across all workers (or specific worker)."""
    dispatch_log = _load_dispatch_log()
    scores = _load_scores()

    workers = [target_worker] if target_worker else sorted(WORKER_NAMES)
    results = []
    for w in workers:
        results.append(audit_worker(w, dispatch_log, scores))
    return results
    # signed: delta


def print_audit(results: list, summary_only: bool = False) -> None:
    """Print audit results."""
    print("=" * 70)
    print("SKYNET SCORE FAIRNESS AUDIT REPORT")
    print(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    for r in results:
        w = r["worker"]
        if summary_only:
            gap = r["unscored_tasks"]
            marker = " ⚠ GAP" if gap > 0 else " ✓"
            print(f"  {w:8s}  score={r['current_score']:.3f}  "
                  f"completed={r['real_completed']}  "
                  f"scored={r['already_scored']}  "
                  f"unscored={gap}{marker}")
            continue

        print(f"\n{'─' * 50}")
        print(f"Worker: {w.upper()}")
        print(f"  Current score:      {r['current_score']:.3f}")
        print(f"  Awards/Deductions:  {r['current_awards']}/{r['current_deductions']}")
        print(f"  Tasks dispatched:   {r['total_dispatched']}")
        print(f"  Tasks completed:    {r['total_completed']}")
        print(f"  Real work tasks:    {r['real_completed']} "
              f"(filtered {r['noise_filtered']} noise)")
        print(f"  Already scored:     {r['already_scored']}")
        print(f"  UNSCORED TASKS:     {r['unscored_tasks']}")

        if r["unscored_details"]:
            print(f"\n  Unscored task details:")
            for i, t in enumerate(r["unscored_details"], 1):
                print(f"    {i}. [{t['timestamp'][:16]}] {t['summary'][:80]}")
                print(f"       Result at: {t['result_at']}")

    print(f"\n{'=' * 70}")
    total_gap = sum(r["unscored_tasks"] for r in results)
    if total_gap > 0:
        print(f"TOTAL SCORING GAP: {total_gap} completed tasks never awarded points")
        print("FIX: Run with --fix --apply to retroactively award 0.01 per task")
    else:
        print("No scoring gaps detected.")
    print("=" * 70)
    # signed: delta


def fix_scores(results: list, apply: bool = False) -> int:
    """Award retroactive points for unscored completed tasks."""
    if not apply:
        print("[DRY RUN] Would award the following points:")

    total_fixed = 0
    for r in results:
        if r["unscored_tasks"] == 0:
            continue
        w = r["worker"]
        for task in r["unscored_details"]:
            amount = 0.01
            task_id = f"retro_{task['timestamp'][:19]}"
            if apply:
                try:
                    sys.path.insert(0, str(BASE))
                    from tools.skynet_scoring import award_points
                    # Use 'orchestrator' as validator for retroactive awards
                    award_points(w, task_id, "orchestrator", amount)
                    print(f"  AWARDED {w}: +{amount} for {task['summary'][:60]}")
                    total_fixed += 1
                except Exception as e:
                    print(f"  FAILED {w}: {e}")
            else:
                print(f"  [DRY] {w}: +{amount} for {task['summary'][:60]}")
                total_fixed += 1

    if apply:
        print(f"\nFixed {total_fixed} scoring gaps.")
    else:
        print(f"\n[DRY RUN] Would fix {total_fixed} gaps. Use --apply to execute.")
    return total_fixed
    # signed: delta


def main():
    parser = argparse.ArgumentParser(description="Skynet Score Fairness Audit")
    parser.add_argument("--worker", type=str, help="Audit specific worker")
    parser.add_argument("--fix", action="store_true", help="Show retroactive fix plan")
    parser.add_argument("--apply", action="store_true", help="Apply fixes (with --fix)")
    parser.add_argument("--summary", action="store_true", help="One-line summary")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    results = full_audit(args.worker)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    elif args.fix:
        print_audit(results, summary_only=True)
        fix_scores(results, apply=args.apply)
    else:
        print_audit(results, summary_only=args.summary)
    # signed: delta


if __name__ == "__main__":
    main()
