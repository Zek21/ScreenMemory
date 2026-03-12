#!/usr/bin/env python3
"""
Skynet Bus Relay -- Holds relayable bus traffic and forwards hourly digest to orchestrator.

Critical rule: relayable worker traffic is no longer ghost-typed directly into
worker chat windows. Instead, relayable messages are held in a Skynet queue and
sent to the orchestrator once per hour as a consolidated digest for action.

Usage:
    python tools/skynet_bus_relay.py              # Start relay daemon
    python tools/skynet_bus_relay.py --once       # Single poll cycle
    python tools/skynet_bus_relay.py --status     # Show relay stats
    python tools/skynet_bus_relay.py --dry-run    # Show what would be queued/sent
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
QUEUE_FILE = DATA_DIR / "bus_relay_queue.json"
WORKERS_FILE = DATA_DIR / "workers.json"
ORCH_FILE = DATA_DIR / "orchestrator.json"

sys.path.insert(0, str(ROOT / "tools"))

SKYNET_URL = "http://localhost:8420"
POLL_INTERVAL = 3.0  # Must be >= 2.0s to prevent CPU spin
MIN_POLL_INTERVAL = 2.0
_consecutive_fetch_failures = 0  # backoff counter for bus fetch errors  # signed: delta
_shutting_down = False  # graceful shutdown flag  # signed: delta
HOLD_INTERVAL_S = 3600
WORKER_NAMES = {"alpha", "beta", "gamma", "delta"}
# Topics that should be delivered to workers
RELAY_TOPICS = {"workers", "convene"} | WORKER_NAMES
# Message types worth delivering (skip heartbeats, daemon_health, etc.)
RELAY_TYPES = {"request", "proposal", "vote", "task", "sub-task", "urgent",
               "urgent-task", "directive", "gate-proposal", "alert"}
MAX_DELIVERED_IDS = 500
MAX_QUEUED_MESSAGES = 500


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


def _load_queue_state():
    state = {
        "messages": [],
        "window_started_at": "",
        "last_digest_at": "",
    }
    if QUEUE_FILE.exists():
        try:
            raw = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state.update(raw)
        except Exception:
            pass
    if not isinstance(state.get("messages"), list):
        state["messages"] = []
    return state


def _save_queue_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state = dict(state or {})
    messages = state.get("messages", [])
    if isinstance(messages, list) and len(messages) > MAX_QUEUED_MESSAGES:
        state["messages"] = messages[-MAX_QUEUED_MESSAGES:]
    QUEUE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _fetch_messages(limit=50):
    global _consecutive_fetch_failures
    try:
        req = urllib.request.Request(
            f"{SKYNET_URL}/bus/messages?limit={limit}", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        _consecutive_fetch_failures = 0  # reset on success  # signed: delta
        return data if isinstance(data, list) else data.get("messages", [])
    except Exception as e:
        _consecutive_fetch_failures += 1
        # Log every 10th failure to avoid log spam during backend downtime  # signed: delta
        if _consecutive_fetch_failures <= 3 or _consecutive_fetch_failures % 10 == 0:
            log(f"Bus fetch failed (attempt {_consecutive_fetch_failures}): {e}", "ERR")
        return []


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _ts(value):
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        return 0.0


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
                    f"To vote GO: from tools.skynet_spam_guard import guarded_publish; "
                    f"guarded_publish({{'sender':'YOUR_NAME','topic':'convene','type':'vote','content':'GO on {session}'}})"
                )  # signed: alpha
        except (json.JSONDecodeError, TypeError):
            pass

    return (
        f"[BUS RELAY] {mtype.upper()} from {sender} (topic={topic}):\n"
        f"{content}\n"
        f"Reply via bus: from tools.skynet_spam_guard import guarded_publish; "
        f"guarded_publish({{'sender':'YOUR_NAME','topic':'{sender}','type':'reply','content':'YOUR_REPLY'}})"
    )  # signed: alpha


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


def _queue_entry(msg):
    return {
        "id": msg.get("id", ""),
        "sender": msg.get("sender", "unknown"),
        "topic": msg.get("topic", ""),
        "type": msg.get("type", ""),
        "content": str(msg.get("content", "")),
        "queued_at": _now_iso(),
    }


def _queue_message(msg, delivered_ids, queue_state, dry_run=False):
    msg_id = msg.get("id", "")
    if not msg_id or msg_id in delivered_ids:
        return 0

    topic = msg.get("topic", "")
    mtype = str(msg.get("type", "")).lower()
    if topic not in RELAY_TOPICS:
        return 0
    if mtype not in RELAY_TYPES:
        delivered_ids.add(msg_id)
        return 0

    targets = _determine_targets(msg)
    if not targets:
        delivered_ids.add(msg_id)
        return 0

    existing_ids = {str(item.get("id", "")) for item in queue_state.get("messages", [])}
    if msg_id in existing_ids:
        delivered_ids.add(msg_id)
        return 0

    if dry_run:
        log(f"[DRY-RUN] Would queue [{msg.get('sender', '?')}] {mtype} from topic={topic}")
        delivered_ids.add(msg_id)
        return 1

    if not queue_state.get("window_started_at"):
        queue_state["window_started_at"] = _now_iso()
    queue_state.setdefault("messages", []).append(_queue_entry(msg))
    delivered_ids.add(msg_id)
    log(f"Queued [{msg.get('sender', '?')}] {mtype} topic={topic} for hourly orchestrator digest")
    return 1


def _format_digest(messages):
    stamp = _now_iso()
    lines = [
        f"[BUS RELAY DIGEST] held_window=60m count={len(messages)} generated_at={stamp}",
        "Relayable worker/convene traffic was held instead of direct worker delivery.",
        "Review these queued messages and take action explicitly.",
        "",
    ]
    for idx, msg in enumerate(messages, start=1):
        content = " ".join(str(msg.get("content", "")).split())
        lines.append(
            f"{idx}. id={msg.get('id', '-')}"
            f" sender={msg.get('sender', 'unknown')}"
            f" topic={msg.get('topic', '')}"
            f" type={msg.get('type', '')}"
            f" queued_at={msg.get('queued_at', '')}"
        )
        lines.append(f"   {content[:260]}")
    return "\n".join(lines)[:4000]


def _bus_post(sender, topic, msg_type, content):
    # NOTE: Raw bus/publish is INTENTIONAL for the relay daemon. This function
    # forwards queued/digested bus messages on behalf of the relay. Using SpamGuard
    # would block forwarded messages as duplicates (same fingerprint as original)
    # and rate-limit relay traffic. The relay IS the transport layer — it must
    # bypass application-level spam filtering.  # signed: delta
    payload = json.dumps({
        "sender": sender,
        "topic": topic,
        "type": msg_type,
        "content": content,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{SKYNET_URL}/bus/publish",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception as e:
        log(f"Bus publish failed: {e}", "ERR")
        return False


def _send_digest_to_orchestrator(messages, dry_run=False):
    digest = _format_digest(messages)
    if dry_run:
        log(f"[DRY-RUN] Would send hourly digest to orchestrator ({len(messages)} queued messages)")
        return {"bus_ok": True, "prompt_ok": True, "count": len(messages)}

    bus_ok = _bus_post("bus_relay", "orchestrator", "bus_relay_digest", digest)
    prompt_ok = False
    try:
        from skynet_delivery import deliver_to_orchestrator
        result = deliver_to_orchestrator(digest, sender="bus_relay", also_bus=False)
        prompt_ok = bool(result.get("success"))
    except Exception as e:
        log(f"Orchestrator direct delivery failed: {e}", "ERR")

    return {"bus_ok": bus_ok, "prompt_ok": prompt_ok, "count": len(messages)}


def _flush_due_queue(queue_state, dry_run=False):
    messages = queue_state.get("messages", [])
    if not messages:
        return 0

    window_started = _ts(queue_state.get("window_started_at", ""))
    if not window_started:
        queue_state["window_started_at"] = messages[0].get("queued_at", _now_iso())
        window_started = _ts(queue_state["window_started_at"])

    elapsed = time.time() - window_started
    if elapsed < HOLD_INTERVAL_S:
        return 0

    result = _send_digest_to_orchestrator(messages, dry_run=dry_run)
    if result.get("bus_ok"):
        log(
            f"Sent hourly bus relay digest to orchestrator "
            f"(count={result.get('count', 0)} prompt_ok={result.get('prompt_ok', False)})",
            "OK",
        )
        queue_state["messages"] = []
        queue_state["window_started_at"] = ""
        queue_state["last_digest_at"] = _now_iso()
        return int(result.get("count", 0))
    return 0


def _process_message(msg, delivered_ids, workers, orch_hwnd, dry_run):
    """Compatibility wrapper -- relayable messages now queue for orchestrator digest."""
    return _queue_message(msg, delivered_ids, workers, dry_run=dry_run)


def poll_and_relay(dry_run=False):
    """Single poll cycle: queue relayable messages and flush hourly digest if due."""
    messages = _fetch_messages(limit=50)
    delivered_ids = _load_delivered()
    queue_state = _load_queue_state()

    new_queued = 0
    for msg in messages:
        new_queued += _process_message(msg, delivered_ids, queue_state, None, dry_run)

    _save_delivered(delivered_ids)
    flushed = _flush_due_queue(queue_state, dry_run=dry_run)
    _save_queue_state(queue_state)
    return new_queued + flushed


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


def _handle_signal(signum, frame):
    """Graceful shutdown on SIGTERM/SIGINT."""
    global _shutting_down
    sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
    log(f"Received {sig_name} -- shutting down gracefully")
    _shutting_down = True
    # signed: delta


def run_daemon():
    """Main relay loop."""
    global _shutting_down, _consecutive_fetch_failures
    effective_interval = max(POLL_INTERVAL, MIN_POLL_INTERVAL)
    log(f"Bus Relay daemon started -- polling every {effective_interval}s")
    log(f"Relay topics: {RELAY_TOPICS}")
    log(f"Relay types: {RELAY_TYPES}")

    # Register signal handlers for graceful shutdown  # signed: delta
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while not _shutting_down:
        try:
            # Backoff: if backend is persistently down, slow polling  # signed: delta
            backoff = min(_consecutive_fetch_failures * 2, 30)
            n = poll_and_relay()
            if n > 0:
                log(f"Delivered {n} messages this cycle")
        except KeyboardInterrupt:
            log("Shutting down")
            break
        except Exception as e:
            log(f"Relay error: {e}", "ERR")
        time.sleep(effective_interval + backoff)


def print_status():
    """Show relay statistics."""
    delivered = _load_delivered()
    queue_state = _load_queue_state()
    queued = queue_state.get("messages", [])
    print(f"\n{'='*50}")
    print(f"  SKYNET BUS RELAY -- Status")
    print(f"{'='*50}")
    print(f"  Relayable IDs tracked: {len(delivered)}")
    print(f"  Queued for orchestrator digest: {len(queued)}")
    print(f"  Window started: {queue_state.get('window_started_at') or 'none'}")
    print(f"  Last digest sent: {queue_state.get('last_digest_at') or 'never'}")
    print(f"  PID file: {'exists' if PID_FILE.exists() else 'none'}")
    print(f"  Relay topics: {RELAY_TOPICS}")
    print(f"  Relay types: {RELAY_TYPES}")
    print(f"  Hold interval: {HOLD_INTERVAL_S}s")
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
