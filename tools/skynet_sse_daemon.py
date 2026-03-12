#!/usr/bin/env python3
"""
skynet_sse_daemon.py -- Real-Time SSE Event Loop daemon for Skynet.

SSE-subscribes to http://localhost:8420/stream, parses every 1-second tick,
writes live state to data/realtime.json atomically. The orchestrator reads
this file INSTANTLY instead of sleep-polling.

Complements skynet_realtime.py (UIA-based) with network-based SSE streaming.

Usage:
    python tools/skynet_sse_daemon.py                     # default
    python tools/skynet_sse_daemon.py --port 8420         # custom port
    python tools/skynet_sse_daemon.py --output path.json  # custom output
    python tools/skynet_sse_daemon.py --verbose           # print every tick
    python tools/skynet_sse_daemon.py --read              # just read current state
"""

import argparse
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_FILE = DATA_DIR / "realtime.json"
CONSUMED_FILE = DATA_DIR / "realtime_consumed.json"
PID_FILE = DATA_DIR / "sse_daemon.pid"

_lock = threading.Lock()


# ─── Atomic File Write ─────────────────────────────────

def _atomic_write(path: Path, data):
    """Write JSON atomically: write .tmp then rename."""
    tmp = path.with_suffix(".tmp")
    content = json.dumps(data, indent=2, default=str) if isinstance(data, dict) else json.dumps(data, default=str)
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(path))


# ─── Consumed IDs Management ──────────────────────────

def _load_consumed() -> set:
    try:
        if CONSUMED_FILE.exists():
            data = json.loads(CONSUMED_FILE.read_text(encoding="utf-8"))
            return set(data) if isinstance(data, list) else set()
    except Exception:
        pass
    return set()


def _save_consumed(ids: set):
    _atomic_write(CONSUMED_FILE, list(ids))


def consume_message(msg_id: str):
    """Mark a message as consumed by the orchestrator."""
    with _lock:
        consumed = _load_consumed()
        consumed.add(msg_id)
        if len(consumed) > 500:
            consumed = set(sorted(consumed)[-300:])
        _save_consumed(consumed)


# ─── Reader Functions (importable, INSTANT, no network) ─

def read_state() -> dict:
    """Read state file and return parsed dict. INSTANT, no network."""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def get_pending_results() -> list:
    """Return pending_results from state file."""
    return read_state().get("pending_results", [])


def get_pending_alerts() -> list:
    """Return pending_alerts from state file."""
    return read_state().get("pending_alerts", [])


def worker_states() -> dict:
    """Return just the workers dict from state."""
    return read_state().get("workers", {})


def is_worker_idle(name: str) -> bool:
    """Check if a worker is IDLE from the state file."""
    w = worker_states().get(name, {})
    return w.get("status", "").upper() == "IDLE" if w else False


def wait_for_result(key: str, timeout: float = 90.0) -> Optional[dict]:
    """Poll state file every 0.5s until a result matching key appears.

    Searches pending_results for messages whose 'content' contains the key.
    Returns the matching message dict, or None on timeout.
    No network calls -- file reads only.
    """
    deadline = time.monotonic() + timeout
    seen = set()

    while time.monotonic() < deadline:
        for r in get_pending_results():
            msg_id = r.get("id", "")
            content = r.get("content", "")
            if isinstance(content, str) and key in content and msg_id not in seen:
                if msg_id:
                    consume_message(msg_id)
                return r
            seen.add(msg_id)
        time.sleep(0.5)

    return None


# ─── SSE Parser ────────────────────────────────────────

def _sse_connect(host: str, port: int):
    """Connect to SSE stream, return HTTPConnection and response."""
    conn = HTTPConnection(host, port, timeout=10)
    conn.request("GET", "/stream", headers={
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
    })
    resp = conn.getresponse()
    if resp.status != 200:
        raise ConnectionError(f"SSE returned HTTP {resp.status}")
    return conn, resp


def _parse_sse_line(line: str) -> Optional[dict]:
    """Parse a single SSE 'data: {...}' line into a dict."""
    line = line.strip()
    if not line.startswith("data: "):
        return None
    try:
        return json.loads(line[6:])
    except json.JSONDecodeError:
        return None


