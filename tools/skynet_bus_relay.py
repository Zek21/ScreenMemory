#!/usr/bin/env python3
"""
Skynet Bus Relay -- Delivers bus messages to worker chat windows.

THE MISSING LINK: Workers are VS Code chat windows. They cannot poll the bus
themselves. This daemon bridges the gap: it polls the bus for messages addressed
to workers (topic=workers, topic=<worker_name>, topic=convene) and ghost-types
them into the target worker's chat window via the dispatch system.

Without this daemon, inter-worker communication via the bus is one-way:
workers can POST to the bus, but never RECEIVE messages from it.

Usage:
    python tools/skynet_bus_relay.py              # Start relay daemon
    python tools/skynet_bus_relay.py --once       # Single poll cycle
    python tools/skynet_bus_relay.py --status     # Show relay stats
    python tools/skynet_bus_relay.py --dry-run    # Show what would be delivered
"""

import atexit
import ctypes
import json
import os
import signal
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PID_FILE = DATA_DIR / "bus_relay.pid"
DELIVERED_FILE = DATA_DIR / "bus_relay_delivered.json"
WORKERS_FILE = DATA_DIR / "workers.json"
ORCH_FILE = DATA_DIR / "orchestrator.json"

sys.path.insert(0, str(ROOT / "tools"))

SKYNET_URL = "http://localhost:8420"
POLL_INTERVAL = 3.0  # Must be >= 2.0s to prevent CPU spin
MIN_POLL_INTERVAL = 2.0
WORKER_NAMES = {"alpha", "beta", "gamma", "delta"}
# Topics that should be delivered to workers
RELAY_TOPICS = {"workers", "convene"} | WORKER_NAMES
# Message types worth delivering (skip heartbeats, daemon_health, etc.)
RELAY_TYPES = {"request", "proposal", "vote", "task", "sub-task", "urgent",
               "urgent-task", "directive", "gate-proposal", "alert"}
MAX_DELIVERED_IDS = 500


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def _load_workers():
    if not WORKERS_FILE.exists():
        return {}
    data = json.loads(WORKERS_FILE.read_text())
    return {w["name"]: w for w in data.get("workers", [])}


def _load_orch_hwnd():
    if ORCH_FILE.exists():
        data = json.loads(ORCH_FILE.read_text())
        return data.get("orchestrator_hwnd")
    return None


def _load_delivered():
    if DELIVERED_FILE.exists():
        try:
            return set(json.loads(DELIVERED_FILE.read_text()))
        except Exception:
            pass
    return set()


def _save_delivered(ids):
    trimmed = sorted(ids)[-MAX_DELIVERED_IDS:]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DELIVERED_FILE.write_text(json.dumps(trimmed))


