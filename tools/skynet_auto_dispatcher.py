#!/usr/bin/env python3
"""skynet_auto_dispatcher.py -- Automated scan→prioritize→dispatch loop.

Reads upgrade scan results (from Alpha's skynet_upgrade_scanner.py),
groups findings by severity and file, generates TODO items in data/todos.json,
and can auto-dispatch to idle workers via skynet_dispatch.py.

Closes the improvement loop: scan → prioritize → dispatch → execute → re-scan.

Usage:
    python tools/skynet_auto_dispatcher.py scan          # Read scan results, show summary
    python tools/skynet_auto_dispatcher.py generate      # Generate TODO items from findings
    python tools/skynet_auto_dispatcher.py dispatch      # Auto-dispatch to idle workers
    python tools/skynet_auto_dispatcher.py full           # scan + generate + dispatch
    python tools/skynet_auto_dispatcher.py --dry-run full # Preview without changes
    python tools/skynet_auto_dispatcher.py --max-todos 20 generate  # Limit TODO generation
# signed: gamma
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
SCAN_RESULTS_FILE = DATA_DIR / "upgrade_scan_results.json"
TODOS_FILE = DATA_DIR / "todos.json"
SECURITY_AUDIT_FILE = DATA_DIR / "security_audit.json"
BUS_URL = "http://localhost:8420"
WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

# Severity priority order
SEVERITY_PRIORITY = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}

# Categories that map to worker specializations
WORKER_SPECIALIZATIONS = {
    "alpha": {"frontend", "dashboard", "ui", "architecture", "systems"},
    "beta": {"backend", "infrastructure", "daemons", "python", "resilience"},
    "gamma": {"security", "analysis", "optimization", "performance", "research"},
    "delta": {"testing", "validation", "auditing", "config", "docs"},
}

# Max findings per TODO to keep tasks manageable
MAX_FINDINGS_PER_TODO = 5


# ── Scan Reader ──────────────────────────────────────────────────

def load_scan_results() -> Optional[dict]:
    """Load upgrade scan results from disk."""  # signed: gamma
    if not SCAN_RESULTS_FILE.exists():
        print(f"[auto-dispatch] No scan results at {SCAN_RESULTS_FILE}")
        return None
    try:
        return json.loads(SCAN_RESULTS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[auto-dispatch] Failed to load scan results: {e}")
        return None


def load_security_audit() -> Optional[dict]:
    """Load security audit results from disk."""  # signed: gamma
    if not SECURITY_AUDIT_FILE.exists():
        return None
    try:
        return json.loads(SECURITY_AUDIT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def group_findings(findings: list) -> dict:
    """Group findings by severity → file → category.

    Returns:
        dict: {severity: {file: [findings]}}
    """  # signed: gamma
    grouped = defaultdict(lambda: defaultdict(list))
    for f in findings:
        sev = f.get("severity", "MEDIUM").upper()
        fname = f.get("file", "unknown")
        grouped[sev][fname].append(f)
    return dict(grouped)


def summarize_scan(scan_data: dict) -> dict:
    """Produce a summary of scan results."""  # signed: gamma
    findings = scan_data.get("findings", [])
    by_severity = scan_data.get("findings_by_severity", {})
    by_category = defaultdict(int)
    by_file = defaultdict(int)

    for f in findings:
        by_category[f.get("category", "unknown")] += 1
        by_file[f.get("file", "unknown")] += 1

    top_files = sorted(by_file.items(), key=lambda x: x[1], reverse=True)[:10]

    return {
        "total_findings": len(findings),
        "by_severity": dict(by_severity),
        "by_category": dict(by_category),
        "top_files": top_files,
        "scan_timestamp": scan_data.get("timestamp", "unknown"),
    }


# ── TODO Generator ───────────────────────────────────────────────

def load_todos() -> dict:
    """Load existing todos from data/todos.json."""  # signed: gamma
    if not TODOS_FILE.exists():
        return {"todos": []}
    try:
        return json.loads(TODOS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"todos": []}


def save_todos(todo_data: dict):
    """Save todos to data/todos.json."""  # signed: gamma
    TODOS_FILE.write_text(json.dumps(todo_data, indent=2), encoding="utf-8")


def _route_to_worker(category: str, file_path: str) -> str:
    """Route a finding to the best-matched worker based on category and file."""  # signed: gamma
    cat_lower = category.lower()
    file_lower = file_path.lower()

    # Direct category matches
    for worker, specialties in WORKER_SPECIALIZATIONS.items():
        if any(s in cat_lower for s in specialties):
            return worker

    # File-path heuristics
    if "test" in file_lower or "verify" in file_lower:
        return "delta"
    if "daemon" in file_lower or "boot" in file_lower:
        return "beta"
    if "security" in file_lower or "guard" in file_lower:
        return "gamma"
    if "dashboard" in file_lower or "ui" in file_lower:
        return "alpha"

    # Round-robin fallback
    return WORKER_NAMES[hash(file_path) % len(WORKER_NAMES)]


def generate_todos(scan_data: dict, max_todos: int = 30, dry_run: bool = False) -> list:
    """Generate TODO items from scan findings grouped by severity and file.

    Groups related findings into actionable work items, assigns to appropriate
    workers based on specialization, and writes to data/todos.json.

    Returns:
        list: Generated TODO items
    """  # signed: gamma
    findings = scan_data.get("findings", [])
    if not findings:
        print("[auto-dispatch] No findings to generate TODOs from.")
        return []

    grouped = group_findings(findings)
    existing = load_todos()
    existing_ids = {t["id"] for t in existing.get("todos", [])}
    new_todos = []
    todo_count = 0

    # Process by severity priority
    for severity in sorted(grouped.keys(),
                           key=lambda s: SEVERITY_PRIORITY.get(s, 99)):
        if todo_count >= max_todos:
            break

        files = grouped[severity]
        for file_path, file_findings in files.items():
            if todo_count >= max_todos:
                break

            # Group findings into batches of MAX_FINDINGS_PER_TODO
            for batch_idx in range(0, len(file_findings), MAX_FINDINGS_PER_TODO):
                if todo_count >= max_todos:
                    break

                batch = file_findings[batch_idx:batch_idx + MAX_FINDINGS_PER_TODO]
                categories = list(set(f.get("category", "") for f in batch))
                todo_id = f"autoscan_{severity.lower()}_{todo_count}"

                if todo_id in existing_ids:
                    continue

                # Build descriptive title
                short_file = Path(file_path).name
                cat_str = ", ".join(categories[:3])
                title = (f"[{severity}] Fix {len(batch)} {cat_str} issues "
                         f"in {short_file}")

                # Build description with specific line numbers
                description_parts = []
                for f in batch:
                    line = f.get("line", 0)
                    desc = f.get("description", "")[:100]
                    fix = f.get("suggested_fix", "")[:100]
                    description_parts.append(
                        f"  L{line}: {desc}" + (f" → {fix}" if fix else ""))

                worker = _route_to_worker(cat_str, file_path)

                todo = {
                    "id": todo_id,
                    "title": title,
                    "status": "pending",
                    "assignee": worker,
                    "priority": severity.lower(),
                    "wave": "autoscan",
                    "description": "\n".join(description_parts),
                    "file": file_path,
                    "finding_count": len(batch),
                    "categories": categories,
                    "generated_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                new_todos.append(todo)
                todo_count += 1

    if new_todos and not dry_run:
        existing["todos"].extend(new_todos)
        save_todos(existing)
        print(f"[auto-dispatch] Generated {len(new_todos)} TODO items → "
              f"{TODOS_FILE}")
    elif dry_run:
        print(f"[auto-dispatch] DRY RUN: Would generate {len(new_todos)} "
              f"TODO items")

    return new_todos


# ── Auto-Dispatcher ──────────────────────────────────────────────

def get_idle_workers() -> list:
    """Get currently idle workers from Skynet status."""  # signed: gamma
    try:
        from urllib.request import urlopen
        with urlopen(f"{BUS_URL}/status", timeout=5) as resp:
            data = json.loads(resp.read())
        agents = data.get("agents", {})
        idle = [name for name, info in agents.items()
                if isinstance(info, dict) and
                info.get("status", "").upper() == "IDLE" and
                name in WORKER_NAMES]
        return idle
    except Exception:
        return []


def dispatch_pending_todos(dry_run: bool = False, max_dispatch: int = 4) -> list:
    """Dispatch pending autoscan TODO items to idle workers.

    Matches pending TODOs to idle workers based on assignee preference.
    Uses skynet_dispatch.py for actual delivery.

    Returns:
        list: Dispatched todo items
    """  # signed: gamma
    todos_data = load_todos()
    pending = [t for t in todos_data.get("todos", [])
               if t.get("status") == "pending" and t.get("wave") == "autoscan"]

    if not pending:
        print("[auto-dispatch] No pending autoscan TODOs to dispatch.")
        return []

    idle = get_idle_workers()
    if not idle and not dry_run:
        print("[auto-dispatch] No idle workers available.")
        return []

    dispatched = []
    dispatch_count = 0

    # Sort by severity priority
    pending.sort(key=lambda t: SEVERITY_PRIORITY.get(
        t.get("priority", "medium").upper(), 99))

    for todo in pending:
        if dispatch_count >= max_dispatch:
            break

        assignee = todo.get("assignee", "")
        if assignee in idle:
            target_worker = assignee
        elif idle:
            target_worker = idle[0]
        else:
            if dry_run:
                target_worker = todo.get("assignee", "alpha")
            else:
                continue

        # Build dispatch task
        task_text = (
            f"FIX {todo['title']}.\n"
            f"File: {todo.get('file', 'unknown')}\n"
            f"Details:\n{todo.get('description', 'See TODO')}\n"
            f"After fixing, run py_compile on changed files to verify."
        )

        if dry_run:
            print(f"[DRY RUN] Would dispatch to {target_worker}: "
                  f"{todo['title'][:80]}")
        else:
            try:
                from tools.skynet_dispatch import dispatch_to_worker
                success = dispatch_to_worker(target_worker, task_text)
                if success:
                    todo["status"] = "active"
                    todo["assignee"] = target_worker
                    todo["dispatched_at"] = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    print(f"[auto-dispatch] → {target_worker}: {todo['title'][:80]}")
                else:
                    print(f"[auto-dispatch] FAILED dispatch to {target_worker}")
                    continue
            except Exception as e:
                print(f"[auto-dispatch] Dispatch error: {e}")
                continue

        dispatched.append(todo)
        dispatch_count += 1
        if target_worker in idle:
            idle.remove(target_worker)

    if dispatched and not dry_run:
        save_todos(todos_data)

    return dispatched


# ── Commands ─────────────────────────────────────────────────────

def cmd_scan():
    """Load and summarize scan results."""  # signed: gamma
    scan_data = load_scan_results()
    if not scan_data:
        return

    summary = summarize_scan(scan_data)
    print("\n" + "=" * 60)
    print("  UPGRADE SCAN SUMMARY")
    print("=" * 60)
    print(f"Total findings: {summary['total_findings']}")
    print(f"Scan timestamp: {summary['scan_timestamp']}")
    print(f"\nBy severity:")
    for sev, count in sorted(summary["by_severity"].items(),
                             key=lambda x: SEVERITY_PRIORITY.get(x[0], 99)):
        print(f"  {sev:>10}: {count}")
    print(f"\nTop categories:")
    for cat, count in sorted(summary["by_category"].items(),
                             key=lambda x: x[1], reverse=True)[:10]:
        print(f"  {cat:.<40} {count}")
    print(f"\nTop files:")
    for fname, count in summary["top_files"]:
        print(f"  {Path(fname).name:.<40} {count}")
    print("=" * 60)

    # Also load security audit if available
    sec = load_security_audit()
    if sec:
        print(f"\nSecurity audit: {sec.get('summary', {})}")

    return summary


def cmd_generate(max_todos=30, dry_run=False):
    """Generate TODO items from scan findings."""  # signed: gamma
    scan_data = load_scan_results()
    if not scan_data:
        return []
    return generate_todos(scan_data, max_todos, dry_run)


def cmd_dispatch(dry_run=False, max_dispatch=4):
    """Dispatch pending TODOs to idle workers."""  # signed: gamma
    return dispatch_pending_todos(dry_run, max_dispatch)


def cmd_full(max_todos=30, dry_run=False, max_dispatch=4):
    """Full pipeline: scan → generate → dispatch."""  # signed: gamma
    print("[auto-dispatch] Full pipeline: scan → generate → dispatch")
    cmd_scan()
    new_todos = cmd_generate(max_todos, dry_run)
    if new_todos:
        dispatched = cmd_dispatch(dry_run, max_dispatch)
        return dispatched
    return []


# ── Main ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Skynet Auto-Dispatcher: scan → prioritize → dispatch")
    parser.add_argument("command", choices=["scan", "generate", "dispatch", "full"],
                        help="Command to run")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without making changes")
    parser.add_argument("--max-todos", type=int, default=30,
                        help="Max TODO items to generate (default: 30)")
    parser.add_argument("--max-dispatch", type=int, default=4,
                        help="Max workers to dispatch to (default: 4)")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON")
    args = parser.parse_args()

    if args.command == "scan":
        result = cmd_scan()
    elif args.command == "generate":
        result = cmd_generate(args.max_todos, args.dry_run)
    elif args.command == "dispatch":
        result = cmd_dispatch(args.dry_run, args.max_dispatch)
    elif args.command == "full":
        result = cmd_full(args.max_todos, args.dry_run, args.max_dispatch)
    else:
        parser.print_help()
        return

    if args.json and result:
        if isinstance(result, list):
            print(json.dumps(result, indent=2, default=str))
        elif isinstance(result, dict):
            print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
