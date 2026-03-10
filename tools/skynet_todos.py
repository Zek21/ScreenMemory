#!/usr/bin/env python3
"""
skynet_todos.py — Per-worker TODO sync system for Skynet.

Stores TODOs in data/todos.json, provides CLI + library API.
Each TODO: {worker, task, status, priority, created_at, completed_at, id}

Usage:
    python skynet_todos.py add alpha "Fix dashboard CSS" --priority high
    python skynet_todos.py done alpha <id>
    python skynet_todos.py list [worker]
    python skynet_todos.py sync alpha            # post to bus
    python skynet_todos.py serve                 # dump JSON for endpoint
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
TODOS_FILE = DATA_DIR / "todos.json"
BUS_URL = "http://localhost:8420/bus/publish"

VALID_STATUSES = ("pending", "active", "done", "cancelled")
VALID_PRIORITIES = ("low", "normal", "high", "critical")


def _load():
    """Load todos from disk."""
    if TODOS_FILE.exists():
        try:
            return json.loads(TODOS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"todos": [], "version": 1}
    return {"todos": [], "version": 1}


def _save(data):
    """Persist todos to disk."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TODOS_FILE.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def add_todo(worker: str, task: str, priority: str = "normal") -> dict:
    """Add a new TODO item."""
    data = _load()
    item = {
        "id": uuid.uuid4().hex[:8],
        "worker": worker.lower(),
        "task": task,
        "status": "pending",
        "priority": priority if priority in VALID_PRIORITIES else "normal",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "completed_at": None,
    }
    data["todos"].append(item)
    _save(data)
    return item


