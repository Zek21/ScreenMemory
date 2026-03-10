#!/usr/bin/env python3
"""
skynet_orch_poller.py -- Orchestrator bus poller daemon.

Polls the bus for messages addressed to the orchestrator (topic=orchestrator,
type=directive or type=task) and queues them in data/orch_queue.json.

The self-prompt daemon (skynet_self_prompt.py) reads orch_queue.json and
types pending directives into the orchestrator window. After delivery,
entries are marked status=delivered.

This decouples task submission from delivery:
  - Anyone POSTs to bus: topic=orchestrator, type=directive, content="do X"
  - This daemon catches it and queues it
  - self_prompt daemon delivers it to the orchestrator chat

Usage:
    python tools/skynet_orch_poller.py start         # run as daemon
    python tools/skynet_orch_poller.py once           # single poll cycle
    python tools/skynet_orch_poller.py status         # show queue state
    python tools/skynet_orch_poller.py drain          # mark all as delivered
    python tools/skynet_orch_poller.py stop           # show PID for manual stop
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

DATA_DIR = ROOT / "data"
PID_FILE = DATA_DIR / "orch_poller.pid"
QUEUE_FILE = DATA_DIR / "orch_queue.json"
BUS_URL = "http://localhost:8420"

POLL_INTERVAL = 5      # seconds
MAX_QUEUE_SIZE = 100    # cap queue to prevent unbounded growth
STALE_HOURS = 24        # auto-expire entries older than this

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [ORCH-POLLER] [{level}] {msg}", flush=True)


def _fetch_json(url, timeout=5):
    try:
        return json.loads(urllib.request.urlopen(url, timeout=timeout).read())
    except Exception:
        return None


def _post_bus(topic, msg_type, content):
    try:
        payload = json.dumps({
            "sender": "orch_poller",
            "topic": topic,
            "type": msg_type,
            "content": content,
        }).encode()
        req = urllib.request.Request(
            f"{BUS_URL}/bus/publish", payload,
            {"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


def load_queue():
    """Load the orchestrator directive queue."""
    try:
        data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"queue": [], "stats": {"total_queued": 0, "total_delivered": 0, "total_expired": 0}}


def save_queue(data):
    """Atomically save the queue."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(QUEUE_FILE)


def enqueue_directive(sender, content, msg_id, msg_type="directive", priority="normal"):
    """Add a directive to the orchestrator queue."""
    data = load_queue()
    queue = data.get("queue", [])

    # Dedup: skip if msg_id already queued
    existing_ids = {e.get("msg_id") for e in queue}
    if msg_id in existing_ids:
        return False

    entry = {
        "msg_id": msg_id,
        "sender": sender,
        "type": msg_type,
        "content": content,
        "priority": priority,
        "status": "pending",
        "queued_at": datetime.now().isoformat(),
        "delivered_at": None,
    }
    queue.append(entry)

    # Trim to MAX_QUEUE_SIZE (drop oldest delivered first, then oldest pending)
    if len(queue) > MAX_QUEUE_SIZE:
        delivered = [e for e in queue if e.get("status") == "delivered"]
        pending = [e for e in queue if e.get("status") != "delivered"]
        # Keep all pending, trim delivered from oldest
        keep_delivered = delivered[-(MAX_QUEUE_SIZE - len(pending)):] if len(pending) < MAX_QUEUE_SIZE else []
        queue = keep_delivered + pending
        queue = queue[-MAX_QUEUE_SIZE:]

    data["queue"] = queue
    data["stats"]["total_queued"] = data["stats"].get("total_queued", 0) + 1
    save_queue(data)
    return True


def get_pending():
    """Get all pending (undelivered) directives."""
    data = load_queue()
    return [e for e in data.get("queue", []) if e.get("status") == "pending"]


def mark_delivered(msg_id):
    """Mark a directive as delivered. Called by self_prompt after typing it."""
    data = load_queue()
    for entry in data.get("queue", []):
        if entry.get("msg_id") == msg_id and entry.get("status") == "pending":
            entry["status"] = "delivered"
            entry["delivered_at"] = datetime.now().isoformat()
            data["stats"]["total_delivered"] = data["stats"].get("total_delivered", 0) + 1
            save_queue(data)
            return True
    return False


def mark_all_delivered():
    """Mark all pending as delivered (drain the queue)."""
    data = load_queue()
    count = 0
    for entry in data.get("queue", []):
        if entry.get("status") == "pending":
            entry["status"] = "delivered"
            entry["delivered_at"] = datetime.now().isoformat()
            count += 1
    data["stats"]["total_delivered"] = data["stats"].get("total_delivered", 0) + count
    save_queue(data)
    return count


def expire_stale():
    """Expire entries older than STALE_HOURS."""
    data = load_queue()
    cutoff = datetime.now() - timedelta(hours=STALE_HOURS)
    count = 0
    for entry in data.get("queue", []):
        queued_str = entry.get("queued_at", "")
        if queued_str and entry.get("status") == "pending":
            try:
                queued_dt = datetime.fromisoformat(queued_str)
                if queued_dt < cutoff:
                    entry["status"] = "expired"
                    count += 1
            except Exception:
                pass
    if count > 0:
        data["stats"]["total_expired"] = data["stats"].get("total_expired", 0) + count
        save_queue(data)
    return count


