#!/usr/bin/env python3
"""
daemon_health.py -- Quick diagnostic: checks all Skynet daemons are alive.

Usage:
    python tools/daemon_health.py          # Report alive/dead for all 9 daemons
    python tools/daemon_health.py --fix    # Auto-start dead daemons
    python tools/daemon_health.py --json   # Machine-readable JSON output

Exit codes: 0 = all healthy, 1 = one or more dead.
Designed to be called from Orch-Start.ps1 for comprehensive daemon verification.
"""
# signed: delta

import argparse
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# ── Resolve real Python to avoid venv launcher duplicate-process issues ──
def _real_python():
    cfg = ROOT.parent / "env" / "pyvenv.cfg"
    if cfg.exists():
        for line in cfg.read_text().splitlines():
            if line.strip().startswith("executable"):
                _, _, val = line.partition("=")
                p = val.strip()
                if Path(p).exists():
                    return p
    return sys.executable
# signed: delta

PYTHON = _real_python()
_BG = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS

# ── Daemon registry: name → (pid_file, start_cmd, description) ──
DAEMONS = {
    "monitor":      ("monitor.pid",      ["skynet_monitor.py"],                "Worker window health monitor"),
    "watchdog":     ("watchdog.pid",      ["skynet_watchdog.py", "start"],      "Service health watchdog"),
    "self_prompt":  ("self_prompt.pid",   ["skynet_self_prompt.py", "start"],   "Orchestrator heartbeat"),
    "self_improve": ("self_improve.pid",  ["skynet_self_improve.py", "start"],  "Self-improvement scanner"),
    "bus_relay":    ("bus_relay.pid",     ["skynet_bus_relay.py"],              "Bus message relay"),
    "sse_daemon":   ("sse_daemon.pid",   ["skynet_sse_daemon.py"],             "SSE real-time streamer"),
    "learner":      ("learner.pid",      ["skynet_learner.py", "--daemon"],    "Learning engine"),
    "overseer":     ("overseer.pid",     ["skynet_overseer.py"],               "Autonomous overseer"),
    "realtime":     ("realtime.pid",     ["skynet_realtime.py"],               "Real-time state writer"),
}
# signed: delta


def _alive(pid: int) -> bool:
    """Windows-safe process alive check via kernel32."""
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    return False
# signed: delta


def check_daemon(name: str) -> dict:
    """Check one daemon. Returns {name, pid, alive, pid_file, stale}."""
    pid_file_name, _, desc = DAEMONS[name]
    pid_path = DATA / pid_file_name
    result = {"name": name, "description": desc, "pid": None,
              "alive": False, "stale": False, "pid_file": str(pid_path)}
    if not pid_path.exists():
        return result
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        result["stale"] = True
        return result
    result["pid"] = pid
    if _alive(pid):
        result["alive"] = True
    else:
        result["stale"] = True
    return result
# signed: delta


def fix_daemon(name: str) -> bool:
    """Start a dead daemon. Returns True on success."""
    _, cmd_parts, _ = DAEMONS[name]
    full_cmd = [PYTHON] + [str(ROOT / "tools" / c) if c.endswith(".py") else c for c in cmd_parts]
    env = os.environ.copy()
    site_pkg = str(ROOT.parent / "env" / "Lib" / "site-packages")
    env["PYTHONPATH"] = f"{site_pkg};{env.get('PYTHONPATH', '')}"
    try:
        proc = subprocess.Popen(full_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=env, creationflags=_BG, cwd=str(ROOT))
        import time; time.sleep(1.5)
        return proc.poll() is None
    except Exception as e:
        print(f"  FAILED to start {name}: {e}", file=sys.stderr)
        return False
# signed: delta


def main():
    parser = argparse.ArgumentParser(description="Skynet Daemon Health Check")
    parser.add_argument("--fix", action="store_true", help="Auto-start dead daemons")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    results = {name: check_daemon(name) for name in DAEMONS}
    alive_count = sum(1 for r in results.values() if r["alive"])
    total = len(DAEMONS)
    dead_names = [n for n, r in results.items() if not r["alive"]]

    if args.json:
        print(json.dumps({"daemons": results, "alive": alive_count,
                          "total": total, "healthy": alive_count == total}))
    else:
        print(f"\n  Skynet Daemon Health ({alive_count}/{total})\n  {'='*40}")
        for name, r in results.items():
            icon = "OK" if r["alive"] else ("STALE" if r["stale"] else "DOWN")
            pid_s = str(r["pid"]) if r["pid"] else "-"
            print(f"  {icon:>5}  {name:<15} PID {pid_s:>7}  {r['description']}")
        print()

    fixed = 0
    if args.fix and dead_names:
        print(f"  Fixing {len(dead_names)} dead daemon(s)...")
        for name in dead_names:
            ok = fix_daemon(name)
            status = "STARTED" if ok else "FAILED"
            print(f"    {name}: {status}")
            if ok:
                fixed += 1
        print(f"  Fixed {fixed}/{len(dead_names)}\n")

    sys.exit(0 if alive_count + fixed >= total else 1)


if __name__ == "__main__":
    main()
# signed: delta
