#!/usr/bin/env python3
"""skynet_worker_check.py -- Simple worker state utility for the orchestrator.

Replaces fragile inline `python -c` scripts with a robust CLI tool.
Reads state from data/realtime.json (zero-network) with HTTP fallback.

Usage:
    python tools/skynet_worker_check.py scan              # All workers: name:state
    python tools/skynet_worker_check.py scan-one alpha     # Single worker state
    python tools/skynet_worker_check.py wait alpha 60      # Poll until IDLE (max 60s)
    python tools/skynet_worker_check.py idle               # List idle workers
    python tools/skynet_worker_check.py busy               # List busy workers
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REALTIME_FILE = ROOT / "data" / "realtime.json"
SKYNET = "http://localhost:8420"
ALL_WORKERS = ["alpha", "beta", "gamma", "delta"]


def _read_state() -> dict:
    """Read worker states from realtime.json, fallback to HTTP."""
    if REALTIME_FILE.exists():
        try:
            data = json.loads(REALTIME_FILE.read_text(encoding="utf-8"))
            ts = data.get("timestamp") or data.get("last_update") or 0
            if isinstance(ts, str):
                from datetime import datetime
                try:
                    ts = datetime.fromisoformat(ts).timestamp()
                except Exception:
                    ts = 0
            age = time.time() - ts
            if age < 10:
                agents = data.get("agents") or data.get("workers") or {}
                return agents
        except (json.JSONDecodeError, OSError):
            pass

    # HTTP fallback
    try:
        import urllib.request
        r = urllib.request.urlopen(f"{SKYNET}/status", timeout=3)
        status = json.loads(r.read())
        return status.get("agents", {})
    except Exception:
        return {}


def _get_worker_status(agents: dict, name: str) -> str:
    """Extract status string for a worker."""
    agent = agents.get(name, {})
    return agent.get("status", "UNKNOWN")


def cmd_scan():
    """Print all workers as name:state pairs."""
    agents = _read_state()
    for name in ALL_WORKERS:
        st = _get_worker_status(agents, name)
        print(f"{name}:{st}")


def cmd_scan_one(name: str):
    """Print a single worker's state."""
    agents = _read_state()
    st = _get_worker_status(agents, name)
    print(f"{name}:{st}")


def cmd_wait(name: str, timeout: float):
    """Poll until worker reaches IDLE state. Exits 0 on success, 1 on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        agents = _read_state()
        st = _get_worker_status(agents, name)
        if st == "IDLE":
            print(f"{name}:IDLE")
            sys.exit(0)
        time.sleep(0.5)
    st = _get_worker_status(_read_state(), name)
    print(f"TIMEOUT:{name}:{st}")
    sys.exit(1)


def cmd_idle():
    """List all idle workers."""
    agents = _read_state()
    idle = [n for n in ALL_WORKERS if _get_worker_status(agents, n) == "IDLE"]
    if idle:
        print(",".join(idle))
    else:
        print("NONE")


def cmd_busy():
    """List all busy (non-IDLE) workers."""
    agents = _read_state()
    busy = [n for n in ALL_WORKERS if _get_worker_status(agents, n) != "IDLE"]
    if busy:
        for n in busy:
            print(f"{n}:{_get_worker_status(agents, n)}")
    else:
        print("NONE")


def main():
    if len(sys.argv) < 2:
        print("Usage: skynet_worker_check.py <scan|scan-one|wait|idle|busy> [args]")
        print("  scan              All workers: name:state")
        print("  scan-one NAME     Single worker state")
        print("  wait NAME TIMEOUT Poll until IDLE (exit 0=ok, 1=timeout)")
        print("  idle              List idle workers (comma-separated)")
        print("  busy              List busy workers with states")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "scan":
        cmd_scan()
    elif cmd == "scan-one":
        if len(sys.argv) < 3:
            print("Usage: skynet_worker_check.py scan-one NAME")
            sys.exit(1)
        cmd_scan_one(sys.argv[2].lower())
    elif cmd == "wait":
        if len(sys.argv) < 4:
            print("Usage: skynet_worker_check.py wait NAME TIMEOUT_SECONDS")
            sys.exit(1)
        cmd_wait(sys.argv[2].lower(), float(sys.argv[3]))
    elif cmd == "idle":
        cmd_idle()
    elif cmd == "busy":
        cmd_busy()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
