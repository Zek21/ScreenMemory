"""Boids-inspired conflict avoidance for multi-worker concurrent editing.

Implements three flocking rules adapted from Reynolds' Boids model:

  1. SEPARATION — Detect when 2+ workers target the same file or module.
     Flag conflict risk before edits collide.
  2. ALIGNMENT — Workers editing related files (same directory, same import
     graph) should share context via bus so their changes stay compatible.
  3. COHESION — Workers on the same logical feature should stay aware of
     each other's progress to avoid drift and duplication.

Primary API:
    check_conflicts(active_tasks)         → list of ConflictWarning dicts
    suggest_coordination(worker_tasks)    → list of CoordinationSuggestion dicts
    scan_live()                           → run both checks against live system state

Usage:
    python tools/skynet_flocking.py check          # live conflict scan
    python tools/skynet_flocking.py suggest        # coordination suggestions
    python tools/skynet_flocking.py scan           # full scan (check + suggest)
    python tools/skynet_flocking.py monitor        # continuous monitoring loop
"""
# signed: gamma

import json
import os
import re
import time
import argparse
from collections import defaultdict
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
FLOCKING_LOG_PATH = DATA_DIR / "flocking_log.json"
BUS_URL = "http://localhost:8420"

WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

# ── Separation thresholds ────────────────────────────────────────
CONFLICT_LEVEL_CRITICAL = "CRITICAL"   # same file, both actively editing
CONFLICT_LEVEL_HIGH = "HIGH"           # same file, one editing one queued
CONFLICT_LEVEL_MEDIUM = "MEDIUM"       # same directory / tightly coupled files
CONFLICT_LEVEL_LOW = "LOW"             # same broad module (e.g. tools/)

# ── File-path extraction patterns ────────────────────────────────
# Matches common path references in task descriptions
_PATH_PATTERNS = [
    # Explicit paths: tools/skynet_dispatch.py, core/ocr.py, Skynet/server.go
    re.compile(r'(?:^|\s|[`\'"])([a-zA-Z_][\w./\\-]*\.(?:py|go|js|ts|html|css|json|md|ps1))\b'),
    # Directory refs: tools/, core/, Skynet/
    re.compile(r'(?:^|\s)([a-zA-Z_][\w-]*/)\s'),
    # data/ file refs with extension
    re.compile(r'(data/[\w./\\-]+\.(?:json|jsonl|txt|md|csv))'),
]

# Known tightly-coupled file pairs (edits to one often require edits to the other)
_COUPLED_FILES = {
    "tools/skynet_dispatch.py": ["tools/skynet_spam_guard.py", "tools/skynet_monitor.py"],
    "tools/skynet_spam_guard.py": ["tools/skynet_dispatch.py", "tools/skynet_scoring.py"],
    "tools/skynet_monitor.py": ["tools/skynet_dispatch.py", "tools/skynet_self.py"],
    "tools/skynet_self.py": ["tools/skynet_collective.py", "tools/skynet_monitor.py"],
    "tools/skynet_start.py": ["tools/new_chat.ps1", "tools/skynet_monitor.py"],
    "tools/skynet_realtime.py": ["tools/orch_realtime.py"],
    "tools/orch_realtime.py": ["tools/skynet_realtime.py", "tools/skynet_dispatch.py"],
    "core/input_guard.py": ["core/security.py"],
    "core/capture.py": ["core/ocr.py", "core/change_detector.py"],
    "Skynet/server.go": ["god_console.py", "dashboard.html"],
    "god_console.py": ["Skynet/server.go", "dashboard.html"],
    "data/agent_profiles.json": ["tools/skynet_specialization.py"],
    "data/workers.json": ["tools/skynet_dispatch.py", "tools/skynet_start.py"],
    "AGENTS.md": [".github/copilot-instructions.md"],
}

