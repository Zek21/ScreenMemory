#!/usr/bin/env python3
"""
skynet_task_tracker.py -- Task lifecycle tracker for Skynet.

Every task flows: queued -> delivered -> executing -> done | failed

Provides CRUD operations on data/task_queue.json and integrates with
the Zero Ticket Stop rule -- no worker may stop while tasks remain
with status not in (done, failed, cancelled).

Usage:
    python tools/skynet_task_tracker.py create --target alpha --task "scan codebase"
    python tools/skynet_task_tracker.py update --id TASK-xxx --status executing
    python tools/skynet_task_tracker.py pending
    python tools/skynet_task_tracker.py pending --worker beta
    python tools/skynet_task_tracker.py summary
    python tools/skynet_task_tracker.py can-stop --worker beta
    python tools/skynet_task_tracker.py gc --days 7
"""

import argparse
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
TASK_FILE = DATA_DIR / "task_queue.json"

_lock = threading.Lock()

VALID_STATUSES = ("queued", "delivered", "executing", "done", "failed", "cancelled")
TERMINAL_STATUSES = ("done", "failed", "cancelled")
VALID_PRIORITIES = ("normal", "urgent", "critical")
VALID_TARGETS = ("alpha", "beta", "gamma", "delta", "orchestrator", "all")


def _load():
    """Load task queue from disk."""
    try:
        return json.loads(TASK_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"tasks": [], "version": 0}


def _save(data):
    """Atomically save task queue."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data["version"] = data.get("version", 0) + 1
    data["updated_at"] = datetime.now().isoformat()
    tmp = TASK_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(TASK_FILE)


def create_task(target, task_text, priority="normal", sender="god-console", task_id=None):
    """Create a new task. Returns the task record."""
    if target not in VALID_TARGETS:
        raise ValueError(f"Invalid target: {target}. Must be one of {VALID_TARGETS}")
    if priority not in VALID_PRIORITIES:
        priority = "normal"
    if not task_text or not task_text.strip():
        raise ValueError("Task text cannot be empty")

    task_id = task_id or f"TASK-{uuid.uuid4().hex[:8]}"
    now = datetime.now().isoformat()

    record = {
        "task_id": task_id,
        "target": target,
        "task": task_text.strip(),
        "priority": priority,
        "status": "queued",
        "sender": sender,
        "created_at": now,
        "updated_at": now,
        "delivered_at": None,
        "started_at": None,
        "completed_at": None,
        "result": None,
    }

    with _lock:
        data = _load()
        data["tasks"].append(record)
        _save(data)

    return record


def update_task(task_id, status=None, result=None):
    """Update task status and/or result. Returns updated record or None."""
    if status and status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}. Must be one of {VALID_STATUSES}")

    with _lock:
        data = _load()
        for t in data["tasks"]:
            if t["task_id"] == task_id:
                now = datetime.now().isoformat()
                if status:
                    t["status"] = status
                    t["updated_at"] = now
                    if status == "delivered":
                        t["delivered_at"] = now
                    elif status == "executing":
                        t["started_at"] = now
                    elif status in TERMINAL_STATUSES:
                        t["completed_at"] = now
                if result is not None:
                    t["result"] = str(result)[:500]
                _save(data)
                return t
    return None


def get_pending(worker=None):
    """Get all tasks not in terminal state. Optionally filter by worker."""
    data = _load()
    tasks = data.get("tasks", [])
    pending = [t for t in tasks if t.get("status") not in TERMINAL_STATUSES]
    if worker:
        pending = [t for t in pending if t.get("target") in (worker, "all")]
    return pending


def get_summary():
    """Count tasks by status and by worker."""
    data = _load()
    tasks = data.get("tasks", [])

    by_status = {}
    by_worker = {}
    for t in tasks:
        s = t.get("status", "unknown")
        w = t.get("target", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
        if w not in by_worker:
            by_worker[w] = {"total": 0, "pending": 0, "done": 0, "failed": 0}
        by_worker[w]["total"] += 1
        if s in TERMINAL_STATUSES:
            by_worker[w]["done" if s == "done" else "failed"] += 1
        else:
            by_worker[w]["pending"] += 1

    return {
        "total": len(tasks),
        "by_status": by_status,
        "by_worker": by_worker,
        "timestamp": datetime.now().isoformat(),
    }


def can_stop(worker):
    """Zero Ticket Stop check. Returns (can_stop: bool, pending_count: int, tasks: list)."""
    pending = get_pending(worker)
    return len(pending) == 0, len(pending), pending


def get_task(task_id):
    """Get a single task by ID."""
    data = _load()
    for t in data.get("tasks", []):
        if t["task_id"] == task_id:
            return t
    return None


def gc_old_tasks(days=7):
    """Remove completed tasks older than N days."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with _lock:
        data = _load()
        before = len(data["tasks"])
        data["tasks"] = [
            t for t in data["tasks"]
            if t.get("status") not in TERMINAL_STATUSES
            or (t.get("completed_at") or "") > cutoff
        ]
        after = len(data["tasks"])
        _save(data)
    return before - after