class OrchPollerDaemon:
    """Polls bus for orchestrator directives and queues them."""

    def __init__(self):
        self.seen_ids = set()
        self._load_seen_ids()

    def _load_seen_ids(self):
        """Pre-populate seen IDs from existing queue to avoid re-queuing."""
        data = load_queue()
        for entry in data.get("queue", []):
            mid = entry.get("msg_id")
            if mid:
                self.seen_ids.add(mid)

    def poll(self):
        """Single poll cycle. Returns count of new directives queued."""
        msgs = _fetch_json(f"{BUS_URL}/bus/messages?limit=30&topic=orchestrator")
        if not msgs or not isinstance(msgs, list):
            return 0

        queued = 0
        for m in msgs:
            msg_type = m.get("type", "")
            msg_id = m.get("id", "")
            sender = m.get("sender", "?")

            # Skip non-actionable types
            if msg_type not in ("directive", "task"):
                continue

            # Skip already seen
            if msg_id in self.seen_ids:
                continue

            self.seen_ids.add(msg_id)

            content = m.get("content", "")
            if not content:
                continue

            # Determine priority: urgent directives from god/system are high
            priority = "normal"
            if "urgent" in msg_type.lower() or "URGENT" in content:
                priority = "urgent"
            if sender in ("god", "system", "god-console"):
                priority = "high"

            ok = enqueue_directive(sender, content, msg_id, msg_type, priority)
            if ok:
                queued += 1
                log(f"Queued [{priority}] from {sender}: {content[:60]}...")

        # Expire stale entries periodically
        expired = expire_stale()
        if expired:
            log(f"Expired {expired} stale entries")

        return queued

    def run(self):
        """Main daemon loop."""
        log("Orchestrator poller daemon starting")
        _post_bus("orchestrator", "monitor_alert",
                  "ORCH_POLLER_ONLINE: Orchestrator bus poller started")

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))

        try:
            while True:
                try:
                    n = self.poll()
                    if n > 0:
                        log(f"Queued {n} new directive(s)")
                except Exception as e:
                    log(f"Poll error: {e}", "ERROR")
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log("Shutting down")
        finally:
            _post_bus("orchestrator", "monitor_alert",
                      "ORCH_POLLER_OFFLINE: Orchestrator bus poller stopped")
            if PID_FILE.exists():
                try:
                    PID_FILE.unlink()
                except Exception:
                    pass


def _check_existing():
    """Check if daemon is already running."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            # Check if process exists
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                kernel32.CloseHandle(h)
                return pid
            else:
                PID_FILE.unlink()
        except Exception:
            pass
    return None


def cmd_status():
    """Show queue status."""
    data = load_queue()
    queue = data.get("queue", [])
    stats = data.get("stats", {})
    pending = [e for e in queue if e.get("status") == "pending"]
    delivered = [e for e in queue if e.get("status") == "delivered"]

    pid = _check_existing()
    daemon_status = f"RUNNING (PID {pid})" if pid else "NOT RUNNING"

    print(f"Orch Poller: {daemon_status}")
    print(f"Queue: {len(queue)} total, {len(pending)} pending, {len(delivered)} delivered")
    print(f"Stats: {json.dumps(stats)}")

    if pending:
        print(f"\nPending directives ({len(pending)}):")
        for e in pending:
            print(f"  [{e.get('priority','?')}] from {e.get('sender','?')} at {e.get('queued_at','?')}: {str(e.get('content',''))[:80]}")

    existing_pid = _check_existing()
    if existing_pid:
        print(f"\nDaemon PID: {existing_pid}")


def main():
    parser = argparse.ArgumentParser(description="Skynet Orchestrator Bus Poller")
    parser.add_argument("command", nargs="?", default="start",
                        choices=["start", "once", "status", "drain", "stop"],
                        help="Command to run")
    args = parser.parse_args()

    if args.command == "status":
        cmd_status()

    elif args.command == "stop":
        pid = _check_existing()
        if pid:
            print(f"Daemon running as PID {pid}. Stop with: Stop-Process -Id {pid}")
        else:
            print("No daemon running")

    elif args.command == "drain":
        n = mark_all_delivered()
        print(f"Drained {n} pending directives")

    elif args.command == "once":
        daemon = OrchPollerDaemon()
        n = daemon.poll()
        print(f"Queued {n} new directive(s)")
        pending = get_pending()
        print(f"Total pending: {len(pending)}")

    elif args.command == "start":
        existing = _check_existing()
        if existing:
            print(f"Already running (PID {existing}). Stop first or use 'once'.")
            sys.exit(1)
        daemon = OrchPollerDaemon()
        daemon.run()


if __name__ == "__main__":
    main()
