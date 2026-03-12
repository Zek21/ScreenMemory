"""
skynet_pid_guard.py -- Shared atomic PID guard for all Skynet daemons.

Provides a single reusable function that handles:
  1. Atomic PID file creation via os.O_CREAT|os.O_EXCL (race-free)
  2. Stale PID detection: checks alive AND matches process name via psutil
  3. One retry after unlinking stale PID file
  4. Automatic cleanup via atexit + SIGTERM/SIGBREAK signal handlers

Usage:
    from tools.skynet_pid_guard import acquire_pid_guard

    if not acquire_pid_guard("data/self_prompt.pid", "skynet_self_prompt"):
        sys.exit(1)  # another instance is running

    # ... daemon main loop ...
    # cleanup is automatic on exit/signal

Extracted from tools/skynet_monitor.py _acquire_monitor_pid_guard.
"""
# signed: alpha

import atexit
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Registry of active guards for cleanup
_active_guards: list[Path] = []


def _pid_alive(pid: int) -> bool:
    """Check if a process with given PID exists (cross-platform)."""
    try:
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False
    # signed: alpha


def _pid_matches_daemon(pid: int, daemon_name: str) -> bool:
    """Check if the PID belongs to a Python process running the named daemon.

    Uses psutil to inspect the command line of the running process.
    ``daemon_name`` is matched case-insensitively against the joined
    command-line string (with backslashes normalised to forward slashes).
    """
    try:
        import psutil
        proc = psutil.Process(pid)
        cmd = " ".join(proc.cmdline() or []).replace("\\", "/").lower()
        name = str(proc.name() or "").lower()
        return "python" in name and daemon_name.lower() in cmd
    except Exception:
        return False
    # signed: alpha


def _owned_by_current_process(pid_path: Path) -> bool:
    """Return True if the PID file contains THIS process's PID."""
    try:
        return pid_path.exists() and int(pid_path.read_text().strip()) == os.getpid()
    except Exception:
        return False
    # signed: alpha


def _cleanup_pid_file(pid_path: Path) -> None:
    """Remove PID file only if we own it."""
    try:
        if _owned_by_current_process(pid_path):
            pid_path.unlink(missing_ok=True)
    except Exception:
        pass
    # signed: alpha


def _register_cleanup(pid_path: Path) -> None:
    """Register atexit + signal handlers to clean up the PID file.

    Signal handlers set a flag and re-raise so existing daemon shutdown
    logic (KeyboardInterrupt / finally blocks) still executes.
    """
    # atexit -- runs on normal interpreter exit
    atexit.register(_cleanup_pid_file, pid_path)

    # Build a signal handler that cleans up then re-raises
    _prev_sigterm = signal.getsignal(signal.SIGTERM)

    def _sigterm_handler(signum, frame):
        _cleanup_pid_file(pid_path)
        # Chain to previous handler if it was a callable
        if callable(_prev_sigterm) and _prev_sigterm not in (signal.SIG_IGN, signal.SIG_DFL):
            _prev_sigterm(signum, frame)
        else:
            raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    # SIGBREAK on Windows (Ctrl+Break)
    try:
        _prev_sigbreak = signal.getsignal(signal.SIGBREAK)

        def _sigbreak_handler(signum, frame):
            _cleanup_pid_file(pid_path)
            if callable(_prev_sigbreak) and _prev_sigbreak not in (signal.SIG_IGN, signal.SIG_DFL):
                _prev_sigbreak(signum, frame)
            else:
                raise SystemExit(128 + signum)

        signal.signal(signal.SIGBREAK, _sigbreak_handler)
    except (AttributeError, OSError):
        pass  # SIGBREAK only available on Windows

    _active_guards.append(pid_path)
    # signed: alpha


def acquire_pid_guard(pid_file: str | Path, daemon_name: str,
                      *, logger=None) -> bool:
    """Atomically acquire a PID guard file for a daemon.

    Parameters
    ----------
    pid_file : str | Path
        Path to the PID file (absolute or relative to repo root).
        Parent directories are created automatically.
    daemon_name : str
        Identifier matched against running process command lines to
        distinguish this daemon from recycled PIDs.  Typically the
        script filename without extension, e.g. ``"skynet_watchdog"``.
    logger : callable, optional
        ``logger(msg, level)`` used for status messages.  Falls back
        to ``print`` when *None*.

    Returns
    -------
    bool
        *True* if the guard was acquired (caller should proceed).
        *False* if another live instance already holds the lock.
    """
    pid_path = Path(pid_file)
    if not pid_path.is_absolute():
        pid_path = ROOT / pid_path

    def _log(msg, level="INFO"):
        if logger:
            try:
                logger(msg, level)
            except TypeError:
                logger(msg)
        else:
            print(f"[PID_GUARD] {msg}")

    pid_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(2):
        try:
            fd = os.open(str(pid_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            _register_cleanup(pid_path)
            return True
        except FileExistsError:
            # PID file already exists -- check if the holder is still alive
            try:
                old_pid = int(pid_path.read_text().strip())
            except Exception:
                old_pid = 0

            if old_pid and _pid_alive(old_pid) and _pid_matches_daemon(old_pid, daemon_name):
                _log(f"{daemon_name} already running (PID {old_pid}) -- exiting to prevent duplicate", "WARN")
                return False

            # Stale PID file -- remove and retry once
            try:
                pid_path.unlink(missing_ok=True)
            except Exception:
                _log(f"Cannot unlink stale PID file {pid_path}", "ERROR")
                return False

            time.sleep(0.05)

    _log(f"Failed to acquire PID guard after 2 attempts for {daemon_name}", "ERROR")
    return False
    # signed: alpha


def release_pid_guard(pid_file: str | Path) -> None:
    """Manually release a PID guard (for explicit shutdown paths).

    Safe to call multiple times; no-ops if already released or not owned.
    """
    pid_path = Path(pid_file)
    if not pid_path.is_absolute():
        pid_path = ROOT / pid_path
    _cleanup_pid_file(pid_path)
    # signed: alpha
