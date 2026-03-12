#!/usr/bin/env python3
"""Skynet Worker Autonomy — Auto-generates improvement tasks for idle workers.

When workers are idle and no bus tasks are pending, this module generates
improvement proposals and dispatches them as directives. Tracks activity
in data/autonomy_log.json.

Usage:
    python tools/skynet_worker_autonomy.py              # Single scan + dispatch cycle
    python tools/skynet_worker_autonomy.py --daemon      # Continuous loop (60s interval)
    python tools/skynet_worker_autonomy.py --dry-run     # Show what would be dispatched
    python tools/skynet_worker_autonomy.py --status      # Show autonomy log stats
"""

import argparse
import json
import os
import pathlib
import random
import sys
import time
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
BUS_URL = os.environ.get("SKYNET_URL", "http://localhost:8420")
AUTONOMY_LOG = DATA_DIR / "autonomy_log.json"
TODOS_FILE = DATA_DIR / "todos.json"
PID_FILE = DATA_DIR / "worker_autonomy.pid"

WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

IMPROVEMENT_CATEGORIES = [
    {
        "category": "code_quality",
        "tasks": [
            "Scan tools/*.py for functions longer than 80 lines. Pick the worst offender, refactor it into smaller functions, and verify syntax. Post the file and function name to bus.",
            "Find Python files under tools/ with no docstrings on public functions. Add concise docstrings to the 3 worst files. Post file names to bus.",
            "Search for bare except clauses (except:) in the codebase. Replace them with specific exception types. Post count and files changed to bus.",
            "Find any hardcoded localhost URLs or port numbers that should be constants. Centralize them. Post changes to bus.",
            "Look for TODO/FIXME/HACK comments in the codebase. Pick the most impactful one and fix it. Post what you fixed to bus.",
        ],
    },
    {
        "category": "testing",
        "tasks": [
            "Run pytest tests/ -x --tb=short and report results to bus. If any fail, diagnose the root cause and post findings.",
            "Check test coverage for tools/skynet_dispatch.py. If any critical function lacks tests, write one focused test. Post to bus.",
            "Verify all daemon PID files in data/ correspond to alive processes. Report stale entries to bus.",
        ],
    },
    {
        "category": "documentation",
        "tasks": [
            "Check if AGENTS.md incident log is up to date. Cross-reference with data/incidents.json. Report any gaps to bus.",
            "Scan tools/skynet_*.py files and verify each has a module-level docstring explaining purpose and usage. Fix any missing ones. Post to bus.",
            "Review README.md and check if all tools mentioned actually exist. Report any stale references to bus.",
        ],
    },
    {
        "category": "performance",
        "tasks": [
            "Profile the import time of core/*.py modules. Find the slowest import and see if it can be lazy-loaded. Post findings to bus.",
            "Check data/ directory for files larger than 1MB that could be trimmed or archived. Report sizes to bus.",
            "Scan for any sleep() calls longer than 5 seconds in the codebase. Evaluate if they can be reduced. Post findings to bus.",
        ],
    },
    {
        "category": "system_health",
        "tasks": [
            "Verify all Skynet endpoints respond: /status, /bus/messages, /stream. Report any failures to bus.",
            "Check if data/workers.json HWNDs are still valid windows. Report stale entries to bus.",
            "Scan for any Python processes consuming excessive CPU (>50%). Report PIDs and command lines to bus.",
        ],
    },
]


def _fetch_json(url, timeout=5):
    """Fetch JSON from URL, return None on failure."""
    try:
        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode())
    except Exception:
        return None


