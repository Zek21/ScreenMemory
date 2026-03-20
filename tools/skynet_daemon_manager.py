"""
Skynet Daemon Manager — unified lifecycle management for ALL 16+ daemons.

Replaces ad-hoc daemon management with a single CLI that handles:
  - start/stop/restart/status for all daemons
  - PID file validation (alive AND correct process)
  - Dependency ordering (realtime before monitor, backend before all)
  - Health checks with auto-restart on crash
  - Graceful shutdown with signal propagation

CLI:
    python tools/skynet_daemon_manager.py status                # Show all daemon status
    python tools/skynet_daemon_manager.py start-all             # Start all daemons in order
    python tools/skynet_daemon_manager.py stop-all              # Stop all daemons safely
    python tools/skynet_daemon_manager.py start   DAEMON_NAME   # Start a specific daemon
    python tools/skynet_daemon_manager.py stop    DAEMON_NAME   # Stop a specific daemon
    python tools/skynet_daemon_manager.py restart DAEMON_NAME   # Restart a specific daemon
    python tools/skynet_daemon_manager.py health                # Health check + auto-restart dead
    python tools/skynet_daemon_manager.py --json status         # Machine-readable output

# signed: beta
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PYTHON = sys.executable

# ── Dependency Tiers ─────────────────────────────────────────────────────
# Daemons are started in tier order (0 first). Within a tier, order is preserved.
# Stop order is reversed (highest tier first).
DAEMON_REGISTRY = [
    # Tier 0: Core infrastructure (must be up before anything else)
    {
        "name": "skynet_backend",
        "label": "Skynet Backend (Go)",
        "script": None,
        "binary": "Skynet/skynet.exe",
        "pid_file": None,
        "port": 8420,
        "health_url": "http://localhost:8420/health",
        "criticality": "CRITICAL",
        "tier": 0,
        "start_cmd": None,  # started by Orch-Start.ps1 directly
        "process_pattern": "skynet.exe",
    },
    {
        "name": "god_console",
        "label": "GOD Console",
        "script": "god_console.py",
        "pid_file": "data/god_console.pid",
        "port": 8421,
        "health_url": "http://localhost:8421/health",
        "criticality": "CRITICAL",
        "tier": 0,
        "start_cmd": [PYTHON, "god_console.py", "--no-open"],
        "process_pattern": "god_console.py",
    },
    # Tier 1: Core monitoring (depends on backend)
    {
        "name": "realtime",
        "label": "Realtime SSE Daemon",
        "script": "tools/skynet_realtime.py",
        "pid_file": "data/realtime.pid",
        "port": None,
        "health_url": None,
        "criticality": "CRITICAL",
        "tier": 1,
        "start_cmd": [PYTHON, "tools/skynet_realtime.py"],
        "process_pattern": "skynet_realtime.py",
    },
    {
        "name": "watchdog",
        "label": "Service Watchdog",
        "script": "tools/skynet_watchdog.py",
        "pid_file": "data/watchdog.pid",
        "port": None,
        "health_url": None,
        "criticality": "HIGH",
        "tier": 1,
        "start_cmd": [PYTHON, "tools/skynet_watchdog.py"],
        "process_pattern": "skynet_watchdog.py",
    },
    # Tier 2: Worker monitoring (depends on realtime)
    {
        "name": "monitor",
        "label": "Worker Monitor",
        "script": "tools/skynet_monitor.py",
        "pid_file": "data/monitor.pid",
        "port": None,
        "health_url": None,
        "criticality": "HIGH",
        "tier": 2,
        "start_cmd": [PYTHON, "tools/skynet_monitor.py"],
        "process_pattern": "skynet_monitor.py",
    },
    {
        "name": "overseer",
        "label": "Idle Overseer",
        "script": "tools/skynet_overseer.py",
        "pid_file": "data/overseer.pid",
        "port": None,
        "health_url": None,
        "criticality": "MEDIUM",
        "tier": 2,
        "start_cmd": [PYTHON, "tools/skynet_overseer.py"],
        "process_pattern": "skynet_overseer.py",
    },
    # Tier 3: Communication & intelligence
    {
        "name": "bus_relay",
        "label": "Bus Relay",
        "script": "tools/skynet_bus_relay.py",
        "pid_file": "data/bus_relay.pid",
        "port": None,
        "health_url": None,
        "criticality": "MEDIUM",
        "tier": 3,
        "start_cmd": [PYTHON, "tools/skynet_bus_relay.py"],
        "process_pattern": "skynet_bus_relay.py",
    },
    {
        "name": "bus_persist",
        "label": "Bus Persist (JSONL)",
        "script": "tools/skynet_bus_persist.py",
        "pid_file": "data/bus_persist.pid",
        "port": None,
        "health_url": None,
        "criticality": "MEDIUM",
        "tier": 3,
        "start_cmd": [PYTHON, "tools/skynet_bus_persist.py"],
        "process_pattern": "skynet_bus_persist.py",
    },
    {
        "name": "sse_daemon",
        "label": "SSE Daemon",
        "script": "tools/skynet_sse_daemon.py",
        "pid_file": "data/sse_daemon.pid",
        "port": None,
        "health_url": None,
        "criticality": "MEDIUM",
        "tier": 3,
        "start_cmd": [PYTHON, "tools/skynet_sse_daemon.py"],
        "process_pattern": "skynet_sse_daemon.py",
    },
    {
        "name": "self_prompt",
        "label": "Self-Prompt Heartbeat",
        "script": "tools/skynet_self_prompt.py",
        "pid_file": "data/self_prompt.pid",
        "port": None,
        "health_url": None,
        "criticality": "MEDIUM",
        "tier": 3,
        "start_cmd": [PYTHON, "tools/skynet_self_prompt.py", "start"],
        "process_pattern": "skynet_self_prompt.py",
    },
    # Tier 4: Learning & improvement
    {
        "name": "self_improve",
        "label": "Self-Improve Engine",
        "script": "tools/skynet_self_improve.py",
        "pid_file": "data/self_improve.pid",
        "port": None,
        "health_url": None,
        "criticality": "LOW",
        "tier": 4,
        "start_cmd": [PYTHON, "tools/skynet_self_improve.py", "start"],
        "process_pattern": "skynet_self_improve.py",
    },
    {
        "name": "learner",
        "label": "Learner Daemon",
        "script": "tools/skynet_learner.py",
        "pid_file": "data/learner.pid",
        "port": None,
        "health_url": None,
        "criticality": "LOW",
        "tier": 4,
        "start_cmd": [PYTHON, "tools/skynet_learner.py", "--daemon"],
        "process_pattern": "skynet_learner.py",
    },
    {
        "name": "knowledge_distill",
        "label": "Knowledge Distill Daemon",
        "script": "tools/skynet_knowledge_distill_daemon.py",
        "pid_file": "data/knowledge_distill.pid",
        "port": None,
        "health_url": None,
        "criticality": "LOW",
        "tier": 4,
        "start_cmd": [PYTHON, "tools/skynet_knowledge_distill_daemon.py"],
        "process_pattern": "skynet_knowledge_distill_daemon.py",
    },
    # Tier 5: Optional services
    {
        "name": "proactive_handler",
        "label": "Proactive Handler",
        "script": "tools/skynet_proactive_handler.py",
        "pid_file": "data/proactive_handler.pid",
        "port": None,
        "health_url": None,
        "criticality": "LOW",
        "tier": 5,
        "start_cmd": [PYTHON, "tools/skynet_proactive_handler.py", "start"],
        "process_pattern": "skynet_proactive_handler.py",
    },
    {
        "name": "consultant_bridge_codex",
        "label": "Codex Consultant Bridge",
        "script": "tools/skynet_consultant_bridge.py",
        "pid_file": "data/consultant_bridge.pid",
        "port": 8422,
        "health_url": "http://localhost:8422/health",
        "criticality": "LOW",
        "tier": 5,
        "start_cmd": [PYTHON, "tools/skynet_consultant_bridge.py"],
        "process_pattern": "skynet_consultant_bridge.py",
    },
    {
        "name": "consultant_bridge_gemini",
        "label": "Gemini Consultant Bridge",
        "script": "tools/skynet_consultant_bridge.py",
        "pid_file": "data/gemini_consultant_bridge.pid",
        "port": 8425,
        "health_url": "http://localhost:8425/health",
        "criticality": "LOW",
        "tier": 5,
        "start_cmd": [PYTHON, "tools/skynet_consultant_bridge.py",
                      "--id", "gemini_consultant", "--display-name", "Gemini Consultant",
                      "--model", "Gemini 3 Pro", "--source", "GC-Start", "--api-port", "8425"],
        "process_pattern": "skynet_consultant_bridge.py",
    },
]


def _pid_alive(pid: int) -> bool:
    """Check if a process with given PID is alive."""
    if pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _pid_matches_daemon(pid: int, daemon: dict) -> bool:
    """Verify the PID actually belongs to the expected daemon process."""
    pattern = daemon.get("process_pattern", "")
    if not pattern:
        return _pid_alive(pid)
    try:
        import psutil
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.cmdline()).lower()
        return pattern.lower() in cmdline
    except (ImportError, psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    # Fallback: just check alive (can't verify identity without psutil)
    return _pid_alive(pid)


def _read_pid(pid_file: str) -> int:
    """Read PID from file. Returns 0 if absent or invalid."""
    if not pid_file:
        return 0
    p = ROOT / pid_file
    if not p.exists():
        return 0
    try:
        content = p.read_text(encoding="utf-8", errors="replace").strip()
        pid = int(content)
        return pid if pid > 0 else 0
    except (ValueError, OSError):
        return 0


def _find_process_by_pattern(daemon: dict) -> int:
    """Scan running processes to find a daemon matching its pattern."""
    pattern = daemon.get("process_pattern", "")
    if not pattern:
        return 0
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if "python" not in name and pattern.endswith(".py"):
                    continue
                if pattern.endswith(".exe") and pattern.lower() not in name:
                    continue
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                if pattern.lower() in cmdline:
                    return proc.info["pid"]
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except ImportError:
        pass
    return 0


def _check_port(port: int) -> bool:
    """Check if a port is open/listening."""
    if not port:
        return False
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _check_health_url(url: str) -> bool:
    """Check if a health URL responds with 200."""
    if not url:
        return False
    try:
        import urllib.request
        resp = urllib.request.urlopen(url, timeout=3)
        return resp.getcode() == 200
    except (OSError, ValueError):
        return False


def get_daemon_status(daemon: dict) -> dict:
    """Get comprehensive status for a single daemon.

    Returns dict with: name, label, alive, pid, pid_valid, port_open,
    health_ok, tier, criticality, status (summary string).
    """
    name = daemon["name"]
    info = {
        "name": name,
        "label": daemon.get("label", name),
        "tier": daemon.get("tier", 99),
        "criticality": daemon.get("criticality", "LOW"),
        "pid": 0,
        "pid_valid": False,
        "alive": False,
        "port_open": None,
        "health_ok": None,
        "status": "STOPPED",
    }

    # Check PID file
    pid = _read_pid(daemon.get("pid_file"))
    if pid:
        info["pid"] = pid
        info["pid_valid"] = _pid_matches_daemon(pid, daemon)
        info["alive"] = info["pid_valid"]
    else:
        # No PID file — try process scan
        found_pid = _find_process_by_pattern(daemon)
        if found_pid:
            info["pid"] = found_pid
            info["pid_valid"] = True
            info["alive"] = True

    # Check port
    port = daemon.get("port")
    if port:
        info["port_open"] = _check_port(port)
        if info["port_open"] and not info["alive"]:
            info["alive"] = True  # port open means something is running

    # Check health URL
    health_url = daemon.get("health_url")
    if health_url:
        info["health_ok"] = _check_health_url(health_url)
        if info["health_ok"] and not info["alive"]:
            info["alive"] = True

    # Determine status string
    if info["alive"]:
        if info.get("health_ok") is False:
            info["status"] = "UNHEALTHY"
        elif info["pid"] and not info["pid_valid"]:
            info["status"] = "PID_STALE"
        else:
            info["status"] = "RUNNING"
    else:
        info["status"] = "STOPPED"

    return info


def get_all_status() -> list:
    """Get status for all registered daemons."""
    return [get_daemon_status(d) for d in DAEMON_REGISTRY]


def start_daemon(daemon: dict, quiet: bool = False) -> bool:
    """Start a daemon. Returns True if started successfully."""
    name = daemon["name"]
    start_cmd = daemon.get("start_cmd")

    if not start_cmd:
        if not quiet:
            print(f"  [{name}] No start command configured — skipping")
        return False

    # Check if already running
    status = get_daemon_status(daemon)
    if status["alive"]:
        if not quiet:
            print(f"  [{name}] Already running (PID={status['pid']})")
        return True

    if not quiet:
        print(f"  [{name}] Starting...", end=" ", flush=True)

    try:
        # Start as detached background process
        kwargs = {
            "cwd": str(ROOT),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
            )
        proc = subprocess.Popen(start_cmd, **kwargs)

        # Wait briefly for startup
        time.sleep(1.5)

        # Verify it started
        post_status = get_daemon_status(daemon)
        if post_status["alive"]:
            if not quiet:
                print(f"OK (PID={post_status['pid']})")
            return True
        else:
            if not quiet:
                print(f"FAILED (process may have exited immediately)")
            return False
    except (OSError, FileNotFoundError, subprocess.SubprocessError) as e:
        if not quiet:
            print(f"ERROR: {e}")
        return False


def stop_daemon(daemon: dict, quiet: bool = False) -> bool:
    """Stop a daemon gracefully. Returns True if stopped."""
    name = daemon["name"]
    status = get_daemon_status(daemon)

    if not status["alive"]:
        if not quiet:
            print(f"  [{name}] Not running")
        # Clean up stale PID file
        pid_file = daemon.get("pid_file")
        if pid_file:
            p = ROOT / pid_file
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        return True

    pid = status["pid"]
    if not pid:
        if not quiet:
            print(f"  [{name}] Running but no PID — cannot stop")
        return False

    if not quiet:
        print(f"  [{name}] Stopping PID={pid}...", end=" ", flush=True)

    try:
        # Try graceful SIGTERM first
        if sys.platform == "win32":
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)

        # Wait for graceful shutdown
        for _ in range(10):
            time.sleep(0.5)
            if not _pid_alive(pid):
                break

        if _pid_alive(pid):
            # Force kill if still alive
            try:
                if sys.platform == "win32":
                    import ctypes
                    handle = ctypes.windll.kernel32.OpenProcess(1, False, pid)
                    if handle:
                        ctypes.windll.kernel32.TerminateProcess(handle, 1)
                        ctypes.windll.kernel32.CloseHandle(handle)
                else:
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            time.sleep(0.5)

        # Clean up PID file
        pid_file = daemon.get("pid_file")
        if pid_file:
            p = ROOT / pid_file
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

        stopped = not _pid_alive(pid)
        if not quiet:
            print("OK" if stopped else "FAILED")
        return stopped
    except OSError as e:
        if not quiet:
            print(f"ERROR: {e}")
        return False


def restart_daemon(daemon: dict, quiet: bool = False) -> bool:
    """Stop then start a daemon."""
    stop_daemon(daemon, quiet=quiet)
    time.sleep(1)
    return start_daemon(daemon, quiet=quiet)


def start_all(quiet: bool = False) -> dict:
    """Start all daemons in dependency order (tier 0 → tier 5)."""
    results = {}
    tiers = sorted(set(d["tier"] for d in DAEMON_REGISTRY))
    for tier in tiers:
        tier_daemons = [d for d in DAEMON_REGISTRY if d["tier"] == tier]
        if not quiet:
            print(f"\n── Tier {tier} ──")
        for daemon in tier_daemons:
            ok = start_daemon(daemon, quiet=quiet)
            results[daemon["name"]] = ok
        # Brief pause between tiers for dependencies to initialize
        if tier < max(tiers):
            time.sleep(1)
    return results


def stop_all(quiet: bool = False) -> dict:
    """Stop all daemons in reverse dependency order (tier 5 → tier 0)."""
    results = {}
    tiers = sorted(set(d["tier"] for d in DAEMON_REGISTRY), reverse=True)
    for tier in tiers:
        tier_daemons = [d for d in DAEMON_REGISTRY if d["tier"] == tier]
        if not quiet:
            print(f"\n── Tier {tier} (stopping) ──")
        for daemon in reversed(tier_daemons):
            ok = stop_daemon(daemon, quiet=quiet)
            results[daemon["name"]] = ok
    return results


def health_check(auto_restart: bool = True, quiet: bool = False) -> list:
    """Run health checks on all daemons. Optionally auto-restart dead ones.

    Returns list of daemons that were found dead.
    """
    dead_daemons = []
    all_status = get_all_status()

    for status in all_status:
        if status["status"] == "RUNNING":
            continue
        if status["status"] == "STOPPED":
            daemon = next((d for d in DAEMON_REGISTRY if d["name"] == status["name"]), None)
            if not daemon or not daemon.get("start_cmd"):
                continue
            dead_daemons.append(status)
            if auto_restart:
                if not quiet:
                    print(f"  [{status['name']}] DEAD — auto-restarting...")
                start_daemon(daemon, quiet=quiet)
        elif status["status"] == "UNHEALTHY":
            dead_daemons.append(status)
            if not quiet:
                print(f"  [{status['name']}] UNHEALTHY — health check failed")

    return dead_daemons


def find_daemon(name: str) -> dict:
    """Find a daemon by name (case-insensitive, partial match)."""
    name_lower = name.lower().replace("-", "_")
    # Exact match first
    for d in DAEMON_REGISTRY:
        if d["name"].lower() == name_lower:
            return d
    # Partial match
    for d in DAEMON_REGISTRY:
        if name_lower in d["name"].lower():
            return d
    return None


def print_status_table(statuses: list, use_json: bool = False):
    """Print a formatted status table."""
    if use_json:
        print(json.dumps(statuses, indent=2))
        return

    print(f"\n{'Name':<28s} {'Status':<12s} {'PID':<8s} {'Tier':<5s} {'Crit':<10s} {'Port':<6s} {'Health':<8s}")
    print("-" * 85)
    for s in statuses:
        pid_str = str(s["pid"]) if s["pid"] else "—"
        port_str = "—"
        if s["port_open"] is True:
            port_str = "open"
        elif s["port_open"] is False:
            port_str = "closed"
        health_str = "—"
        if s["health_ok"] is True:
            health_str = "OK"
        elif s["health_ok"] is False:
            health_str = "FAIL"

        status_icon = {"RUNNING": "+", "STOPPED": "x", "UNHEALTHY": "!", "PID_STALE": "?"}.get(
            s["status"], "?"
        )
        print(
            f"  {status_icon} {s['name']:<25s} {s['status']:<12s} {pid_str:<8s} "
            f"T{s['tier']:<4d} {s['criticality']:<10s} {port_str:<6s} {health_str:<8s}"
        )

    running = sum(1 for s in statuses if s["status"] == "RUNNING")
    total = len(statuses)
    print(f"\n  {running}/{total} daemons running")


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Skynet Daemon Manager — unified lifecycle management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    parser.add_argument(
        "command",
        choices=["status", "start-all", "stop-all", "start", "stop", "restart", "health"],
        help="Action to perform",
    )
    parser.add_argument("daemon", nargs="?", help="Daemon name (for start/stop/restart)")
    args = parser.parse_args()

    if args.command == "status":
        statuses = get_all_status()
        print_status_table(statuses, use_json=args.json)

    elif args.command == "start-all":
        print("Starting all Skynet daemons in dependency order...")
        results = start_all()
        started = sum(1 for v in results.values() if v)
        print(f"\n  Started {started}/{len(results)} daemons")
        if args.json:
            print(json.dumps(results, indent=2))

    elif args.command == "stop-all":
        print("Stopping all Skynet daemons in reverse order...")
        results = stop_all()
        stopped = sum(1 for v in results.values() if v)
        print(f"\n  Stopped {stopped}/{len(results)} daemons")
        if args.json:
            print(json.dumps(results, indent=2))

    elif args.command in ("start", "stop", "restart"):
        if not args.daemon:
            print(f"Error: '{args.command}' requires a daemon name")
            sys.exit(1)
        daemon = find_daemon(args.daemon)
        if not daemon:
            print(f"Error: daemon '{args.daemon}' not found")
            print(f"Available: {', '.join(d['name'] for d in DAEMON_REGISTRY)}")
            sys.exit(1)
        if args.command == "start":
            start_daemon(daemon)
        elif args.command == "stop":
            stop_daemon(daemon)
        elif args.command == "restart":
            restart_daemon(daemon)

    elif args.command == "health":
        print("Running health checks...")
        dead = health_check(auto_restart=True)
        if dead:
            print(f"\n  {len(dead)} daemon(s) were dead/unhealthy")
            if args.json:
                print(json.dumps(dead, indent=2))
        else:
            print("  All daemons healthy")


if __name__ == "__main__":
    main()