# ─── State Builder ─────────────────────────────────────

def _build_state(sse_data: dict, update_count: int, last_tick_time: float) -> dict:
    """Build the state dict from an SSE tick payload."""
    now = time.time()
    latency_ms = round((now - last_tick_time) * 1000, 1) if last_tick_time > 0 else 0

    agents = sse_data.get("agents", {})
    workers = {}
    for name, info in agents.items():
        workers[name] = {
            "status": info.get("status", "UNKNOWN"),
            "model": info.get("model", "unknown"),
            "tasks_completed": info.get("tasks_completed", 0),
            "total_errors": info.get("total_errors", 0),
            "current_task": info.get("current_task", ""),
            "progress": info.get("progress", 0),
            "last_heartbeat": info.get("last_heartbeat", ""),
            "avg_task_ms": info.get("avg_task_ms", 0),
            "queue_depth": info.get("queue_depth", 0),
            "circuit_state": info.get("circuit_state", ""),
            "consecutive_fails": info.get("consecutive_fails", 0),
            "uptime_s": info.get("uptime_s", 0),
        }

    bus = sse_data.get("bus", [])
    bus_recent = bus[-10:] if isinstance(bus, list) else []

    consumed = _load_consumed()

    pending_results = []
    pending_alerts = []
    for msg in (bus if isinstance(bus, list) else []):
        msg_id = msg.get("id", "")
        if msg_id in consumed:
            continue
        msg_type = msg.get("type", "")
        msg_topic = msg.get("topic", "")
        if msg_type == "result" and msg_topic == "orchestrator":
            pending_results.append(msg)
        elif msg_type == "alert":
            pending_alerts.append(msg)

    return {
        "workers": workers,
        "bus_recent": bus_recent,
        "bus_depth": sse_data.get("bus_depth", 0),
        "pending_results": pending_results,
        "pending_alerts": pending_alerts,
        "tasks_dispatched": sse_data.get("tasks_dispatched", 0),
        "tasks_completed": sse_data.get("tasks_completed", 0),
        "tasks_failed": sse_data.get("tasks_failed", 0),
        "uptime_s": round(sse_data.get("uptime_s", 0), 1),
        "orch_thinking": sse_data.get("orch_thinking", [])[-5:],
        "last_update": datetime.now(timezone.utc).isoformat(),
        "update_count": update_count,
        "latency_ms": latency_ms,
    }


def _init_pid_guard(pid_file: Path) -> bool:
    """Check for existing daemon instance via shared atomic PID guard."""
    from tools.skynet_pid_guard import acquire_pid_guard
    return acquire_pid_guard(pid_file, "skynet_sse_daemon")
    # signed: gamma


def _process_tick(buf, last_tick, update_count, output, verbose, last_status_print):
    """Process SSE lines from buffer. Returns (remaining_buf, last_tick, update_count, last_status_print)."""
    while "\n" in buf:
        line, buf = buf.split("\n", 1)
        data = _parse_sse_line(line)
        if data is None:
            continue

        tick_time = time.time()
        update_count += 1

        with _lock:
            state = _build_state(data, update_count, last_tick)
            _atomic_write(output, state)

        last_tick = tick_time

        now = time.time()
        if now - last_status_print >= 10 or verbose:
            w = state.get("workers", {})
            statuses = " ".join(f"{n}={v.get('status','?')}" for n, v in sorted(w.items()))
            pr = len(state.get("pending_results", []))
            pa = len(state.get("pending_alerts", []))
            print(f"[sse-daemon] tick#{update_count} [{statuses}] "
                  f"bus={state.get('bus_depth',0)} results={pr} alerts={pa} "
                  f"latency={state.get('latency_ms',0)}ms", flush=True)
            last_status_print = now

    return buf, last_tick, update_count, last_status_print