# ─── Dispatch Coherence Analysis ──────────────────────────────────────────
# Root cause fix (2026-03-23): dispatch_log showed result_received=False for
# ALL dispatches. This correlator detects the gap between dispatches and
# results, identifies lost tasks, and provides coherence metrics.
# signed: orchestrator

DISPATCH_LOG_FILE = DATA_DIR / "dispatch_log.json"
STALE_DISPATCH_THRESHOLD_S = 300  # 5 minutes without result = stale
LOST_DISPATCH_THRESHOLD_S = 600   # 10 minutes = lost


def _load_dispatch_log() -> list:
    """Load dispatch_log.json safely."""
    if not DISPATCH_LOG_FILE.exists():
        return []
    try:
        return json.loads(DISPATCH_LOG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def correlate_dispatches() -> dict:
    """Correlate dispatch log with results to find coherence gaps.
    
    Returns dict with coherence_ratio, per_worker stats, and stale tasks.
    """
    dispatches = _load_dispatch_log()
    now = time.time()
    
    coherence = {
        "total_dispatches": len(dispatches),
        "with_results": 0,
        "without_results": 0,
        "success_dispatches": 0,
        "failed_dispatches": 0,
        "stale_tasks": [],
        "per_worker": {},
        "coherence_ratio": 0.0,
    }
    
    for d in dispatches:
        worker = d.get("worker", "?")
        success = d.get("success", False)
        has_result = d.get("result_received", False)
        
        if success:
            coherence["success_dispatches"] += 1
        else:
            coherence["failed_dispatches"] += 1
        
        if has_result:
            coherence["with_results"] += 1
        else:
            coherence["without_results"] += 1
            # Check if stale
            ts = d.get("timestamp", "")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("+08:00", ""))
                    age = (datetime.now() - dt).total_seconds()
                    if age > STALE_DISPATCH_THRESHOLD_S and success:
                        coherence["stale_tasks"].append({
                            "worker": worker,
                            "task": d.get("task", "?")[:80],
                            "age_s": int(age),
                            "status": "LOST" if age > LOST_DISPATCH_THRESHOLD_S else "STALE",
                        })
                except (ValueError, TypeError):
                    pass
        
        # Per-worker stats
        ws = coherence["per_worker"].get(worker, {"dispatched": 0, "results": 0, "failures": 0})
        ws["dispatched"] += 1
        if has_result:
            ws["results"] += 1
        if not success:
            ws["failures"] += 1
        coherence["per_worker"][worker] = ws
    
    if coherence["success_dispatches"] > 0:
        coherence["coherence_ratio"] = round(
            coherence["with_results"] / coherence["success_dispatches"] * 100, 1
        )
    
    return coherence


def mark_dispatch_result(worker: str, success: bool = True):
    """Mark the most recent dispatch for a worker as having received a result.
    
    Called by bus polling / result processing when a worker reports DONE.
    """
    dispatches = _load_dispatch_log()
    changed = False
    
    # Walk backwards to find the most recent unresolved dispatch for this worker
    for d in reversed(dispatches):
        if d.get("worker") == worker and not d.get("result_received"):
            d["result_received"] = True
            d["result_at"] = datetime.now().isoformat()
            d["result_success"] = success
            changed = True
            break
    
    if changed:
        try:
            tmp = DISPATCH_LOG_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(dispatches, indent=2, default=str), encoding="utf-8")
            tmp.replace(DISPATCH_LOG_FILE)
        except OSError:
            pass
    
    return changed


