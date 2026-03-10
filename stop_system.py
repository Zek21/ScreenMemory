"""
ScreenMemory System Stopper — kills all managed processes and orphans.

Usage: D:\\Prospects\\env\\Scripts\\python.exe stop_system.py
"""
import os
import sys
import json
import time
from pathlib import Path

import psutil

BASE_DIR = Path(__file__).parent
PID_FILE = BASE_DIR / "data" / "pids.json"

TARGET_SCRIPTS = ["dashboard_server.py", "auto_orchestrator.py", "agent_worker.py", "worker_pool.py"]


def kill_pid(pid, name=""):
    """Kill a single PID. Returns True if killed."""
    try:
        proc = psutil.Process(pid)
        proc.kill()
        proc.wait(timeout=5)
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
        return False


def main():
    print("=" * 60)
    print("  ScreenMemory System Stopper")
    print("=" * 60)

    killed_total = 0

    # Step 1: Kill PIDs from pids.json
    print("\n[1/3] Killing processes from PID file...")
    if PID_FILE.exists():
        with open(PID_FILE) as f:
            pids = json.load(f)
        # Collect all integer PIDs from the file (handles flat and nested formats)
        pid_entries = []
        launcher_pids = []
        for name, val in pids.items():
            if isinstance(val, int):
                pid_entries.append((name, val))
            elif isinstance(val, list) and name == "_launchers":
                launcher_pids = val
            elif isinstance(val, dict):
                for sub_name, sub_val in val.items():
                    if isinstance(sub_val, int):
                        pid_entries.append((f"{name}.{sub_name}", sub_val))
                    elif isinstance(sub_val, dict):
                        for k, v in sub_val.items():
                            if isinstance(v, int):
                                pid_entries.append((f"{name}.{sub_name}.{k}", v))
        for name, pid in pid_entries:
            if kill_pid(pid, name):
                print(f"  Killed {name:30s} PID {pid}")
                killed_total += 1
            else:
                print(f"  Skip  {name:30s} PID {pid} (already dead)")
        # Kill launcher (venv wrapper) PIDs
        for pid in launcher_pids:
            if kill_pid(pid, "launcher"):
                killed_total += 1
    else:
        print("  No PID file found")

    # Step 2: Scan for orphan processes
    print("\n[2/3] Scanning for orphan processes...")
    orphans = 0
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
            if any(s in cmdline for s in TARGET_SCRIPTS) and proc.pid != os.getpid():
                proc.kill()
                print(f"  Killed orphan PID {proc.pid}: {cmdline[:80]}")
                orphans += 1
                killed_total += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if orphans == 0:
        print("  No orphan processes found")

    # Give OS time to clean up
    if killed_total > 0:
        time.sleep(1)

    # Step 3: Delete PID file
    print("\n[3/3] Cleaning up PID file...")
    if PID_FILE.exists():
        PID_FILE.unlink()
        print(f"  Deleted {PID_FILE}")
    else:
        print("  No PID file to delete")

    # Summary
    print("\n" + "=" * 60)
    print(f"  Stopped {killed_total} process(es). System is clean.")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
