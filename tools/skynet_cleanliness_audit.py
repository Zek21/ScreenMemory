def guarded_publish(sender, topic, type_, content):
    import requests
    url = 'http://localhost:8420/bus/publish'
    payload = {
        'sender': sender,
        'topic': topic,
        'type': type_,
        'content': content
    }
    try:
        resp = requests.post(url, json=payload)
        return resp.status_code == 200
    except Exception as e:
        return False

def run_full_cleanliness_audit():
    audit_result = {}
    audit_result['cleanliness_audit'] = 'completed'
    audit_result['pid_check'] = audit_stale_pids()
    audit_result['dispatch_log'] = audit_dispatch_log()
    audit_result['orphaned_markdown'] = audit_orphaned_markdown()
    improvement_actions = []
    # Remove orphaned markdown files if any
    for md in audit_result['orphaned_markdown']:
        try:
            import os
            os.remove(md)
            improvement_actions.append(f"Removed orphaned markdown: {md}")
        except Exception:
            improvement_actions.append(f"Failed to remove: {md}")
    audit_result['improvement_actions'] = improvement_actions
    summary = f"Cleanliness audit complete. PID check: {len(audit_result['pid_check'])} files checked. Dispatch log: {len(audit_result['dispatch_log'])} stale dispatches. Orphaned markdown: {len(audit_result['orphaned_markdown'])} files. Improvements: {len(improvement_actions)} actions. signed:delta"
    guarded_publish('delta', 'orchestrator', 'result', summary)
    return audit_result
def audit_orphaned_markdown():
    import os
    import glob
    repo_root = os.path.dirname(os.path.abspath(__file__))
    md_files = glob.glob(os.path.join(repo_root, '*.md'))
    orphaned = []
    for md in md_files:
        # Exclude known non-orphan files
        basename = os.path.basename(md)
        if basename not in ['AGENTS.md', 'README.md', 'ISSUES_BY_FILE.md', 'CLAUDE_SKILLS_INTEGRATION_PROPOSAL.md']:
            orphaned.append(md)
    return orphaned
def audit_dispatch_log():
    import os
    import json
    import time
    dispatch_log_path = os.path.join('data', 'dispatch_log.json')
    if not os.path.exists(dispatch_log_path):
        return []
    with open(dispatch_log_path, 'r', encoding='utf-8') as f:
        try:
            dispatches = json.load(f)
        except Exception:
            return []
    now = time.time()
    stale = []
    for d in dispatches:
        created = d.get('created_at', 0)
        result = d.get('result', None)
        if not result and (now - created > 3600):
            stale.append(d)
    return stale
def audit_stale_pids():
    import glob
    import ctypes
    import os
    pid_files = glob.glob(os.path.join('data', '*.pid'))
    results = []
    for pid_file in pid_files:
        try:
            with open(pid_file, 'r') as f:
                pid = int(f.read().strip())
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle == 0:
                results.append({'file': pid_file, 'status': 'dead', 'action': 'remove'})
                os.remove(pid_file)
            else:
                ctypes.windll.kernel32.CloseHandle(handle)
                results.append({'file': pid_file, 'status': 'alive', 'action': 'keep'})
        except Exception as e:
            results.append({'file': pid_file, 'status': 'corrupt', 'action': 'remove', 'error': str(e)})
            try:
                os.remove(pid_file)
            except Exception:
                pass
    return results