def update_status(todo_id: str, status: str) -> dict | None:
    """Update a TODO's status."""
    data = _load()
    for item in data["todos"]:
        if item["id"] == todo_id:
            item["status"] = status
            if status == "done":
                item["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            _save(data)
            return item
    return None


def mark_done(worker: str, todo_id: str) -> dict | None:
    """Mark a TODO as done."""
    data = _load()
    for item in data["todos"]:
        if item["id"] == todo_id and item["worker"] == worker.lower():
            item["status"] = "done"
            item["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            _save(data)
            return item
    return None


def list_todos(worker: str = None, status: str = None) -> list:
    """List TODOs, optionally filtered by worker and/or status."""
    data = _load()
    items = data["todos"]
    if worker:
        items = [t for t in items if t["worker"] == worker.lower()]
    if status:
        items = [t for t in items if t["status"] == status]
    return items


def get_summary() -> dict:
    """Summary stats for dashboard consumption."""
    data = _load()
    todos = data["todos"]
    by_worker = {}
    for t in todos:
        w = t["worker"]
        if w not in by_worker:
            by_worker[w] = {"pending": 0, "active": 0, "done": 0, "cancelled": 0, "items": []}
        by_worker[w][t["status"]] = by_worker[w].get(t["status"], 0) + 1
        if t["status"] in ("pending", "active"):
            by_worker[w]["items"].append(t)

    total = len(todos)
    pending = sum(1 for t in todos if t["status"] == "pending")
    active = sum(1 for t in todos if t["status"] == "active")
    done = sum(1 for t in todos if t["status"] == "done")
    return {
        "total": total,
        "pending": pending,
        "active": active,
        "done": done,
        "by_worker": by_worker,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def bulk_update(worker: str, items: list):
    """Replace all TODOs for a worker from a bus sync message."""
    data = _load()
    # Remove old items for this worker that are pending/active
    data["todos"] = [t for t in data["todos"] if t["worker"] != worker.lower() or t["status"] == "done"]
    # Add new items
    for item in items:
        if isinstance(item, str):
            item = {"task": item, "status": "pending", "priority": "normal"}
        todo = {
            "id": item.get("id", uuid.uuid4().hex[:8]),
            "worker": worker.lower(),
            "task": item.get("task", str(item)),
            "status": item.get("status", "pending"),
            "priority": item.get("priority", "normal"),
            "created_at": item.get("created_at", time.strftime("%Y-%m-%dT%H:%M:%S")),
            "completed_at": item.get("completed_at"),
        }
        data["todos"].append(todo)
    _save(data)


def sync_to_bus(worker: str):
    """Post worker's TODO list to the Skynet bus."""
    items = list_todos(worker)
    active = [t for t in items if t["status"] in ("pending", "active")]
    import urllib.request
    payload = json.dumps({
        "sender": worker.lower(),
        "topic": "todos",
        "type": "update",
        "content": json.dumps(active),
    }).encode()
    try:
        req = urllib.request.Request(BUS_URL, payload, {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


def pending_count(worker: str) -> int:
    """Return count of pending/active TODOs for a worker."""
    items = list_todos(worker)
    return sum(1 for t in items if t["status"] in ("pending", "active"))


def can_stop(worker: str) -> bool:
    """Return True only if worker has ZERO pending/active TODOs."""
    return pending_count(worker) == 0


def cleanup(days_old: int = 7):
    """Remove completed TODOs older than N days."""
    data = _load()
    cutoff = time.time() - (days_old * 86400)
    before = len(data["todos"])
    data["todos"] = [
        t for t in data["todos"]
        if t["status"] != "done" or not t.get("completed_at")
        or time.mktime(time.strptime(t["completed_at"], "%Y-%m-%dT%H:%M:%S")) > cutoff
    ]
    _save(data)
    return before - len(data["todos"])


def main():
    parser = argparse.ArgumentParser(description="Skynet TODO Sync System")
    sub = parser.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add", help="Add a TODO")
    p_add.add_argument("worker")
    p_add.add_argument("task")
    p_add.add_argument("--priority", default="normal", choices=VALID_PRIORITIES)

    p_done = sub.add_parser("done", help="Mark TODO as done")
    p_done.add_argument("worker")
    p_done.add_argument("id")

    p_active = sub.add_parser("activate", help="Mark TODO as active")
    p_active.add_argument("id")

    p_list = sub.add_parser("list", help="List TODOs")
    p_list.add_argument("worker", nargs="?")
    p_list.add_argument("--status")

    p_sync = sub.add_parser("sync", help="Sync worker TODOs to bus")
    p_sync.add_argument("worker")

    sub.add_parser("summary", help="Print summary JSON")
    sub.add_parser("serve", help="Print full TODO JSON for endpoint")

    p_check = sub.add_parser("check", help="Check if worker can stop")
    p_check.add_argument("worker")

    p_clean = sub.add_parser("cleanup", help="Remove old completed TODOs")
    p_clean.add_argument("--days", type=int, default=7)

    args = parser.parse_args()

    if args.cmd == "add":
        item = add_todo(args.worker, args.task, args.priority)
        print(f"Added: [{item['id']}] {item['task']} ({item['priority']})")
    elif args.cmd == "done":
        item = mark_done(args.worker, args.id)
        if item:
            print(f"Done: [{item['id']}] {item['task']}")
        else:
            print(f"Not found: {args.id}", file=sys.stderr)
            sys.exit(1)
    elif args.cmd == "activate":
        item = update_status(args.id, "active")
        if item:
            print(f"Active: [{item['id']}] {item['task']}")
        else:
            print(f"Not found: {args.id}", file=sys.stderr)
            sys.exit(1)
    elif args.cmd == "list":
        items = list_todos(args.worker, args.status)
        if not items:
            print("No TODOs found.")
        else:
            for t in items:
                flag = {"pending": "○", "active": "◉", "done": "✓", "cancelled": "✗"}.get(t["status"], "?")
                pri = {"critical": "!!!", "high": "!!", "normal": "", "low": "~"}.get(t["priority"], "")
                print(f"  {flag} [{t['id']}] {t['worker']:>8} {pri}{t['task']}")
    elif args.cmd == "sync":
        ok = sync_to_bus(args.worker)
        print(f"Synced {'OK' if ok else 'FAILED'}")
    elif args.cmd == "summary":
        print(json.dumps(get_summary(), indent=2))
    elif args.cmd == "serve":
        data = _load()
        print(json.dumps(data, indent=2))
    elif args.cmd == "check":
        count = pending_count(args.worker)
        stop_ok = can_stop(args.worker)
        print(f"Worker: {args.worker}")
        print(f"Pending/Active TODOs: {count}")
        print(f"Can stop: {'YES' if stop_ok else 'NO -- KEEP WORKING'}")
        if not stop_ok:
            items = list_todos(args.worker)
            for t in items:
                if t["status"] in ("pending", "active"):
                    flag = {"pending": "○", "active": "◉"}.get(t["status"], "?")
                    print(f"  {flag} [{t['id']}] {t['task']}")
    elif args.cmd == "cleanup":
        removed = cleanup(args.days)
        print(f"Removed {removed} old completed TODOs")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