def _post_bus(sender, topic, msg_type, content):
    """Post message to Skynet bus via SpamGuard, raw fallback if unavailable."""
    try:
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish({
            "sender": sender,
            "topic": topic,
            "type": msg_type,
            "content": content,
        })
        return result.get("allowed", False) or result.get("published", False)
    except Exception:
        pass
    # Raw fallback only when SpamGuard is unavailable
    try:
        data = json.dumps({
            "sender": sender,
            "topic": topic,
            "type": msg_type,
            "content": content,
        }).encode()
        req = urllib.request.Request(
            f"{BUS_URL}/bus/publish",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False
    # signed: delta


def _load_autonomy_log():
    """Load autonomy log from disk."""
    if AUTONOMY_LOG.exists():
        try:
            return json.loads(AUTONOMY_LOG.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"dispatches": [], "stats": {"total": 0, "by_category": {}, "by_worker": {}}}


def _save_autonomy_log(log_data):
    """Save autonomy log to disk, keeping last 200 entries."""
    if len(log_data["dispatches"]) > 200:
        log_data["dispatches"] = log_data["dispatches"][-200:]
    AUTONOMY_LOG.write_text(json.dumps(log_data, indent=2), encoding="utf-8")


def get_idle_workers():
    """Return list of worker names that are currently IDLE."""
    status = _fetch_json(f"{BUS_URL}/status")
    if not status or "agents" not in status:
        return []

    idle = []
    for name in WORKER_NAMES:
        agent = status["agents"].get(name, {})
        if agent.get("status", "").upper() == "IDLE":
            idle.append(name)
    return idle


def get_pending_todos(worker_name=None):
    """Count pending TODO items, optionally filtered by worker."""
    if not TODOS_FILE.exists():
        return 0
    try:
        todos = json.loads(TODOS_FILE.read_text(encoding="utf-8"))
        if isinstance(todos, list):
            items = todos
        elif isinstance(todos, dict):
            items = todos.get("items", todos.get("todos", []))
        else:
            return 0

        count = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            status = item.get("status", "pending")
            if status in ("pending", "active"):
                if worker_name:
                    assignee = item.get("assignee", item.get("worker", ""))
                    if assignee and assignee.lower() != worker_name.lower():
                        continue
                count += 1
        return count
    except Exception:
        return 0


def get_pending_bus_tasks():
    """Check bus for pending task requests addressed to workers."""
    msgs = _fetch_json(f"{BUS_URL}/bus/messages?limit=30")
    if not msgs or not isinstance(msgs, list):
        return []

    pending = []
    for m in msgs:
        if m.get("topic") in ("workers", "worker") and m.get("type") in ("request", "task", "directive", "sub-task"):
            pending.append(m)
    return pending


def pick_improvement(worker_name, log_data):
    """Select an improvement task that hasn't been recently dispatched."""
    recent_tasks = set()
    for entry in log_data["dispatches"][-30:]:
        recent_tasks.add(entry.get("task_hash", ""))

    all_tasks = []
    for cat in IMPROVEMENT_CATEGORIES:
        for task in cat["tasks"]:
            task_hash = str(hash(task))
            if task_hash not in recent_tasks:
                all_tasks.append((cat["category"], task, task_hash))

    if not all_tasks:
        # All tasks exhausted recently — reset and pick random
        cat = random.choice(IMPROVEMENT_CATEGORIES)
        task = random.choice(cat["tasks"])
        return cat["category"], task, str(hash(task))

    return random.choice(all_tasks)


def _dispatch_improvement(worker: str, category: str, task: str) -> bool:
    """Dispatch an improvement task to a worker. Returns success flag."""
    try:
        sys.path.insert(0, str(ROOT))
        from tools.skynet_dispatch import dispatch_to_worker
        return dispatch_to_worker(worker, task)
    except Exception as e:
        print(f"  [ERROR] Failed to dispatch to {worker}: {e}")
        return False


def _record_dispatch(log_data: dict, worker: str, category: str,
                     task: str, task_hash: str, success: bool):
    """Record a dispatch entry in the autonomy log."""
    log_data["dispatches"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "worker": worker, "category": category,
        "task": task[:200], "task_hash": task_hash, "success": success,
    })
    log_data["stats"]["total"] += 1
    log_data["stats"]["by_category"][category] = log_data["stats"]["by_category"].get(category, 0) + 1
    log_data["stats"]["by_worker"][worker] = log_data["stats"]["by_worker"].get(worker, 0) + 1


