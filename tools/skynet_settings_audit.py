#!/usr/bin/env python3
"""Skynet Settings Audit — programmatic readiness check for all intelligence tools.

Verifies that every tool in the Skynet intelligence stack is importable, callable,
and correctly wired into the live pipeline. Returns a 0-100 readiness score.

CLI:
    python tools/skynet_settings_audit.py           # Full audit with details
    python tools/skynet_settings_audit.py --json    # Machine-readable JSON
    python tools/skynet_settings_audit.py --quiet   # Score only

# signed: gamma
"""

import argparse
import importlib
import json
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# ── check definitions ────────────────────────────────────────────────────────

# Each check: (id, description, weight)
# Weight determines how much this check contributes to the total score.
# Total weight across all checks = 100.

TOOL_CHECKS = [
    # PART 1 — Tool importability (5 pts each = 40 pts)
    ("backup_safe_write", "skynet_backup.safe_write_json() callable", 5),
    ("edit_guard", "skynet_edit_guard.guard_edit() callable", 5),
    ("dispatch_resilience", "skynet_dispatch_resilience.DispatchResilience importable", 5),
    ("post_task", "skynet_post_task.execute_post_task_lifecycle() callable", 5),
    ("autonomous_worker", "skynet_autonomous_worker.AutonomousWorker importable", 5),
    ("collective_dashboard", "skynet_collective_dashboard importable", 5),
    ("boot_workers", "skynet_boot_workers.boot_all_workers() callable", 5),
    ("skynet_self", "skynet_self.SkynetSelf instantiate + pulse < 5s", 5),
]

WIRING_CHECKS = [
    # PART 2 — Integration wiring (10 pts each = 40 pts)
    ("dispatch_resilience_wired", "skynet_dispatch.py has resilient_dispatch_to_worker", 10),
    ("start_boot_wired", "skynet_start.py imports boot_all_workers", 10),
    ("monitor_cli_error", "skynet_monitor.py has detect_cli_error", 10),
    ("brain_intelligence_stack", "brain_config.json has intelligence_stack section", 10),
]

INFRA_CHECKS = [
    # PART 3 — Infrastructure health (5 pts each = 20 pts)
    ("spam_guard", "skynet_spam_guard.guarded_publish importable", 5),
    ("scoring", "skynet_scoring.get_leaderboard() callable", 5),
    ("knowledge", "skynet_knowledge.broadcast_learning callable", 5),
    ("collective", "skynet_collective.intelligence_score callable", 5),
]


# ── check implementations ───────────────────────────────────────────────────

def _check_module_callable(module_path: str, attr_name: str) -> tuple[bool, str]:
    """Import module, check attr is callable. Returns (pass, detail)."""
    try:
        mod = importlib.import_module(module_path)
        obj = getattr(mod, attr_name, None)
        if obj is None:
            return False, f"{attr_name} not found in {module_path}"
        if callable(obj):
            return True, f"{attr_name} callable"
        return False, f"{attr_name} exists but not callable"
    except Exception as e:
        return False, f"import failed: {e}"


def _check_module_class(module_path: str, class_name: str) -> tuple[bool, str]:
    """Import module, check class exists."""
    try:
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name, None)
        if cls is None:
            return False, f"{class_name} not found in {module_path}"
        return True, f"{class_name} importable"
    except Exception as e:
        return False, f"import failed: {e}"


def _check_file_contains(filepath: str, pattern: str) -> tuple[bool, str]:
    """Check if file contains pattern (regex)."""
    path = REPO_ROOT / filepath
    if not path.exists():
        return False, f"{filepath} does not exist"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        matches = re.findall(pattern, text)
        if matches:
            return True, f"found {len(matches)} match(es)"
        return False, f"pattern not found: {pattern}"
    except Exception as e:
        return False, f"read error: {e}"


def _check_json_key(filepath: str, key: str) -> tuple[bool, str]:
    """Check if JSON file has a top-level key."""
    path = REPO_ROOT / filepath
    if not path.exists():
        return False, f"{filepath} does not exist"
    try:
        with open(path) as f:
            data = json.load(f)
        if key in data:
            return True, f"key '{key}' present"
        return False, f"key '{key}' missing"
    except Exception as e:
        return False, f"parse error: {e}"


