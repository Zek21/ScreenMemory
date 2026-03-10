#!/usr/bin/env python3
"""
skynet_task_queue.py -- Pull-based task queue for Skynet workers.

Workers can post tasks, browse pending tasks, claim work, and report completion.
This replaces the push-only bus model with a pull-based work distribution system
where any idle worker can grab uncompleted tasks.

Usage:
    python tools/skynet_task_queue.py list                    # Show pending tasks
    python tools/skynet_task_queue.py list --all              # Show all tasks
    python tools/skynet_task_queue.py add "task description"  # Add a task
    python tools/skynet_task_queue.py claim TASK_ID WORKER    # Claim a task
    python tools/skynet_task_queue.py done TASK_ID WORKER "result"  # Mark complete

Python API:
    from tools.skynet_task_queue import post_task, list_tasks, claim_task, complete_task
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

SKYNET = "http://localhost:8420"


def _post(path: str, body: dict) -> dict | None:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{SKYNET}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return None


def _get(path: str) -> list | dict | None:
    try:
        with urllib.request.urlopen(f"{SKYNET}{path}", timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return None


def post_task(task: str, source: str = "orchestrator", priority: int = 0) -> str | None:
    """Add a task to the queue. Returns task_id or None."""
    r = _post("/bus/tasks", {"task": task, "source": source, "priority": priority})
    return r.get("task_id") if r else None


def list_tasks(show_all: bool = False) -> list:
    """List tasks. Default: pending only. show_all=True for all statuses."""
    path = "/bus/tasks?all=true" if show_all else "/bus/tasks"
    r = _get(path)
    return r if isinstance(r, list) else []


def claim_task(task_id: str, worker: str) -> bool:
    """Claim a pending task. Returns True if claimed."""
    r = _post("/bus/tasks/claim", {"task_id": task_id, "worker": worker})
    return r is not None and r.get("status") == "claimed"


def complete_task(task_id: str, worker: str, result: str, failed: bool = False) -> bool:
    """Mark a task as completed or failed."""
    r = _post("/bus/tasks/complete", {
        "task_id": task_id, "worker": worker,
        "result": result, "status": "failed" if failed else "completed",
    })
    return r is not None


def grab_next(worker: str) -> dict | None:
    """Grab the highest-priority pending task. Returns task dict or None."""
    tasks = list_tasks(show_all=False)
    if not tasks:
        return None
    # Sort by priority descending (2=critical, 1=high, 0=normal)
    tasks.sort(key=lambda t: t.get("priority", 0), reverse=True)
    for t in tasks:
        if claim_task(t["id"], worker):
            return t
    return None


def main():
    parser = argparse.ArgumentParser(description="Skynet Task Queue")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list").add_argument("--all", action="store_true")
    add_p = sub.add_parser("add")
    add_p.add_argument("task")
    add_p.add_argument("--source", default="orchestrator")
    add_p.add_argument("--priority", type=int, default=0)

    claim_p = sub.add_parser("claim")
    claim_p.add_argument("task_id")
    claim_p.add_argument("worker")

    done_p = sub.add_parser("done")
    done_p.add_argument("task_id")
    done_p.add_argument("worker")
    done_p.add_argument("result", nargs="?", default="")
    done_p.add_argument("--failed", action="store_true")

    grab_p = sub.add_parser("grab")
    grab_p.add_argument("worker")

    args = parser.parse_args()

    if args.cmd == "list":
        tasks = list_tasks(show_all=args.all)
        if not tasks:
            print("No tasks in queue")
        for t in tasks:
            claimed = f" [claimed by {t['claimed_by']}]" if t.get("claimed_by") else ""
            print(f"  {t['id']} | {t['status']:9s} | p={t.get('priority',0)} | {t['task'][:70]}{claimed}")
    elif args.cmd == "add":
        tid = post_task(args.task, source=args.source, priority=args.priority)
        print(f"Queued: {tid}" if tid else "Failed to queue task")
    elif args.cmd == "claim":
        ok = claim_task(args.task_id, args.worker)
        print(f"Claimed: {args.task_id}" if ok else "Failed to claim (already taken?)")
    elif args.cmd == "done":
        ok = complete_task(args.task_id, args.worker, args.result, failed=args.failed)
        print(f"Completed: {args.task_id}" if ok else "Failed to complete")
    elif args.cmd == "grab":
        t = grab_next(args.worker)
        if t:
            print(f"Grabbed: {t['id']} | {t['task']}")
        else:
            print("No pending tasks to grab")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
