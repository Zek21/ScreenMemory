#!/usr/bin/env python3
"""
skynet_process_guard.py — Code-level process kill prevention for Skynet.

Maintains a live registry of critical processes in data/critical_processes.json.
Provides safe_kill() that REFUSES to terminate protected processes.
Auto-refreshes on import so the registry is always current.

Usage:
    from skynet_process_guard import safe_kill, is_protected, refresh_registry

    # Before any process termination:
    if safe_kill(pid):
        os.kill(pid, signal.SIGTERM)  # allowed
    else:
        pass  # BLOCKED — pid is protected

    # Quick check:
    if is_protected(pid):
        print("Cannot kill — protected Skynet process")

    # Manual refresh:
    refresh_registry()

CLI:
    python skynet_process_guard.py refresh     # rebuild registry
    python skynet_process_guard.py list        # show all protected
    python skynet_process_guard.py check <pid> # check if protected
"""

import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import psutil
except Exception:
    psutil = None

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REGISTRY_FILE = DATA_DIR / "critical_processes.json"
BUS_URL = "http://localhost:8420/bus/publish"

# Win32 for HWND → PID resolution
user32 = ctypes.windll.user32


def _hidden_subprocess_kwargs(**kwargs):
    merged = dict(kwargs)
    if sys.platform == "win32":
        merged["creationflags"] = merged.get("creationflags", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = merged.get("startupinfo")
        if startupinfo is None and hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            merged["startupinfo"] = startupinfo
    return merged


def _hidden_check_output(args, **kwargs):
    return subprocess.check_output(args, **_hidden_subprocess_kwargs(**kwargs))


def _hwnd_to_pid(hwnd):
    """Get the process ID that owns a window handle."""
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    return pid.value


def _find_python_processes(script_name):
    """Find PIDs of python processes running a specific script."""
    pids = []
    # Try Get-CimInstance (works on modern Windows where wmic is deprecated)
    try:
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" "
            "| ForEach-Object { $_.ProcessId.ToString() + '|' + $_.CommandLine } "
        )
        out = _hidden_check_output(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            text=True, timeout=15, stderr=subprocess.DEVNULL
        )
        for line in out.strip().split("\n"):
            line = line.strip()
            if not line or "|" not in line:
                continue
            pid_str, cmdline = line.split("|", 1)
            if script_name.lower() in cmdline.lower():
                try:
                    pids.append(int(pid_str.strip()))
                except ValueError:
                    pass
        if pids:
            return _collapse_wrapper_pids(pids)
    except Exception:
        pass
    # Fallback: wmic (legacy)
    try:
        out = _hidden_check_output(
            ["wmic", "process", "where",
             "Name like '%python%'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            text=True, timeout=10, stderr=subprocess.DEVNULL
        )
        for line in out.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("Node"):
                continue
            parts = line.split(",")
            if len(parts) >= 3:
                cmdline = ",".join(parts[1:-1])
                pid_str = parts[-1].strip()
                if script_name.lower() in cmdline.lower():
                    try:
                        pids.append(int(pid_str))
                    except ValueError:
                        pass
    except Exception:
        pass
    return _collapse_wrapper_pids(pids)


def _collapse_wrapper_pids(pids):
    """Drop virtualenv launcher parents when a child interpreter runs the same command."""
    unique = []
    seen = set()
    for pid in pids:
        try:
            pid_i = int(pid)
        except Exception:
            continue
        if pid_i > 0 and pid_i not in seen:
            seen.add(pid_i)
            unique.append(pid_i)
    if psutil is None or not unique:
        return unique

    dropped = set()
    for pid in unique:
        if pid in dropped:
            continue
        try:
            parent = psutil.Process(pid)
            parent_cmd = tuple(parent.cmdline())
            parent_exe = (parent.exe() or "").lower()
            children = parent.children(recursive=False)
        except Exception:
            continue
        if not parent_cmd:
            continue
        for child in children:
            if child.pid not in seen:
                continue
            try:
                child_cmd = tuple(child.cmdline())
                child_exe = (child.exe() or "").lower()
            except Exception:
                continue
            if child_cmd != parent_cmd:
                continue
            if child_exe == parent_exe:
                continue
            dropped.add(pid)
            break

    return [pid for pid in unique if pid not in dropped]


def _find_process_by_name(exe_name):
    """Find PIDs of processes with a given executable name."""
    pids = []
    try:
        out = _hidden_check_output(
            ["tasklist", "/fi", f"imagename eq {exe_name}", "/fo", "csv", "/nh"],
            text=True, timeout=10, stderr=subprocess.DEVNULL
        )
        for line in out.strip().split("\n"):
            line = line.strip().strip('"')
            if not line:
                continue
            parts = line.split('","')
            if len(parts) >= 2:
                try:
                    pids.append(int(parts[1].strip('"')))
                except ValueError:
                    pass
    except Exception:
        pass
    return pids


def _scan_named_processes():
    """Scan for backend and daemon processes by name/script."""
    processes = []
    for pid in _find_process_by_name("skynet.exe"):
        processes.append({"pid": pid, "name": "skynet.exe", "role": "backend", "protected": True})

    _SCRIPT_ROLES = [
        ("god_console", "god_console.py", "god_console"),
        ("skynet_sse_daemon", "skynet_sse_daemon.py", "sse_daemon"),
        ("skynet_monitor", "skynet_monitor.py", "monitor"),
        ("skynet_overseer", "skynet_overseer.py", "overseer"),
    ]
    for script, name, role in _SCRIPT_ROLES:
        for pid in _find_python_processes(script):
            processes.append({"pid": pid, "name": name, "role": role, "protected": True})
    return processes


def _scan_watchdog_processes():
    """Scan for watchdog via PID file and command line."""
    processes = []
    pid_file = DATA_DIR / "watchdog.pid"
    if pid_file.exists():
        try:
            wpid = int(pid_file.read_text().strip())
            out = _hidden_check_output(
                ["tasklist", "/fi", f"pid eq {wpid}", "/fo", "csv", "/nh"],
                text=True, timeout=5, stderr=subprocess.DEVNULL
            )
            if str(wpid) in out:
                processes.append({"pid": wpid, "name": "skynet_watchdog.py",
                                  "role": "watchdog", "protected": True})
        except Exception:
            pass
    for pid in _find_python_processes("skynet_watchdog"):
        if not any(p["pid"] == pid for p in processes):
            processes.append({"pid": pid, "name": "skynet_watchdog.py",
                              "role": "watchdog", "protected": True})
    return processes


def _scan_hwnd_processes():
    """Scan worker HWNDs and orchestrator from JSON files."""
    processes = []
    workers_file = DATA_DIR / "workers.json"
    if workers_file.exists():
        try:
            wdata = json.loads(workers_file.read_text(encoding="utf-8"))
            workers = wdata.get("workers", [])
            if isinstance(workers, list):
                for w in workers:
                    hwnd = w.get("hwnd", 0)
                    pid = _hwnd_to_pid(hwnd) if hwnd else 0
                    processes.append({"pid": pid, "hwnd": hwnd, "name": w.get("name", "?"),
                                      "role": "worker", "protected": True})
        except Exception:
            pass

    orch_file = DATA_DIR / "orchestrator.json"
    if orch_file.exists():
        try:
            odata = json.loads(orch_file.read_text(encoding="utf-8"))
            hwnd = odata.get("orchestrator_hwnd", odata.get("hwnd", 0))
            pid = _hwnd_to_pid(hwnd) if hwnd else odata.get("pid", 0)
            processes.append({"pid": pid, "hwnd": hwnd, "name": "orchestrator",
                              "role": "orchestrator", "protected": True})
        except Exception:
            pass
    return processes


def _deduplicate_processes(processes):
    """Deduplicate by (PID, role) pair."""
    seen = set()
    deduped = []
    for p in processes:
        pid = p.get("pid", 0)
        role = p.get("role", "?")
        key = (pid, role)
        if pid and key in seen:
            continue
        if pid:
            seen.add(key)
        deduped.append(p)
    return deduped


def refresh_registry():
    """Scan all running Skynet processes and write critical_processes.json."""
    processes = []
    processes.extend(_scan_named_processes())
    processes.extend(_scan_watchdog_processes())
    processes.extend(_scan_hwnd_processes())

    deduped = _deduplicate_processes(processes)

    registry = {
        "description": "Critical Skynet processes -- NEVER kill these",
        "protected_names": [
            "skynet.exe", "god_console.py", "skynet_watchdog.py",
            "skynet_sse_daemon.py", "skynet_monitor.py", "skynet_overseer.py"
        ],
        "processes": deduped,
        "process_count": len(deduped),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "updated_by": "skynet_process_guard.refresh_registry()"
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    return registry


def _load_registry():
    """Load the current registry from disk."""
    if REGISTRY_FILE.exists():
        try:
            return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"protected_names": [], "processes": []}


def is_protected(pid=None, name=None):
    """Check if a PID or process name is in the protected list.

    Returns: (bool, str) — (is_protected, reason)
    """
    reg = _load_registry()

    # Check by name against protected_names
    if name:
        name_lower = name.lower()
        for pn in reg.get("protected_names", []):
            if pn.lower() in name_lower or name_lower in pn.lower():
                return True, f"Protected service: {pn}"

    # Check by PID or HWND against registered processes
    if pid:
        for proc in reg.get("processes", []):
            if proc.get("pid") == pid:
                return True, f"Protected {proc['role']}: {proc['name']} (PID {pid})"
            if proc.get("hwnd") == pid:
                return True, f"Protected {proc['role']}: {proc['name']} (HWND {pid})"

    return False, ""


def safe_kill(pid, caller="unknown"):
    """Check if it's safe to kill a PID. Returns True if ALLOWED.

    If the PID is protected:
      - Prints a red warning to stderr
      - Posts an alert to the Skynet bus
      - Returns False (kill BLOCKED)

    If the PID is not protected:
      - Logs the kill attempt
      - Returns True (kill allowed)
    """
    protected, reason = is_protected(pid=pid)
    if protected:
        msg = f"KILL BLOCKED: {caller} attempted to kill PID {pid}. {reason}"
        print(f"\033[91m[PROCESS GUARD] {msg}\033[0m", file=sys.stderr)
        _post_alert(msg)
        return False

    # Not protected — log and allow
    print(f"[PROCESS GUARD] Kill allowed: PID {pid} by {caller}", file=sys.stderr)
    return True


def safe_kill_by_name(name, caller="unknown"):
    """Check if it's safe to kill a process by name. Returns True if ALLOWED."""
    protected, reason = is_protected(name=name)
    if protected:
        msg = f"KILL BLOCKED: {caller} attempted to kill '{name}'. {reason}"
        print(f"\033[91m[PROCESS GUARD] {msg}\033[0m", file=sys.stderr)
        _post_alert(msg)
        return False
    return True


def _post_alert(message):
    """Post a protection alert to the Skynet bus."""
    try:
        import urllib.request
        payload = json.dumps({
            "sender": "process_guard",
            "topic": "orchestrator",
            "type": "alert",
            "content": message,
        }).encode()
        req = urllib.request.Request(BUS_URL, payload, {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass


# Auto-refresh on import (keeps registry current)
try:
    refresh_registry()
except Exception:
    pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skynet Process Guard")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("refresh", help="Rebuild the protected process registry")
    sub.add_parser("list", help="Show all protected processes")

    p_chk = sub.add_parser("check", help="Check if a PID is protected")
    p_chk.add_argument("pid", type=int)

    args = parser.parse_args()

    if args.cmd == "refresh":
        reg = refresh_registry()
        print(f"Registry refreshed: {reg['process_count']} protected processes")
        for p in reg["processes"]:
            print(f"  PID {p.get('pid', '?'):>8}  {p['role']:>12}  {p['name']}")
    elif args.cmd == "list":
        reg = _load_registry()
        print(f"Protected processes ({len(reg.get('processes', []))}):")
        print(f"  Updated: {reg.get('updated_at', '?')}")
        for p in reg.get("processes", []):
            hwnd = f"  HWND {p['hwnd']}" if p.get("hwnd") else ""
            print(f"  PID {p.get('pid', '?'):>8}  {p.get('role', '?'):>12}  {p.get('name', '?')}{hwnd}")
    elif args.cmd == "check":
        ok, reason = is_protected(pid=args.pid)
        if ok:
            print(f"PROTECTED: {reason}")
        else:
            print(f"NOT PROTECTED: PID {args.pid} can be terminated")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
