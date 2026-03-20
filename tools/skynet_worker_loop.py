#!/usr/bin/env python3
"""
skynet_worker_loop.py -- Worker autonomy daemon.

Each worker runs this loop to stay productive without orchestrator babysitting.
Polls bus for tasks, checks TODOs, picks up planning proposals when idle.

Usage:
    python tools/skynet_worker_loop.py --worker beta          # run loop
    python tools/skynet_worker_loop.py --worker beta --once   # single cycle
    python tools/skynet_worker_loop.py --worker beta --status # show state

Importable:
    from skynet_worker_loop import WorkerLoop
    loop = WorkerLoop("beta")
    task = loop.next_action()  # returns (action_type, payload) or None
"""

import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
TODOS_FILE = DATA_DIR / "todos.json"
TASK_QUEUE_FILE = DATA_DIR / "task_queue.json"
BUS_URL = "http://localhost:8420"

TASK_POLL_INTERVAL = 5      # check bus for tasks every 5s
TODO_CHECK_INTERVAL = 15    # check TODOs every 15s
PROPOSAL_CHECK_INTERVAL = 60  # check proposals every 60s
STANDING_BY_COOLDOWN = 30   # min seconds between STANDING_BY posts

TERMINAL_STATUSES = ("done", "failed", "cancelled")


def _fetch_json(url, timeout=5):
    try:
        return json.loads(urllib.request.urlopen(url, timeout=timeout).read())
    except Exception as e:
        _log("system", f"_fetch_json({url}): {e}", "WARN")
        return None


