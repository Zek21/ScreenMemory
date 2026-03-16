"""Skynet Supervision Trees for Daemons — P2.13

Erlang/OTP-inspired supervision tree managing parent-child daemon
relationships with automatic restart strategies and circuit breakers.

Restart strategies:
  - one_for_one:  restart only the failed child
  - one_for_all:  restart ALL children if one fails
  - rest_for_one: restart the failed child + all started after it

Circuit breaker:
  MaxRestarts(count, window_seconds) — if a child restarts too many
  times within a window, escalate to the parent supervisor.

Usage:
    from tools.skynet_supervisor import SupervisorTree, ChildSpec
    tree = SupervisorTree.default_tree()
    tree.start_all()
    tree.monitor()  # blocking health loop

CLI:
    python tools/skynet_supervisor.py start          # start all supervised daemons
    python tools/skynet_supervisor.py stop            # stop all supervised daemons
    python tools/skynet_supervisor.py status          # show health of all children
    python tools/skynet_supervisor.py tree            # show tree structure
    python tools/skynet_supervisor.py restart NAME    # restart a specific child
"""
# signed: gamma

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

DATA_DIR = _REPO / "data"
SUPERVISOR_STATE_PATH = DATA_DIR / "supervisor_state.json"

# ── Constants ────────────────────────────────────────────────────────
# signed: gamma

DEFAULT_CHECK_INTERVAL_S = 10
DEFAULT_SHUTDOWN_TIMEOUT_S = 10
DEFAULT_MAX_RESTARTS = 5
DEFAULT_MAX_RESTART_WINDOW_S = 300  # 5 minutes
STARTUP_GRACE_S = 3  # wait after spawning before health check


# ── Enums ────────────────────────────────────────────────────────────

class RestartStrategy(Enum):
    """How the supervisor responds when a child fails."""
    ONE_FOR_ONE = "one_for_one"    # restart only the failed child
    ONE_FOR_ALL = "one_for_all"    # restart all children
    REST_FOR_ONE = "rest_for_one"  # restart failed + all started after it


class RestartType(Enum):
    """Whether a child should be restarted on failure."""
    PERMANENT = "permanent"     # always restart
    TEMPORARY = "temporary"     # never restart (one-shot)
    TRANSIENT = "transient"     # restart only on abnormal exit


class ChildStatus(Enum):
    """Current status of a supervised child."""
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    CIRCUIT_OPEN = "circuit_open"  # too many restarts, escalated


# ── Data Structures ─────────────────────────────────────────────────
# signed: gamma

@dataclass
class MaxRestarts:
    """Circuit breaker: max restart count within a time window.

    If a child exceeds `count` restarts within `window_s` seconds,
    the supervisor escalates to its parent (or halts the child).
    """
    count: int = DEFAULT_MAX_RESTARTS
    window_s: float = DEFAULT_MAX_RESTART_WINDOW_S


@dataclass
class ChildSpec:
    """Specification for a supervised child daemon.

    Attributes:
        name:              Unique identifier for this child.
        module_path:       Python script path (relative to repo root).
        args:              Additional CLI arguments.
        restart_type:      permanent / temporary / transient.
        shutdown_timeout:  Seconds to wait for graceful shutdown.
        pid_file:          Path to PID file (relative to repo root).
        health_url:        HTTP URL for health checks (optional).
        port:              Service port (optional, for port-based checks).
        check_interval_s:  Health check interval in seconds.
        max_restarts:      Circuit breaker configuration.
        depends_on:        Names of children that must start first.
        criticality:       CATASTROPHIC / HIGH / MODERATE / LOW.
        is_binary:         True if this is a binary (not Python script).
        binary_path:       Path to binary executable.
        cwd:               Working directory override.
    """
    name: str
    module_path: str = ""
    args: List[str] = field(default_factory=list)
    restart_type: RestartType = RestartType.PERMANENT
    shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_S
    pid_file: str = ""
    health_url: str = ""
    port: int = 0
    check_interval_s: float = DEFAULT_CHECK_INTERVAL_S
    max_restarts: MaxRestarts = field(default_factory=MaxRestarts)
    depends_on: List[str] = field(default_factory=list)
    criticality: str = "MODERATE"
    is_binary: bool = False
    binary_path: str = ""
    cwd: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "module_path": self.module_path,
            "args": self.args,
            "restart_type": self.restart_type.value,
            "shutdown_timeout": self.shutdown_timeout,
            "pid_file": self.pid_file,
            "health_url": self.health_url,
            "port": self.port,
            "check_interval_s": self.check_interval_s,
            "max_restarts": {"count": self.max_restarts.count,
                             "window_s": self.max_restarts.window_s},
            "depends_on": self.depends_on,
            "criticality": self.criticality,
        }