def get_pending_count(worker=None):
    """Quick count of pending tasks for a worker (or all)."""
    return len(get_pending(worker))


def _build_tracker_parser():
    """Build the task tracker CLI parser."""
    parser = argparse.ArgumentParser(description="Skynet Task Tracker")
    sub = parser.add_subparsers(dest="cmd")

    c = sub.add_parser("create", help="Create a new task")
    c.add_argument("--target", required=True, choices=list(VALID_TARGETS))
    c.add_argument("--task", required=True)
    c.add_argument("--priority", default="normal", choices=list(VALID_PRIORITIES))
    c.add_argument("--sender", default="cli")

    u = sub.add_parser("update", help="Update task status")
    u.add_argument("--id", required=True)
    u.add_argument("--status", choices=list(VALID_STATUSES))
    u.add_argument("--result")

    sub.add_parser("pending", help="List pending tasks").add_argument("--worker")
    sub.add_parser("summary", help="Task summary")

    cs = sub.add_parser("can-stop", help="Zero Ticket Stop check")
    cs.add_argument("--worker", required=True)

    g = sub.add_parser("gc", help="Garbage collect old tasks")
    g.add_argument("--days", type=int, default=7)

    sub.add_parser("coherence", help="Dispatch coherence analysis")

    return parser


def _dispatch_tracker_command(args) -> int:
    """Dispatch parsed CLI command. Returns 0 on success, 1 on error, -1 for help."""
    if args.cmd == "create":
        print(json.dumps(create_task(args.target, args.task, args.priority, args.sender), indent=2))
        return 0
    if args.cmd == "update":
        rec = update_task(args.id, args.status, args.result)
        if rec:
            print(json.dumps(rec, indent=2))
            return 0
        print(f"Task {args.id} not found", file=sys.stderr)
        return 1
    if args.cmd == "pending":
        worker = getattr(args, "worker", None)
        tasks = get_pending(worker)
        print(json.dumps(tasks, indent=2))
        print(f"\n{len(tasks)} pending task(s)")
        return 0
    if args.cmd == "summary":
        print(json.dumps(get_summary(), indent=2))
        return 0
    if args.cmd == "can-stop":
        ok, count, tasks = can_stop(args.worker)
        if ok:
            print(f"{args.worker} CAN stop -- no pending tasks")
        else:
            print(f"{args.worker} CANNOT stop -- {count} pending task(s):")
            for t in tasks:
                print(f"  [{t['status']}] {t['task_id']}: {t['task'][:60]}")
        sys.exit(0 if ok else 1)
    if args.cmd == "gc":
        removed = gc_old_tasks(args.days)
        print(f"Removed {removed} completed task(s) older than {args.days} days")
        return 0
    if args.cmd == "coherence":
        c = correlate_dispatches()
        print(f"\n{'='*60}")
        print(f"  SKYNET DISPATCH COHERENCE REPORT")
        print(f"{'='*60}")
        print(f"  Coherence Ratio: {c['coherence_ratio']}%")
        print(f"  Total Dispatches: {c['total_dispatches']}")
        print(f"  With Results: {c['with_results']}")
        print(f"  Without Results: {c['without_results']}")
        print(f"  Failed Dispatches: {c['failed_dispatches']}")
        print()
        for worker, ws in c["per_worker"].items():
            ratio = round(ws["results"] / ws["dispatched"] * 100, 1) if ws["dispatched"] > 0 else 0
            print(f"  {worker:12} dispatched={ws['dispatched']} results={ws['results']} ({ratio}%) failures={ws['failures']}")
        if c["stale_tasks"]:
            print(f"\n  STALE/LOST TASKS ({len(c['stale_tasks'])}):")
            for t in c["stale_tasks"][:10]:
                print(f"    [{t['status']}] {t['worker']:6} {t['age_s']}s ago: {t['task'][:50]}")
        return 0
    return -1


def main():
    parser = _build_tracker_parser()
    args = parser.parse_args()
    result = _dispatch_tracker_command(args)
    if result == -1:
        parser.print_help()


if __name__ == "__main__":
    main()