"""Skynet Workspace Cleanliness Audit Tool (Rule 0.9).

Scans the Skynet system for uncleared items across 6 categories:
    1. Pending/active TODOs in data/todos.json
  2. Stale incident/remediation/containment MDs in repo root
  3. Stale tasks in data/task_queue.json
  4. Dispatches with no results in data/dispatch_log.json
  5. Stale PID files for dead processes
  6. Uncleared bus alerts (repeated IDLE_UNPRODUCTIVE, DEAD, etc.)

Reports a per-agent inventory of uncleared items with recommended actions.
Can be run standalone or imported as a library for the monitor daemon.

Usage:
    python tools/skynet_cleanliness_audit.py              # Full audit report
    python tools/skynet_cleanliness_audit.py --quiet       # Summary only
    python tools/skynet_cleanliness_audit.py --fix         # Auto-fix safe items
    python tools/skynet_cleanliness_audit.py --json        # JSON output
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STALE_DISPATCH_THRESHOLD_S = 3600  # 1 hour
STALE_TODO_THRESHOLD_S = 86400  # 24 hours

# Incident/remediation MD patterns in repo root
INCIDENT_MD_PATTERNS = [
    r"INCIDENT.*\.md$",
    r"URGENT.*\.md$",
    r"SUPER_URGENT.*\.md$",
    r"CONTAINMENT.*\.md$",
    r"REMEDIATION.*\.md$",
    r"FAILURE.*\.md$",
    r"IMPROVEMENT_.*\.md$",
    r"OPERATION_.*\.md$",
    r"SIGNATURE_.*\.md$",
    r"DASHBOARD_.*PLAN.*\.md$",
]

# MDs to keep (known active docs)
KEEP_MDS = frozenset({
    "README.md",
    "AGENTS.md",
    "SKYNET_CLAW_AND_BEYOND.md",
    "SKYNET_CONTEXT_EFFICIENCY_RESEARCH.md",
    "SKYNET_CONTROL_PLANE_PROPOSAL.md",
    "SKYNET_SUPREMACY_PROPOSAL.md",
})


def _is_alive(pid: int) -> bool:
    """Check if a process is still running."""
    try:
        import ctypes
        PROCESS_QUERY_LIMITED = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == STILL_ACTIVE
    except Exception:
        return False


def audit_todos() -> list:
    """Check data/todos.json for pending/active items."""
    issues = []
    todos_file = DATA_DIR / "todos.json"
    if not todos_file.exists():
        return issues
    try:
        raw = json.loads(todos_file.read_text(encoding="utf-8"))
        todos = raw.get("todos", raw) if isinstance(raw, dict) else raw
        if not isinstance(todos, list):
            return issues
        for t in todos:
            if not isinstance(t, dict):
                continue
            status = t.get("status", "")
            if status in ("pending", "active"):
                issues.append({
                    "category": "todo",
                    "id": t.get("id", "unknown"),
                    "title": t.get("title", "")[:100],
                    "status": status,
                    "assignee": t.get("assignee", t.get("worker", "unassigned")),
                    "action": "complete_or_cancel",
                })
    except Exception:
        pass
    return issues


def audit_incident_mds() -> list:
    """Check repo root for stale incident/remediation MDs."""
    issues = []
    for f in ROOT.iterdir():
        if not f.is_file() or not f.suffix == ".md":
            continue
        if f.name in KEEP_MDS:
            continue
        for pattern in INCIDENT_MD_PATTERNS:
            if re.match(pattern, f.name):
                issues.append({
                    "category": "incident_md",
                    "file": f.name,
                    "size_bytes": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat()[:19],
                    "action": "archive_or_delete",
                })
                break
    return issues


def audit_task_queue() -> list:
    """Check data/task_queue.json for stale pending tasks."""
    issues = []
    queue_file = DATA_DIR / "task_queue.json"
    if not queue_file.exists():
        return issues
    try:
        tasks = json.loads(queue_file.read_text(encoding="utf-8"))
        if not isinstance(tasks, list):
            return issues
        now = time.time()
        for t in tasks:
            if not isinstance(t, dict):
                continue
            if t.get("status") == "pending":
                created = t.get("created_at", "")
                issues.append({
                    "category": "task_queue",
                    "task_id": t.get("task_id", "unknown"),
                    "task": str(t.get("task", ""))[:80],
                    "status": "pending",
                    "action": "dispatch_or_cancel",
                })
    except Exception:
        pass
    return issues


def audit_dispatch_log() -> list:
    """Check data/dispatch_log.json for dispatches with no results."""
    issues = []
    log_file = DATA_DIR / "dispatch_log.json"
    if not log_file.exists():
        return issues
    try:
        entries = json.loads(log_file.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            return issues
        now = time.time()
        for e in entries:
            if not isinstance(e, dict):
                continue
            if e.get("result_received"):
                continue
            if not e.get("success"):
                continue
            ts = e.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age_s = now - dt.timestamp()
            except Exception:
                age_s = STALE_DISPATCH_THRESHOLD_S + 1
            if age_s > STALE_DISPATCH_THRESHOLD_S:
                issues.append({
                    "category": "dispatch_no_result",
                    "worker": e.get("worker", "unknown"),
                    "task_summary": str(e.get("task_summary", ""))[:60],
                    "timestamp": ts[:19],
                    "age_hours": round(age_s / 3600, 1),
                    "action": "mark_stale_or_redispatch",
                })
    except Exception:
        pass
    return issues


def audit_stale_pids() -> list:
    """Check data/*.pid for dead processes."""
    issues = []
    for f in DATA_DIR.glob("*.pid"):
        try:
            pid = int(f.read_text().strip())
            if not _is_alive(pid):
                issues.append({
                    "category": "stale_pid",
                    "file": f.name,
                    "pid": pid,
                    "daemon": f.stem,
                    "action": "remove_pid_file",
                })
        except (ValueError, OSError):
            issues.append({
                "category": "stale_pid",
                "file": f.name,
                "pid": None,
                "daemon": f.stem,
                "action": "remove_corrupt_pid_file",
            })
    return issues


def audit_root_md_clutter() -> list:
    """Check for non-essential MD files cluttering the repo root."""
    issues = []
    essential = KEEP_MDS | {"ISSUES_BY_FILE.md"}
    for f in ROOT.iterdir():
        if not f.is_file() or f.suffix != ".md":
            continue
        if f.name in essential:
            continue
        # Already caught by incident_md patterns? Skip duplicate
        is_incident = False
        for pattern in INCIDENT_MD_PATTERNS:
            if re.match(pattern, f.name):
                is_incident = True
                break
        if not is_incident:
            issues.append({
                "category": "root_md_clutter",
                "file": f.name,
                "size_bytes": f.stat().st_size,
                "action": "review_and_archive",
            })
    return issues


def run_audit(quiet: bool = False) -> dict:
    """Run the full cleanliness audit. Returns structured report."""
    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "categories": {},
        "total_issues": 0,
        "issues": [],
    }

    checks = [
        ("todos", audit_todos),
        ("incident_mds", audit_incident_mds),
        ("task_queue", audit_task_queue),
        ("dispatch_no_result", audit_dispatch_log),
        ("stale_pids", audit_stale_pids),
        ("root_md_clutter", audit_root_md_clutter),
    ]

    for name, fn in checks:
        issues = fn()
        results["categories"][name] = len(issues)
        results["issues"].extend(issues)

    results["total_issues"] = len(results["issues"])

    if not quiet:
        print(f"\n=== Skynet Workspace Cleanliness Audit ===")
        print(f"Timestamp: {results['timestamp'][:19]}")
        print(f"Total uncleared items: {results['total_issues']}")
        print()
        for cat, count in results["categories"].items():
            status = "CLEAN" if count == 0 else f"{count} ISSUES"
            print(f"  {cat:<25} {status}")
        if results["issues"]:
            print(f"\n--- Detailed Issues ---")
            for issue in results["issues"]:
                cat = issue.get("category", "?")
                action = issue.get("action", "?")
                if cat == "todo":
                    print(f"  [{cat}] {issue['id']}: {issue['title']} -> {action}")
                elif cat == "incident_md":
                    print(f"  [{cat}] {issue['file']} ({issue['size_bytes']}B) -> {action}")
                elif cat == "task_queue":
                    print(f"  [{cat}] {issue['task_id']}: {issue['task']} -> {action}")
                elif cat == "dispatch_no_result":
                    print(f"  [{cat}] {issue['worker']} {issue['task_summary']} ({issue['age_hours']}h ago) -> {action}")
                elif cat == "stale_pid":
                    print(f"  [{cat}] {issue['file']} (PID {issue['pid']}) -> {action}")
                elif cat == "root_md_clutter":
                    print(f"  [{cat}] {issue['file']} ({issue['size_bytes']}B) -> {action}")
        else:
            print("\n  WORKSPACE IS CLEAN")

    return results


def fix_safe_items(dry_run: bool = False) -> list:
    """Auto-fix safe items (stale PIDs, etc.). Returns list of actions taken."""
    actions = []
    # Fix stale PIDs
    for issue in audit_stale_pids():
        pid_file = DATA_DIR / issue["file"]
        if dry_run:
            actions.append(f"[DRY RUN] Would remove {issue['file']}")
        else:
            try:
                pid_file.unlink()
                actions.append(f"Removed stale PID file: {issue['file']}")
            except OSError as e:
                actions.append(f"Failed to remove {issue['file']}: {e}")
    return actions


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skynet Workspace Cleanliness Audit (Rule 0.9)")
    parser.add_argument("--quiet", "-q", action="store_true", help="Summary output only")
    parser.add_argument("--fix", action="store_true", help="Auto-fix safe items (stale PIDs)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if args.fix:
        actions = fix_safe_items()
        for a in actions:
            print(f"  {a}")
        if not actions:
            print("  No safe items to fix.")
        return

    results = run_audit(quiet=args.json)

    if args.json:
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
