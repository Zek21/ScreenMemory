#!/usr/bin/env python3
"""
orch_realtime.py -- Orchestrator Real-Time Interface for Skynet.
Provides INSTANT access to system state via data/realtime.json
(written by skynet_realtime.py daemon) with HTTP fallback.

All read operations are zero-network when realtime.json is fresh.
wait() and wait_all() poll the local file at 0.5s resolution, not HTTP.

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# QUICK REFERENCE -- 5 most common orchestrator commands:
#
#   python tools/orch_realtime.py status                          # Worker state table (instant, zero-network)
#   python tools/orch_realtime.py pending                         # Unread results/alerts
#   python tools/orch_realtime.py dispatch-wait --worker NAME --task "task" --timeout 90   # Dispatch + block for result
#   python tools/orch_realtime.py dispatch-parallel-wait --task "task" --timeout 120       # All workers + wait
#   python tools/orch_realtime.py wait-all [--timeout 120] [--non-blocking]               # Wait or snapshot
#
# NEVER use Start-Sleep or Invoke-RestMethod polling loops.
# These commands handle all waiting internally at 0.5s file-poll resolution.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Usage:
    python orch_realtime.py status
    python orch_realtime.py pending
    python orch_realtime.py consume MSG_ID
    python orch_realtime.py consume-all
    python orch_realtime.py wait KEY [--timeout 90]
    python orch_realtime.py wait-all [--timeout 120] [--non-blocking]
    python orch_realtime.py health
    python orch_realtime.py bus [--limit 10]
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REALTIME_FILE = DATA / "realtime.json"
CONSUMED_FILE = DATA / "realtime_consumed.json"
SKYNET = "http://localhost:8420"

# ANSI colors
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_RED = "\033[91m"
C_GREEN = "\033[92m"
C_GOLD = "\033[93m"
C_BLUE = "\033[94m"
C_PURPLE = "\033[95m"
C_CYAN = "\033[96m"


# ── State Reading ──────────────────────────────────────────────────

def _read_realtime() -> dict:
    """Read state from data/realtime.json. Falls back to HTTP if stale/missing."""
    if REALTIME_FILE.exists():
        try:
            data = json.loads(REALTIME_FILE.read_text(encoding="utf-8"))
            # Daemon uses 'last_update', normalize to 'timestamp'
            ts = data.get("timestamp") or data.get("last_update") or 0
            if isinstance(ts, str):
                from datetime import datetime
                try:
                    ts = datetime.fromisoformat(ts).timestamp()
                except Exception:
                    ts = 0
            age = time.time() - ts
            if age < 10:
                data["timestamp"] = ts
                data["_source"] = "realtime.json"
                data["_age"] = round(age, 1)
                # Normalize: daemon writes 'workers', HTTP writes 'agents'
                if "workers" in data and "agents" not in data:
                    data["agents"] = data["workers"]
                if "bus_recent" in data and "bus" not in data:
                    data["bus"] = data["bus_recent"]
                return data
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            pass  # signed: alpha — handles null/non-dict JSON gracefully

    # Fallback: fetch from Skynet HTTP
    try:
        import urllib.request
        status_req = urllib.request.urlopen(f"{SKYNET}/status", timeout=3)
        status = json.loads(status_req.read())
        bus_req = urllib.request.urlopen(f"{SKYNET}/bus/messages?limit=50", timeout=3)
        bus = json.loads(bus_req.read())
        return {
            "agents": status.get("agents", {}),
            "bus": bus if isinstance(bus, list) else status.get("bus", []),
            "uptime_s": status.get("uptime_s", 0),
            "orch_thinking": status.get("orch_thinking", []),
            "timestamp": time.time(),
            "_source": "http_fallback",
        }
    except Exception as e:
        print(f"[orch_realtime] Skynet HTTP fallback failed: {e}", file=sys.stderr)  # signed: beta
        return {"agents": {}, "bus": [], "uptime_s": 0, "timestamp": 0, "_source": "unavailable"}


def _read_consumed() -> set:
    """Read set of consumed message IDs."""
    if CONSUMED_FILE.exists():
        try:
            data = json.loads(CONSUMED_FILE.read_text(encoding="utf-8"))
            return set(data.get("consumed", []))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def _write_consumed(consumed: set):
    """Write consumed message IDs to file atomically (rename-based)."""  # signed: beta
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = CONSUMED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "consumed": list(consumed),
        "updated_at": time.time(),
    }, indent=2), encoding="utf-8")
    tmp.replace(CONSUMED_FILE)  # atomic on Windows NTFS  # signed: beta


# ── Core Functions ─────────────────────────────────────────────────

def status():
    """Print formatted table of all workers: name, status, model, heartbeat, tasks."""
    state = _read_realtime()
    agents = state.get("agents", {})
    source = state.get("_source", "realtime.json")
    age = time.time() - state.get("timestamp", 0)

    print(f"{C_GOLD}{C_BOLD}SKYNET STATUS{C_RESET} {C_DIM}(source: {source}, age: {age:.1f}s){C_RESET}")
    print(f"{'NAME':<14} {'STATUS':<10} {'MODEL':<12} {'HEARTBEAT':<12} {'TASKS':<6} {'ERRORS':<6} {'AVG_MS':<8} {'QUEUE':<5}")
    print(f"{'-'*14} {'-'*10} {'-'*12} {'-'*12} {'-'*6} {'-'*6} {'-'*8} {'-'*5}")

    for name in sorted(agents.keys()):
        a = agents[name]
        st = a.get("status", "?")
        if st == "IDLE":
            sc = C_GREEN
        elif st == "WORKING":
            sc = C_GOLD
        elif st in ("ERROR", "OFFLINE"):
            sc = C_RED
        else:
            sc = C_CYAN

        model = a.get("model", "?")
        hb = a.get("last_heartbeat", "-")
        tasks = a.get("tasks_completed", 0)
        errors = a.get("total_errors", 0)
        avg = f"{a.get('avg_task_ms', 0):.0f}" if a.get("avg_task_ms") else "-"
        q = a.get("queue_depth", 0)

        print(f"{C_BOLD}{name:<14}{C_RESET} {sc}{st:<10}{C_RESET} {model:<12} {hb:<12} {tasks:<6} {errors:<6} {avg:<8} {q:<5}")

    task = None
    for name in sorted(agents.keys()):
        a = agents[name]
        if a.get("current_task"):
            if task is None:
                print(f"\n{C_GOLD}Active Tasks:{C_RESET}")
                task = True
            print(f"  {C_BOLD}{name}{C_RESET}: {a['current_task']}")


def pending():
    """Print pending results and alerts not yet consumed."""
    state = _read_realtime()
    consumed = _read_consumed()
    bus = state.get("bus", [])

    results = [m for m in bus if m.get("id") not in consumed
               and m.get("type") in ("result", "alert", "error")]

    if not results:
        print(f"{C_DIM}No pending results or alerts.{C_RESET}")
        return

    print(f"{C_GOLD}{C_BOLD}PENDING ({len(results)}){C_RESET}")
    for m in results:
        mtype = m.get("type", "?")
        if mtype == "result":
            tc = C_GREEN
        elif mtype == "alert":
            tc = C_RED
        else:
            tc = C_GOLD

        sender = m.get("sender", "?")
        content = (m.get("content", ""))[:100]
        mid = m.get("id", "?")
        print(f"  {tc}{mtype.upper():<7}{C_RESET} {C_BOLD}{sender:<12}{C_RESET} {content} {C_DIM}[{mid}]{C_RESET}")


def consume(msg_id: str) -> bool:
    """Mark a message as consumed."""
    consumed = _read_consumed()
    consumed.add(msg_id)
    _write_consumed(consumed)
    print(f"{C_GREEN}Consumed: {msg_id}{C_RESET}")
    return True


def consume_all() -> list:
    """Mark all current pending as consumed. Returns the list of consumed IDs."""
    state = _read_realtime()
    consumed = _read_consumed()
    bus = state.get("bus", [])

    new_ids = []
    for m in bus:
        mid = m.get("id", "")
        if mid and mid not in consumed:
            if m.get("type") in ("result", "alert", "error"):
                consumed.add(mid)
                new_ids.append(mid)

    _write_consumed(consumed)
    if new_ids:
        print(f"{C_GREEN}Consumed {len(new_ids)} messages.{C_RESET}")
    else:
        print(f"{C_DIM}Nothing to consume.{C_RESET}")
    return new_ids


def wait(key: str, timeout: float = 600) -> dict | None:
    """Block until a result matching key appears. Polls FILE every 2s."""
    consumed = _read_consumed()
    deadline = time.time() + timeout
    print(f"{C_CYAN}Waiting for result matching '{key}' (timeout {timeout}s)...{C_RESET}")

    while time.time() < deadline:
        state = _read_realtime()
        bus = state.get("bus", [])

        for m in bus:
            mid = m.get("id", "")
            if mid in consumed:
                continue
            if m.get("type") not in ("result",):
                continue
            sender = (m.get("sender") or "").lower()
            content = (m.get("content") or "").lower()
            topic = (m.get("topic") or "").lower()
            kl = key.lower()

            if kl in sender or kl in content or kl in topic:
                print(f"{C_GREEN}Match found: [{m.get('sender')}] {(m.get('content', ''))[:80]}{C_RESET}")
                # Mark dispatch as received in dispatch_log.json  # signed: alpha
                if sender:
                    try:
                        from tools.skynet_dispatch import mark_dispatch_received
                        mark_dispatch_received(sender)  # signed: alpha
                    except Exception:
                        pass
                return m

        time.sleep(2.0)

    # Bus HTTP fallback: if realtime.json had no match, try live bus  # signed: delta
    try:
        import urllib.request
        import urllib.error  # signed: beta
        r = urllib.request.urlopen(f"{SKYNET}/bus/messages?limit=20", timeout=3)
        fallback_msgs = json.loads(r.read())
        if isinstance(fallback_msgs, list):
            kl = key.lower()
            for m in fallback_msgs:
                mid = m.get("id", "")
                if mid in consumed:
                    continue
                if m.get("type") != "result":
                    continue
                sender = (m.get("sender") or "").lower()
                content = (m.get("content") or "").lower()
                topic = (m.get("topic") or "").lower()
                if kl in sender or kl in content or kl in topic:
                    print(f"{C_GREEN}Match found (bus fallback): [{m.get('sender')}] {(m.get('content', ''))[:80]}{C_RESET}")
                    if sender:
                        try:
                            from tools.skynet_dispatch import mark_dispatch_received
                            mark_dispatch_received(sender)
                        except Exception:
                            pass
                    return m
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as e:
        print(f"{C_DIM}[DEBUG] Bus fallback failed: {type(e).__name__}: {e}{C_RESET}", file=sys.stderr)  # signed: beta
    except json.JSONDecodeError as e:
        print(f"{C_DIM}[DEBUG] Bus response not valid JSON: {e}{C_RESET}", file=sys.stderr)  # signed: beta

    print(f"{C_RED}Timeout waiting for '{key}'.{C_RESET}")
    return None


def _scan_bus_for_results(workers: list, consumed: set, results: dict):
    """Scan current bus state for unconsumed result messages from target workers."""
    state = _read_realtime()
    bus = state.get("bus", [])
    newly_found = []
    for m in bus:
        mid = m.get("id", "")
        if mid in consumed or m.get("type") != "result":
            continue
        sender = (m.get("sender") or "").lower()
        if sender in workers and sender not in results:
            results[sender] = m
            newly_found.append(sender)
            # Mark dispatch as received in dispatch_log.json  # signed: alpha
            try:
                from tools.skynet_dispatch import mark_dispatch_received
                mark_dispatch_received(sender)  # signed: alpha
            except Exception:
                pass
    return newly_found


def wait_all(workers: list | None = None, timeout: float = 600, non_blocking: bool = False) -> tuple[dict, bool]:
    """Block until ALL specified workers have posted results since last consume_all.

    Returns:
        (results_dict, all_complete) -- results_dict maps worker name to message,
        all_complete is True only if every requested worker responded before timeout.
    """  # signed: beta
    if workers is None:
        workers = ["alpha", "beta", "gamma", "delta"]

    consumed = _read_consumed()
    results = {}

    if non_blocking:
        _scan_bus_for_results(workers, consumed, results)
        found = [w for w in workers if w in results]
        missing = [w for w in workers if w not in results]
        if found:
            print(f"{C_GREEN}Found results from: {', '.join(found)}{C_RESET}")
            for w in found:
                print(f"  {C_GREEN}{w.upper()}{C_RESET}: {(results[w].get('content', ''))[:60]}")
        if missing:
            print(f"{C_GOLD}Still waiting: {', '.join(missing)}{C_RESET}")
        return results, len(missing) == 0

    return _poll_until_all(workers, consumed, results, timeout)


def _poll_until_all(workers: list, consumed: set, results: dict, timeout: float) -> tuple[dict, bool]:
    """Poll bus until all workers respond or timeout.

    Returns:
        (results_dict, all_complete) -- all_complete is False on timeout with missing workers.
    """  # signed: beta
    deadline = time.time() + timeout
    print(f"{C_CYAN}Waiting for results from: {', '.join(workers)} (timeout {timeout}s)...{C_RESET}")

    while time.time() < deadline:
        newly_found = _scan_bus_for_results(workers, consumed, results)
        for s in newly_found:
            print(f"  {C_GREEN}{s.upper()}{C_RESET}: {(results[s].get('content', ''))[:60]}")

        if all(w in results for w in workers):
            print(f"{C_GREEN}All {len(workers)} workers responded.{C_RESET}")
            return results, True
        time.sleep(2.0)

    missing = [w for w in workers if w not in results]
    print(f"{C_RED}Timeout. Missing: {', '.join(missing)}{C_RESET}")
    return results, False


def health():
    """Print system health: uptime, latency, bus depth, data source."""
    state = _read_realtime()
    agents = state.get("agents", {})
    bus = state.get("bus", [])
    uptime = state.get("uptime_s", 0)
    source = state.get("_source", "realtime.json")
    age = time.time() - state.get("timestamp", 0)

    online = sum(1 for a in agents.values() if a.get("status") in ("IDLE", "WORKING"))
    working = sum(1 for a in agents.values() if a.get("status") == "WORKING")
    total = len(agents)
    total_tasks = sum(a.get("tasks_completed", 0) for a in agents.values())
    total_errors = sum(a.get("total_errors", 0) for a in agents.values())

    # Uptime formatting
    h, rem = divmod(int(uptime), 3600)
    m, s = divmod(rem, 60)
    uptime_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

    print(f"{C_GOLD}{C_BOLD}SKYNET HEALTH{C_RESET}")
    print(f"  Uptime:      {C_BOLD}{uptime_str}{C_RESET}")
    print(f"  Data source: {source} (age: {age:.1f}s)")
    print(f"  Workers:     {C_GREEN}{online}/{total} online{C_RESET}, {C_GOLD}{working} working{C_RESET}")
    print(f"  Tasks:       {total_tasks} completed, {C_RED}{total_errors} errors{C_RESET}")
    print(f"  Bus depth:   {len(bus)} messages")

    # Fetch latency from /metrics if available
    try:
        import urllib.request
        r = urllib.request.urlopen(f"{SKYNET}/metrics", timeout=2)
        metrics = json.loads(r.read())
        lat = metrics.get("avg_latency_us", 0)
        mem = metrics.get("mem_alloc_mb", 0)
        if lat:
            print(f"  Latency:     {lat/1000:.1f}ms (server avg)")
        if mem:
            print(f"  Memory:      {mem:.1f}MB")
    except Exception:
        pass


def bus_messages(n: int = 10):
    """Print last N bus messages."""
    state = _read_realtime()
    bus = state.get("bus", [])
    consumed = _read_consumed()
    msgs = bus[-n:] if len(bus) > n else bus

    if not msgs:
        print(f"{C_DIM}No bus messages.{C_RESET}")
        return

    print(f"{C_GOLD}{C_BOLD}BUS MESSAGES (last {len(msgs)}){C_RESET}")
    for m in msgs:
        mid = m.get("id", "?")
        sender = m.get("sender", "?")
        topic = m.get("topic", "?")
        mtype = m.get("type", "?")
        content = (m.get("content", ""))[:80]
        is_consumed = mid in consumed

        if mtype == "result":
            tc = C_GREEN
        elif mtype == "alert":
            tc = C_RED
        elif mtype == "directive":
            tc = C_GOLD
        else:
            tc = C_DIM

        consumed_mark = f" {C_DIM}[consumed]{C_RESET}" if is_consumed else ""
        print(f"  {tc}{mtype:<9}{C_RESET} {C_BOLD}{sender:<12}{C_RESET} -> {topic:<12} {content}{consumed_mark}")


# ── Dispatch Integration ──────────────────────────────────────────

def dispatch_and_wait(worker: str, task: str, timeout: float = 600) -> dict | None:
    """Dispatch to a worker and wait for result.
    1. consume_all() to clear old results
    2. Dispatch via skynet_dispatch.py CLI
    3. wait(worker, timeout)
    4. Return the result message
    """
    consume_all()

    # Dispatch via subprocess (avoids import complexity with UIA singletons)
    cmd = [sys.executable, str(ROOT / "tools" / "skynet_dispatch.py"),
           "--worker", worker, "--task", task]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                              cwd=str(ROOT), encoding='utf-8', errors='replace')  # signed: delta
        if proc.returncode != 0:
            print(f"{C_RED}Dispatch failed: {proc.stderr[:200]}{C_RESET}")
            return None
    except subprocess.TimeoutExpired:
        print(f"{C_RED}Dispatch timed out.{C_RESET}")
        return None

    return wait(worker, timeout)


def dispatch_parallel_and_wait(task: str, timeout: float = 600) -> tuple[dict, bool]:
    """Dispatch to all workers in parallel and wait for all results.
    1. consume_all()
    2. Dispatch via --parallel
    3. wait_all(timeout)
    4. Return (dict of worker -> result, all_complete bool)
    """  # signed: beta
    consume_all()

    cmd = [sys.executable, str(ROOT / "tools" / "skynet_dispatch.py"),
           "--parallel", "--task", task]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                              cwd=str(ROOT), encoding='utf-8', errors='replace')  # signed: delta
        if proc.returncode != 0:
            print(f"{C_RED}Parallel dispatch failed: {proc.stderr[:200]}{C_RESET}")
            return {}, False
    except subprocess.TimeoutExpired:
        print(f"{C_RED}Parallel dispatch timed out.{C_RESET}")
        return {}, False

    return wait_all(timeout=timeout)


# ── CLI ───────────────────────────────────────────────────────────

def _build_realtime_parser():
    """Build argparse parser with all subcommands."""
    parser = argparse.ArgumentParser(description="Orchestrator Real-Time Interface")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Worker status table")
    sub.add_parser("pending", help="Pending results/alerts")

    p_consume = sub.add_parser("consume", help="Consume a message by ID")
    p_consume.add_argument("msg_id", help="Message ID to consume")

    sub.add_parser("consume-all", help="Consume all pending")

    p_wait = sub.add_parser("wait", help="Wait for a result matching key")
    p_wait.add_argument("key", help="Key to match (worker name, keyword)")
    p_wait.add_argument("--timeout", type=float, default=600)

    p_wa = sub.add_parser("wait-all", help="Wait for all workers to respond")
    p_wa.add_argument("--timeout", type=float, default=600)
    p_wa.add_argument("--workers", nargs="*", default=None)
    p_wa.add_argument("--non-blocking", action="store_true",
                       help="Return immediately with current state instead of blocking")

    sub.add_parser("health", help="System health overview")

    p_bus = sub.add_parser("bus", help="Recent bus messages")
    p_bus.add_argument("--limit", type=int, default=10)

    p_dw = sub.add_parser("dispatch-wait", help="Dispatch to worker and wait")
    p_dw.add_argument("--worker", required=True)
    p_dw.add_argument("--task", required=True)
    p_dw.add_argument("--timeout", type=float, default=600)

    p_dpw = sub.add_parser("dispatch-parallel-wait", help="Dispatch to all and wait")
    p_dpw.add_argument("--task", required=True)
    p_dpw.add_argument("--timeout", type=float, default=600)

    return parser


_COMMAND_MAP = {
    "status": lambda a: status(),
    "pending": lambda a: pending(),
    "consume": lambda a: consume(a.msg_id),
    "consume-all": lambda a: consume_all(),
    "wait": lambda a: wait(a.key, a.timeout),
    "wait-all": lambda a: wait_all(a.workers, a.timeout, non_blocking=a.non_blocking),
    "health": lambda a: health(),
    "bus": lambda a: bus_messages(a.limit),
    "dispatch-wait": lambda a: dispatch_and_wait(a.worker, a.task, a.timeout),
    "dispatch-parallel-wait": lambda a: dispatch_parallel_and_wait(a.task, a.timeout),
}


def main():
    parser = _build_realtime_parser()
    args = parser.parse_args()
    handler = _COMMAND_MAP.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
