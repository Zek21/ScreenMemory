"""
ScreenMemory Process Guardian — Safety guardrails for all spawned processes.

Prevents orphan processes by:
1. Tracking every spawned process in a PID file
2. Enforcing max lifetime (auto-kill after timeout)
3. Heartbeat monitoring (kill if no heartbeat)
4. Single-command kill-all for emergency shutdown
5. Startup cleanup of stale processes from previous sessions

Usage:
    python guardian.py status        # Show all tracked processes
    python guardian.py kill-all      # Emergency: kill everything
    python guardian.py cleanup       # Kill stale/orphan processes
    python guardian.py watch         # Monitor loop (run in background)
"""
import os
import sys
import json
import time
import signal
import logging
import argparse
import atexit
from pathlib import Path
from typing import Optional, Dict, List
from dataclasses import dataclass, field, asdict
from datetime import datetime

logger = logging.getLogger("guardian")

# PID registry lives next to the DB
REGISTRY_PATH = Path(__file__).parent / "data" / "process_registry.json"
MAX_PROCESS_LIFETIME_SECONDS = 3600  # 1 hour max for any spawned process
HEARTBEAT_TIMEOUT_SECONDS = 120      # Kill if no heartbeat for 2 minutes
DAEMON_MAX_LIFETIME_SECONDS = 86400  # 24 hours max for the daemon itself


@dataclass
class TrackedProcess:
    pid: int
    name: str                   # e.g., "daemon", "streamlit", "vlm-worker"
    command: str                # Full command line
    started_at: float           # Unix timestamp
    max_lifetime: int           # Seconds before auto-kill
    last_heartbeat: float       # Last heartbeat timestamp
    parent_pid: int = 0        # PID of the process that spawned this
    category: str = "worker"    # "daemon", "ui", "worker", "agent"

    @property
    def age_seconds(self) -> float:
        return time.time() - self.started_at

    @property
    def age_human(self) -> str:
        age = self.age_seconds
        if age < 60:
            return f"{age:.0f}s"
        elif age < 3600:
            return f"{age/60:.0f}m"
        else:
            return f"{age/3600:.1f}h"

    @property
    def is_expired(self) -> bool:
        return self.age_seconds > self.max_lifetime

    @property
    def heartbeat_stale(self) -> bool:
        return (time.time() - self.last_heartbeat) > HEARTBEAT_TIMEOUT_SECONDS

    @property
    def is_alive(self) -> bool:
        """Check if the process is actually running."""
        try:
            import psutil
            return psutil.pid_exists(self.pid)
        except ImportError:
            # Fallback: try to send signal 0 (doesn't kill, just checks)
            try:
                os.kill(self.pid, 0)
                return True
            except (OSError, ProcessLookupError):
                return False


