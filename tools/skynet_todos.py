#!/usr/bin/env python3
"""
skynet_todos.py — Per-worker TODO sync system for Skynet.

Stores TODOs in data/todos.json, provides CLI + library API.
Each TODO: {worker, task, status, priority, created_at, updated_at, completed_at, claimed_at, id}

Usage:
    python skynet_todos.py add alpha "Fix dashboard CSS" --priority high
    python skynet_todos.py done alpha <id>
    python skynet_todos.py list [worker]
    python skynet_todos.py claim WORKER           # auto-claim next shared TODO
    python skynet_todos.py sync alpha              # post to bus
    python skynet_todos.py serve                   # dump JSON for endpoint
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
SHARED_ASSIGNEES = ("", "all", "shared", "any", "unassigned", "backlog")


def _todo_target(item: dict) -> str:
    """Return normalized assignee/worker field for a TODO item."""
    return str(item.get("assignee", item.get("worker", "")) or "").strip().lower()


def _matches_actor(item: dict, actor: str, include_claimable: bool = False) -> bool:
    target = _todo_target(item)
    actor_norm = actor.lower()
    if target == actor_norm:
        return True
    return include_claimable and target in SHARED_ASSIGNEES


def _load():
    """Load todos from disk (safe against corruption)."""
    try:
        from tools.skynet_atomic import safe_read_json
    except ModuleNotFoundError:
        from skynet_atomic import safe_read_json
    return safe_read_json(TODOS_FILE, default={"todos": [], "version": 1})


def _save(data):
    """Persist todos to disk (atomic write)."""
    try:
        from tools.skynet_atomic import atomic_write_json
    except ModuleNotFoundError:
        from skynet_atomic import atomic_write_json
    atomic_write_json(TODOS_FILE, data)


def _now_iso() -> str:
    """Return current UTC-ish ISO timestamp."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")  # signed: gamma


def add_todo(worker: str, task: str, priority: str = "normal") -> dict:
    """Add a new TODO item. Always sets created_at and updated_at."""
    data = _load()
    now = _now_iso()
    item = {
        "id": uuid.uuid4().hex[:8],
        "worker": worker.lower(),
        "task": task,
        "status": "pending",
        "priority": priority if priority in VALID_PRIORITIES else "normal",
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    data["todos"].append(item)
    _save(data)
    return item  # signed: gamma


def _resolve_completed_by(item: dict, completed_by: str | None = None) -> str:
    actor = str(completed_by or item.get("completed_by") or "").strip().lower()
    if actor:
        return actor
    target = _todo_target(item)
    return "" if target in SHARED_ASSIGNEES else target


def open_todo_count() -> int:
    """Return total open TODO count across the entire system."""
    data = _load()
    return sum(1 for item in data["todos"] if item["status"] in ("pending", "active"))


def all_tickets_cleared() -> bool:
    """Return True only when there are no pending or active TODO items left."""
    return open_todo_count() == 0


def _maybe_award_zero_ticket_bonus(item: dict) -> None:
    """Award the zero-ticket bonus when the final live TODO is closed."""
    if item.get("status") != "done" or not all_tickets_cleared():
        return
    completed_by = _resolve_completed_by(item)
    if not completed_by:
        return
    try:
        from tools import skynet_scoring as scoring
    except ModuleNotFoundError:
        import skynet_scoring as scoring
    try:
        scoring.award_zero_ticket_clear(item["id"], completed_by, "god")
    except Exception as exc:
        print(
            f"[skynet_todos] zero-ticket bonus award failed for {item['id']}: {exc}",
            file=sys.stderr,
        )


def update_status(todo_id: str, status: str, completed_by: str | None = None) -> dict | None:
    """Update a TODO's status. Always sets updated_at."""
    data = _load()
    for item in data["todos"]:
        if item["id"] == todo_id:
            item["status"] = status
            item["updated_at"] = _now_iso()  # signed: gamma
            if status == "done":
                item["completed_at"] = _now_iso()
                resolved = _resolve_completed_by(item, completed_by)
                if resolved:
                    item["completed_by"] = resolved
            _save(data)
            if status == "done":
                _maybe_award_zero_ticket_bonus(item)
            return item
    return None


def mark_done(worker: str, todo_id: str) -> dict | None:
    """Mark a TODO as done."""
    data = _load()
    now = _now_iso()
    for item in data["todos"]:
        if item["id"] == todo_id and _todo_target(item) == worker.lower():
            item["status"] = "done"
            item["completed_at"] = now
            item["updated_at"] = now  # signed: gamma
            item["completed_by"] = worker.lower()
            _save(data)
            _maybe_award_zero_ticket_bonus(item)
            return item
    return None


def list_todos(worker: str = None, status: str = None, include_claimable: bool = False) -> list:
    """List TODOs, optionally filtered by worker and/or status."""
    data = _load()
    items = data["todos"]
    if worker:
        items = [t for t in items if _matches_actor(t, worker, include_claimable)]
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
    now = _now_iso()
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
            "created_at": item.get("created_at") or now,
            "updated_at": now,
            "completed_at": item.get("completed_at"),
        }
        data["todos"].append(todo)
    _save(data)  # signed: gamma


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


def claimable_count(worker: str) -> int:
    """Return count of shared claimable TODOs for an actor."""
    items = list_todos(worker, include_claimable=True)
    return sum(
        1 for t in items
        if t["status"] in ("pending", "active") and _todo_target(t) in SHARED_ASSIGNEES
    )


