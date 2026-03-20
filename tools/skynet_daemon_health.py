#!/usr/bin/env python3
"""
skynet_daemon_health.py -- Tier-aware daemon health checker and auto-starter.

Checks all Skynet daemons, reports status, and auto-starts dead CRITICAL/HIGH
tier daemons. Respects kill switches (e.g. self_prompt disabled per INCIDENT 016).

Usage:
    python tools/skynet_daemon_health.py              # Report all daemon status
    python tools/skynet_daemon_health.py --auto-start  # Auto-start dead CRITICAL+HIGH daemons
    python tools/skynet_daemon_health.py --json        # Machine-readable JSON output
    python tools/skynet_daemon_health.py --clean-stale # Remove PID files for dead processes
    python tools/skynet_daemon_health.py --all         # Report + auto-start + clean stale

Exit codes: 0 = all tier-appropriate daemons healthy, 1 = issues found.
"""
# signed: delta

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TOOLS = ROOT / "tools"

# ── Resolve real Python interpreter ──
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
_BG = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)

# ── Tier definitions ──
CRITICAL = "CRITICAL"
HIGH = "HIGH"
MEDIUM = "MEDIUM"
LOW = "LOW"

# ── Daemon registry ──
# Each entry: (pid_file, start_cmd, description, tier, kill_switch_path)
# kill_switch_path: dot-separated path in brain_config.json; if value is False, skip start
DAEMON_REGISTRY = {
    "monitor": {
        "pid_file": "monitor.pid",
        "start_cmd": ["skynet_monitor.py"],
        "description": "Worker HWND liveness + model drift detection",
        "tier": CRITICAL,
        "kill_switch": None,
    },
    "watchdog": {
        "pid_file": "watchdog.pid",
        "start_cmd": ["skynet_watchdog.py", "start"],
        "description": "Backend/GOD Console process liveness",
        "tier": CRITICAL,
        "kill_switch": None,
    },
    "sse_daemon": {
        "pid_file": "sse_daemon.pid",
        "start_cmd": ["skynet_sse_daemon.py"],
        "description": "SSE subscriber, writes realtime.json",
        "tier": CRITICAL,
        "kill_switch": None,
    },
    "overseer": {
        "pid_file": "overseer.pid",
        "start_cmd": ["skynet_overseer.py", "start"],
        "description": "Idle worker + pending TODO detection",
        "tier": HIGH,
        "kill_switch": None,
    },
    "self_prompt": {
        "pid_file": "self_prompt.pid",
        "start_cmd": ["skynet_self_prompt.py", "start"],
        "description": "Orchestrator heartbeat (DISABLED per INCIDENT 016)",
        "tier": HIGH,
        "kill_switch": "self_prompt.enabled",
    },
    "bus_relay": {
        "pid_file": "bus_relay.pid",
        "start_cmd": ["skynet_bus_relay.py"],
        "description": "Bus message relay",
        "tier": HIGH,
        "kill_switch": None,
    },
    "self_improve": {
        "pid_file": "self_improve.pid",
        "start_cmd": ["skynet_self_improve.py", "start"],
        "description": "Self-improvement scanner",
        "tier": HIGH,
        "kill_switch": None,
    },
    "learner": {
        "pid_file": "learner.pid",
        "start_cmd": ["skynet_learner.py", "--daemon"],
        "description": "Learning engine daemon",
        "tier": HIGH,
        "kill_switch": None,
    },
    "bus_persist": {
        "pid_file": "bus_persist.pid",
        "start_cmd": ["skynet_bus_persist.py"],
        "description": "JSONL bus message archival",
        "tier": MEDIUM,
        "kill_switch": None,
    },
    "bus_watcher": {
        "pid_file": "bus_watcher.pid",
        "start_cmd": ["skynet_bus_watcher.py"],
        "description": "Auto-routes pending tasks to idle workers",
        "tier": MEDIUM,
        "kill_switch": None,
    },
    "ws_monitor": {
        "pid_file": "ws_monitor.pid",
        "start_cmd": ["skynet_ws_monitor.py"],
        "description": "WebSocket security alert listener",
        "tier": MEDIUM,
        "kill_switch": None,
    },
    "idle_monitor": {
        "pid_file": "idle_monitor.pid",
        "start_cmd": ["skynet_idle_monitor.py"],
        "description": "Extended idle period detection",
        "tier": MEDIUM,
        "kill_switch": None,
    },
    "consultant_consumer": {
        "pid_file": "consultant_consumer.pid",
        "start_cmd": ["skynet_consultant_consumer.py"],
        "description": "Consultant bridge queue consumer",
        "tier": MEDIUM,
        "kill_switch": None,
    },
    "worker_loop": {
        "pid_file": "worker_loop.pid",
        "start_cmd": ["skynet_worker_loop.py"],
        "description": "Worker autonomous task polling",
        "tier": LOW,
        "kill_switch": None,
    },
    "health_report": {
        "pid_file": "health_report.pid",
        "start_cmd": ["skynet_health_report.py"],
        "description": "Periodic health report generation",
        "tier": LOW,
        "kill_switch": None,
    },
}
# signed: delta