def _post_bus(sender, topic, msg_type, content):
    msg = {"sender": sender, "topic": topic,
           "type": msg_type, "content": content}
    try:
        from tools.skynet_spam_guard import guarded_publish
        guarded_publish(msg)
        return True
    except Exception as e:
        # Raw fallback for when SpamGuard is unavailable
        _log(sender, f"_post_bus SpamGuard failed ({e}), trying raw HTTP", "WARN")
        try:
            payload = json.dumps(msg).encode()
            req = urllib.request.Request(
                f"{BUS_URL}/bus/publish", payload,
                {"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
            return True
        except Exception as e2:
            _log(sender, f"_post_bus raw fallback failed: {e2}", "ERROR")
            return False
    # signed: alpha  # error-logging: signed: beta


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        _log("system", f"_load_json({path}): {e}", "WARN")
        return None


class WorkerLoop:
    """Autonomy loop for a single worker."""

    def __init__(self, worker_name):
        self.name = worker_name
        self._last_task_poll = 0
        self._last_todo_check = 0
        self._last_proposal_check = 0
        self._last_standing_by = 0
        self._seen_task_ids = set()
        self._seen_proposal_ids = set()
        self._current_action = None

    def poll_bus_tasks(self):
        """Check bus for tasks addressed to this worker. Returns list of task messages."""
        msgs = _fetch_json(f"{BUS_URL}/bus/messages?limit=50")
        if not msgs or not isinstance(msgs, list):
            return []

        tasks = []
        for m in msgs:
            mid = m.get("id", "")
            if mid in self._seen_task_ids:
                continue

            topic = m.get("topic", "")
            msg_type = m.get("type", "")

            # Match: topic is this worker's name, or topic is "workers" (broadcast)
            is_for_me = (
                topic == self.name
                or topic == f"worker_{self.name}"
                or (topic == "workers" and msg_type in ("task", "urgent-task", "request"))
            )

            if is_for_me and msg_type in ("task", "urgent-task", "sub-task", "request"):
                self._seen_task_ids.add(mid)
                tasks.append(m)

        # Urgent tasks first
        tasks.sort(key=lambda t: 0 if t.get("type") == "urgent-task" else 1)
        return tasks

    def check_todos(self):
        """Check pending TODOs for this worker. Returns list of pending items."""
        # Check todos.json
        pending = []
        data = _load_json(TODOS_FILE)
        if data:
            for t in data.get("todos", []):
                if t.get("worker") == self.name and t.get("status") in ("pending", "active"):
                    pending.append({
                        "source": "todos",
                        "id": t.get("id", "?"),
                        "task": t.get("task", ""),
                        "priority": t.get("priority", "normal"),
                        "status": t.get("status"),
                    })

        # Check task_queue.json
        tq = _load_json(TASK_QUEUE_FILE)
        if tq:
            for t in tq.get("tasks", []):
                if t.get("target") in (self.name, "all") and t.get("status") not in TERMINAL_STATUSES:
                    pending.append({
                        "source": "task_queue",
                        "id": t.get("task_id", "?"),
                        "task": t.get("task", ""),
                        "priority": t.get("priority", "normal"),
                        "status": t.get("status"),
                    })

        # Active items first, then by priority
        pri_order = {"critical": 0, "urgent": 1, "normal": 2}
        pending.sort(key=lambda x: (
            0 if x["status"] == "active" else 1,
            pri_order.get(x["priority"], 2),
        ))
        return pending

    def check_proposals(self):
        """Check bus for planning proposals to work on when idle."""
        msgs = _fetch_json(f"{BUS_URL}/bus/messages?limit=100")
        if not msgs or not isinstance(msgs, list):
            return []

        proposals = []
        for m in msgs:
            mid = m.get("id", "")
            if mid in self._seen_proposal_ids:
                continue
            if m.get("topic") == "planning" and m.get("type") == "proposal":
                self._seen_proposal_ids.add(mid)
                proposals.append(m)

        return proposals

    def next_action(self):
        """Determine the next action for this worker.

        Returns:
            tuple: (action_type, payload) where action_type is one of:
                - "bus_task": a task from the bus (payload = message dict)
                - "todo": a pending TODO (payload = todo dict)
                - "proposal": a planning proposal to work on (payload = message dict)
                - "standing_by": no work available (payload = None)
                - None: too early to check (rate limited)
        """
        now = time.time()

        # 1. Check bus for direct tasks (highest priority, most frequent)
        if now - self._last_task_poll >= TASK_POLL_INTERVAL:
            self._last_task_poll = now
            tasks = self.poll_bus_tasks()
            if tasks:
                self._current_action = ("bus_task", tasks[0])
                return self._current_action

        # 2. Check TODOs
        if now - self._last_todo_check >= TODO_CHECK_INTERVAL:
            self._last_todo_check = now
            todos = self.check_todos()
            if todos:
                self._current_action = ("todo", todos[0])
                return self._current_action

        # 3. Check proposals (least frequent)
        if now - self._last_proposal_check >= PROPOSAL_CHECK_INTERVAL:
            self._last_proposal_check = now
            proposals = self.check_proposals()
            if proposals:
                self._current_action = ("proposal", proposals[-1])  # most recent
                return self._current_action

            # Truly nothing to do -- post STANDING_BY (rate limited)
            if now - self._last_standing_by >= STANDING_BY_COOLDOWN:
                self._last_standing_by = now
                self._current_action = ("standing_by", None)
                return self._current_action

        return None  # rate limited, no check needed yet

    def run_once(self):
        """Single check cycle. Returns action taken."""
        self._last_task_poll = 0
        self._last_todo_check = 0
        self._last_proposal_check = 0
        return self.next_action()

    def run(self):
        """Main daemon loop."""
        _log(self.name, f"Worker loop starting for {self.name.upper()}")
        _post_bus(self.name, "orchestrator", "monitor_alert",
                  f"WORKER_LOOP_ONLINE: {self.name.upper()} autonomy loop started")

        try:
            while True:
                action = self.next_action()
                if action:
                    atype, payload = action
                    if atype == "bus_task":
                        content = str(payload.get("content", ""))[:80]
                        sender = payload.get("sender", "?")
                        _log(self.name, f"BUS_TASK from {sender}: {content}")
                        _post_bus(self.name, "orchestrator", "ack",
                                  f"PICKED_UP: bus task from {sender} -- {content}")
                    elif atype == "todo":
                        _log(self.name, f"TODO: [{payload['id']}] {payload['task'][:60]}")
                    elif atype == "proposal":
                        content = str(payload.get("content", ""))[:80]
                        _log(self.name, f"PROPOSAL: {content}")
                        _post_bus(self.name, "orchestrator", "ack",
                                  f"SELF_ASSIGNED: working on proposal -- {content}")
                    elif atype == "standing_by":
                        _log(self.name, "STANDING_BY -- zero pending items")
                        _post_bus(self.name, "orchestrator", "status", "STANDING_BY")

                time.sleep(TASK_POLL_INTERVAL)
        except KeyboardInterrupt:
            _log(self.name, "Shutting down (Ctrl+C)")


def _log(worker, msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [LOOP:{worker.upper()}] [{level}] {msg}", flush=True)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skynet Worker Autonomy Loop")
    parser.add_argument("--worker", required=True, help="Worker name (alpha/beta/gamma/delta)")
    parser.add_argument("--once", action="store_true", help="Single check cycle")
    parser.add_argument("--status", action="store_true", help="Show current state")
    args = parser.parse_args()

    name = args.worker.lower()
    if name not in ("alpha", "beta", "gamma", "delta"):
        print(f"Invalid worker: {name}")
        sys.exit(1)

    loop = WorkerLoop(name)

    if args.status:
        action = loop.run_once()
        if action:
            atype, payload = action
            print(f"Next action: {atype}")
            if payload:
                print(json.dumps(payload, indent=2, default=str))
        else:
            print("No action needed right now")
        return

    if args.once:
        action = loop.run_once()
        if action:
            atype, payload = action
            print(f"Action: {atype}")
            if payload:
                if isinstance(payload, dict):
                    print(json.dumps(payload, indent=2, default=str))
                else:
                    print(payload)
        else:
            print("Nothing to do")
        return

    loop.run()


if __name__ == "__main__":
    main()