def pending_count(worker: str, include_claimable: bool = True) -> int:
    """Return count of pending/active TODOs for an actor.

    When include_claimable is True, shared/unassigned backlog items count as
    stop-blocking work because the actor can proactively pull them.
    Unrecognized statuses are treated as blocking (fail-safe against corruption).
    """
    items = list_todos(worker, include_claimable=include_claimable)
    count = 0
    for t in items:
        status = t.get("status", "unknown")
        if status in ("pending", "active"):
            count += 1
        elif status not in VALID_STATUSES:
            # Corrupted/unknown status -- treat as blocking to be safe
            count += 1
    return count  # signed: alpha


def can_stop(worker: str, include_claimable: bool = True) -> bool:
    """Return True only if an actor has ZERO pending/active or claimable TODOs."""
    return pending_count(worker, include_claimable=include_claimable) == 0


# Priority ordering for auto_claim: critical > high > normal > low
_PRIORITY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}


def claim_todo(todo_id: str, worker: str) -> dict | None:
    """Claim a specific TODO: set assignee=worker, status='active', claimed_at=now.

    Returns the updated item or None if not found.
    """
    data = _load()
    now = _now_iso()
    for item in data["todos"]:
        if item["id"] == todo_id:
            item["assignee"] = worker.lower()
            item["worker"] = worker.lower()
            item["status"] = "active"
            item["claimed_at"] = now
            item["updated_at"] = now
            _save(data)
            return item
    return None  # signed: gamma


def auto_claim(worker: str) -> dict | None:
    """Find the highest-priority pending shared/unassigned TODO and claim it.

    Returns the claimed item or None if nothing is claimable.
    """
    data = _load()
    candidates = [
        t for t in data["todos"]
        if t["status"] == "pending" and _todo_target(t) in SHARED_ASSIGNEES
    ]
    if not candidates:
        return None
    # Sort by priority rank (critical first), then by created_at (oldest first)
    candidates.sort(key=lambda t: (
        _PRIORITY_RANK.get(t.get("priority", "normal"), 2),
        t.get("created_at", ""),
    ))
    best = candidates[0]
    return claim_todo(best["id"], worker)  # signed: gamma


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


def _build_todo_parser():
    """Build the TODO CLI parser."""
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

    p_claim = sub.add_parser("claim", help="Auto-claim next highest-priority shared TODO")
    p_claim.add_argument("worker")  # signed: gamma

    return parser


_STATUS_ICONS = {"pending": "○", "active": "◉", "done": "✓", "cancelled": "✗"}
_PRIORITY_ICONS = {"critical": "!!!", "high": "!!", "normal": "", "low": "~"}


def _print_todo_list(items: list):
    """Print a formatted TODO list."""
    if not items:
        print("No TODOs found.")
        return
    for t in items:
        flag = _STATUS_ICONS.get(t["status"], "?")
        pri = _PRIORITY_ICONS.get(t["priority"], "")
        target = _todo_target(t) or "-"
        print(f"  {flag} [{t['id']}] {target:>12} {pri}{t['task']}")


def _dispatch_todo_command(args) -> int:
    """Dispatch parsed CLI command. Returns 0 on success, 1 on error, -1 for help."""
    if args.cmd == "add":
        item = add_todo(args.worker, args.task, args.priority)
        print(f"Added: [{item['id']}] {item['task']} ({item['priority']})")
        return 0
    if args.cmd == "done":
        item = mark_done(args.worker, args.id)
        if item:
            print(f"Done: [{item['id']}] {item.get('title', item.get('task', ''))}")  # signed: gamma
            return 0
        print(f"Not found: {args.id}", file=sys.stderr)
        return 1
    if args.cmd == "activate":
        item = update_status(args.id, "active")
        if item:
            print(f"Active: [{item['id']}] {item['task']}")
            return 0
        print(f"Not found: {args.id}", file=sys.stderr)
        return 1
    if args.cmd == "list":
        _print_todo_list(list_todos(args.worker, args.status))
        return 0
    if args.cmd == "sync":
        ok = sync_to_bus(args.worker)
        print(f"Synced {'OK' if ok else 'FAILED'}")
        return 0
    if args.cmd == "summary":
        print(json.dumps(get_summary(), indent=2))
        return 0
    if args.cmd == "serve":
        print(json.dumps(_load(), indent=2))
        return 0
    if args.cmd == "check":
        assigned = pending_count(args.worker, include_claimable=False)
        claimable = claimable_count(args.worker)
        count = pending_count(args.worker, include_claimable=True)
        stop_ok = can_stop(args.worker, include_claimable=True)
        print(f"Worker: {args.worker}")
        print(f"Assigned Pending/Active TODOs: {assigned}")
        print(f"Claimable Shared TODOs: {claimable}")
        print(f"Total Stop-Blocking TODOs: {count}")
        print(f"Can stop: {'YES' if stop_ok else 'NO -- KEEP WORKING'}")
        if not stop_ok:
            for t in list_todos(args.worker, include_claimable=True):
                if t["status"] in ("pending", "active"):
                    flag = _STATUS_ICONS.get(t["status"], "?")
                    target = _todo_target(t) or "-"
                    print(f"  {flag} [{t['id']}] [{target}] {t.get('title', t.get('task', ''))}")  # signed: beta
        return 0
    if args.cmd == "cleanup":
        removed = cleanup(args.days)
        print(f"Removed {removed} old completed TODOs")
        return 0
    if args.cmd == "claim":
        item = auto_claim(args.worker)
        if item:
            tid = item.get("title", item.get("task", ""))
            print(f"Claimed: [{item['id']}] {tid} (priority={item.get('priority','normal')})")
            return 0
        print(f"No claimable TODOs available for {args.worker}")
        return 0  # signed: gamma
    return -1


def main():
    parser = _build_todo_parser()
    args = parser.parse_args()
    result = _dispatch_todo_command(args)
    if result == -1:
        parser.print_help()
    elif result == 1:
        sys.exit(1)


if __name__ == "__main__":
    main()