def run_check(check_id: str) -> tuple[bool, str]:
    """Run a single check by ID. Returns (passed, detail)."""
    # PART 1 — Tool checks
    if check_id == "backup_safe_write":
        return _check_module_callable("tools.skynet_backup", "safe_write_json")
    elif check_id == "edit_guard":
        return _check_module_callable("tools.skynet_edit_guard", "guard_edit")
    elif check_id == "dispatch_resilience":
        return _check_module_class("tools.skynet_dispatch_resilience", "DispatchResilience")
    elif check_id == "post_task":
        return _check_module_callable("tools.skynet_post_task", "execute_post_task_lifecycle")
    elif check_id == "autonomous_worker":
        return _check_module_class("tools.skynet_autonomous_worker", "AutonomousWorker")
    elif check_id == "collective_dashboard":
        try:
            importlib.import_module("tools.skynet_collective_dashboard")
            return True, "importable"
        except Exception as e:
            return False, f"import failed: {e}"
    elif check_id == "boot_workers":
        return _check_module_callable("tools.skynet_boot_workers", "boot_all_workers")
    elif check_id == "skynet_self":
        try:
            from tools.skynet_self import SkynetSelf
            t0 = time.time()
            obj = SkynetSelf()
            obj.quick_pulse()
            elapsed = time.time() - t0
            if elapsed < 5.0:
                return True, f"pulse completed in {elapsed:.2f}s"
            return False, f"pulse took {elapsed:.2f}s (> 5s limit)"
        except Exception as e:
            return False, f"failed: {e}"

    # PART 2 — Wiring checks
    elif check_id == "dispatch_resilience_wired":
        return _check_file_contains(
            "tools/skynet_dispatch.py",
            r"resilient_dispatch_to_worker"
        )
    elif check_id == "start_boot_wired":
        return _check_file_contains(
            "tools/skynet_start.py",
            r"boot_all_workers"
        )
    elif check_id == "monitor_cli_error":
        return _check_file_contains(
            "tools/skynet_monitor.py",
            r"detect_cli_error"
        )
    elif check_id == "brain_intelligence_stack":
        return _check_json_key("data/brain_config.json", "intelligence_stack")

    # PART 3 — Infrastructure checks
    elif check_id == "spam_guard":
        return _check_module_callable("tools.skynet_spam_guard", "guarded_publish")
    elif check_id == "scoring":
        return _check_module_callable("tools.skynet_scoring", "get_leaderboard")
    elif check_id == "knowledge":
        return _check_module_callable("tools.skynet_knowledge", "broadcast_learning")
    elif check_id == "collective":
        return _check_module_callable("tools.skynet_collective", "intelligence_score")

    return False, f"unknown check: {check_id}"


# ── audit runner ─────────────────────────────────────────────────────────────

def run_audit() -> dict:
    """Run all checks and compute readiness score."""
    all_checks = TOOL_CHECKS + WIRING_CHECKS + INFRA_CHECKS
    total_weight = sum(w for _, _, w in all_checks)
    earned = 0
    results = []

    for check_id, description, weight in all_checks:
        passed, detail = run_check(check_id)
        status = "LIVE" if passed else "DEAD"
        if passed:
            earned += weight
        results.append({
            "id": check_id,
            "description": description,
            "weight": weight,
            "status": status,
            "detail": detail,
        })

    score = int((earned / total_weight) * 100) if total_weight > 0 else 0

    return {
        "score": score,
        "earned": earned,
        "total": total_weight,
        "checks": results,
        "timestamp": time.time(),
    }


# ── ASCII rendering ─────────────────────────────────────────────────────────

def render_audit(audit: dict) -> str:
    """Render audit results as formatted ASCII."""
    lines = []
    score = audit["score"]

    lines.append("=" * 72)
    lines.append("  SKYNET SETTINGS AUDIT — System Readiness Report")
    lines.append("=" * 72)
    lines.append("")

    # Score bar
    bar_len = 40
    filled = int(score / 100 * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)
    grade = "EXCELLENT" if score >= 90 else "GOOD" if score >= 75 else "FAIR" if score >= 50 else "POOR"
    lines.append(f"  READINESS SCORE: {score}/100  [{bar}]  {grade}")
    lines.append(f"  Points: {audit['earned']}/{audit['total']}")
    lines.append("")

    # Group checks
    sections = [
        ("TOOL IMPORTABILITY", TOOL_CHECKS),
        ("PIPELINE WIRING", WIRING_CHECKS),
        ("INFRASTRUCTURE", INFRA_CHECKS),
    ]

    check_map = {c["id"]: c for c in audit["checks"]}

    for section_name, checks in sections:
        lines.append(f"  --- {section_name} ---")
        for check_id, description, weight in checks:
            result = check_map.get(check_id, {})
            status = result.get("status", "?")
            detail = result.get("detail", "")
            icon = "✅" if status == "LIVE" else "❌"
            lines.append(f"    {icon} [{status:<4}] {description}")
            if status != "LIVE":
                lines.append(f"           └─ {detail}")
        lines.append("")

    lines.append("-" * 72)
    lines.append(f"  {score}/100 — {grade}")
    lines.append("  signed: gamma")
    lines.append("")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Skynet Settings Audit")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--quiet", action="store_true", help="Score only")
    args = parser.parse_args()

    audit = run_audit()

    if args.quiet:
        print(audit["score"])
    elif args.json:
        print(json.dumps(audit, indent=2, default=str))
    else:
        print(render_audit(audit))


if __name__ == "__main__":
    main()
# signed: gamma