class ProcessGuardian:
    """
    Central process tracker and enforcer.
    Every spawned process MUST register here, or it risks being killed as an orphan.
    """

    def __init__(self, registry_path: Path = REGISTRY_PATH):
        self.registry_path = registry_path
        self._processes: Dict[int, TrackedProcess] = {}
        self._load_registry()
        # Register cleanup on exit
        atexit.register(self._on_exit)

    def _load_registry(self):
        """Load existing process registry from disk."""
        if self.registry_path.exists():
            try:
                with open(self.registry_path) as f:
                    data = json.load(f)
                for entry in data.get("processes", []):
                    proc = TrackedProcess(**entry)
                    self._processes[proc.pid] = proc
            except Exception as e:
                logger.warning("Failed to load registry: %s", e)
                self._processes = {}

    def _save_registry(self):
        """Persist registry to disk."""
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "last_updated": time.time(),
            "last_updated_human": datetime.now().isoformat(),
            "processes": [asdict(p) for p in self._processes.values()],
        }
        with open(self.registry_path, "w") as f:
            json.dump(data, f, indent=2)

    def register(self, pid: int, name: str, command: str = "",
                 max_lifetime: int = MAX_PROCESS_LIFETIME_SECONDS,
                 category: str = "worker") -> TrackedProcess:
        """
        Register a spawned process. MUST be called for every new process.
        """
        proc = TrackedProcess(
            pid=pid,
            name=name,
            command=command,
            started_at=time.time(),
            max_lifetime=max_lifetime,
            last_heartbeat=time.time(),
            parent_pid=os.getpid(),
            category=category,
        )
        self._processes[pid] = proc
        self._save_registry()
        logger.info("Registered process: %s (PID %d, max %ds)", name, pid, max_lifetime)
        return proc

    def register_self(self, name: str = "daemon",
                      max_lifetime: int = DAEMON_MAX_LIFETIME_SECONDS) -> TrackedProcess:
        """Register the current process."""
        return self.register(
            pid=os.getpid(),
            name=name,
            command=" ".join(sys.argv),
            max_lifetime=max_lifetime,
            category="daemon",
        )

    def heartbeat(self, pid: Optional[int] = None):
        """Update heartbeat for a process (default: current process)."""
        pid = pid or os.getpid()
        if pid in self._processes:
            self._processes[pid].last_heartbeat = time.time()
            self._save_registry()

    def unregister(self, pid: int):
        """Remove a process from tracking (it exited cleanly)."""
        if pid in self._processes:
            name = self._processes[pid].name
            del self._processes[pid]
            self._save_registry()
            logger.info("Unregistered process: %s (PID %d)", name, pid)

    def kill_process(self, pid: int, reason: str = "guardian enforcement") -> bool:
        """Kill a tracked process."""
        try:
            import psutil
            if psutil.pid_exists(pid):
                proc = psutil.Process(pid)
                proc_name = proc.name()
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    proc.kill()  # Force kill if terminate didn't work
                logger.warning("KILLED PID %d (%s): %s", pid, proc_name, reason)
                self.unregister(pid)
                return True
        except Exception as e:
            logger.error("Failed to kill PID %d: %s", pid, e)

        # Cleanup registry even if kill failed
        self.unregister(pid)
        return False

    def enforce(self) -> List[str]:
        """
        Run enforcement checks. Kill expired or stale processes.
        Returns list of actions taken.
        """
        actions = []
        dead_pids = []

        for pid, proc in list(self._processes.items()):
            # Already dead? Clean up registry
            if not proc.is_alive:
                dead_pids.append(pid)
                actions.append(f"CLEANED: {proc.name} (PID {pid}) — already dead")
                continue

            # Expired (exceeded max lifetime)?
            if proc.is_expired:
                self.kill_process(pid, f"exceeded max lifetime ({proc.max_lifetime}s)")
                actions.append(
                    f"KILLED: {proc.name} (PID {pid}) — expired after {proc.age_human}"
                )
                continue

            # Stale heartbeat (for non-daemon processes)?
            if proc.category != "daemon" and proc.heartbeat_stale:
                self.kill_process(pid, f"heartbeat stale ({HEARTBEAT_TIMEOUT_SECONDS}s)")
                actions.append(
                    f"KILLED: {proc.name} (PID {pid}) — no heartbeat for {HEARTBEAT_TIMEOUT_SECONDS}s"
                )
                continue

        # Clean dead entries
        for pid in dead_pids:
            self.unregister(pid)

        if actions:
            self._save_registry()

        return actions

    def kill_all(self, include_self: bool = False) -> List[str]:
        """
        EMERGENCY: Kill all tracked processes.
        """
        actions = []
        my_pid = os.getpid()

        for pid, proc in list(self._processes.items()):
            if pid == my_pid and not include_self:
                continue
            if proc.is_alive:
                self.kill_process(pid, "kill-all command")
                actions.append(f"KILLED: {proc.name} (PID {pid})")
            else:
                self.unregister(pid)
                actions.append(f"CLEANED: {proc.name} (PID {pid}) — already dead")

        return actions

    def find_orphans(self) -> List[Dict]:
        """
        Find Python processes NOT in our registry (potential orphans).
        """
        orphans = []
        try:
            import psutil
            tracked_pids = set(self._processes.keys())

            for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
                try:
                    if proc.info["name"] and "python" in proc.info["name"].lower():
                        pid = proc.info["pid"]
                        if pid not in tracked_pids and pid != os.getpid():
                            cmdline = " ".join(proc.info["cmdline"] or [])
                            # Skip VS Code language servers
                            if "ms-python" in cmdline or "pylance" in cmdline:
                                continue
                            orphans.append({
                                "pid": pid,
                                "name": proc.info["name"],
                                "command": cmdline[:150],
                                "started": datetime.fromtimestamp(
                                    proc.info["create_time"]
                                ).isoformat(),
                                "age": time.time() - proc.info["create_time"],
                            })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except ImportError:
            logger.warning("psutil not available — cannot detect orphans")

        return orphans

    def kill_orphans(self) -> List[str]:
        """Find and kill orphan Python processes (not in registry, not VS Code)."""
        actions = []
        for orphan in self.find_orphans():
            pid = orphan["pid"]
            try:
                import psutil
                proc = psutil.Process(pid)
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    proc.kill()
                actions.append(f"KILLED ORPHAN: PID {pid} — {orphan['command'][:80]}")
            except Exception as e:
                actions.append(f"FAILED to kill orphan PID {pid}: {e}")
        return actions

    def status(self) -> str:
        """Human-readable status report."""
        lines = []
        lines.append("=" * 70)
        lines.append("  PROCESS GUARDIAN — STATUS REPORT")
        lines.append(f"  Time: {datetime.now().isoformat()}")
        lines.append("=" * 70)

        # Tracked processes
        if self._processes:
            lines.append(f"\n  TRACKED PROCESSES ({len(self._processes)}):")
            lines.append(f"  {'PID':>6}  {'Name':<15} {'Cat':<8} {'Age':<8} {'Alive':<6} {'Status'}")
            lines.append(f"  {'-'*6}  {'-'*15} {'-'*8} {'-'*8} {'-'*6} {'-'*20}")
            for pid, proc in sorted(self._processes.items()):
                alive = "YES" if proc.is_alive else "DEAD"
                status = ""
                if proc.is_expired:
                    status = "EXPIRED!"
                elif proc.heartbeat_stale:
                    status = "STALE HEARTBEAT"
                else:
                    remaining = proc.max_lifetime - proc.age_seconds
                    status = f"OK ({remaining/60:.0f}m left)"

                lines.append(
                    f"  {pid:>6}  {proc.name:<15} {proc.category:<8} "
                    f"{proc.age_human:<8} {alive:<6} {status}"
                )
        else:
            lines.append("\n  No tracked processes.")

        # Orphans
        orphans = self.find_orphans()
        if orphans:
            lines.append(f"\n  ORPHAN PROCESSES ({len(orphans)}):")
            for o in orphans:
                age_h = o["age"]
                if age_h < 3600:
                    age_str = f"{age_h/60:.0f}m"
                else:
                    age_str = f"{age_h/3600:.1f}h"
                lines.append(f"  PID {o['pid']:>6} ({age_str}) — {o['command'][:70]}")
        else:
            lines.append("\n  No orphan processes detected.")

        lines.append("")
        return "\n".join(lines)

    def _on_exit(self):
        """Cleanup on process exit — unregister self."""
        my_pid = os.getpid()
        if my_pid in self._processes:
            self.unregister(my_pid)

    def watch(self, interval: int = 30):
        """
        Monitoring loop — runs enforcement checks periodically.
        Designed to run as a lightweight background watchdog.
        """
        self.register_self("guardian-watchdog", max_lifetime=DAEMON_MAX_LIFETIME_SECONDS)
        logger.info("Guardian watchdog started (check every %ds)", interval)

        try:
            while True:
                actions = self.enforce()
                if actions:
                    for a in actions:
                        logger.warning("  %s", a)
                self.heartbeat()
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Guardian watchdog stopped")


