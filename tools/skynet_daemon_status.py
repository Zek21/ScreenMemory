"""
Unified daemon status CLI tool for Skynet.

Checks ALL daemons in the Skynet ecosystem and prints a comprehensive status table.
For each daemon: name, PID, port, alive, uptime, criticality.

Supports:
  --json       Machine-readable JSON output
  --restart-dead  Restart any dead daemons automatically

Reference: docs/DAEMON_ARCHITECTURE.md (Section 8: Criticality Matrix)

Usage:
    python tools/skynet_daemon_status.py
    python tools/skynet_daemon_status.py --json
    python tools/skynet_daemon_status.py --restart-dead

# signed: beta
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PYTHON = sys.executable


# ── Daemon Registry ──────────────────────────────────────────────────────────
# Canonical list of ALL daemons in the Skynet ecosystem.
# Reference: docs/DAEMON_ARCHITECTURE.md Section 13: Related Files
DAEMON_REGISTRY = [
    {
        "name": "skynet_backend",
        "label": "Skynet Backend (Go)",
        "script": None,
        "binary": "Skynet/skynet.exe",
        "pid_file": None,
        "port": 8420,
        "health_url": "http://localhost:8420/health",
        "criticality": "CATASTROPHIC",
        "restart_cmd": None,
    },
    {
        "name": "god_console",
        "label": "GOD Console",
        "script": "god_console.py",
        "pid_file": "data/god_console.pid",
        "port": 8421,
        "health_url": "http://localhost:8421/health",
        "criticality": "HIGH",
        "restart_cmd": [PYTHON, "god_console.py", "--no-open"],
    },
    {
        "name": "monitor",
        "label": "Worker Monitor",
        "script": "tools/skynet_monitor.py",
        "pid_file": "data/monitor.pid",
        "port": None,
        "health_url": None,
        "criticality": "HIGH",
        "restart_cmd": [PYTHON, "tools/skynet_monitor.py"],
    },
    {
        "name": "watchdog",
        "label": "Service Watchdog",
        "script": "tools/skynet_watchdog.py",
        "pid_file": "data/watchdog.pid",
        "port": None,
        "health_url": None,
        "criticality": "HIGH",
        "restart_cmd": [PYTHON, "tools/skynet_watchdog.py"],
    },
    {
        "name": "overseer",
        "label": "Idle Overseer",
        "script": "tools/skynet_overseer.py",
        "pid_file": "data/overseer.pid",
        "port": None,
        "health_url": None,
        "criticality": "MODERATE",
        "restart_cmd": [PYTHON, "tools/skynet_overseer.py"],
    },
    {
        "name": "self_prompt",
        "label": "Self-Prompt Heartbeat",
        "script": "tools/skynet_self_prompt.py",
        "pid_file": "data/self_prompt.pid",
        "port": None,
        "health_url": None,
        "criticality": "MODERATE",
        "restart_cmd": [PYTHON, "tools/skynet_self_prompt.py", "start"],
    },
    {
        "name": "self_improve",
        "label": "Self-Improve Engine",
        "script": "tools/skynet_self_improve.py",
        "pid_file": "data/self_improve.pid",
        "port": None,
        "health_url": None,
        "criticality": "LOW",
        "restart_cmd": [PYTHON, "tools/skynet_self_improve.py", "start"],
    },
    {
        "name": "bus_relay",
        "label": "Bus Relay",
        "script": "tools/skynet_bus_relay.py",
        "pid_file": "data/bus_relay.pid",
        "port": None,
        "health_url": None,
        "criticality": "MODERATE",
        "restart_cmd": [PYTHON, "tools/skynet_bus_relay.py"],
    },
    {
        "name": "bus_persist",
        "label": "Bus Persist (JSONL)",
        "script": "tools/skynet_bus_persist.py",
        "pid_file": "data/bus_persist.pid",
        "port": None,
        "health_url": None,
        "criticality": "MODERATE",
        "restart_cmd": [PYTHON, "tools/skynet_bus_persist.py"],
    },
    {
        "name": "sse_daemon",
        "label": "SSE Daemon",
        "script": "tools/skynet_sse_daemon.py",
        "pid_file": "data/sse_daemon.pid",
        "port": None,
        "health_url": None,
        "criticality": "MODERATE",
        "restart_cmd": [PYTHON, "tools/skynet_sse_daemon.py"],
    },
    {
        "name": "learner",
        "label": "Learner Daemon",
        "script": "tools/skynet_learner.py",
        "pid_file": "data/learner.pid",
        "port": None,
        "health_url": None,
        "criticality": "LOW",
        "restart_cmd": [PYTHON, "tools/skynet_learner.py", "--daemon"],
    },
    {
        "name": "consultant_bridge_codex",
        "label": "Codex Consultant Bridge",
        "script": "tools/skynet_consultant_bridge.py",
        "pid_file": "data/consultant_bridge.pid",
        "port": 8422,
        "health_url": "http://localhost:8422/health",
        "criticality": "LOW",
        "restart_cmd": [PYTHON, "tools/skynet_consultant_bridge.py"],
    },
    {
        "name": "consultant_bridge_gemini",
        "label": "Gemini Consultant Bridge",
        "script": "tools/skynet_consultant_bridge.py",
        "pid_file": "data/gemini_consultant_bridge.pid",
        "port": 8425,
        "health_url": "http://localhost:8425/health",
        "criticality": "LOW",
        "restart_cmd": [PYTHON, "tools/skynet_consultant_bridge.py",
                        "--id", "gemini_consultant", "--display-name", "Gemini Consultant",
                        "--model", "Gemini 3 Pro", "--source", "GC-Start", "--api-port", "8425"],
    },
    {
        "name": "consultant_consumer_codex",
        "label": "Codex Consultant Consumer",
        "script": "tools/skynet_consultant_consumer.py",
        "pid_file": "data/consultant_consumer_8422.pid",
        "port": None,
        "health_url": None,
        "criticality": "LOW",
        "restart_cmd": [PYTHON, "tools/skynet_consultant_consumer.py", "--port", "8422"],
    },
    {
        "name": "consultant_consumer_gemini",
        "label": "Gemini Consultant Consumer",
        "script": "tools/skynet_consultant_consumer.py",
        "pid_file": "data/consultant_consumer_8425.pid",
        "port": None,
        "health_url": None,
        "criticality": "LOW",
        "restart_cmd": [PYTHON, "tools/skynet_consultant_consumer.py", "--port", "8425"],
    },
    {
        "name": "convene_gate",
        "label": "Convene Gate",
        "script": "convene_gate.py",
        "pid_file": "data/convene_gate.pid",
        "port": None,
        "health_url": None,
        "criticality": "LOW",
        "restart_cmd": [PYTHON, "convene_gate.py", "--monitor"],
    },
]
# signed: beta


def _pid_alive(pid: int) -> bool:
    """Check if a process with given PID is alive.

    Uses psutil as primary method (most reliable on Windows).
    Falls back to os.kill(pid, 0) with correct PermissionError handling.
    On Windows, PermissionError means the process exists but we lack
    signal rights — still alive, not dead.

    Reference: docs/DAEMON_ARCHITECTURE.md Section 7: PID Management
    """
    if pid <= 0:
        return False
    # Primary: psutil (handles Windows quirks correctly)
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    # Fallback: os.kill signal 0
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # Process exists but we lack permission — still alive
        return True
    except OSError:
        return False
    # signed: delta


def _find_daemon_process(daemon: dict) -> int:
    """Scan running processes to find a daemon that matches the script name.

    When the PID file is stale or missing, this provides a fallback by
    scanning all running Python processes for a command line matching the
    daemon's script path. Returns the PID if found, 0 otherwise.

    Reference: docs/DAEMON_ARCHITECTURE.md Section 7: PID Management
    """
    script = daemon.get("script")
    if not script:
        return 0
    # Extract the key identifier from the script path (e.g. "skynet_monitor" from "tools/skynet_monitor.py")
    script_key = Path(script).stem
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if "python" not in name:
                    continue
                cmdline = " ".join(proc.info.get("cmdline") or []).replace("\\", "/").lower()
                if script_key in cmdline and script.replace("\\", "/").lower() in cmdline:
                    return proc.info["pid"]
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except ImportError:
        pass
    return 0
    # signed: delta


def _read_pid(pid_file_path: str) -> int:
    """Read PID from a file. Returns 0 if unreadable.

    Reference: docs/DAEMON_ARCHITECTURE.md Section 7: PID Management
    """
    try:
        p = ROOT / pid_file_path
        if p.exists():
            return int(p.read_text().strip())
    except (ValueError, OSError):
        pass
    return 0
    # signed: beta


def _check_url(url: str, timeout: float = 3.0) -> bool:
    """Check if a URL is reachable. Returns True if HTTP 2xx.

    Reference: docs/DAEMON_ARCHITECTURE.md Section 9: Health Check Mechanisms
    """
    try:
        req = urllib.request.Request(url, method="GET")
        resp = urllib.request.urlopen(req, timeout=timeout)
        return 200 <= resp.status < 300
    except Exception:
        return False
    # signed: beta


def _get_process_start_time(pid: int) -> float:
    """Get process start time as epoch. Returns 0 on failure."""
    try:
        import psutil
        proc = psutil.Process(pid)
        return proc.create_time()
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).StartTime.ToUniversalTime().ToString('o')"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.stdout.strip():
            from datetime import datetime as dt
            ts = result.stdout.strip().rstrip("Z")
            if "+" in ts:
                ts = ts.split("+")[0]
            return dt.fromisoformat(ts).timestamp()
    except Exception:
        pass
    return 0.0
    # signed: beta


def _read_heartbeat(service_name: str) -> dict:
    """Read last heartbeat data from data/service_heartbeats.json.

    Reference: docs/DAEMON_ARCHITECTURE.md Section 9: Health Check Mechanisms
    """
    hb_file = DATA_DIR / "service_heartbeats.json"
    try:
        if hb_file.exists():
            data = json.loads(hb_file.read_text(encoding="utf-8"))
            return data.get(service_name, {})
    except Exception:
        pass
    return {}
    # signed: beta


def check_daemon(daemon: dict) -> dict:
    """Check a single daemon's status and return a status dict.

    Performs PID file check, process liveness check, port/HTTP health check,
    and heartbeat lookup. Returns a dict with all status fields.

    Reference: docs/DAEMON_ARCHITECTURE.md (Sections 7-9)
    """
    result = {
        "name": daemon["name"],
        "label": daemon["label"],
        "criticality": daemon["criticality"],
        "pid": 0,
        "pid_file": daemon.get("pid_file", ""),
        "port": daemon.get("port"),
        "alive": False,
        "pid_alive": False,
        "port_alive": False,
        "uptime_s": 0,
        "uptime_human": "",
        "last_heartbeat": None,
        "heartbeat_status": "unknown",
    }

    # PID check — two-tier: PID file first, then process scan fallback
    pid_file = daemon.get("pid_file")
    if pid_file:
        pid = _read_pid(pid_file)
        result["pid"] = pid
        if pid and _pid_alive(pid):
            result["pid_alive"] = True
        else:
            # Fallback: PID file is stale/missing — scan for the actual process
            found_pid = _find_daemon_process(daemon)
            if found_pid:
                result["pid"] = found_pid
                result["pid_alive"] = True
                result["pid_source"] = "process_scan"
        # signed: delta
        if result["pid_alive"]:
            start = _get_process_start_time(result["pid"])
            if start > 0:
                uptime = time.time() - start
                result["uptime_s"] = int(uptime)
                result["uptime_human"] = _format_uptime(uptime)

    # Port / HTTP check
    health_url = daemon.get("health_url")
    if health_url:
        result["port_alive"] = _check_url(health_url)
    elif daemon.get("port"):
        result["port_alive"] = _check_url(f"http://localhost:{daemon['port']}/health")

    # Special case: skynet.exe has no PID file — alive = port alive
    if not pid_file and daemon.get("port"):
        result["alive"] = result["port_alive"]
    else:
        result["alive"] = result["pid_alive"]

    # Also consider port-alive as alive for services with both
    if result["port_alive"] and not result["alive"]:
        result["alive"] = True

    # Heartbeat
    hb = _read_heartbeat(daemon["name"])
    if hb:
        result["last_heartbeat"] = hb.get("last_seen_ts")
        result["heartbeat_status"] = hb.get("status", "unknown")

    return result
    # signed: beta


def check_all_daemons() -> list:
    """Check ALL daemons and return a list of status dicts.

    Iterates through DAEMON_REGISTRY and probes each daemon.
    Reference: docs/DAEMON_ARCHITECTURE.md (full ecosystem scan)
    """
    results = []
    for daemon in DAEMON_REGISTRY:
        results.append(check_daemon(daemon))
    return results
    # signed: beta


def _format_uptime(seconds: float) -> str:
    """Format seconds into human-readable uptime string."""
    if seconds <= 0:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m}m"
    # signed: beta


def _criticality_color(tier: str) -> str:
    """ANSI color for criticality tier."""
    colors = {
        "CATASTROPHIC": "\033[91m",  # bright red
        "HIGH": "\033[93m",          # yellow
        "MODERATE": "\033[96m",      # cyan
        "LOW": "\033[92m",           # green
    }
    return colors.get(tier, "")
    # signed: beta


def _status_icon(alive: bool) -> str:
    """Status indicator."""
    return "\033[92m OK \033[0m" if alive else "\033[91mDEAD\033[0m"
    # signed: beta


def print_status_table(results: list) -> None:
    """Print a formatted daemon status table to stdout.

    Reference: docs/DAEMON_ARCHITECTURE.md Section 8: Criticality Matrix
    """
    reset = "\033[0m"
    bold = "\033[1m"
    header = f"{bold}{'Daemon':<30} {'PID':>6} {'Port':>5} {'Status':<6} {'Uptime':>10} {'Criticality':<13} {'Heartbeat':<10}{reset}"
    print()
    print(f"{bold}{'=' * 90}")
    print(f"  SKYNET DAEMON STATUS  ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"{'=' * 90}{reset}")
    print(header)
    print("-" * 90)

    alive_count = 0
    dead_count = 0
    for r in results:
        crit_color = _criticality_color(r["criticality"])
        status = _status_icon(r["alive"])
        pid_str = str(r["pid"]) if r["pid"] else "—"
        port_str = str(r["port"]) if r["port"] else "—"
        uptime_str = r["uptime_human"] if r["uptime_human"] else "—"
        hb_str = r["heartbeat_status"][:8] if r["heartbeat_status"] else "—"

        print(f"  {r['label']:<28} {pid_str:>6} {port_str:>5} {status} {uptime_str:>10} "
              f"{crit_color}{r['criticality']:<13}{reset} {hb_str:<10}")

        if r["alive"]:
            alive_count += 1
        else:
            dead_count += 1

    print("-" * 90)
    print(f"  Total: {len(results)} daemons | "
          f"\033[92m{alive_count} alive\033[0m | "
          f"\033[91m{dead_count} dead\033[0m")
    print()
    # signed: beta


def restart_dead_daemons(results: list) -> list:
    """Restart any dead daemons that have a restart_cmd defined.

    Uses subprocess.Popen with DETACHED_PROCESS to start daemons in background.
    Returns list of restart results.

    Reference: docs/DAEMON_ARCHITECTURE.md Section 10: Startup Sequence
    """
    restarted = []
    for r in results:
        if r["alive"]:
            continue
        daemon = next((d for d in DAEMON_REGISTRY if d["name"] == r["name"]), None)
        if not daemon or not daemon.get("restart_cmd"):
            restarted.append({"name": r["name"], "action": "skip", "reason": "no restart command"})
            continue

        print(f"  Restarting {r['label']}...")
        try:
            subprocess.Popen(
                daemon["restart_cmd"],
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                             | getattr(subprocess, "DETACHED_PROCESS", 0),
            )
            time.sleep(2)
            # Re-check
            new_status = check_daemon(daemon)
            if new_status["alive"]:
                print(f"    \033[92m✓ {r['label']} restarted (PID {new_status['pid']})\033[0m")
                restarted.append({"name": r["name"], "action": "restarted", "pid": new_status["pid"]})
            else:
                print(f"    \033[93m? {r['label']} started but not yet verified\033[0m")
                restarted.append({"name": r["name"], "action": "started_unverified"})
        except Exception as e:
            print(f"    \033[91m✗ {r['label']} restart failed: {e}\033[0m")
            restarted.append({"name": r["name"], "action": "failed", "error": str(e)})

    return restarted
    # signed: beta


def main():
    """CLI entry point for skynet_daemon_status.py.

    Reference: docs/DAEMON_ARCHITECTURE.md for full daemon ecosystem details.
    """
    parser = argparse.ArgumentParser(
        description="Skynet Daemon Status — check all daemons in the ecosystem",
        epilog="Reference: docs/DAEMON_ARCHITECTURE.md",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    parser.add_argument("--restart-dead", action="store_true", help="Restart any dead daemons")
    args = parser.parse_args()

    results = check_all_daemons()

    if args.json and not args.restart_dead:
        output = {
            "timestamp": datetime.now().isoformat(),
            "daemons": results,
            "summary": {
                "total": len(results),
                "alive": sum(1 for r in results if r["alive"]),
                "dead": sum(1 for r in results if not r["alive"]),
            },
        }
        print(json.dumps(output, indent=2, default=str))
        return

    print_status_table(results)

    if args.restart_dead:
        dead = [r for r in results if not r["alive"]]
        if not dead:
            print("  All daemons are alive — nothing to restart.")
        else:
            print(f"  Restarting {len(dead)} dead daemon(s)...")
            restart_results = restart_dead_daemons(results)
            if args.json:
                print(json.dumps({"restarts": restart_results}, indent=2, default=str))
    # signed: beta


if __name__ == "__main__":
    main()
