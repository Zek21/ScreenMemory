#!/usr/bin/env python3
"""skynet_worker_poll.py — Pull-based task discovery for Skynet workers.

Workers are push-dispatched (text typed into window), but push can fail.
This module lets workers (or the self-prompt daemon) PULL pending work.

Usage:
    python tools/skynet_worker_poll.py alpha        # What should alpha be doing?
    python tools/skynet_worker_poll.py --all        # All workers' pending work
    python tools/skynet_worker_poll.py --idle       # Only workers with pending work

API:
    from tools.skynet_worker_poll import poll_for_work
    result = poll_for_work("alpha")
    # result = {pending_tasks: [...], bus_requests: [...], todos: [...],
    #           queued_tasks: [...], summary_text: "...", has_work: True}
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BUS_URL = "http://localhost:8420"
WORKER_NAMES = ("alpha", "beta", "gamma", "delta")
KNOWN_ACTORS = WORKER_NAMES + ("orchestrator", "consultant", "gemini_consultant")
SHARED_ASSIGNEES = ("", "all", "shared", "any", "unassigned", "backlog")

# How far back to look for bus messages (seconds)
BUS_LOOKBACK_S = 300  # 5 minutes


def _load_json(path):
    """Load JSON file, return empty dict on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _todo_target(item):
    """Return normalized assignee/worker target for a TODO-like object."""
    return str(item.get("assignee", item.get("worker", "")) or "").strip().lower()


def _todo_item_view(item, target):
    return {
        "id": item.get("id", ""),
        "task": item.get("task", item.get("title", ""))[:200],
        "status": item.get("status", "").lower(),
        "priority": item.get("priority", "normal"),
        "target": target or "-",
    }


def _get_pending_tasks(worker_name):
    """Check data/task_queue.json for tasks targeting this worker with status=pending."""
    data = _load_json(DATA / "task_queue.json")
    tasks = data.get("tasks", [])
    pending = []
    for t in tasks:
        target = t.get("target", "").lower()
        status = t.get("status", "").lower()
        if target == worker_name.lower() and status in ("pending", "active"):
            pending.append({
                "task_id": t.get("task_id", ""),
                "task": t.get("task", ""),
                "priority": t.get("priority", "normal"),
                "status": status,
                "sender": t.get("sender", ""),
                "created_at": t.get("created_at", ""),
            })
    return pending


def _get_queued_tasks(worker_name):
    """Check Go backend /bus/tasks for unclaimed tasks this worker could pick up."""
    try:
        import requests
        resp = requests.get(f"{BUS_URL}/bus/tasks", timeout=3)
        if resp.status_code != 200:
            return []
        tasks = resp.json()
        if not isinstance(tasks, list):
            return []
        # Unclaimed tasks (no claimed_by) or tasks targeting this worker
        relevant = []
        for t in tasks:
            claimed = t.get("claimed_by", "")
            status = t.get("status", "").lower()
            if status != "pending":
                continue
            # Unclaimed tasks anyone can pick up, or tasks with this worker's name in them
            task_text = t.get("task", "").lower()
            if not claimed or claimed.lower() == worker_name.lower() or worker_name.lower() in task_text:
                relevant.append({
                    "id": t.get("id", ""),
                    "task": t.get("task", ""),
                    "priority": t.get("priority", 0),
                    "source": t.get("source", ""),
                    "status": status,
                })
        return relevant
    except Exception:
        return []


def _get_bus_requests(worker_name):
    """Check bus messages for topic=workers addressed to this worker."""
    try:
        import requests
        resp = requests.get(f"{BUS_URL}/bus/messages", params={
            "topic": "workers",
            "limit": 50,
        }, timeout=3)
        if resp.status_code != 200:
            return []
        messages = resp.json()
        if not isinstance(messages, list):
            return []

        now = time.time()
        relevant = []
        for m in messages:
            content = m.get("content", "").lower()
            msg_type = m.get("type", "").lower()
            # Look for sub-tasks, requests, or directives mentioning this worker
            if worker_name.lower() in content or msg_type in ("sub-task", "request", "directive"):
                # Check age — only recent messages
                ts = m.get("timestamp", "")
                relevant.append({
                    "id": m.get("id", ""),
                    "sender": m.get("sender", ""),
                    "type": msg_type,
                    "content": m.get("content", "")[:200],
                    "timestamp": ts,
                })
        return relevant
    except Exception:
        return []


def _get_todos(worker_name):
    """Check data/todos.json for this worker's pending/active items."""
    data = _load_json(DATA / "todos.json")
    todos = data.get("todos", [])
    pending = []
    for t in todos:
        worker = _todo_target(t)
        status = t.get("status", "").lower()
        if worker == worker_name.lower() and status in ("pending", "active"):
            pending.append(_todo_item_view(t, worker))
    return pending


def _get_claimable_todos(actor_name):
    """Return shared/unassigned TODOs this actor can proactively pull."""
    data = _load_json(DATA / "todos.json")
    todos = data.get("todos", [])
    claimable = []
    for t in todos:
        target = _todo_target(t)
        status = t.get("status", "").lower()
        if status not in ("pending", "active"):
            continue
        if target in SHARED_ASSIGNEES:
            claimable.append(_todo_item_view(t, target))
    return claimable