def _alive(pid: int) -> bool:
    """Check if process is alive via Windows kernel32."""
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False
# signed: delta


def _check_kill_switch(switch_path: str) -> bool:
    """Check brain_config.json kill switch. Returns True if daemon is allowed to run."""
    if not switch_path:
        return True
    try:
        with open(DATA / "brain_config.json") as f:
            config = json.load(f)
        parts = switch_path.split(".")
        val = config
        for p in parts:
            val = val[p]
        return bool(val)
    except (KeyError, TypeError, FileNotFoundError, json.JSONDecodeError):
        return True  # if config missing, allow by default
# signed: delta


def check_all_daemons() -> list:
    """Check status of all registered daemons. Returns list of status dicts."""
    results = []
    for name, info in DAEMON_REGISTRY.items():
        pid_path = DATA / info["pid_file"]
        entry = {
            "name": name,
            "tier": info["tier"],
            "description": info["description"],
            "pid": None,
            "alive": False,
            "status": "NO_PID",
            "pid_file": str(pid_path),
            "kill_switch": info["kill_switch"],
            "kill_switch_blocked": False,
        }

        # Check kill switch
        if info["kill_switch"] and not _check_kill_switch(info["kill_switch"]):
            entry["status"] = "DISABLED"
            entry["kill_switch_blocked"] = True
            results.append(entry)
            continue

        if pid_path.exists():
            try:
                pid = int(pid_path.read_text().strip())
                entry["pid"] = pid
                if _alive(pid):
                    entry["alive"] = True
                    entry["status"] = "ALIVE"
                else:
                    entry["status"] = "STALE_PID"
            except (ValueError, OSError):
                entry["status"] = "BAD_PID"
        else:
            entry["status"] = "NO_PID"

        results.append(entry)
    return results
# signed: delta


def auto_start_dead(results: list, max_tier: str = HIGH) -> list:
    """Auto-start dead daemons at or above the given tier. Returns list of actions taken."""
    tier_order = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3}
    max_rank = tier_order.get(max_tier, 1)
    actions = []

    for entry in results:
        if entry["alive"] or entry.get("kill_switch_blocked"):
            continue
        daemon_rank = tier_order.get(entry["tier"], 3)
        if daemon_rank > max_rank:
            continue

        name = entry["name"]
        info = DAEMON_REGISTRY[name]
        cmd = [PYTHON] + [str(TOOLS / c) if i == 0 else c
               for i, c in enumerate(info["start_cmd"])]

        action = {
            "name": name,
            "tier": entry["tier"],
            "cmd": " ".join(info["start_cmd"]),
            "success": False,
            "pid": None,
            "error": None,
        }

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_BG,
            )
            time.sleep(2)  # give daemon time to write PID file

            # Verify it started
            pid_path = DATA / info["pid_file"]
            if pid_path.exists():
                try:
                    pid = int(pid_path.read_text().strip())
                    if _alive(pid):
                        action["success"] = True
                        action["pid"] = pid
                        entry["alive"] = True
                        entry["pid"] = pid
                        entry["status"] = "STARTED"
                    else:
                        action["error"] = f"PID {pid} not alive after start"
                except ValueError:
                    action["error"] = "Invalid PID file after start"
            else:
                # Some daemons take longer; check process directly
                if proc.poll() is None:
                    action["success"] = True
                    action["pid"] = proc.pid
                    action["error"] = "Running but no PID file yet"
                else:
                    action["error"] = f"Process exited with code {proc.returncode}"
        except Exception as e:
            action["error"] = str(e)

        actions.append(action)
    return actions
# signed: delta


