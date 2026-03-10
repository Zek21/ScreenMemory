#!/usr/bin/env python3
"""
skynet_orch_guard.py -- Orchestrator compliance enforcement.

Prevents the orchestrator from violating delegation rules by detecting
hands-on actions (file edits, script execution, code changes) and
injecting compliance reminders.

Usage:
    from tools.skynet_orch_guard import check_violation, COMPLIANCE_REMINDER

    warning = check_violation("editing core/security.py")
    if warning:
        print(warning)  # "VIOLATION: ..."

CLI:
    python tools/skynet_orch_guard.py --check "editing file X"
    python tools/skynet_orch_guard.py --violations
    python tools/skynet_orch_guard.py --reset
"""

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
BRAIN_CONFIG = DATA_DIR / "brain_config.json"

# Appended to every self-prompt message
COMPLIANCE_REMINDER = (
    "REMINDER: Orchestrator delegates ALL work to workers via skynet_dispatch.py. "
    "Never edit files, run scripts, or do hands-on work directly."
)

# Action keywords that indicate hands-on work
VIOLATION_KEYWORDS = [
    "edit", "editing", "modify", "modifying", "change", "changing",
    "write", "writing", "create file", "creating file",
    "fix", "fixing", "patch", "patching",
    "refactor", "refactoring", "rewrite", "rewriting",
    "run script", "running script", "execute", "executing",
    "grep", "scanning code", "reading file", "read file",
    "install", "installing", "pip install",
    "test", "testing", "running test",
    "build", "building", "compile", "compiling",
    "delete", "deleting", "remove file", "removing file",
]

# Allowed orchestrator actions (not violations)
ALLOWED_ACTIONS = [
    "dispatch", "dispatching", "delegate", "delegating",
    "poll", "polling", "check status", "checking status",
    "synthesize", "synthesizing", "summarize", "summarizing",
    "decompose", "decomposing", "plan", "planning",
    "reply", "replying", "report", "reporting",
    "monitor", "monitoring", "decide", "deciding",
]


def check_violation(action_description: str) -> str | None:
    """Check if an action description indicates a compliance violation.
    
    Returns a warning string if violation detected, None if action is allowed.
    """
    if not action_description:
        return None
    
    action_lower = action_description.lower().strip()
    
    # Check if it's an explicitly allowed action
    for allowed in ALLOWED_ACTIONS:
        if allowed in action_lower:
            return None
    
    # Check for violation keywords
    for keyword in VIOLATION_KEYWORDS:
        if keyword in action_lower:
            _record_violation(action_description)
            return (
                f"COMPLIANCE VIOLATION DETECTED: '{action_description}' "
                f"is hands-on work. Orchestrator must delegate this to a worker "
                f"via skynet_dispatch.py. Dispatch to an idle worker instead."
            )
    
    return None


def _record_violation(action: str):
    """Record a violation in brain_config.json compliance section."""
    try:
        cfg = json.loads(BRAIN_CONFIG.read_text(encoding="utf-8"))
        compliance = cfg.setdefault("compliance", {
            "violations": 0,
            "last_violation": "",
            "guard_enabled": True,
            "history": [],
        })
        compliance["violations"] = compliance.get("violations", 0) + 1
        compliance["last_violation"] = action
        compliance["last_violation_at"] = datetime.now().isoformat()
        history = compliance.setdefault("history", [])
        history.append({
            "action": action,
            "timestamp": datetime.now().isoformat(),
        })
        # Keep last 20 violations
        if len(history) > 20:
            compliance["history"] = history[-20:]
        BRAIN_CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_violations() -> dict:
    """Get current violation stats."""
    try:
        cfg = json.loads(BRAIN_CONFIG.read_text(encoding="utf-8"))
        return cfg.get("compliance", {"violations": 0, "guard_enabled": False})
    except Exception:
        return {"violations": 0, "guard_enabled": False}


def is_guard_enabled() -> bool:
    """Check if the compliance guard is active."""
    return get_violations().get("guard_enabled", True)


def reset_violations():
    """Reset violation counter (admin only)."""
    try:
        cfg = json.loads(BRAIN_CONFIG.read_text(encoding="utf-8"))
        cfg["compliance"] = {
            "violations": 0,
            "last_violation": "",
            "guard_enabled": True,
            "history": [],
        }
        BRAIN_CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Orchestrator Compliance Guard")
    parser.add_argument("--check", type=str, help="Check if action is a violation")
    parser.add_argument("--violations", action="store_true", help="Show violation stats")
    parser.add_argument("--reset", action="store_true", help="Reset violation counter")
    args = parser.parse_args()

    if args.check:
        result = check_violation(args.check)
        if result:
            print(f"VIOLATION: {result}")
            sys.exit(1)
        else:
            print(f"OK: '{args.check}' is an allowed orchestrator action.")
            sys.exit(0)

    if args.violations:
        stats = get_violations()
        print(json.dumps(stats, indent=2))
        return

    if args.reset:
        if reset_violations():
            print("Violation counter reset.")
        else:
            print("Failed to reset.")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
