"""
External Worker Isolation Guard — security boundary enforcement for external workers.
# signed: delta

External workers (e.g., workers attached to external projects like blogs or
portfolios) are sandboxed: they cannot modify core Skynet files, dispatch to
core workers, or kill processes. Their results go through quarantine before
being trusted by the core system.

Usage:
    python tools/skynet_external_guard.py check <worker> modify <path>
    python tools/skynet_external_guard.py check <worker> dispatch <target>
    python tools/skynet_external_guard.py check <worker> kill
    python tools/skynet_external_guard.py check <worker> approve_quarantine
    python tools/skynet_external_guard.py info <worker>

Examples:
    python tools/skynet_external_guard.py check ext_blog modify data/workers.json
    # -> DENIED: external worker cannot modify core state file

    python tools/skynet_external_guard.py check alpha modify tools/skynet_self.py
    # -> ALLOWED: alpha is a core worker

    python tools/skynet_external_guard.py info ext_blog
    # -> Shows worker type, allowed paths, restrictions
"""
# signed: delta

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    _SCRIPT_DIR = Path(os.path.abspath(__file__)).parent
    _REPO_ROOT = _SCRIPT_DIR.parent
except NameError:
    _SCRIPT_DIR = Path.cwd() / "tools"
    _REPO_ROOT = Path.cwd()

AGENT_PROFILES_PATH = _REPO_ROOT / "data" / "agent_profiles.json"
EXTERNAL_WORKERS_DIR = _REPO_ROOT / "data" / "external_workers"

# Core Skynet workers that are always trusted  # signed: delta
CORE_WORKERS = frozenset({
    "orchestrator", "alpha", "beta", "gamma", "delta",
    "consultant", "gemini_consultant",
})

# Paths that external workers are FORBIDDEN from modifying
# These protect core Skynet infrastructure, protocol, and state
FORBIDDEN_PATH_PATTERNS = [
    r"^tools[/\\]skynet_.*\.py$",         # Core Skynet tools
    r"^tools[/\\]uia_engine\.py$",        # UIA engine
    r"^tools[/\\]new_chat\.ps1$",         # Worker window management
    r"^tools[/\\]set_copilot_cli\.py$",   # Session target guard
    r"^tools[/\\]set_autopilot\.py$",     # Autopilot guard
    r"^tools[/\\]chrome_bridge[/\\]",     # Chrome bridge stack
    r"^data[/\\]workers\.json$",          # Worker registry
    r"^data[/\\]orchestrator\.json$",     # Orchestrator state
    r"^data[/\\]agent_profiles\.json$",   # Agent profiles
    r"^data[/\\]brain_config\.json$",     # Brain configuration
    r"^data[/\\]worker_scores\.json$",    # Score tracking
    r"^data[/\\]dispatch_log\.json$",     # Dispatch audit log
    r"^data[/\\]critical_processes\.json$",  # Process protection list
    r"^data[/\\]consultant_state\.json$",    # Consultant state
    r"^data[/\\]gemini_consultant_state\.json$",  # Gemini state
    r"^data[/\\]realtime\.json$",         # Realtime daemon state
    r"^AGENTS\.md$",                      # Protocol rules
    r"^\.github[/\\]",                    # GitHub config and instructions
    r"^core[/\\]",                        # Core engine stack
    r"^Skynet[/\\]",                      # Go backend
    r"^Orch-Start\.ps1$",                 # Boot scripts
    r"^CC-Start\.ps1$",
    r"^GC-Start\.ps1$",
    r"^god_console\.py$",                 # GOD Console
    r"^dashboard_server\.py$",            # Dashboard server
]

# Compiled patterns for performance  # signed: delta
_FORBIDDEN_PATTERNS = [re.compile(p) for p in FORBIDDEN_PATH_PATTERNS]

# Keywords in task text that indicate attempts to touch core infrastructure
FORBIDDEN_TASK_KEYWORDS = [
    "kill process", "stop-process", "taskkill", "terminate",
    "workers.json", "orchestrator.json", "agent_profiles",
    "brain_config", "dispatch_log", "critical_processes",
    "skynet_dispatch", "skynet_monitor", "skynet_start",
    "ghost_type", "new_chat.ps1", "orch-start",
    "bus/publish",  # Direct bus access (must use guarded_publish)
    "delete workers", "remove workers", "modify core",
]


