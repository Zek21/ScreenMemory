#!/usr/bin/env python3
"""Skynet Edit Guard — Pre-edit protection for critical protocol and data files.

Validates edits before they happen:
- Change percentage check (warns >20%, blocks >50%)
- Auto-backup before any edit
- Schema validation for JSON data files
- Protected file registry with risk levels

Usage:
    python tools/skynet_edit_guard.py check FILE              # Pre-edit validation
    python tools/skynet_edit_guard.py validate FILE            # Schema validation (JSON)
    python tools/skynet_edit_guard.py protect                  # Show all protected files
    python tools/skynet_edit_guard.py history [FILE]           # Show edit history

Programmatic API:
    from tools.skynet_edit_guard import guard_edit, validate_json_schema
    ok, warnings = guard_edit("data/workers.json", new_content)
    valid, errors = validate_json_schema("data/brain_config.json")

# signed: orchestrator
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
EDIT_LOG = DATA_DIR / "edit_guard_log.json"

# Protected file registry with risk levels and validation rules
PROTECTED_FILES = {
    # CRITICAL — system won't function without these
    "data/workers.json": {
        "risk": "CRITICAL",
        "max_change_pct": 30,
        "description": "Worker registry with HWNDs — runtime state",
        "schema": "workers",
    },
    "data/orchestrator.json": {
        "risk": "CRITICAL",
        "max_change_pct": 50,
        "description": "Orchestrator HWND and identity",
        "schema": "orchestrator",
    },
    "data/brain_config.json": {
        "risk": "CRITICAL",
        "max_change_pct": 20,
        "description": "All agent operational parameters",
        "schema": "brain_config",
    },
    "AGENTS.md": {
        "risk": "CRITICAL",
        "max_change_pct": 10,
        "description": "Governs ALL agent behavior — 136KB",
    },
    ".github/copilot-instructions.md": {
        "risk": "CRITICAL",
        "max_change_pct": 10,
        "description": "Governs ALL agent behavior — 54KB",
    },
    "Orch-Start.ps1": {
        "risk": "CRITICAL",
        "max_change_pct": 15,
        "description": "Orchestrator boot script",
    },
    "tools/skynet_start.py": {
        "risk": "CRITICAL",
        "max_change_pct": 15,
        "description": "Worker window opening, UIA, model guard",
    },
    "tools/skynet_dispatch.py": {
        "risk": "CRITICAL",
        "max_change_pct": 15,
        "description": "All worker communication flows through this",
    },
    # HIGH — degraded operation without these
    "data/agent_profiles.json": {
        "risk": "HIGH",
        "max_change_pct": 30,
        "description": "Agent identity and specialties",
        "schema": "agent_profiles",
    },
    "data/todos.json": {
        "risk": "HIGH",
        "max_change_pct": 50,
        "description": "Live work queue — 38 items",
        "schema": "todos",
    },
    "data/incidents.json": {
        "risk": "HIGH",
        "max_change_pct": 20,
        "description": "Institutional memory",
    },
    "data/worker_scores.json": {
        "risk": "HIGH",
        "max_change_pct": 30,
        "description": "Score tracking for all agents",
    },
    "tools/skynet_monitor.py": {
        "risk": "HIGH",
        "max_change_pct": 20,
        "description": "Health monitoring, model drift correction",
    },
    "data/boot_config.json": {
        "risk": "HIGH",
        "max_change_pct": 30,
        "description": "Boot sequence configuration",
    },
    "tools/new_chat.ps1": {
        "risk": "HIGH",
        "max_change_pct": 20,
        "description": "Only way to open worker windows",
    },
    "CC-Start.ps1": {
        "risk": "HIGH",
        "max_change_pct": 15,
        "description": "Codex Consultant bootstrap",
    },
    "GC-Start.ps1": {
        "risk": "HIGH",
        "max_change_pct": 15,
        "description": "Gemini Consultant bootstrap",
    },
    # MEDIUM — important but recoverable
    "data/critical_processes.json": {
        "risk": "MEDIUM",
        "max_change_pct": 50,
        "description": "Process protection list",
    },
    "data/consultant_state.json": {
        "risk": "MEDIUM",
        "max_change_pct": 50,
        "description": "Codex consultant state",
    },
    "data/gemini_consultant_state.json": {
        "risk": "MEDIUM",
        "max_change_pct": 50,
        "description": "Gemini consultant state",
    },
    "config.json": {
        "risk": "MEDIUM",
        "max_change_pct": 30,
        "description": "Main config file",
    },
}

# JSON schemas for validation
SCHEMAS = {
    "workers": {
        "required_keys": ["workers"],
        "workers_item_keys": ["name", "hwnd"],
        "valid_names": ["alpha", "beta", "gamma", "delta"],
    },
    "orchestrator": {
        "required_keys": ["hwnd"],
    },
    "brain_config": {
        "required_keys": [],  # Complex nested structure, just check it's valid JSON
        "min_size": 1000,
    },
    "todos": {
        "required_keys": ["todos"],
        "todos_item_keys": ["id", "title", "status"],
        "valid_statuses": ["pending", "active", "done", "cancelled"],
    },
    "agent_profiles": {
        "type": "dict_of_agents",
        "agent_keys": ["name", "role"],
    },
}


def _load_edit_log() -> List[Dict]:
    """Load edit history log."""
    if EDIT_LOG.exists():
        try:
            with open(EDIT_LOG, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_edit_log(log: List[Dict]):
    """Save edit history."""
    # Keep last 200 entries
    log = log[-200:]
    tmp = str(EDIT_LOG) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, default=str)
    os.replace(tmp, str(EDIT_LOG))


def _normalize_path(filepath: str) -> str:
    """Normalize a filepath relative to repo root."""
    p = Path(filepath).resolve()
    try:
        return str(p.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)


def _compute_change_pct(old_content: str, new_content: str) -> float:
    """Compute what percentage of the file changed (line-based)."""
    old_lines = set(old_content.splitlines())
    new_lines = set(new_content.splitlines())

    if not old_lines:
        return 100.0

    added = new_lines - old_lines
    removed = old_lines - new_lines
    changed_lines = len(added) + len(removed)
    total_lines = max(len(old_lines), len(new_lines), 1)

    return round((changed_lines / total_lines) * 100, 1)


def guard_edit(filepath: str, new_content: Optional[str] = None) -> Tuple[bool, List[str]]:
    """Pre-edit validation for a protected file.

    Args:
        filepath: Path to the file being edited
        new_content: New content (if available) for change % analysis

    Returns:
        (allowed: bool, warnings: list of warning strings)
    """
    rel_path = _normalize_path(filepath)
    warnings = []
    allowed = True

    # Check if file is protected
    config = PROTECTED_FILES.get(rel_path)
    if config is None:
        return True, []  # Not a protected file, allow

    risk = config["risk"]
    warnings.append(f"PROTECTED FILE [{risk}]: {config['description']}")

    # Auto-backup before edit
    try:
        from tools.skynet_backup import snapshot
        snap_id = snapshot(label=f"pre-edit-{Path(filepath).name}", auto=True)
        warnings.append(f"Auto-backup created: {snap_id}")
    except Exception as e:
        warnings.append(f"WARNING: Auto-backup failed: {e}")

    # Change percentage analysis
    abs_path = REPO_ROOT / rel_path
    if new_content and abs_path.exists():
        try:
            old_content = abs_path.read_text(encoding="utf-8", errors="replace")
            change_pct = _compute_change_pct(old_content, new_content)
            max_pct = config.get("max_change_pct", 50)

            if change_pct > max_pct:
                warnings.append(
                    f"BLOCKED: {change_pct}% change exceeds {max_pct}% limit for {risk} file"
                )
                allowed = False
            elif change_pct > max_pct * 0.7:
                warnings.append(
                    f"WARNING: {change_pct}% change approaching {max_pct}% limit"
                )
            else:
                warnings.append(f"Change: {change_pct}% (limit: {max_pct}%)")
        except Exception as e:
            warnings.append(f"Could not compute change %: {e}")

    # Log the edit attempt
    log = _load_edit_log()
    log.append({
        "timestamp": datetime.now().isoformat(),
        "file": rel_path,
        "risk": risk,
        "allowed": allowed,
        "warnings": warnings,
    })
    _save_edit_log(log)

    return allowed, warnings


def validate_json_schema(filepath: str) -> Tuple[bool, List[str]]:
    """Validate a JSON data file against its schema.

    Args:
        filepath: Path to the JSON file

    Returns:
        (valid: bool, errors: list of error strings)
    """
    rel_path = _normalize_path(filepath)
    config = PROTECTED_FILES.get(rel_path, {})
    schema_name = config.get("schema")
    errors = []

    abs_path = REPO_ROOT / rel_path if not Path(filepath).is_absolute() else Path(filepath)

    if not abs_path.exists():
        return False, [f"File not found: {filepath}"]

    # Parse JSON
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return False, [f"Invalid JSON: {e}"]

    if not schema_name or schema_name not in SCHEMAS:
        return True, []  # No schema defined, just check valid JSON

    schema = SCHEMAS[schema_name]

    # Check minimum size
    if "min_size" in schema:
        size = abs_path.stat().st_size
        if size < schema["min_size"]:
            errors.append(f"File too small: {size}B < {schema['min_size']}B minimum")

    # Check required top-level keys
    if isinstance(data, dict):
        for key in schema.get("required_keys", []):
            if key not in data:
                errors.append(f"Missing required key: '{key}'")

    # Schema-specific validation
    if schema_name == "workers":
        workers = data.get("workers", data) if isinstance(data, dict) else data
        if isinstance(workers, list):
            for i, w in enumerate(workers):
                if not isinstance(w, dict):
                    errors.append(f"Worker [{i}] is not a dict")
                    continue
                for key in schema.get("workers_item_keys", []):
                    if key not in w:
                        errors.append(f"Worker [{i}] missing '{key}'")
                name = w.get("name", "")
                if name and name not in schema["valid_names"]:
                    errors.append(f"Worker [{i}] invalid name: '{name}'")
                hwnd = w.get("hwnd")
                if hwnd is not None and not isinstance(hwnd, int):
                    errors.append(f"Worker [{i}] hwnd must be int, got {type(hwnd).__name__}")

    elif schema_name == "todos":
        todos = data.get("todos", [])
        if not isinstance(todos, list):
            errors.append("'todos' must be a list")
        else:
            for i, t in enumerate(todos[:5]):  # Check first 5
                if not isinstance(t, dict):
                    errors.append(f"Todo [{i}] is not a dict")
                    continue
                for key in schema.get("todos_item_keys", []):
                    if key not in t:
                        errors.append(f"Todo [{i}] missing '{key}'")
                status = t.get("status", "")
                if status and status not in schema["valid_statuses"]:
                    errors.append(f"Todo [{i}] invalid status: '{status}'")

    elif schema_name == "agent_profiles":
        if not isinstance(data, dict):
            errors.append("agent_profiles must be a dict")
        else:
            for agent_name, profile in data.items():
                if not isinstance(profile, dict):
                    errors.append(f"Agent '{agent_name}' profile is not a dict")
                    continue
                for key in schema.get("agent_keys", []):
                    if key not in profile:
                        errors.append(f"Agent '{agent_name}' missing '{key}'")

    valid = len(errors) == 0
    return valid, errors


def get_protected_files() -> List[Dict]:
    """Get list of all protected files with their status."""
    result = []
    for rel_path, config in PROTECTED_FILES.items():
        abs_path = REPO_ROOT / rel_path
        entry = {
            "path": rel_path,
            "risk": config["risk"],
            "description": config["description"],
            "max_change_pct": config.get("max_change_pct", 50),
            "exists": abs_path.exists(),
        }
        if abs_path.exists():
            stat = abs_path.stat()
            entry["size"] = stat.st_size
            entry["mtime"] = datetime.fromtimestamp(stat.st_mtime).isoformat()
            entry["sha256"] = hashlib.sha256(abs_path.read_bytes()).hexdigest()[:16]
        result.append(entry)

    result.sort(key=lambda x: ({"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}.get(x["risk"], 3),
                                x["path"]))
    return result


def main():
    parser = argparse.ArgumentParser(description="Skynet Edit Guard")
    sub = parser.add_subparsers(dest="command")

    # check
    check_p = sub.add_parser("check", help="Pre-edit validation")
    check_p.add_argument("file", help="File to check")

    # validate
    val_p = sub.add_parser("validate", help="Schema validation for JSON files")
    val_p.add_argument("file", help="JSON file to validate")

    # protect
    sub.add_parser("protect", help="Show all protected files")

    # history
    hist_p = sub.add_parser("history", help="Show edit history")
    hist_p.add_argument("file", nargs="?", help="Filter by file")

    args = parser.parse_args()

    if args.command == "check":
        allowed, warnings = guard_edit(args.file)
        for w in warnings:
            prefix = "  " if not w.startswith(("BLOCKED", "WARNING", "PROTECTED")) else ""
            print(f"{prefix}{w}")
        status = "ALLOWED" if allowed else "BLOCKED"
        print(f"\nResult: {status}")
        sys.exit(0 if allowed else 1)

    elif args.command == "validate":
        valid, errors = validate_json_schema(args.file)
        if valid:
            print(f"VALID: {args.file}")
        else:
            print(f"INVALID: {args.file}")
            for e in errors:
                print(f"  - {e}")
        sys.exit(0 if valid else 1)

    elif args.command == "protect":
        files = get_protected_files()
        print(f"{'Risk':<10} {'File':<45} {'Size':<10} {'Max%':<6} Description")
        print("-" * 110)
        for f in files:
            size = f"{f['size']//1024}KB" if f.get("exists") else "MISSING"
            print(f"{f['risk']:<10} {f['path']:<45} {size:<10} {f['max_change_pct']:<6} "
                  f"{f['description']}")

    elif args.command == "history":
        log = _load_edit_log()
        if args.file:
            norm = _normalize_path(args.file)
            log = [e for e in log if e.get("file") == norm]
        if not log:
            print("No edit history found.")
            return
        for entry in log[-20:]:
            status = "ALLOWED" if entry["allowed"] else "BLOCKED"
            print(f"[{entry['timestamp'][:19]}] [{status}] {entry['file']} ({entry['risk']})")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