def clean_stale_pids(results: list) -> list:
    """Remove PID files for processes that are no longer alive."""
    cleaned = []
    for entry in results:
        if entry["status"] == "STALE_PID":
            pid_path = Path(entry["pid_file"])
            try:
                pid_path.unlink()
                cleaned.append({"name": entry["name"], "pid": entry["pid"],
                                "pid_file": str(pid_path)})
                entry["status"] = "CLEANED"
            except OSError as e:
                cleaned.append({"name": entry["name"], "error": str(e)})
    return cleaned
# signed: delta


def format_table(results: list) -> str:
    """Format daemon status as human-readable table."""
    lines = []
    lines.append("=" * 90)
    lines.append("  SKYNET DAEMON HEALTH CHECK")
    lines.append("=" * 90)
    lines.append(f"  {'Daemon':<25} {'Tier':<10} {'PID':>8}  {'Status':<12} Description")
    lines.append("-" * 90)

    tier_order = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3}
    sorted_results = sorted(results, key=lambda r: (tier_order.get(r["tier"], 9), r["name"]))

    alive_count = 0
    dead_count = 0
    disabled_count = 0

    for r in sorted_results:
        pid_str = str(r["pid"]) if r["pid"] else "--"
        status = r["status"]
        marker = " "
        if status == "ALIVE" or status == "STARTED":
            marker = "+"
            alive_count += 1
        elif status == "DISABLED":
            marker = "~"
            disabled_count += 1
        else:
            marker = "!"
            dead_count += 1

        lines.append(f"{marker} {r['name']:<25} {r['tier']:<10} {pid_str:>8}  {status:<12} {r['description'][:40]}")

    lines.append("-" * 90)
    lines.append(f"  Total: {len(results)} daemons | {alive_count} alive | {dead_count} dead | {disabled_count} disabled")
    lines.append("=" * 90)
    return "\n".join(lines)
# signed: delta


def main():
    parser = argparse.ArgumentParser(description="Skynet daemon health checker")
    parser.add_argument("--auto-start", action="store_true",
                        help="Auto-start dead CRITICAL and HIGH tier daemons")
    parser.add_argument("--clean-stale", action="store_true",
                        help="Remove PID files for dead processes")
    parser.add_argument("--all", action="store_true",
                        help="Report + auto-start + clean stale")
    parser.add_argument("--json", action="store_true",
                        help="Output machine-readable JSON")
    parser.add_argument("--tier", default="HIGH",
                        choices=["CRITICAL", "HIGH", "MEDIUM", "LOW"],
                        help="Max tier to auto-start (default: HIGH)")
    args = parser.parse_args()

    if args.all:
        args.auto_start = True
        args.clean_stale = True

    # 1. Check all daemons
    results = check_all_daemons()

    # 2. Clean stale PIDs if requested
    cleaned = []
    if args.clean_stale:
        cleaned = clean_stale_pids(results)

    # 3. Auto-start dead daemons if requested
    started = []
    if args.auto_start:
        started = auto_start_dead(results, max_tier=args.tier)

    # 4. Output
    if args.json:
        output = {
            "timestamp": time.time(),
            "daemons": results,
            "started": started,
            "cleaned": cleaned,
            "summary": {
                "total": len(results),
                "alive": sum(1 for r in results if r["alive"]),
                "dead": sum(1 for r in results if not r["alive"] and not r.get("kill_switch_blocked")),
                "disabled": sum(1 for r in results if r.get("kill_switch_blocked")),
            },
        }
        print(json.dumps(output, indent=2))
    else:
        print(format_table(results))
        if cleaned:
            print(f"\n  Cleaned {len(cleaned)} stale PID file(s):")
            for c in cleaned:
                print(f"    - {c['name']}: PID {c.get('pid', '?')}")
        if started:
            print(f"\n  Auto-started {len(started)} daemon(s):")
            for s in started:
                ok = "OK" if s["success"] else f"FAILED: {s['error']}"
                print(f"    - {s['name']} ({s['tier']}): {ok}")

    # Exit code: 0 if all CRITICAL+HIGH alive, 1 otherwise
    critical_high_dead = sum(
        1 for r in results
        if r["tier"] in (CRITICAL, HIGH)
        and not r["alive"]
        and not r.get("kill_switch_blocked")
    )
    return 1 if critical_high_dead > 0 else 0
# signed: delta


if __name__ == "__main__":
    sys.exit(main())