# Feature keyword clusters — tasks mentioning these together are likely same feature
_FEATURE_CLUSTERS = {
    "dispatch": {"dispatch", "ghost_type", "delivery", "clipboard", "paste", "worker"},
    "monitoring": {"monitor", "watchdog", "health", "heartbeat", "daemon", "liveness"},
    "bus": {"bus", "publish", "subscribe", "message", "ring", "sse", "stream"},
    "security": {"security", "injection", "guard", "sanitize", "audit", "hmac"},
    "scoring": {"score", "scoring", "points", "deduction", "penalty", "award"},
    "dashboard": {"dashboard", "god_console", "frontend", "html", "css", "ui"},
    "boot": {"boot", "start", "startup", "init", "orch-start", "skynet-start"},
    "cnp": {"contract", "bid", "announce", "award", "routing", "specialization"},
    "self_awareness": {"self", "identity", "consciousness", "introspect", "pulse"},
    "convene": {"convene", "consensus", "vote", "gate", "proposal", "collaborate"},
}
# signed: gamma


def extract_file_paths(text: str) -> list[str]:
    """Extract file/directory paths mentioned in a task description.

    Normalises backslashes to forward slashes and lowercases for comparison.

    Returns:
        Sorted deduplicated list of path strings.
    """
    paths = set()
    for pat in _PATH_PATTERNS:
        for m in pat.finditer(text):
            p = m.group(1).replace("\\", "/").strip("'\"` ")
            if p and len(p) > 2:
                paths.add(p)
    return sorted(paths)
    # signed: gamma


def extract_feature_keywords(text: str) -> set[str]:
    """Identify which feature clusters a task description relates to.

    Returns set of cluster names (e.g. {'dispatch', 'bus'}).
    """
    text_lower = text.lower()
    words = set(re.findall(r'[a-z_]+', text_lower))
    matched = set()
    for cluster_name, keywords in _FEATURE_CLUSTERS.items():
        if words & keywords:
            matched.add(cluster_name)
    return matched
    # signed: gamma


def _get_directory(path: str) -> str:
    """Return the parent directory portion of a path."""
    parts = path.replace("\\", "/").rsplit("/", 1)
    return parts[0] if len(parts) > 1 else ""
    # signed: gamma


def _are_coupled(file_a: str, file_b: str) -> bool:
    """Check if two files are in the known-coupled registry."""
    coupled_a = _COUPLED_FILES.get(file_a, [])
    coupled_b = _COUPLED_FILES.get(file_b, [])
    return file_b in coupled_a or file_a in coupled_b
    # signed: gamma


# ── RULE 1: SEPARATION ──────────────────────────────────────────

def check_conflicts(active_tasks: dict[str, str]) -> list[dict]:
    """Detect file-level conflicts between workers' active tasks.

    Implements the SEPARATION rule: when two or more workers target the
    same file (or tightly coupled files), flag the conflict with a
    severity level.

    Args:
        active_tasks: Mapping of worker_name → task_description.
                      Only include workers that are actively working.
                      Example: {"alpha": "Fix bug in tools/skynet_dispatch.py",
                                "beta": "Refactor tools/skynet_dispatch.py error handling"}

    Returns:
        List of conflict warning dicts, each with:
            level: CRITICAL / HIGH / MEDIUM / LOW
            workers: list of worker names in conflict
            files: list of conflicting file paths
            reason: human-readable explanation
            rule: "SEPARATION"
    """
    if not active_tasks or len(active_tasks) < 2:
        return []

    # Extract file paths per worker
    worker_files: dict[str, list[str]] = {}
    for worker, task in active_tasks.items():
        worker_files[worker] = extract_file_paths(task)

    warnings = []
    workers = list(active_tasks.keys())

    for i in range(len(workers)):
        for j in range(i + 1, len(workers)):
            w_a, w_b = workers[i], workers[j]
            files_a = set(worker_files.get(w_a, []))
            files_b = set(worker_files.get(w_b, []))

            # CRITICAL: exact same file
            shared_files = files_a & files_b
            if shared_files:
                warnings.append({
                    "level": CONFLICT_LEVEL_CRITICAL,
                    "workers": [w_a, w_b],
                    "files": sorted(shared_files),
                    "reason": (
                        f"{w_a} and {w_b} are both targeting "
                        f"{', '.join(sorted(shared_files))} — merge conflict risk"
                    ),
                    "rule": "SEPARATION",
                })
                continue  # no need to check lower levels for this pair

            # HIGH: same directory
            dirs_a = {_get_directory(f) for f in files_a if f}
            dirs_b = {_get_directory(f) for f in files_b if f}
            shared_dirs = dirs_a & dirs_b - {""}
            if shared_dirs:
                warnings.append({
                    "level": CONFLICT_LEVEL_MEDIUM,
                    "workers": [w_a, w_b],
                    "files": sorted(files_a | files_b),
                    "reason": (
                        f"{w_a} and {w_b} editing files in same directory "
                        f"({', '.join(sorted(shared_dirs))}) — review for side effects"
                    ),
                    "rule": "SEPARATION",
                })

            # MEDIUM: known coupled files
            for fa in files_a:
                for fb in files_b:
                    if _are_coupled(fa, fb):
                        warnings.append({
                            "level": CONFLICT_LEVEL_HIGH,
                            "workers": [w_a, w_b],
                            "files": [fa, fb],
                            "reason": (
                                f"{w_a}({fa}) and {w_b}({fb}) are "
                                f"editing tightly coupled files — coordinate changes"
                            ),
                            "rule": "SEPARATION",
                        })

    # Sort by severity
    level_order = {
        CONFLICT_LEVEL_CRITICAL: 0,
        CONFLICT_LEVEL_HIGH: 1,
        CONFLICT_LEVEL_MEDIUM: 2,
        CONFLICT_LEVEL_LOW: 3,
    }
    warnings.sort(key=lambda w: level_order.get(w["level"], 99))
    return warnings
    # signed: gamma