def main():
    parser = argparse.ArgumentParser(description="ScreenMemory Process Guardian")
    parser.add_argument("command", choices=["status", "kill-all", "cleanup", "watch", "orphans"],
                        help="Guardian command")
    parser.add_argument("--interval", type=int, default=30, help="Watch interval (seconds)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    guardian = ProcessGuardian()

    if args.command == "status":
        print(guardian.status())

    elif args.command == "kill-all":
        print("EMERGENCY KILL-ALL")
        # Kill tracked
        actions = guardian.kill_all()
        for a in actions:
            print(f"  {a}")
        # Kill orphans
        orphan_actions = guardian.kill_orphans()
        for a in orphan_actions:
            print(f"  {a}")
        if not actions and not orphan_actions:
            print("  No processes to kill.")

    elif args.command == "cleanup":
        # Enforce rules + kill orphans
        actions = guardian.enforce()
        orphan_actions = guardian.kill_orphans()
        all_actions = actions + orphan_actions
        if all_actions:
            for a in all_actions:
                print(f"  {a}")
        else:
            print("  Nothing to clean up.")

    elif args.command == "orphans":
        orphans = guardian.find_orphans()
        if orphans:
            print(f"Found {len(orphans)} orphan Python processes:")
            for o in orphans:
                print(f"  PID {o['pid']} — {o['command'][:80]}")
        else:
            print("No orphans found.")

    elif args.command == "watch":
        guardian.watch(interval=args.interval)


if __name__ == "__main__":
    main()
