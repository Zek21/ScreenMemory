#!/usr/bin/env python3
"""
skynet_daemon_ctrl.py -- Unified daemon lifecycle controller for Skynet.

Manages all background daemons: start, stop, status, restart.
Reports lifecycle events to the Skynet bus.

Usage:
    python tools/skynet_daemon_ctrl.py status              # Show all daemon states
    python tools/skynet_daemon_ctrl.py stop-all            # Gracefully stop all daemons
    python tools/skynet_daemon_ctrl.py stop <name>         # Stop a specific daemon
    python tools/skynet_daemon_ctrl.py start <name>        # Start a specific daemon
    python tools/skynet_daemon_ctrl.py restart <name>      # Restart a specific daemon
    python tools/skynet_daemon_ctrl.py health              # Quick health summary
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SKYNET_URL = "http://localhost:8420"


def _resolve_real_python():
    """Return (real_python_path, env_dict) to avoid venv launcher duplicate processes."""
    venv_dir = ROOT.parent / "env"
    cfg = venv_dir / "pyvenv.cfg"
    base_python = None
    if cfg.exists():
        for line in cfg.read_text().splitlines():
            if line.strip().startswith("executable"):
                _, _, val = line.partition("=")
                candidate = val.strip()
                if Path(candidate).exists():
                    base_python = candidate
                    break
    if not base_python:
        base_python = str(Path(sys.executable))
    env = os.environ.copy()
    site_packages = str(venv_dir / "Lib" / "site-packages")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{site_packages};{existing}" if existing else site_packages
    env["VIRTUAL_ENV"] = str(venv_dir)
    return base_python, env


PYTHON, DAEMON_ENV = _resolve_real_python()
BACKGROUND_FLAGS = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS


def _python_cmd(script: str, *args: str) -> list[str]:
    return [PYTHON, str(ROOT / "tools" / script), *args]


# Registry of all known daemons with PID file locations and start commands
DAEMONS = {
    "monitor": {
        "pid_file": DATA_DIR / "monitor.pid",
        "start_cmd": _python_cmd("skynet_monitor.py"),
        "description": "Worker window health monitor",
    },
    "watchdog": {
        "pid_file": DATA_DIR / "watchdog.pid",
        "start_cmd": _python_cmd("skynet_watchdog.py", "start"),
        "description": "Service health watchdog",
    },
    "self_prompt": {
        "pid_file": DATA_DIR / "self_prompt.pid",
        "start_cmd": _python_cmd("skynet_self_prompt.py", "start"),
        "description": "Orchestrator heartbeat daemon",
    },
    "self_improve": {
        "pid_file": DATA_DIR / "self_improve.pid",
        "start_cmd": _python_cmd("skynet_self_improve.py", "start"),
        "description": "Self-improvement scanner",
    },
    "bus_relay": {
        "pid_file": DATA_DIR / "bus_relay.pid",
        "start_cmd": _python_cmd("skynet_bus_relay.py"),
        "description": "Bus message relay to workers",
    },
    "sse_daemon": {
        "pid_file": DATA_DIR / "sse_daemon.pid",
        "start_cmd": _python_cmd("skynet_sse_daemon.py"),
        "description": "SSE real-time state streamer",
    },
}


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def _bus_post(msg: str, msg_type: str = "lifecycle"):
    """Post a lifecycle event to the Skynet bus."""
    try:
        data = json.dumps({
            "sender": "daemon_ctrl",
            "topic": "orchestrator",
            "type": msg_type,
            "content": msg
        }).encode()
        req = urllib.request.Request(
            f"{SKYNET_URL}/bus/publish", data=data,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass


def _is_alive(pid: int) -> bool:
    """Check if a process with given PID is alive (Windows-compatible)."""
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def _get_daemon_pid(name: str) -> int | None:
    """Read the PID from a daemon's PID file. Returns None if not found or stale."""
    info = DAEMONS.get(name)
    if not info:
        return None
    pid_file = info["pid_file"]
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
        return pid if _is_alive(pid) else None
    except (ValueError, OSError):
        return None


def _get_process_uptime(pid: int) -> float | None:
    """Get process uptime in seconds via Win32."""
    try:
        import subprocess
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).StartTime.ToString('o')"],
            capture_output=True, text=True, timeout=5
        )
        if result.stdout.strip():
            start = datetime.fromisoformat(result.stdout.strip())
            return (datetime.now() - start).total_seconds()
    except Exception:
        pass
    return None