@dataclass
class ChildState:
    """Runtime state of a supervised child."""
    spec: ChildSpec
    status: ChildStatus = ChildStatus.STOPPED
    pid: int = 0
    restart_timestamps: List[float] = field(default_factory=list)
    total_restarts: int = 0
    last_check: float = 0.0
    last_healthy: float = 0.0
    start_time: float = 0.0
    failure_reason: str = ""

    @property
    def uptime_s(self) -> float:
        if self.status == ChildStatus.RUNNING and self.start_time > 0:
            return time.time() - self.start_time
        return 0.0

    def recent_restart_count(self) -> int:
        """Count restarts within the MaxRestarts window."""
        cutoff = time.time() - self.spec.max_restarts.window_s
        return sum(1 for ts in self.restart_timestamps if ts > cutoff)

    def circuit_tripped(self) -> bool:
        """Check if circuit breaker has tripped."""
        return self.recent_restart_count() >= self.spec.max_restarts.count


# ── Process Utilities ────────────────────────────────────────────────
# signed: gamma

def _resolve_python() -> Tuple[str, Dict[str, str]]:
    """Resolve the real Python interpreter, bypassing venv trampoline."""
    venv_dir = _REPO.parent / "env"
    cfg = venv_dir / "pyvenv.cfg"
    base_python = sys.executable
    if cfg.exists():
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("executable"):
                _, _, val = line.partition("=")
                candidate = val.strip()
                if Path(candidate).exists():
                    base_python = candidate
                    break

    env = os.environ.copy()
    site_packages = str(venv_dir / "Lib" / "site-packages")
    env["PYTHONPATH"] = f"{site_packages};{str(_REPO)}"
    env["VIRTUAL_ENV"] = str(venv_dir)
    return base_python, env


_PYTHON, _DAEMON_ENV = _resolve_python()


def _pid_alive(pid: int) -> bool:
    """Check if a process with the given PID exists."""
    if pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _read_pid_file(pid_file: str) -> int:
    """Read PID from a PID file. Returns 0 if file doesn't exist or is invalid."""
    path = _REPO / pid_file if not os.path.isabs(pid_file) else Path(pid_file)
    if not path.exists():
        return 0
    try:
        return int(path.read_text().strip())
    except (ValueError, OSError):
        return 0