# ── RULE 2: ALIGNMENT + RULE 3: COHESION ────────────────────────

def suggest_coordination(worker_tasks: dict[str, str]) -> list[dict]:
    """Suggest coordination actions between workers on related work.

    Implements two rules:
      ALIGNMENT — Workers editing related files should share context.
      COHESION  — Workers on the same feature should track each other.

    Args:
        worker_tasks: Mapping of worker_name → task_description (all workers,
                      including idle ones with empty strings).

    Returns:
        List of suggestion dicts, each with:
            type: "ALIGNMENT" or "COHESION"
            workers: list of worker names that should coordinate
            action: recommended coordination action
            feature: (COHESION only) detected shared feature cluster
            files: (ALIGNMENT only) related files involved
    """
    active = {w: t for w, t in worker_tasks.items() if t and t.strip()}
    if len(active) < 2:
        return []

    suggestions = []
    workers = list(active.keys())

    # ── ALIGNMENT: related-file context sharing ──
    worker_files = {w: extract_file_paths(t) for w, t in active.items()}

    for i in range(len(workers)):
        for j in range(i + 1, len(workers)):
            w_a, w_b = workers[i], workers[j]
            files_a = worker_files.get(w_a, [])
            files_b = worker_files.get(w_b, [])

            # Check if any file in A is coupled to any file in B
            coupled_pairs = []
            for fa in files_a:
                for fb in files_b:
                    if _are_coupled(fa, fb):
                        coupled_pairs.append((fa, fb))

            if coupled_pairs:
                all_files = sorted({f for pair in coupled_pairs for f in pair})
                suggestions.append({
                    "type": "ALIGNMENT",
                    "workers": [w_a, w_b],
                    "action": (
                        f"Share change context via bus — {w_a} and {w_b} are editing "
                        f"coupled files ({', '.join(all_files)}). Post intended changes "
                        f"to topic=workers type=alignment before committing."
                    ),
                    "files": all_files,
                })

    # ── COHESION: same-feature awareness ──
    worker_features = {w: extract_feature_keywords(t) for w, t in active.items()}

    for i in range(len(workers)):
        for j in range(i + 1, len(workers)):
            w_a, w_b = workers[i], workers[j]
            shared_features = worker_features[w_a] & worker_features[w_b]
            if shared_features:
                for feat in shared_features:
                    suggestions.append({
                        "type": "COHESION",
                        "workers": [w_a, w_b],
                        "action": (
                            f"Feature overlap detected: both {w_a} and {w_b} are working "
                            f"on '{feat}'-related tasks. Post progress updates to "
                            f"topic=workers type=cohesion_sync to avoid duplication."
                        ),
                        "feature": feat,
                    })

    # Deduplicate cohesion suggestions (same worker pair, same feature)
    seen = set()
    deduped = []
    for s in suggestions:
        key = (tuple(sorted(s["workers"])), s.get("feature", ""), s["type"])
        if key not in seen:
            seen.add(key)
            deduped.append(s)

    return deduped
    # signed: gamma