def status_all():
    """Print status of all known daemons."""
    print(f"\n[{_ts()}] === Skynet Daemon Status ===\n")
    print(f"{'Daemon':<15} {'PID':>7} {'Status':<12} {'Uptime':<12} {'Description'}")
    print("-" * 75)

    summary = {}
    for name, info in DAEMONS.items():
        pid = _get_daemon_pid(name)
        if pid:
            uptime = _get_process_uptime(pid)
            uptime_str = f"{int(uptime)}s" if uptime else "?"
            status = "RUNNING"
            summary[name] = {"pid": pid, "status": "running", "uptime_s": uptime}
        else:
            uptime_str = "-"
            status = "STOPPED"
            # Check if PID file exists but process is dead (stale)
            if info["pid_file"].exists():
                status = "STALE_PID"
            summary[name] = {"pid": None, "status": "stopped"}

        print(f"{name:<15} {str(pid or '-'):>7} {status:<12} {uptime_str:<12} {info['description']}")

    running = sum(1 for v in summary.values() if v["status"] == "running")
    print(f"\n{running}/{len(DAEMONS)} daemons running")
    return summary


def stop_daemon(name: str, report: bool = True):
    """Gracefully stop a daemon by terminating its process."""
    pid = _get_daemon_pid(name)
    if not pid:
        print(f"[{_ts()}] {name}: not running")
        return False

    print(f"[{_ts()}] {name}: stopping PID {pid}...")
    try:
        import ctypes
        PROCESS_TERMINATE = 0x0001
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
            ctypes.windll.kernel32.CloseHandle(handle)
        # Wait for exit
        for _ in range(10):
            time.sleep(0.5)
            if not _is_alive(pid):
                break
    except Exception:
        pass

    # Clean up PID file
    info = DAEMONS.get(name, {})
    pid_file = info.get("pid_file")
    if pid_file and pid_file.exists():
        try:
            pid_file.unlink()
        except Exception:
            pass

    alive = _is_alive(pid)
    status = "FAILED" if alive else "stopped"
    print(f"[{_ts()}] {name}: {status}")
    if report:
        _bus_post(f"Daemon {name} (PID {pid}) {status}")
    return not alive


def stop_all():
    """Stop all running daemons."""
    print(f"[{_ts()}] Stopping all daemons...")
    _bus_post("Daemon controller: stopping all daemons")
    results = {}
    for name in DAEMONS:
        results[name] = stop_daemon(name, report=False)
    stopped = sum(1 for v in results.values() if v)
    _bus_post(f"All daemons stop complete: {stopped}/{len(results)} stopped")
    print(f"\n[{_ts()}] {stopped}/{len(results)} daemons stopped")


def start_daemon(name: str):
    """Start a daemon if not already running."""
    info = DAEMONS.get(name)
    if not info:
        print(f"[{_ts()}] Unknown daemon: {name}")
        return False

    pid = _get_daemon_pid(name)
    if pid:
        print(f"[{_ts()}] {name}: already running (PID {pid})")
        return True

    print(f"[{_ts()}] {name}: starting...")
    try:
        proc = subprocess.Popen(
            info["start_cmd"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=DAEMON_ENV,
            creationflags=BACKGROUND_FLAGS,
            cwd=str(ROOT),
        )
        time.sleep(1)
        if proc.poll() is None:
            print(f"[{_ts()}] {name}: started (PID {proc.pid})")
            _bus_post(f"Daemon {name} started (PID {proc.pid})")
            return True
        else:
            print(f"[{_ts()}] {name}: failed to start (exited immediately)")
            return False
    except Exception as e:
        print(f"[{_ts()}] {name}: start failed: {e}")
        return False


def restart_daemon(name: str):
    """Stop then start a daemon."""
    stop_daemon(name)
    time.sleep(1)
    return start_daemon(name)


def health_summary():
    """Quick one-line health summary suitable for bus reporting."""
    parts = []
    for name in DAEMONS:
        pid = _get_daemon_pid(name)
        parts.append(f"{name}={'UP' if pid else 'DOWN'}")
    summary = " | ".join(parts)
    print(f"[{_ts()}] {summary}")
    _bus_post(f"Daemon health: {summary}", "heartbeat")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Skynet Daemon Lifecycle Controller")
    parser.add_argument("action", choices=["status", "stop-all", "stop", "start", "restart", "health"],
                        help="Action to perform")
    parser.add_argument("daemon", nargs="?", help="Daemon name (for stop/start/restart)")
    args = parser.parse_args()

    if args.action == "status":
        status_all()
    elif args.action == "stop-all":
        stop_all()
    elif args.action == "stop":
        if not args.daemon:
            print("Error: daemon name required for 'stop'")
            sys.exit(1)
        stop_daemon(args.daemon)
    elif args.action == "start":
        if not args.daemon:
            print("Error: daemon name required for 'start'")
            sys.exit(1)
        start_daemon(args.daemon)
    elif args.action == "restart":
        if not args.daemon:
            print("Error: daemon name required for 'restart'")
            sys.exit(1)
        restart_daemon(args.daemon)
    elif args.action == "health":
        health_summary()


if __name__ == "__main__":
    main()