def _check_port(port: int) -> bool:
    """Check if a port is open (service listening)."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False


def _check_health_url(url: str) -> bool:
    """Check if a health URL returns HTTP 200."""
    try:
        import urllib.request
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


# ── SupervisorTree ───────────────────────────────────────────────────
# signed: gamma

class SupervisorTree:
    """OTP-inspired supervision tree for Skynet daemons.

    Manages a flat list of children with configurable restart strategies.
    Each child has a ChildSpec defining its restart policy and a ChildState
    tracking runtime information. The supervisor monitors children at
    configurable intervals and applies the restart strategy on failure.

    Args:
        name:     Name of this supervisor node.
        strategy: Restart strategy for child failures.
        children: List of ChildSpec definitions.
        max_restarts: Circuit breaker for this supervisor level.
        parent:   Parent supervisor for escalation (optional).
    """

    def __init__(self, name: str = "root",
                 strategy: RestartStrategy = RestartStrategy.ONE_FOR_ONE,
                 children: Optional[List[ChildSpec]] = None,
                 max_restarts: Optional[MaxRestarts] = None,
                 parent: Optional[SupervisorTree] = None):
        self.name = name
        self.strategy = strategy
        self.max_restarts = max_restarts or MaxRestarts()
        self.parent = parent
        self._children: Dict[str, ChildState] = {}
        self._child_order: List[str] = []

        if children:
            for spec in children:
                self.add_child(spec)

    def add_child(self, spec: ChildSpec) -> None:
        """Add a child specification to the supervision tree."""
        state = ChildState(spec=spec)
        self._children[spec.name] = state
        if spec.name not in self._child_order:
            self._child_order.append(spec.name)

    def get_child(self, name: str) -> Optional[ChildState]:
        return self._children.get(name)

    @property
    def children(self) -> List[ChildState]:
        """Return children in start order."""
        return [self._children[n] for n in self._child_order
                if n in self._children]

    # ── Start / Stop ─────────────────────────────────────────────

    def start_child(self, name: str) -> bool:
        """Start a single child daemon.

        Reads existing PID file first; if the process is already alive,
        marks it as running without spawning a new one.
        """
        child = self._children.get(name)
        if not child:
            return False
        spec = child.spec

        # Check if already running via PID file
        if spec.pid_file:
            existing_pid = _read_pid_file(spec.pid_file)
            if existing_pid and _pid_alive(existing_pid):
                child.pid = existing_pid
                child.status = ChildStatus.RUNNING
                child.start_time = child.start_time or time.time()
                return True

        # Check if already running via port
        if spec.port and _check_port(spec.port):
            if spec.pid_file:
                child.pid = _read_pid_file(spec.pid_file)
            child.status = ChildStatus.RUNNING
            child.start_time = child.start_time or time.time()
            return True

        # Spawn the process
        try:
            if spec.is_binary and spec.binary_path:
                cmd = [str(_REPO / spec.binary_path)] + spec.args
                cwd = str(_REPO / spec.cwd) if spec.cwd else str(_REPO)
            else:
                cmd = [_PYTHON, str(_REPO / spec.module_path)] + spec.args
                cwd = str(_REPO)

            creation_flags = 0
            if sys.platform == "win32":
                creation_flags = (subprocess.CREATE_NO_WINDOW
                                  | subprocess.DETACHED_PROCESS)

            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=_DAEMON_ENV,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )

            time.sleep(STARTUP_GRACE_S)

            # Verify it started
            if spec.pid_file:
                child.pid = _read_pid_file(spec.pid_file)
                if not child.pid:
                    child.pid = proc.pid
            else:
                child.pid = proc.pid

            if spec.health_url and not _check_health_url(spec.health_url):
                child.status = ChildStatus.FAILED
                child.failure_reason = "health check failed after start"
                return False

            if spec.port and not _check_port(spec.port):
                child.status = ChildStatus.FAILED
                child.failure_reason = f"port {spec.port} not open after start"
                return False

            child.status = ChildStatus.RUNNING
            child.start_time = time.time()
            child.failure_reason = ""
            return True

        except Exception as e:
            child.status = ChildStatus.FAILED
            child.failure_reason = str(e)
            return False

    def stop_child(self, name: str) -> bool:
        """Stop a single child daemon gracefully."""
        child = self._children.get(name)
        if not child or child.status == ChildStatus.STOPPED:
            return True

        pid = child.pid
        if not pid or not _pid_alive(pid):
            child.status = ChildStatus.STOPPED
            child.pid = 0
            return True

        try:
            import psutil
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=child.spec.shutdown_timeout)
            except psutil.TimeoutExpired:
                proc.kill()
        except ImportError:
            # Fallback without psutil
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=10)
            else:
                os.kill(pid, 15)  # SIGTERM
                time.sleep(child.spec.shutdown_timeout)
                if _pid_alive(pid):
                    os.kill(pid, 9)  # SIGKILL
        except Exception:
            pass

        # Clean up PID file
        if child.spec.pid_file:
            pid_path = _REPO / child.spec.pid_file
            if pid_path.exists():
                try:
                    pid_path.unlink()
                except OSError:
                    pass

        child.status = ChildStatus.STOPPED
        child.pid = 0
        return True

    def start_all(self) -> Dict[str, bool]:
        """Start all children in order, respecting dependencies."""
        results: Dict[str, bool] = {}
        started: set = set()

        for name in self._child_order:
            child = self._children[name]
            # Check dependencies — all must be started
            deps_ok = True
            for dep in child.spec.depends_on:
                if dep not in started:
                    results[name] = False
                    child.failure_reason = f"dependency {dep} not started"
                    deps_ok = False
                    break
            if not deps_ok:
                continue

            ok = self.start_child(name)
            results[name] = ok
            if ok:
                started.add(name)

        return results

    def stop_all(self) -> Dict[str, bool]:
        """Stop all children in reverse order."""
        results: Dict[str, bool] = {}
        for name in reversed(self._child_order):
            results[name] = self.stop_child(name)
        return results

    # ── Health Checking ──────────────────────────────────────────

    def check_child_health(self, name: str) -> bool:
        """Check if a child is healthy. Returns True if alive."""
        child = self._children.get(name)
        if not child:
            return False

        spec = child.spec
        child.last_check = time.time()

        # Circuit breaker: skip if tripped
        if child.status == ChildStatus.CIRCUIT_OPEN:
            return False

        # PID liveness
        if child.pid and not _pid_alive(child.pid):
            # Re-read PID file in case daemon restarted itself
            if spec.pid_file:
                new_pid = _read_pid_file(spec.pid_file)
                if new_pid and new_pid != child.pid and _pid_alive(new_pid):
                    child.pid = new_pid
                    child.last_healthy = time.time()
                    return True
            child.status = ChildStatus.FAILED
            child.failure_reason = f"PID {child.pid} not alive"
            return False

        # Health URL check
        if spec.health_url and not _check_health_url(spec.health_url):
            child.status = ChildStatus.FAILED
            child.failure_reason = f"health URL {spec.health_url} failed"
            return False

        # Port check
        if spec.port and not _check_port(spec.port):
            child.status = ChildStatus.FAILED
            child.failure_reason = f"port {spec.port} not open"
            return False

        child.last_healthy = time.time()
        child.status = ChildStatus.RUNNING
        return True

    def check_all_health(self) -> Dict[str, bool]:
        """Check health of all children."""
        return {name: self.check_child_health(name) for name in self._child_order}

    # ── Restart Logic ────────────────────────────────────────────

    def _restart_child(self, name: str) -> bool:
        """Restart a single child with circuit breaker tracking."""
        child = self._children.get(name)
        if not child:
            return False

        # Record restart timestamp
        child.restart_timestamps.append(time.time())
        child.total_restarts += 1

        # Prune old timestamps outside the window
        cutoff = time.time() - child.spec.max_restarts.window_s
        child.restart_timestamps = [ts for ts in child.restart_timestamps
                                    if ts > cutoff]

        # Circuit breaker check
        if child.circuit_tripped():
            child.status = ChildStatus.CIRCUIT_OPEN
            child.failure_reason = (
                f"circuit breaker: {child.recent_restart_count()} restarts "
                f"in {child.spec.max_restarts.window_s}s "
                f"(max {child.spec.max_restarts.count})"
            )
            self._escalate(name)
            return False

        # Check restart type policy
        if child.spec.restart_type == RestartType.TEMPORARY:
            child.status = ChildStatus.STOPPED
            return False
        if (child.spec.restart_type == RestartType.TRANSIENT
                and child.failure_reason == "normal_exit"):
            child.status = ChildStatus.STOPPED
            return False

        self.stop_child(name)
        return self.start_child(name)

    def _escalate(self, child_name: str) -> None:
        """Escalate a circuit-broken child to the parent supervisor."""
        child = self._children.get(child_name)
        msg = (f"SUPERVISOR_ESCALATION: {self.name}/{child_name} "
               f"circuit breaker tripped "
               f"({child.recent_restart_count() if child else '?'} restarts)")

        # Post alert to bus
        try:
            from tools.skynet_spam_guard import guarded_publish
            guarded_publish({
                "sender": "supervisor",
                "topic": "orchestrator",
                "type": "alert",
                "content": msg,
            })
        except Exception:
            pass

        # If there's a parent supervisor, notify it
        if self.parent:
            self.parent._handle_child_escalation(self.name, child_name)

    def _handle_child_escalation(self, sub_supervisor: str, child_name: str) -> None:
        """Handle escalation from a child supervisor."""
        # Log the escalation — parent can decide to restart the whole subtree
        pass  # In a full implementation, this would apply parent strategy

    def handle_failure(self, failed_name: str) -> Dict[str, bool]:
        """Apply the restart strategy when a child fails.

        Returns a dict of {child_name: restart_success} for all affected children.
        """
        results: Dict[str, bool] = {}

        if self.strategy == RestartStrategy.ONE_FOR_ONE:
            results[failed_name] = self._restart_child(failed_name)

        elif self.strategy == RestartStrategy.ONE_FOR_ALL:
            # Stop all, then restart all
            for name in reversed(self._child_order):
                self.stop_child(name)
            for name in self._child_order:
                results[name] = self._restart_child(name)

        elif self.strategy == RestartStrategy.REST_FOR_ONE:
            # Find the failed child's position
            try:
                idx = self._child_order.index(failed_name)
            except ValueError:
                return results

            # Stop failed + all after it (reverse order)
            affected = self._child_order[idx:]
            for name in reversed(affected):
                self.stop_child(name)
            # Restart in order
            for name in affected:
                results[name] = self._restart_child(name)

        return results

    # ── Monitor Loop ─────────────────────────────────────────────

    def monitor_once(self) -> List[str]:
        """Run one monitoring pass. Returns names of children that failed."""
        failed: List[str] = []
        for name in self._child_order:
            child = self._children[name]
            if child.status in (ChildStatus.STOPPED, ChildStatus.CIRCUIT_OPEN):
                continue
            if not self.check_child_health(name):
                failed.append(name)
        return failed

    def monitor(self, interval_s: float = DEFAULT_CHECK_INTERVAL_S,
                max_iterations: int = 0) -> None:
        """Blocking monitoring loop. Checks health and restarts on failure.

        Args:
            interval_s:     Seconds between health check passes.
            max_iterations: Stop after this many iterations (0 = infinite).
        """
        iteration = 0
        while True:
            iteration += 1
            failed = self.monitor_once()
            for name in failed:
                self.handle_failure(name)

            self._save_state()

            if max_iterations and iteration >= max_iterations:
                break
            time.sleep(interval_s)

    # ── State Persistence ────────────────────────────────────────

    def _save_state(self) -> None:
        """Save supervisor state to JSON for dashboard visibility."""
        state = {
            "supervisor": self.name,
            "strategy": self.strategy.value,
            "timestamp": time.time(),
            "children": {},
        }
        for name, child in self._children.items():
            state["children"][name] = {
                "status": child.status.value,
                "pid": child.pid,
                "uptime_s": round(child.uptime_s, 1),
                "total_restarts": child.total_restarts,
                "recent_restarts": child.recent_restart_count(),
                "last_check": child.last_check,
                "last_healthy": child.last_healthy,
                "failure_reason": child.failure_reason,
                "criticality": child.spec.criticality,
            }

        try:
            tmp = SUPERVISOR_STATE_PATH.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, default=str)
            tmp.replace(SUPERVISOR_STATE_PATH)
        except Exception:
            pass

    # ── Display ──────────────────────────────────────────────────

    def status_table(self) -> str:
        """Return a formatted status table of all children."""
        lines = [
            f"Supervisor: {self.name} (strategy={self.strategy.value})",
            f"{'Name':<28} {'Status':<14} {'PID':<8} {'Uptime':<10} "
            f"{'Restarts':<10} {'Criticality':<14} Reason",
            "-" * 110,
        ]
        for name in self._child_order:
            child = self._children[name]
            uptime = _format_uptime(child.uptime_s) if child.uptime_s > 0 else "-"
            restarts = f"{child.recent_restart_count()}/{child.total_restarts}"
            reason = child.failure_reason[:30] if child.failure_reason else ""
            status_str = child.status.value.upper()

            lines.append(
                f"{name:<28} {status_str:<14} {child.pid or '-':<8} "
                f"{uptime:<10} {restarts:<10} {child.spec.criticality:<14} "
                f"{reason}"
            )
        return "\n".join(lines)

    def tree_view(self, indent: int = 0) -> str:
        """Return a tree view of the supervision hierarchy."""
        prefix = "  " * indent
        icon = {
            ChildStatus.RUNNING: "[OK]",
            ChildStatus.STOPPED: "[--]",
            ChildStatus.FAILED: "[!!]",
            ChildStatus.CIRCUIT_OPEN: "[CB]",
        }
        lines = [f"{prefix}supervisor:{self.name} "
                 f"(strategy={self.strategy.value})"]
        for name in self._child_order:
            child = self._children[name]
            status_icon = icon.get(child.status, "[??]")
            pid_str = f"pid={child.pid}" if child.pid else "no-pid"
            restarts = child.total_restarts
            lines.append(
                f"{prefix}  +-- {status_icon} {name} "
                f"({pid_str}, restarts={restarts}, "
                f"type={child.spec.restart_type.value})"
            )
        return "\n".join(lines)

    # ── Default Tree from DAEMON_REGISTRY ────────────────────────

    @classmethod
    def default_tree(cls) -> SupervisorTree:
        """Build the default supervision tree from the daemon registry.

        Integrates with tools/skynet_daemon_status.py DAEMON_REGISTRY
        to create ChildSpecs for all known daemons, grouped into tiers
        by criticality.
        """
        try:
            from tools.skynet_daemon_status import DAEMON_REGISTRY
        except ImportError:
            DAEMON_REGISTRY = []

        # Tier 1: Critical infrastructure (must start first)
        critical_specs: List[ChildSpec] = []
        # Tier 2: High-priority daemons
        high_specs: List[ChildSpec] = []
        # Tier 3: Standard daemons
        standard_specs: List[ChildSpec] = []

        for entry in DAEMON_REGISTRY:
            name = entry.get("name", "")
            script = entry.get("script", "")
            binary = entry.get("binary", "")
            pid_file = entry.get("pid_file", "")
            port = entry.get("port", 0) or 0
            health_url = entry.get("health_url", "")
            criticality = entry.get("criticality", "MODERATE")
            restart_cmd = entry.get("restart_cmd", [])

            is_binary = bool(binary and not script)

            # Determine module path
            module_path = ""
            args: List[str] = []
            if script:
                module_path = script
                if restart_cmd and len(restart_cmd) > 1:
                    args = [str(a) for a in restart_cmd[1:]]
            elif binary:
                module_path = binary

            spec = ChildSpec(
                name=name,
                module_path=module_path,
                args=args,
                restart_type=RestartType.PERMANENT,
                pid_file=pid_file or "",
                health_url=health_url or "",
                port=port,
                criticality=criticality,
                is_binary=is_binary,
                binary_path=binary or "",
                cwd="Skynet" if is_binary else "",
                max_restarts=MaxRestarts(
                    count=3 if criticality in ("CATASTROPHIC", "HIGH") else 5,
                    window_s=300,
                ),
            )

            if criticality == "CATASTROPHIC":
                critical_specs.append(spec)
            elif criticality == "HIGH":
                high_specs.append(spec)
            else:
                standard_specs.append(spec)

        # Set dependencies: high depends on critical, standard depends on high
        critical_names = [s.name for s in critical_specs]
        for spec in high_specs:
            spec.depends_on = critical_names[:]

        # Build tree with rest_for_one at top level — if backend dies,
        # restart it and everything that depends on it
        all_specs = critical_specs + high_specs + standard_specs
        tree = cls(
            name="skynet_root",
            strategy=RestartStrategy.REST_FOR_ONE,
            children=all_specs,
            max_restarts=MaxRestarts(count=10, window_s=600),
        )
        return tree

    @classmethod
    def from_state(cls) -> Optional[SupervisorTree]:
        """Reconstruct a SupervisorTree from saved state file."""
        if not SUPERVISOR_STATE_PATH.exists():
            return None
        try:
            with open(SUPERVISOR_STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
            tree = cls.default_tree()
            tree.name = state.get("supervisor", "skynet_root")

            strategy_str = state.get("strategy", "rest_for_one")
            tree.strategy = RestartStrategy(strategy_str)

            for name, child_state in state.get("children", {}).items():
                child = tree._children.get(name)
                if child:
                    child.pid = child_state.get("pid", 0)
                    child.total_restarts = child_state.get("total_restarts", 0)
                    child.last_check = child_state.get("last_check", 0)
                    child.last_healthy = child_state.get("last_healthy", 0)
                    child.failure_reason = child_state.get("failure_reason", "")
                    status_str = child_state.get("status", "stopped")
                    try:
                        child.status = ChildStatus(status_str)
                    except ValueError:
                        child.status = ChildStatus.STOPPED
            return tree
        except Exception:
            return None


# ── Utility ──────────────────────────────────────────────────────────
# signed: gamma

def _format_uptime(seconds: float) -> str:
    """Format seconds into a human-readable uptime string."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s}s"
    else:
        h, remainder = divmod(int(seconds), 3600)
        m = remainder // 60
        return f"{h}h{m}m"