# ── Live system integration ──────────────────────────────────────

def _get_active_tasks_from_live() -> dict[str, str]:
    """Read active worker tasks from live system state.

    Sources checked in order:
      1. data/realtime.json (zero-network, preferred)
      2. data/dispatch_log.json (recent dispatches)
    """
    active = {}

    # Source 1: realtime.json
    rt_path = DATA_DIR / "realtime.json"
    if rt_path.exists():
        try:
            with open(rt_path, "r", encoding="utf-8") as f:
                rt = json.load(f)
            workers = rt.get("workers", {})
            for name in WORKER_NAMES:
                w = workers.get(name, {})
                status = w.get("status", "IDLE").upper()
                task = w.get("current_task", "")
                if status in ("PROCESSING", "ACTIVE", "BUSY") and task:
                    active[name] = task
        except (json.JSONDecodeError, OSError):
            pass

    # Source 2: dispatch_log.json (supplement with recent dispatches)
    if len(active) < 2:
        dl_path = DATA_DIR / "dispatch_log.json"
        if dl_path.exists():
            try:
                with open(dl_path, "r", encoding="utf-8") as f:
                    log = json.load(f)
                # Look at last 10 entries for recent active dispatches
                recent = log[-10:] if isinstance(log, list) else []
                cutoff = time.time() - 300  # last 5 minutes
                for entry in recent:
                    worker = entry.get("worker", "")
                    ts = entry.get("timestamp", "")
                    if not entry.get("result_received", False) and worker in WORKER_NAMES:
                        # Parse ISO timestamp
                        try:
                            import datetime
                            dt = datetime.datetime.fromisoformat(ts)
                            if dt.timestamp() > cutoff:
                                task = entry.get("task_summary", "")
                                if task and worker not in active:
                                    active[worker] = task
                        except (ValueError, TypeError):
                            pass
            except (json.JSONDecodeError, OSError):
                pass

    return active
    # signed: gamma


def scan_live() -> dict:
    """Run full flocking scan against live system state.

    Returns:
        Dict with conflicts, suggestions, and summary.
    """
    active = _get_active_tasks_from_live()

    conflicts = check_conflicts(active)
    suggestions = suggest_coordination(active)

    result = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "active_workers": len(active),
        "worker_tasks": active,
        "conflicts": conflicts,
        "suggestions": suggestions,
        "conflict_count": len(conflicts),
        "suggestion_count": len(suggestions),
        "critical_conflicts": sum(
            1 for c in conflicts if c["level"] == CONFLICT_LEVEL_CRITICAL
        ),
    }

    # Log to flocking history
    _append_log(result)

    return result
    # signed: gamma


def _append_log(scan_result: dict) -> None:
    """Append a scan result to the flocking log (bounded at 100 entries)."""
    log = []
    if FLOCKING_LOG_PATH.exists():
        try:
            with open(FLOCKING_LOG_PATH, "r", encoding="utf-8") as f:
                log = json.load(f)
        except (json.JSONDecodeError, OSError):
            log = []

    log.append({
        "timestamp": scan_result["timestamp"],
        "active_workers": scan_result["active_workers"],
        "conflict_count": scan_result["conflict_count"],
        "critical_conflicts": scan_result["critical_conflicts"],
        "suggestion_count": scan_result["suggestion_count"],
    })

    # Keep bounded
    if len(log) > 100:
        log = log[-100:]

    tmp = str(FLOCKING_LOG_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(FLOCKING_LOG_PATH))
    # signed: gamma


def _publish_conflicts(conflicts: list[dict]) -> None:
    """Publish critical/high conflicts to bus for orchestrator awareness."""
    urgent = [c for c in conflicts if c["level"] in (CONFLICT_LEVEL_CRITICAL, CONFLICT_LEVEL_HIGH)]
    if not urgent:
        return

    try:
        from tools.skynet_spam_guard import guarded_publish
        summary_parts = []
        for c in urgent[:3]:  # cap at 3 to avoid spam
            summary_parts.append(
                f"[{c['level']}] {' & '.join(c['workers'])}: {c['reason'][:80]}"
            )
        guarded_publish({
            "sender": "flocking",
            "topic": "orchestrator",
            "type": "conflict_alert",
            "content": "FLOCKING ALERT: " + " | ".join(summary_parts),
        })
    except Exception:
        pass
    # signed: gamma