def _post_degraded_alert(content):
    """Post DAEMON_DEGRADED alert to bus via guarded_publish with raw fallback."""
    msg = {"sender": "sse_daemon", "topic": "orchestrator", "type": "alert", "content": f"DAEMON_DEGRADED {content}"}
    try:
        from tools.skynet_spam_guard import guarded_publish
        guarded_publish(msg)
    except ImportError:
        try:
            import urllib.request
            payload = json.dumps(msg).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:8420/bus/publish", payload,
                {"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
    except Exception:
        pass
    # signed: beta


# ─── Main Daemon Loop ─────────────────────────────────

def run_daemon(host: str = "127.0.0.1", port: int = 8420,
               output: Path = STATE_FILE, verbose: bool = False):
    """Main SSE event loop. Connects, parses ticks, writes state atomically."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ── Signal handlers for graceful shutdown ──
    # Registered BEFORE _init_pid_guard so the PID guard chains to them  # signed: gamma
    _sse_shutdown = False
    def _sigterm_handler(signum, frame):
        nonlocal _sse_shutdown
        _sse_shutdown = True
        print(f"[sse-daemon] Received signal {signum} -- shutting down", flush=True)
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _sigterm_handler)
    try:
        signal.signal(signal.SIGBREAK, _sigterm_handler)  # Windows Ctrl+Break
    except (AttributeError, OSError):
        pass  # signed: gamma

    if not _init_pid_guard(PID_FILE):
        return

    update_count = 0
    backoff = 2.0
    max_backoff = 30.0
    last_status_print = 0
    _consecutive_errors = 0  # signed: beta
    _DEGRADED_THRESHOLD = 10  # signed: beta

    print(f"[sse-daemon] Connecting to SSE at {host}:{port}/stream", flush=True)
    print(f"[sse-daemon] Writing state to {output}", flush=True)

    while True:
        conn = None
        try:
            conn, resp = _sse_connect(host, port)
            backoff = 2.0
            _consecutive_errors = 0  # reset on successful connect  # signed: beta
            print(f"[sse-daemon] SSE connected, streaming...", flush=True)

            buf = ""
            last_tick = time.time()

            while True:
                chunk = resp.read(4096)
                if not chunk:
                    raise ConnectionError("SSE stream ended")
                buf += chunk.decode("utf-8", errors="replace")
                buf, last_tick, update_count, last_status_print = _process_tick(
                    buf, last_tick, update_count, output, verbose, last_status_print)

        except KeyboardInterrupt:
            print("\n[sse-daemon] Shutting down.", flush=True)
            from tools.skynet_pid_guard import release_pid_guard
            release_pid_guard(PID_FILE)  # signed: gamma
            break
        except (ConnectionError, TimeoutError, OSError) as e:
            _consecutive_errors += 1
            print(f"[sse-daemon] Disconnected (network, {_consecutive_errors}x): {e}. Reconnecting in {backoff}s...", flush=True)
            if _consecutive_errors % _DEGRADED_THRESHOLD == 0:
                _post_degraded_alert(f"sse_daemon {_consecutive_errors} consecutive errors: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
        except Exception as e:
            _consecutive_errors += 1
            print(f"[sse-daemon] Error ({_consecutive_errors}x): {e}. Reconnecting in {backoff}s...", flush=True)
            if _consecutive_errors % _DEGRADED_THRESHOLD == 0:
                _post_degraded_alert(f"sse_daemon {_consecutive_errors} consecutive errors: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


# ─── CLI ───────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Skynet SSE Real-Time Daemon")
    parser.add_argument("--port", type=int, default=8420, help="Skynet backend port")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Skynet backend host")
    parser.add_argument("--output", type=str, default=str(STATE_FILE), help="Output JSON path")
    parser.add_argument("--verbose", action="store_true", help="Print every tick")
    parser.add_argument("--read", action="store_true", help="Read and print current state (no daemon)")
    args = parser.parse_args()

    if args.read:
        state = read_state()
        if state:
            print(json.dumps(state, indent=2, default=str))
        else:
            print("No state file found. Start the daemon first.")
        return

    run_daemon(host=args.host, port=args.port, output=Path(args.output), verbose=args.verbose)


if __name__ == "__main__":
    main()