def scan_and_dispatch(dry_run=False):
    """Main scan cycle: find idle workers, check for pending work, dispatch improvements."""
    idle_workers = get_idle_workers()
    if not idle_workers:
        return {"dispatched": 0, "reason": "no idle workers"}

    pending_todos = get_pending_todos()
    pending_bus = get_pending_bus_tasks()
    if pending_todos > 0 or len(pending_bus) > 0:
        return {
            "dispatched": 0,
            "reason": f"pending work exists (todos={pending_todos}, bus_tasks={len(pending_bus)})",
            "idle_workers": idle_workers,
        }

    log_data = _load_autonomy_log()
    dispatched = []

    for worker in idle_workers:
        if get_pending_todos(worker) > 0:
            continue

        category, task, task_hash = pick_improvement(worker, log_data)

        if dry_run:
            print(f"  [DRY-RUN] Would dispatch to {worker.upper()}: [{category}] {task[:80]}...")
            dispatched.append({"worker": worker, "category": category, "task": task[:80]})
            continue

        success = _dispatch_improvement(worker, category, task)
        _record_dispatch(log_data, worker, category, task, task_hash, success)

        if success:
            dispatched.append({"worker": worker, "category": category})
            _post_bus("autonomy", "orchestrator", "autonomy_dispatch",
                      f"Auto-dispatched [{category}] improvement to {worker.upper()}")
        time.sleep(0.5)

    if not dry_run:
        _save_autonomy_log(log_data)

    return {"dispatched": len(dispatched), "details": dispatched}


def show_status():
    """Print autonomy log statistics."""
    log_data = _load_autonomy_log()
    stats = log_data["stats"]
    print("=== Skynet Worker Autonomy Stats ===")
    print(f"Total dispatches: {stats.get('total', 0)}")
    print(f"By category: {json.dumps(stats.get('by_category', {}), indent=2)}")
    print(f"By worker: {json.dumps(stats.get('by_worker', {}), indent=2)}")
    recent = log_data["dispatches"][-5:]
    if recent:
        print("\nLast 5 dispatches:")
        for e in recent:
            print(f"  {e.get('timestamp', '?')} {e.get('worker', '?').upper()} [{e.get('category', '?')}] {'OK' if e.get('success') else 'FAIL'}")


def _pid_guard():
    """Singleton PID guard for daemon mode."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            os.kill(old_pid, 0)
            print(f"[AUTONOMY] Already running (PID {old_pid}). Exiting.")
            sys.exit(0)
        except (OSError, ValueError):
            pass
    PID_FILE.write_text(str(os.getpid()))


def main():
    parser = argparse.ArgumentParser(description="Skynet Worker Autonomy — auto-improve idle workers")
    parser.add_argument("--daemon", action="store_true", help="Run as continuous daemon (60s cycle)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be dispatched without acting")
    parser.add_argument("--status", action="store_true", help="Show autonomy log stats")
    parser.add_argument("--interval", type=int, default=300, help="Daemon cycle interval in seconds (default 300 = 5min)")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.daemon:
        _pid_guard()
        print(f"[AUTONOMY] Daemon started (PID {os.getpid()}, interval={args.interval}s)")
        try:
            while True:
                result = scan_and_dispatch(dry_run=args.dry_run)
                if result.get("dispatched", 0) > 0:
                    print(f"[AUTONOMY] Dispatched {result['dispatched']} improvements")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n[AUTONOMY] Daemon stopped.")
        finally:
            if PID_FILE.exists():
                PID_FILE.unlink(missing_ok=True)
        return

    # Single scan
    result = scan_and_dispatch(dry_run=args.dry_run)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