# ── CLI ──────────────────────────────────────────────────────────

def _cli():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Boids-inspired conflict avoidance for multi-worker editing"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("check", help="Check for file conflicts between active workers")
    sub.add_parser("suggest", help="Suggest coordination between workers")
    sub.add_parser("scan", help="Full scan: conflicts + suggestions")

    mon = sub.add_parser("monitor", help="Continuous monitoring loop")
    mon.add_argument("--interval", type=int, default=30, help="Scan interval in seconds")

    # Manual test mode
    test_p = sub.add_parser("test", help="Test with manual task descriptions")
    test_p.add_argument("--tasks", required=True,
                        help="JSON dict of worker:task, e.g. '{\"alpha\":\"fix tools/skynet_dispatch.py\"}'")

    args = parser.parse_args()

    if args.command == "check":
        active = _get_active_tasks_from_live()
        if not active:
            print("No active workers detected.")
            return
        print(f"Active workers: {len(active)}")
        for w, t in active.items():
            print(f"  {w}: {t[:80]}")
        print()
        conflicts = check_conflicts(active)
        if not conflicts:
            print("No conflicts detected.")
        else:
            print(f"{len(conflicts)} conflict(s) detected:")
            for c in conflicts:
                print(f"  [{c['level']}] {' & '.join(c['workers'])}")
                print(f"    Files: {', '.join(c.get('files', []))}")
                print(f"    {c['reason']}")

    elif args.command == "suggest":
        active = _get_active_tasks_from_live()
        if not active:
            print("No active workers detected.")
            return
        suggestions = suggest_coordination(active)
        if not suggestions:
            print("No coordination suggestions.")
        else:
            print(f"{len(suggestions)} suggestion(s):")
            for s in suggestions:
                print(f"  [{s['type']}] {' & '.join(s['workers'])}")
                print(f"    {s['action'][:120]}")

    elif args.command == "scan":
        result = scan_live()
        print(f"Flocking Scan @ {result['timestamp']}")
        print(f"  Active workers: {result['active_workers']}")
        print(f"  Conflicts: {result['conflict_count']} "
              f"({result['critical_conflicts']} critical)")
        print(f"  Suggestions: {result['suggestion_count']}")
        if result["conflicts"]:
            print()
            for c in result["conflicts"]:
                print(f"  [{c['level']}] {' & '.join(c['workers'])}: {c['reason'][:100]}")
        if result["suggestions"]:
            print()
            for s in result["suggestions"]:
                print(f"  [{s['type']}] {' & '.join(s['workers'])}: {s['action'][:100]}")
        # Publish critical conflicts
        _publish_conflicts(result["conflicts"])

    elif args.command == "monitor":
        print(f"Flocking monitor started (interval={args.interval}s). Ctrl+C to stop.")
        try:
            while True:
                result = scan_live()
                ts = result["timestamp"]
                cc = result["conflict_count"]
                crit = result["critical_conflicts"]
                sc = result["suggestion_count"]
                marker = " *** CRITICAL ***" if crit > 0 else ""
                print(f"[{ts}] workers={result['active_workers']} "
                      f"conflicts={cc} suggestions={sc}{marker}")
                if crit > 0:
                    _publish_conflicts(result["conflicts"])
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nMonitor stopped.")

    elif args.command == "test":
        tasks = json.loads(args.tasks)
        print(f"Testing with {len(tasks)} tasks:")
        for w, t in tasks.items():
            print(f"  {w}: {t[:80]}")
        print()
        conflicts = check_conflicts(tasks)
        suggestions = suggest_coordination(tasks)
        print(f"Conflicts: {len(conflicts)}")
        for c in conflicts:
            print(f"  [{c['level']}] {' & '.join(c['workers'])}: {c['reason']}")
        print(f"\nSuggestions: {len(suggestions)}")
        for s in suggestions:
            print(f"  [{s['type']}] {' & '.join(s['workers'])}: {s['action'][:120]}")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: gamma