def _load_profiles() -> dict:
    """Load agent profiles from data/agent_profiles.json."""
    if not AGENT_PROFILES_PATH.exists():
        return {}
    try:
        with open(AGENT_PROFILES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _get_worker_profile(worker_name: str) -> dict | None:
    """Get a specific worker's profile."""
    profiles = _load_profiles()
    return profiles.get(worker_name)


def _normalize_path(path_str: str) -> str:
    """Normalize a path relative to repo root for pattern matching."""
    # signed: delta
    p = Path(path_str)
    try:
        rel = p.resolve().relative_to(_REPO_ROOT.resolve())
        return str(rel).replace("\\", "/")
    except ValueError:
        # Path is outside repo — normalize as-is
        return str(p).replace("\\", "/")


class ExternalWorkerGuard:
    """Enforces security boundaries for external workers.

    External workers are sandboxed: they can only modify files within their
    assigned project directory and their own state directory under
    data/external_workers/{worker_id}/. They cannot touch core Skynet files,
    dispatch to core workers, or kill processes.

    Core workers (alpha, beta, gamma, delta, orchestrator, consultants) bypass
    all restrictions.
    """
    # signed: delta

    def __init__(self):
        self._profiles = _load_profiles()

    def is_external(self, worker_name: str) -> bool:
        """Check if a worker is external (non-core).

        Returns True if the worker has type='external' in agent_profiles.json,
        or if the worker is not in the CORE_WORKERS set and has no profile.
        """
        if worker_name in CORE_WORKERS:
            return False
        profile = self._profiles.get(worker_name)
        if profile is None:
            # Unknown worker — treat as external for safety
            return True
        return profile.get("type", "core") == "external"

    def can_modify_core(self, worker_name: str) -> bool:
        """Check if a worker can modify core Skynet files.

        Always False for external workers. Always True for core workers.
        """
        return not self.is_external(worker_name)

    def can_dispatch_to_core(self, worker_name: str) -> bool:
        """Check if a worker can dispatch tasks to core workers.

        External workers CANNOT dispatch to core workers (alpha, beta, etc).
        They can only post results to the bus via guarded_publish.
        """
        return not self.is_external(worker_name)

    def can_kill_process(self, worker_name: str) -> bool:
        """Check if a worker can terminate processes.

        Always False for external workers. Core workers follow the existing
        Process Protection rule (Rule 0.1) — only orchestrator can kill.
        """
        return not self.is_external(worker_name)

    def can_approve_quarantine(self, worker_name: str) -> bool:
        """Check if a worker can approve quarantined results.

        Only core workers can cross-validate and approve external results.
        External workers cannot approve their own or others' quarantined work.
        """
        return not self.is_external(worker_name)

    def _is_forbidden_path(self, rel_path: str) -> bool:
        """Check if a path matches any forbidden pattern."""
        normalized = rel_path.replace("\\", "/")
        return any(pat.search(normalized) for pat in _FORBIDDEN_PATTERNS)

    def get_allowed_paths(self, worker_name: str) -> list[str]:
        """Get list of paths an external worker is allowed to modify.

        Returns:
            List of allowed path prefixes/descriptions.
            Empty list for core workers (they have no restrictions).
        """
        # signed: delta
        if not self.is_external(worker_name):
            return []  # Core workers have no path restrictions

        profile = self._profiles.get(worker_name, {})
        project_dir = profile.get("project_directory", "")
        worker_state_dir = f"data/external_workers/{worker_name}/"

        allowed = [worker_state_dir]
        if project_dir:
            allowed.insert(0, project_dir)

        # Quarantine submission (append only)
        allowed.append("data/quarantine.json (submit results only)")

        return allowed

    def validate_path_access(
        self, worker_name: str, path_str: str
    ) -> tuple[bool, str]:
        """Validate whether a worker can modify a specific path.

        Args:
            worker_name: Name of the worker attempting the modification.
            path_str: Path the worker wants to modify.

        Returns:
            (allowed, reason) tuple.
        """
        # signed: delta
        if not self.is_external(worker_name):
            return True, f"{worker_name} is a core worker — no path restrictions"

        # Normalize to repo-relative path
        rel_path = _normalize_path(path_str)

        # Check forbidden patterns
        if self._is_forbidden_path(rel_path):
            return False, (
                f"DENIED: external worker '{worker_name}' cannot modify "
                f"core Skynet path '{rel_path}'"
            )

        # Check if path is within allowed directories
        profile = self._profiles.get(worker_name, {})
        project_dir = profile.get("project_directory", "")
        worker_state = f"data/external_workers/{worker_name}"

        p = Path(path_str).resolve()

        # Allow: own state directory
        try:
            state_dir = (_REPO_ROOT / worker_state).resolve()
            p.relative_to(state_dir)
            return True, f"Path is within worker state directory"
        except ValueError:
            pass

        # Allow: assigned project directory
        if project_dir:
            try:
                proj = Path(project_dir).resolve()
                p.relative_to(proj)
                return True, f"Path is within assigned project directory"
            except ValueError:
                pass

        # Allow: quarantine.json (submit only)
        quarantine = (_REPO_ROOT / "data" / "quarantine.json").resolve()
        if p == quarantine:
            return True, "Quarantine submission allowed (append only)"

        return False, (
            f"DENIED: external worker '{worker_name}' can only modify files "
            f"in its project directory or data/external_workers/{worker_name}/"
        )

    def validate_task_scope(
        self, worker_name: str, task_text: str
    ) -> tuple[bool, str]:
        """Validate that a task doesn't attempt to touch core infrastructure.

        Scans task text for keywords that indicate core system manipulation.
        This is a heuristic check — path validation is the hard enforcement.

        Args:
            worker_name: Name of the worker receiving the task.
            task_text: Full text of the task being dispatched.

        Returns:
            (allowed, reason) tuple.
        """
        # signed: delta
        if not self.is_external(worker_name):
            return True, f"{worker_name} is a core worker — no task restrictions"

        task_lower = task_text.lower()
        for keyword in FORBIDDEN_TASK_KEYWORDS:
            if keyword.lower() in task_lower:
                return False, (
                    f"DENIED: task for external worker '{worker_name}' "
                    f"contains forbidden keyword '{keyword}' — "
                    f"external workers cannot interact with core infrastructure"
                )

        return True, "Task scope validated — no forbidden keywords detected"

    def get_worker_info(self, worker_name: str) -> dict:
        """Get comprehensive info about a worker's isolation status."""
        # signed: delta
        is_ext = self.is_external(worker_name)
        profile = self._profiles.get(worker_name, {})

        info = {
            "worker": worker_name,
            "type": "external" if is_ext else "core",
            "can_modify_core": self.can_modify_core(worker_name),
            "can_dispatch_to_core": self.can_dispatch_to_core(worker_name),
            "can_kill_process": self.can_kill_process(worker_name),
            "can_approve_quarantine": self.can_approve_quarantine(worker_name),
        }

        if is_ext:
            info["allowed_paths"] = self.get_allowed_paths(worker_name)
            info["project_directory"] = profile.get("project_directory", "N/A")
            info["scoring"] = {
                "approved_result": "+0.01",
                "rejected_result": "-0.02",
            }
        else:
            info["restrictions"] = "None — core worker with full access"

        return info


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------

def _cli_check(args) -> int:
    """Handle 'check' subcommand."""
    # signed: delta
    guard = ExternalWorkerGuard()
    worker = args.worker
    action = args.action

    if action == "modify":
        if not args.path:
            print("ERROR: --path required for 'modify' action")
            return 1
        allowed, reason = guard.validate_path_access(worker, args.path)
    elif action == "dispatch":
        if not args.target:
            print("ERROR: --target required for 'dispatch' action")
            return 1
        if guard.is_external(worker) and args.target in CORE_WORKERS:
            allowed, reason = False, (
                f"DENIED: external worker '{worker}' cannot dispatch "
                f"to core worker '{args.target}'"
            )
        else:
            allowed = guard.can_dispatch_to_core(worker)
            reason = (
                f"{'ALLOWED' if allowed else 'DENIED'}: "
                f"{'core' if not guard.is_external(worker) else 'external'} "
                f"worker dispatch"
            )
    elif action == "kill":
        allowed = guard.can_kill_process(worker)
        reason = (
            f"{'ALLOWED' if allowed else 'DENIED'}: "
            f"process termination for {worker}"
        )
    elif action == "approve_quarantine":
        allowed = guard.can_approve_quarantine(worker)
        reason = (
            f"{'ALLOWED' if allowed else 'DENIED'}: "
            f"quarantine approval for {worker}"
        )
    elif action == "task":
        if not args.task_text:
            print("ERROR: --task-text required for 'task' action")
            return 1
        allowed, reason = guard.validate_task_scope(worker, args.task_text)
    else:
        print(f"ERROR: unknown action '{action}'")
        return 1

    status = "ALLOWED" if allowed else "DENIED"
    print(f"[{status}] {reason}")
    return 0 if allowed else 2


def _cli_info(args) -> int:
    """Handle 'info' subcommand."""
    guard = ExternalWorkerGuard()
    info = guard.get_worker_info(args.worker)
    print(json.dumps(info, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="External Worker Isolation Guard"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # check subcommand
    check_parser = subparsers.add_parser("check", help="Check worker permissions")
    check_parser.add_argument("worker", help="Worker name")
    check_parser.add_argument(
        "action",
        choices=["modify", "dispatch", "kill", "approve_quarantine", "task"],
        help="Action to check",
    )
    check_parser.add_argument("--path", help="Path for 'modify' action")
    check_parser.add_argument("--target", help="Target worker for 'dispatch' action")
    check_parser.add_argument("--task-text", help="Task text for 'task' action")

    # info subcommand
    info_parser = subparsers.add_parser("info", help="Show worker isolation info")
    info_parser.add_argument("worker", help="Worker name")

    args = parser.parse_args()

    if args.command == "check":
        return _cli_check(args)
    elif args.command == "info":
        return _cli_info(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