# ── CLI ──────────────────────────────────────────────────────────────
# signed: gamma

def _cli():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Skynet Supervision Tree -- P2.13"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("start", help="Start all supervised daemons")

    sub.add_parser("stop", help="Stop all supervised daemons")

    sub.add_parser("status", help="Show health status of all children")

    sub.add_parser("tree", help="Show supervision tree structure")

    restart_p = sub.add_parser("restart", help="Restart a specific child")
    restart_p.add_argument("name", help="Child daemon name to restart")

    check_p = sub.add_parser("check", help="Run one health check pass")

    monitor_p = sub.add_parser("monitor",
                               help="Run blocking monitoring loop")
    monitor_p.add_argument("--interval", type=float,
                           default=DEFAULT_CHECK_INTERVAL_S,
                           help="Check interval in seconds")
    monitor_p.add_argument("--iterations", type=int, default=0,
                           help="Max iterations (0 = infinite)")

    args = parser.parse_args()

    if args.command == "start":
        tree = SupervisorTree.default_tree()
        print(f"Starting supervision tree: {tree.name}")
        print(f"Strategy: {tree.strategy.value}")
        print(f"Children: {len(tree.children)}")
        print()
        results = tree.start_all()
        for name, ok in results.items():
            status = "OK" if ok else "FAILED"
            child = tree.get_child(name)
            reason = f" -- {child.failure_reason}" if child and child.failure_reason else ""
            print(f"  {name:<28} {status}{reason}")
        tree._save_state()
        ok_count = sum(1 for v in results.values() if v)
        print(f"\n{ok_count}/{len(results)} children started successfully.")

    elif args.command == "stop":
        tree = SupervisorTree.from_state() or SupervisorTree.default_tree()
        # Refresh PIDs from PID files before stopping
        for child in tree.children:
            if child.spec.pid_file:
                child.pid = _read_pid_file(child.spec.pid_file)
                if child.pid and _pid_alive(child.pid):
                    child.status = ChildStatus.RUNNING
        print(f"Stopping supervision tree: {tree.name}")
        results = tree.stop_all()
        for name, ok in results.items():
            print(f"  {name:<28} {'STOPPED' if ok else 'FAILED'}")
        tree._save_state()

    elif args.command == "status":
        tree = SupervisorTree.default_tree()
        # Refresh from live state
        for child in tree.children:
            if child.spec.pid_file:
                child.pid = _read_pid_file(child.spec.pid_file)
            tree.check_child_health(child.spec.name)

        # Merge saved state (restart counts)
        saved = SupervisorTree.from_state()
        if saved:
            for name in tree._child_order:
                saved_child = saved.get_child(name)
                live_child = tree.get_child(name)
                if saved_child and live_child:
                    live_child.total_restarts = saved_child.total_restarts
                    live_child.restart_timestamps = saved_child.restart_timestamps

        print(tree.status_table())
        running = sum(1 for c in tree.children
                      if c.status == ChildStatus.RUNNING)
        total = len(tree.children)
        print(f"\n{running}/{total} children running.")

    elif args.command == "tree":
        tree = SupervisorTree.default_tree()
        for child in tree.children:
            if child.spec.pid_file:
                child.pid = _read_pid_file(child.spec.pid_file)
            tree.check_child_health(child.spec.name)
        print(tree.tree_view())

    elif args.command == "restart":
        tree = SupervisorTree.default_tree()
        for child in tree.children:
            if child.spec.pid_file:
                child.pid = _read_pid_file(child.spec.pid_file)

        name = args.name
        child = tree.get_child(name)
        if not child:
            print(f"Unknown child: {name}")
            print(f"Known children: {', '.join(tree._child_order)}")
            sys.exit(1)

        print(f"Restarting {name}...")
        tree.stop_child(name)
        ok = tree.start_child(name)
        print(f"  Result: {'OK' if ok else 'FAILED'}")
        if child.failure_reason:
            print(f"  Reason: {child.failure_reason}")
        tree._save_state()

    elif args.command == "check":
        tree = SupervisorTree.default_tree()
        for child in tree.children:
            if child.spec.pid_file:
                child.pid = _read_pid_file(child.spec.pid_file)

        failed = tree.monitor_once()
        if failed:
            print(f"Failed children: {', '.join(failed)}")
            for name in failed:
                results = tree.handle_failure(name)
                for rname, ok in results.items():
                    print(f"  {rname}: {'restarted' if ok else 'restart FAILED'}")
        else:
            print("All children healthy.")
        tree._save_state()

    elif args.command == "monitor":
        tree = SupervisorTree.default_tree()
        for child in tree.children:
            if child.spec.pid_file:
                child.pid = _read_pid_file(child.spec.pid_file)

        print(f"Starting monitoring loop (interval={args.interval}s, "
              f"iterations={'inf' if args.iterations == 0 else args.iterations})")
        tree.monitor(interval_s=args.interval, max_iterations=args.iterations)

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: gamma
