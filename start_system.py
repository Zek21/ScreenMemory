"""
ScreenMemory System Starter — launches all 6 processes with duplicate prevention.

Usage: D:\\Prospects\\env\\Scripts\\python.exe start_system.py
"""
import os
import sys
import json
import time
import socket
import subprocess
from pathlib import Path

import psutil

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
PID_FILE = DATA_DIR / "pids.json"
PYTHON = r"D:\Prospects\env\Scripts\python.exe"
PORT = 8420

TARGET_SCRIPTS = ["dashboard_server.py", "auto_orchestrator.py", "agent_worker.py", "worker_pool.py"]

CREATION_FLAGS = (
    subprocess.CREATE_NEW_PROCESS_GROUP
    | subprocess.DETACHED_PROCESS
    | subprocess.CREATE_NO_WINDOW
)


def kill_existing():
    """Kill ALL existing dashboard_server/auto_orchestrator/agent_worker processes."""
    killed = 0
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
            if any(s in cmdline for s in TARGET_SCRIPTS) and proc.pid != os.getpid():
                proc.kill()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if killed:
        print(f"  Killed {killed} existing process(es)")
        # Give OS time to release resources
        time.sleep(1)
    else:
        print("  No existing processes found")


def wait_for_port_free(port, timeout=10):
    """Wait until the port is free."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return True
        time.sleep(0.5)
    return False


def start_process(script, args=None):
    """Start a detached Python process that survives parent exit.
    
    On Python 3.13+ Windows, the venv python.exe is a launcher that spawns
    a child process. We detect this and return the real child PID.
    """
    cmd = [PYTHON, str(BASE_DIR / script)]
    if args:
        cmd.extend(args)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        cmd,
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        env=env,
        creationflags=CREATION_FLAGS,
    )
    launcher_pid = proc.pid
    # Wait for venv launcher to spawn the real child process
    time.sleep(0.5)
    try:
        parent = psutil.Process(launcher_pid)
        children = parent.children()
        if children:
            return children[0].pid, launcher_pid
    except psutil.NoSuchProcess:
        pass
    return launcher_pid, None


def verify_pid(pid):
    """Check if a PID is alive."""
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def main():
    print("=" * 60)
    print("  ScreenMemory System Starter")
    print("=" * 60)

    # Step 1: Kill existing processes
    print("\n[1/5] Killing existing processes...")
    kill_existing()

    # Step 2: Wait for port 8420 to be free
    print(f"\n[2/5] Waiting for port {PORT} to be free...")
    if wait_for_port_free(PORT):
        print(f"  Port {PORT} is free")
    else:
        print(f"  WARNING: Port {PORT} still in use after timeout")
        sys.exit(1)

    # Step 3: Start all 6 processes
    print("\n[3/5] Starting processes...")
    pids = {}
    launchers = []

    # Dashboard server
    pid, launcher = start_process("dashboard_server.py")
    pids["dashboard_server"] = pid
    if launcher:
        launchers.append(launcher)
    print(f"  dashboard_server.py  -> PID {pid}")

    # Auto orchestrator
    pid, launcher = start_process("auto_orchestrator.py")
    pids["auto_orchestrator"] = pid
    if launcher:
        launchers.append(launcher)
    print(f"  auto_orchestrator.py -> PID {pid}")

    # 4 pool workers (generic — replaces role-based agent workers)
    for wid in range(4):
        pid, launcher = start_process("worker_pool.py", ["--id", str(wid)])
        pids[f"worker_pool_{wid}"] = pid
        if launcher:
            launchers.append(launcher)
        print(f"  worker_pool.py  {wid}     -> PID {pid}")

    # Step 4: Write PID file
    print(f"\n[4/5] Writing PID file to {PID_FILE}...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    pids["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if launchers:
        pids["_launchers"] = launchers
    with open(PID_FILE, "w") as f:
        json.dump(pids, f, indent=2)
    print("  Done")

    # Step 5: Verify processes after 2 seconds
    print("\n[5/5] Verifying processes (waiting 2s)...")
    time.sleep(2)

    all_ok = True
    for name, pid in pids.items():
        if name in ("started_at", "_launchers"):
            continue
        alive = verify_pid(pid)
        status = "RUNNING" if alive else "DEAD"
        marker = "OK" if alive else "FAIL"
        print(f"  [{marker:4s}] {name:25s} PID {pid:6d}  {status}")
        if not alive:
            all_ok = False

    # Summary
    running = sum(1 for k, v in pids.items()
                  if k not in ("started_at", "_launchers") and verify_pid(v))
    print("\n" + "=" * 60)
    if all_ok:
        print(f"  All {running}/6 processes running successfully!")
    else:
        print(f"  WARNING: Only {running}/6 processes running")
    print("=" * 60)

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