def _get_directives(worker_name):
    """Check bus for directives targeting this worker from orchestrator."""
    try:
        import requests
        resp = requests.get(f"{BUS_URL}/bus/messages", params={
            "topic": "orchestrator",
            "limit": 30,
        }, timeout=3)
        if resp.status_code != 200:
            return []
        messages = resp.json()
        if not isinstance(messages, list):
            return []

        relevant = []
        for m in messages:
            msg_type = m.get("type", "").lower()
            content = m.get("content", "").lower()
            if msg_type == "directive" and worker_name.lower() in content:
                relevant.append({
                    "id": m.get("id", ""),
                    "sender": m.get("sender", ""),
                    "content": m.get("content", "")[:200],
                    "timestamp": m.get("timestamp", ""),
                })
        return relevant
    except Exception:
        return []


def _build_work_summary(worker_name: str, sources: dict) -> str:
    """Build a human-readable summary of pending work items."""
    total = sum(len(v) for v in sources.values())
    if not total:
        return f"=== {worker_name.upper()} has NO pending work ==="

    lines = [f"=== {worker_name.upper()} has {total} pending item(s) ==="]
    _formatters = {
        "pending_tasks": ("[TASK QUEUE]", lambda t: f"  - [{t['priority'].upper()}] {t['task_id']}: {t['task'][:80]}"),
        "queued_tasks": ("[GO QUEUE]", lambda t: f"  - {t['id']}: {t['task'][:80]}"),
        "bus_requests": ("[BUS REQUESTS]", lambda m: f"  - from {m['sender']} ({m['type']}): {m['content'][:80]}"),
        "directives": ("[DIRECTIVES]", lambda d: f"  - from {d['sender']}: {d['content'][:80]}"),
        "todos": ("[TODOs]", lambda t: f"  - [{t['priority'].upper()}] {t['id']}: {t['task'][:80]}"),
        "claimable_todos": ("[CLAIMABLE TODOs]", lambda t: f"  - [{t['priority'].upper()}] {t['id']} ({t['target']}): {t['task'][:80]}"),
    }
    for key, (label, fmt) in _formatters.items():
        items = sources.get(key, [])
        if items:
            lines.append(f"\n{label} {len(items)} item(s):")
            lines.extend(fmt(item) for item in items)
    return "\n".join(lines)


def poll_for_work(worker_name):
    """Poll all sources for pending work assigned to a worker."""
    sources = {
        "pending_tasks": _get_pending_tasks(worker_name),
        "queued_tasks": _get_queued_tasks(worker_name),
        "bus_requests": _get_bus_requests(worker_name),
        "directives": _get_directives(worker_name),
        "todos": _get_todos(worker_name),
        "claimable_todos": _get_claimable_todos(worker_name),
    }
    total = sum(len(v) for v in sources.values())

    return {
        "worker": worker_name,
        **sources,
        "has_work": total > 0,
        "total_items": total,
        "summary_text": _build_work_summary(worker_name, sources),
    }


def poll_all_workers(workers=None):
    """Poll all workers and return combined results."""
    if workers is None:
        workers = ["alpha", "beta", "gamma", "delta"]
    results = {}
    for w in workers:
        results[w] = poll_for_work(w)
    return results


def find_idle_with_work(workers=None):
    """Find workers that have pending work but might be idle.

    Returns list of (worker_name, result) tuples for workers with has_work=True.
    Useful for the self-prompt daemon or orchestrator to detect and re-dispatch.
    """
    all_results = poll_all_workers(workers)
    return [(name, r) for name, r in all_results.items() if r["has_work"]]


def _format_table(results):
    """Format poll results as a compact table."""
    lines = [
        f"{'Worker':<8} {'Tasks':<6} {'Queue':<6} {'Bus':<6} {'Dirs':<6} {'TODOs':<6} {'Total':<6} {'Status'}",
        "-" * 60,
    ]
    for name in sorted(results.keys()):
        r = results[name]
        status = "HAS WORK" if r["has_work"] else "clear"
        lines.append(
            f"{name:<8} {len(r['pending_tasks']):<6} {len(r['queued_tasks']):<6} "
            f"{len(r['bus_requests']):<6} {len(r['directives']):<6} {len(r['todos']):<6} "
            f"{r['total_items']:<6} {status}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or "-h" in args or "--help" in args:
        print("Usage:")
        print("  python skynet_worker_poll.py <worker>   -- poll one worker")
        print("  python skynet_worker_poll.py --all       -- poll all workers (table)")
        print("  python skynet_worker_poll.py --idle      -- only workers with pending work")
        print("  python skynet_worker_poll.py --json <w>  -- JSON output for worker")
        sys.exit(0)

    if "--all" in args:
        results = poll_all_workers()
        print(_format_table(results))
        print()
        for name, r in results.items():
            if r["has_work"]:
                print(r["summary_text"])
                print()

    elif "--idle" in args:
        idle_with_work = find_idle_with_work()
        if not idle_with_work:
            print("All workers clear -- no pending work found.")
        else:
            print(f"{len(idle_with_work)} worker(s) have pending work:\n")
            for name, r in idle_with_work:
                print(r["summary_text"])
                print()

    elif "--json" in args:
        idx = args.index("--json")
        worker = args[idx + 1] if idx + 1 < len(args) else "alpha"
        result = poll_for_work(worker)
        print(json.dumps(result, indent=2, default=str))

    else:
        worker = args[0].lower()
        if worker not in KNOWN_ACTORS:
            print(f"Unknown actor: {worker}")
            sys.exit(1)
        result = poll_for_work(worker)
        print(result["summary_text"])
        if result["has_work"]:
            sys.exit(0)
        else:
            sys.exit(0)
