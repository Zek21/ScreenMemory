"""
Shared daemon PID utilities for Skynet.

Provides write_pid, check_pid, cleanup_pid, and ensure_singleton
so every daemon uses consistent PID file management.
Prevents duplicate daemon instances (INCIDENT 003 root cause).

Architecture reference: docs/DAEMON_ARCHITECTURE.md (Section 7: PID Management)

Usage:
    from tools.skynet_daemon_utils import write_pid, cleanup_pid, ensure_singleton

    # In daemon main():
    if not ensure_singleton("monitor"):
        print("Already running"); return
    write_pid("monitor")     # writes data/monitor.pid + registers atexit cleanup
    try:
        run_daemon_loop()
    finally:
        cleanup_pid("monitor")

# signed: beta
"""

import os
import atexit
import signal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def _pid_path(daemon_name: str) -> Path:
    """Return the PID file path for a given daemon name."""
    return DATA_DIR / f"{daemon_name}.pid"
    # signed: beta


def write_pid(daemon_name: str) -> Path:
    """Write current process PID to data/{daemon_name}.pid and register atexit cleanup.

    Returns the PID file path.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    pid_file = _pid_path(daemon_name)
    pid_file.write_text(str(os.getpid()))

    # Register atexit cleanup with ownership check
    def _atexit_cleanup():
        try:
            if pid_file.exists() and int(pid_file.read_text().strip()) == os.getpid():
                pid_file.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(_atexit_cleanup)
    return pid_file
    # signed: beta


def check_pid(daemon_name: str) -> bool:
    """Check if a daemon is running: PID file exists AND process is alive.

    Returns True if the daemon appears to be running, False otherwise.
    Stale PID files (process dead) are cleaned up automatically.
    """
    pid_file = _pid_path(daemon_name)
    if not pid_file.exists():
        return False

    try:
        old_pid = int(pid_file.read_text().strip())
        if old_pid <= 0:
            pid_file.unlink(missing_ok=True)
            return False
        os.kill(old_pid, 0)  # signal 0 = check if alive
        return True
    except (OSError, ValueError):
        # Process dead or PID file corrupted — clean up stale file
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    # signed: beta


def cleanup_pid(daemon_name: str) -> None:
    """Remove PID file if it belongs to current process. Safe to call multiple times."""
    pid_file = _pid_path(daemon_name)
    try:
        if pid_file.exists():
            stored = int(pid_file.read_text().strip())
            if stored == os.getpid():
                pid_file.unlink(missing_ok=True)
    except Exception:
        # Best effort — don't crash on cleanup failure
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass
    # signed: beta


def ensure_singleton(daemon_name: str) -> bool:
    """Check if another instance of this daemon is running.

    Returns True if it's safe to start (no other instance running).
    Returns False if another instance is already running (caller should exit).

    Also verifies on Windows that the process is actually a Python/daemon process
    rather than a recycled PID.
    """
    pid_file = _pid_path(daemon_name)
    if not pid_file.exists():
        return True

    try:
        old_pid = int(pid_file.read_text().strip())
        if old_pid <= 0:
            pid_file.unlink(missing_ok=True)
            return True
        os.kill(old_pid, 0)  # Check if alive
    except (OSError, ValueError):
        # Process dead — stale PID file, safe to proceed
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        return True

    # PID is alive — verify it's actually a Python process (Windows PID recycling)
    try:
        import subprocess
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {old_pid}\").CommandLine"],
            capture_output=True, text=True, timeout=5
        )
        cmd_line = result.stdout.lower()
        if "python" in cmd_line and daemon_name.replace("_", "") in cmd_line.replace("_", ""):
            return False  # Genuine daemon instance running
        # PID is alive but not this daemon — recycled PID
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        return True
    except Exception:
        # Can't verify — assume it's running to be safe
        return False
    # signed: beta


def register_signal_handlers(shutdown_flag_setter=None):
    """Register SIGTERM and SIGBREAK handlers for graceful daemon shutdown.

    Args:
        shutdown_flag_setter: Optional callable invoked on signal to set a shutdown flag.
                             If None, raises KeyboardInterrupt for default handling.
    """
    def _handler(signum, frame):
        if shutdown_flag_setter:
            shutdown_flag_setter()
        else:
            raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handler)
    try:
        signal.signal(signal.SIGBREAK, _handler)  # Windows Ctrl+Break
    except (AttributeError, OSError):
        pass
    # signed: beta