def _fetch_messages(limit=50):
    try:
        req = urllib.request.Request(
            f"{SKYNET_URL}/bus/messages?limit={limit}", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        return data if isinstance(data, list) else data.get("messages", [])
    except Exception as e:
        log(f"Bus fetch failed: {e}", "ERR")
        return []


def _deliver_to_worker(worker_name, text, workers, orch_hwnd):
    """Ghost-type a message into a worker's chat window."""
    worker = workers.get(worker_name)
    if not worker:
        log(f"Worker {worker_name} not in workers.json", "WARN")
        return False

    hwnd = worker.get("hwnd")
    if not hwnd:
        log(f"No HWND for {worker_name}", "WARN")
        return False

    try:
        from skynet_dispatch import ghost_type_to_worker
        ok = ghost_type_to_worker(hwnd, text, orch_hwnd)
        if ok:
            log(f"Delivered to {worker_name.upper()}", "OK")
        else:
            log(f"Ghost-type failed for {worker_name.upper()}", "ERR")
        return ok
    except Exception as e:
        log(f"Delivery error for {worker_name}: {e}", "ERR")
        return False


def _format_bus_message(msg):
    """Format a bus message into a concise prompt for a worker."""
    sender = msg.get("sender", "unknown")
    mtype = msg.get("type", "message")
    topic = msg.get("topic", "")
    content = msg.get("content", "")

    # Try to parse JSON content for structured messages
    if isinstance(content, str) and content.startswith("{"):
        try:
            parsed = json.loads(content)
            if "session" in parsed:
                session = parsed.get("session", "")
                question = parsed.get("question", "")
                vote_req = parsed.get("vote_request", "")
                return (
                    f"[BUS RELAY] Convene session '{session}' from {sender}:\n"
                    f"{question}\n"
                    f"{vote_req}\n"
                    f"To vote GO: import requests; requests.post('http://localhost:8420/bus/publish', "
                    f"json={{'sender':'YOUR_NAME','topic':'convene','type':'vote','content':'GO on {session}'}})"
                )
        except (json.JSONDecodeError, TypeError):
            pass

    return (
        f"[BUS RELAY] {mtype.upper()} from {sender} (topic={topic}):\n"
        f"{content}\n"
        f"Reply via bus: import requests; requests.post('http://localhost:8420/bus/publish', "
        f"json={{'sender':'YOUR_NAME','topic':'{sender}','type':'reply','content':'YOUR_REPLY'}})"
    )


def _determine_targets(msg):
    """Determine which workers should receive this message."""
    topic = msg.get("topic", "")
    sender = msg.get("sender", "")

    # Targeted to specific worker
    if topic in WORKER_NAMES:
        return {topic} - {sender}

    # Broadcast to all workers
    if topic in ("workers", "convene"):
        return WORKER_NAMES - {sender}

    return set()


def poll_and_relay(dry_run=False):
    """Single poll cycle: fetch bus messages and relay to worker windows."""
    messages = _fetch_messages(limit=50)
    delivered_ids = _load_delivered()
    workers = _load_workers()
    orch_hwnd = _load_orch_hwnd()

    if not workers:
        log("No workers.json found -- cannot deliver", "ERR")
        return 0

    new_deliveries = 0
    cooldown = 2.0  # seconds between deliveries to avoid clipboard corruption

    for msg in messages:
        msg_id = msg.get("id", "")
        if not msg_id or msg_id in delivered_ids:
            continue

        topic = msg.get("topic", "")
        mtype = msg.get("type", "").lower()

        # Only relay relevant topics and types
        if topic not in RELAY_TOPICS:
            continue
        if mtype not in RELAY_TYPES:
            delivered_ids.add(msg_id)  # mark as seen but don't deliver
            continue

        targets = _determine_targets(msg)
        if not targets:
            delivered_ids.add(msg_id)
            continue

        formatted = _format_bus_message(msg)
        sender = msg.get("sender", "?")

        if dry_run:
            log(f"[DRY-RUN] Would deliver to {targets}: [{sender}] {mtype} ({formatted[:80]}...)")
            delivered_ids.add(msg_id)
            new_deliveries += 1
            continue

        delivered_any = False
        for target in targets:
            if target not in workers:
                continue
            log(f"Relaying [{sender}] {mtype} -> {target.upper()}")
            ok = _deliver_to_worker(target, formatted, workers, orch_hwnd)
            if ok:
                delivered_any = True
                new_deliveries += 1
                time.sleep(cooldown)  # clipboard cooldown

        if delivered_any:
            delivered_ids.add(msg_id)

    _save_delivered(delivered_ids)
    return new_deliveries


def _pid_is_bus_relay(pid: int) -> bool:
    """Check if a PID is actually a bus_relay process (not a recycled PID)."""
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = ctypes.wintypes.DWORD(260)
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size))
            if ok and "python" in buf.value.lower():
                return True
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass
    # Fallback: just check if process exists
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_singleton():
    """Acquire PID file lock. Returns True if we're the sole instance."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            if _pid_is_bus_relay(old_pid):
                log(f"Bus Relay already running (PID {old_pid}) -- exiting", "WARN")
                return False
            else:
                log(f"Stale PID file (PID {old_pid} dead) -- taking over", "INFO")
        except (ValueError, OSError):
            log("Corrupt PID file -- overwriting", "WARN")
    PID_FILE.write_text(str(os.getpid()))
    atexit.register(_release_singleton)
    return True


def _release_singleton():
    """Clean up PID file on exit."""
    try:
        if PID_FILE.exists():
            stored = int(PID_FILE.read_text().strip())
            if stored == os.getpid():
                PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def run_daemon():
    """Main relay loop."""
    effective_interval = max(POLL_INTERVAL, MIN_POLL_INTERVAL)
    log(f"Bus Relay daemon started -- polling every {effective_interval}s")
    log(f"Relay topics: {RELAY_TOPICS}")
    log(f"Relay types: {RELAY_TYPES}")

    while True:
        try:
            n = poll_and_relay()
            if n > 0:
                log(f"Delivered {n} messages this cycle")
        except KeyboardInterrupt:
            log("Shutting down")
            break
        except Exception as e:
            log(f"Relay error: {e}", "ERR")
        time.sleep(effective_interval)


def print_status():
    """Show relay statistics."""
    delivered = _load_delivered()
    workers = _load_workers()
    print(f"\n{'='*50}")
    print(f"  SKYNET BUS RELAY -- Status")
    print(f"{'='*50}")
    print(f"  Messages delivered (tracked IDs): {len(delivered)}")
    print(f"  Workers known: {', '.join(workers.keys()) if workers else 'NONE'}")
    print(f"  PID file: {'exists' if PID_FILE.exists() else 'none'}")
    print(f"  Relay topics: {RELAY_TOPICS}")
    print(f"  Relay types: {RELAY_TYPES}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Skynet Bus Relay Daemon")
    parser.add_argument("--once", action="store_true", help="Single poll cycle")
    parser.add_argument("--status", action="store_true", help="Print stats and exit")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be delivered")
    args = parser.parse_args()

    if args.status:
        print_status()
        sys.exit(0)

    if args.once or args.dry_run:
        n = poll_and_relay(dry_run=args.dry_run)
        log(f"{'Dry-run' if args.dry_run else 'Delivered'}: {n} messages")
        sys.exit(0)

    # Singleton enforcement
    if not _acquire_singleton():
        sys.exit(0)

    try:
        run_daemon()
    finally:
        _release_singleton()
