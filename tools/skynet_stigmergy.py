"""Stigmergic coordination for Skynet workers.
# signed: delta

Stigmergy is indirect coordination through environment modification.
Ants leave pheromone trails; Skynet workers leave change-markers in
data/stigmergy_markers.json.  When a worker is about to edit a file
that another worker recently touched, the system warns and suggests
reviewing the peer's changes first — preventing merge conflicts,
redundant work, and contradictory edits.

The shared codebase IS the environment.  Git history + explicit markers
form the "pheromone trail" that guides collective behavior.

Usage:
    python tools/skynet_stigmergy.py detect [--since SECONDS]
    python tools/skynet_stigmergy.py tag <file> <change_type> <worker> [--note NOTE]
    python tools/skynet_stigmergy.py read <file>
    python tools/skynet_stigmergy.py coordinate <file> <worker>
    python tools/skynet_stigmergy.py hot            # show hotspots
    python tools/skynet_stigmergy.py cleanup [--age HOURS]

Python API:
    from tools.skynet_stigmergy import (
        detect_changes, tag_change, read_markers, auto_coordinate
    )
    changes = detect_changes(since_seconds=300)
    tag_change("core/security.py", "refactor", "alpha", note="Added input validation")
    markers = read_markers("core/security.py")
    warnings = auto_coordinate("core/security.py", "beta")
"""
# signed: delta

import json
import os
import subprocess
import sys
import time
import argparse
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
MARKERS_PATH = REPO_ROOT / "data" / "stigmergy_markers.json"

# How long markers stay "hot" before aging out of coordination warnings
MARKER_TTL_SECONDS = 3600  # 1 hour default

# Change types recognized by the system
CHANGE_TYPES = [
    "edit",         # general edit
    "refactor",     # structural refactoring
    "bugfix",       # bug fix
    "feature",      # new feature / addition
    "delete",       # file deletion
    "rename",       # file rename
    "config",       # configuration change
    "docs",         # documentation
    "test",         # test addition/modification
    "security",     # security-related change
    "performance",  # performance optimization
    "wiring",       # integration / plumbing change
]

_lock = threading.Lock()


def _load_markers() -> Dict:
    """Load the stigmergy markers file."""
    if not MARKERS_PATH.exists():
        return {"markers": [], "created": datetime.now(timezone.utc).isoformat()}
    try:
        with open(MARKERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "markers" not in data:
            data["markers"] = []
        return data
    except (json.JSONDecodeError, IOError):
        return {"markers": [], "created": datetime.now(timezone.utc).isoformat()}


def _save_markers(data: Dict) -> None:
    """Atomically save markers file."""
    MARKERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(MARKERS_PATH) + ".tmp"
    data["updated"] = datetime.now(timezone.utc).isoformat()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(MARKERS_PATH))


def _normalize_path(file_path: str) -> str:
    """Normalize to forward-slash relative path from repo root."""
    p = Path(file_path)
    try:
        rel = p.resolve().relative_to(REPO_ROOT.resolve())
        return str(rel).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def detect_changes(since_seconds: int = 300) -> List[Dict[str, Any]]:
    """Scan git for recent changes within the given time window.

    Uses git log to find commits in the last `since_seconds` seconds,
    then extracts per-file change summaries.

    Args:
        since_seconds: How far back to look (default 5 minutes).

    Returns:
        List of dicts: [{file, change_type, author, timestamp, message, lines_changed}]
    """
    changes = []

    try:
        # Get commits in the time window  # signed: delta
        result = subprocess.run(
            [
                "git", "--no-pager", "log",
                f"--since={since_seconds} seconds ago",
                "--name-status",
                "--pretty=format:%H|%an|%aI|%s",
            ],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            timeout=15,
        )
        if result.returncode != 0:
            return changes

        current_commit = None
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue

            if "|" in line and line.count("|") >= 3:
                # Commit header line
                parts = line.split("|", 3)
                current_commit = {
                    "hash": parts[0][:8],
                    "author": parts[1],
                    "timestamp": parts[2],
                    "message": parts[3],
                }
            elif current_commit and "\t" in line:
                # File status line (e.g. "M\tcore/security.py")
                status_parts = line.split("\t", 1)
                git_status = status_parts[0].strip()
                file_path = status_parts[1].strip() if len(status_parts) > 1 else ""

                change_type_map = {
                    "M": "edit", "A": "feature", "D": "delete",
                    "R": "rename", "C": "edit", "T": "edit",
                }
                # Handle rename format "R100\told\tnew"
                ct = "edit"
                for prefix, mapped in change_type_map.items():
                    if git_status.startswith(prefix):
                        ct = mapped
                        break

                changes.append({
                    "file": _normalize_path(file_path),
                    "change_type": ct,
                    "author": current_commit["author"],
                    "timestamp": current_commit["timestamp"],
                    "message": current_commit["message"],
                    "commit": current_commit["hash"],
                })

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Also check unstaged/staged changes (working tree)  # signed: delta
    try:
        result = subprocess.run(
            ["git", "--no-pager", "diff", "--name-status", "HEAD"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if not line.strip() or "\t" not in line:
                    continue
                parts = line.split("\t", 1)
                git_status = parts[0].strip()
                file_path = parts[1].strip()

                ct = "edit"
                if git_status.startswith("A"):
                    ct = "feature"
                elif git_status.startswith("D"):
                    ct = "delete"

                changes.append({
                    "file": _normalize_path(file_path),
                    "change_type": ct,
                    "author": "working_tree",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": "(uncommitted)",
                    "commit": "HEAD",
                })
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return changes


def tag_change(
    file_path: str,
    change_type: str,
    worker: str,
    note: str = "",
) -> Dict:
    """Leave a stigmergy marker on a file — record what was changed and why.

    Args:
        file_path:   Path to the changed file (relative or absolute).
        change_type: One of CHANGE_TYPES (edit, refactor, bugfix, etc.).
        worker:      Name of the worker that made the change.
        note:        Optional human-readable description of the change.

    Returns:
        The marker dict that was stored.
    """
    normalized = _normalize_path(file_path)
    ct = change_type.lower().strip()
    if ct not in CHANGE_TYPES:
        ct = "edit"  # Fallback to generic

    marker = {
        "file": normalized,
        "change_type": ct,
        "worker": worker.lower().strip(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "epoch": time.time(),
        "note": note,
    }

    with _lock:
        data = _load_markers()
        data["markers"].append(marker)
        _save_markers(data)

    return marker  # signed: delta


def read_markers(
    file_path: str,
    max_age_seconds: Optional[int] = None,
) -> List[Dict]:
    """Read all stigmergy markers for a given file.

    Args:
        file_path:       Path to query (relative or absolute).
        max_age_seconds: Only return markers newer than this (None = all).

    Returns:
        List of marker dicts, newest first.
    """
    normalized = _normalize_path(file_path)
    data = _load_markers()
    now = time.time()

    results = []
    for m in data["markers"]:
        if m.get("file") != normalized:
            continue
        if max_age_seconds is not None:
            age = now - m.get("epoch", 0)
            if age > max_age_seconds:
                continue
        results.append(m)

    results.sort(key=lambda x: x.get("epoch", 0), reverse=True)
    return results  # signed: delta


def auto_coordinate(
    file_path: str,
    current_worker: str,
    lookback_seconds: int = MARKER_TTL_SECONDS,
) -> List[Dict[str, Any]]:
    """Check if a file was recently changed by another worker and produce warnings.

    Call this BEFORE editing a file.  If another worker touched it recently,
    returns warnings with details so the current worker can review peer changes
    before proceeding.

    Args:
        file_path:        File the current worker intends to edit.
        current_worker:   Name of the worker about to make changes.
        lookback_seconds: How far back to check (default MARKER_TTL_SECONDS).

    Returns:
        List of warning dicts: [{level, message, marker}]
        Empty list = safe to proceed.
    """
    normalized = _normalize_path(file_path)
    worker_lower = current_worker.lower().strip()
    now = time.time()
    warnings: List[Dict[str, Any]] = []

    # 1. Check stigmergy markers  # signed: delta
    markers = read_markers(normalized, max_age_seconds=lookback_seconds)
    for m in markers:
        if m.get("worker") == worker_lower:
            continue  # Own markers are fine

        age_s = now - m.get("epoch", 0)
        age_min = age_s / 60

        if age_s < 300:  # < 5 min — HIGH risk
            level = "HIGH"
        elif age_s < 900:  # < 15 min — MEDIUM
            level = "MEDIUM"
        else:
            level = "LOW"

        warnings.append({
            "level": level,
            "message": (
                f"{level}: {m['worker']} made a '{m['change_type']}' change to "
                f"{normalized} {age_min:.0f}m ago"
                + (f" — {m['note']}" if m.get("note") else "")
                + ". Review their changes before editing."
            ),
            "marker": m,
        })

    # 2. Check git log for recent commits touching this file  # signed: delta
    try:
        result = subprocess.run(
            [
                "git", "--no-pager", "log",
                f"--since={lookback_seconds} seconds ago",
                "--pretty=format:%an|%aI|%s",
                "--", normalized,
            ],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("|", 2)
                if len(parts) < 3:
                    continue
                author = parts[0].strip()
                ts = parts[1].strip()
                msg = parts[2].strip()

                # Skip if it looks like the current worker's commit
                if worker_lower in author.lower():
                    continue

                warnings.append({
                    "level": "MEDIUM",
                    "message": (
                        f"MEDIUM: git commit by '{author}' at {ts[:19]} "
                        f"touched {normalized}: \"{msg}\". Review before editing."
                    ),
                    "marker": {"source": "git", "author": author,
                               "timestamp": ts, "message": msg},
                })
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Deduplicate — same worker+file within same time window
    seen = set()
    deduped = []
    for w in warnings:
        m = w.get("marker", {})
        key = (m.get("worker", m.get("author", "")), m.get("change_type", "git"))
        if key not in seen:
            seen.add(key)
            deduped.append(w)

    # Sort: HIGH first, then MEDIUM, then LOW
    priority = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    deduped.sort(key=lambda x: priority.get(x.get("level", "LOW"), 3))

    return deduped


def get_hotspots(top_n: int = 10, max_age_seconds: int = 3600) -> List[Dict]:
    """Find files with the most recent markers — coordination hotspots.

    Args:
        top_n:           Number of hottest files to return.
        max_age_seconds: Only consider markers within this window.

    Returns:
        List of [{file, marker_count, workers, last_change, change_types}]
        sorted by marker_count descending.
    """
    data = _load_markers()
    now = time.time()

    file_stats: Dict[str, Dict] = {}
    for m in data["markers"]:
        age = now - m.get("epoch", 0)
        if age > max_age_seconds:
            continue

        f = m["file"]
        if f not in file_stats:
            file_stats[f] = {
                "file": f,
                "marker_count": 0,
                "workers": set(),
                "change_types": set(),
                "last_epoch": 0,
            }

        stats = file_stats[f]
        stats["marker_count"] += 1
        stats["workers"].add(m.get("worker", "unknown"))
        stats["change_types"].add(m.get("change_type", "edit"))
        stats["last_epoch"] = max(stats["last_epoch"], m.get("epoch", 0))

    # Convert sets to lists for JSON serialization
    hotspots = []
    for stats in file_stats.values():
        hotspots.append({
            "file": stats["file"],
            "marker_count": stats["marker_count"],
            "workers": sorted(stats["workers"]),
            "change_types": sorted(stats["change_types"]),
            "last_change": datetime.fromtimestamp(
                stats["last_epoch"], tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "multi_worker": len(stats["workers"]) > 1,
        })

    hotspots.sort(key=lambda x: x["marker_count"], reverse=True)
    return hotspots[:top_n]  # signed: delta


def cleanup(max_age_hours: float = 24.0) -> int:
    """Remove markers older than max_age_hours.

    Args:
        max_age_hours: Age threshold (default 24 hours).

    Returns:
        Number of markers removed.
    """
    cutoff = time.time() - (max_age_hours * 3600)

    with _lock:
        data = _load_markers()
        original_count = len(data["markers"])
        data["markers"] = [
            m for m in data["markers"]
            if m.get("epoch", 0) > cutoff
        ]
        removed = original_count - len(data["markers"])
        if removed > 0:
            _save_markers(data)

    return removed  # signed: delta


def _cli():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Skynet stigmergic coordination"
    )
    sub = parser.add_subparsers(dest="command")

    # detect
    det = sub.add_parser("detect", help="Detect recent git changes")
    det.add_argument("--since", type=int, default=300,
                     help="Lookback window in seconds (default 300)")

    # tag
    tg = sub.add_parser("tag", help="Tag a file change")
    tg.add_argument("file", help="File path")
    tg.add_argument("change_type", help=f"One of: {', '.join(CHANGE_TYPES)}")
    tg.add_argument("worker", help="Worker name")
    tg.add_argument("--note", default="", help="Description of the change")

    # read
    rd = sub.add_parser("read", help="Read markers for a file")
    rd.add_argument("file", help="File path")
    rd.add_argument("--age", type=int, default=None,
                    help="Max age in seconds (default: all)")

    # coordinate
    co = sub.add_parser("coordinate", help="Check before editing a file")
    co.add_argument("file", help="File about to be edited")
    co.add_argument("worker", help="Worker about to edit")
    co.add_argument("--lookback", type=int, default=MARKER_TTL_SECONDS,
                    help=f"Lookback window in seconds (default {MARKER_TTL_SECONDS})")

    # hot
    ht = sub.add_parser("hot", help="Show coordination hotspots")
    ht.add_argument("--top", type=int, default=10, help="Top N (default 10)")
    ht.add_argument("--age", type=int, default=3600,
                    help="Max age in seconds (default 3600)")

    # cleanup
    cl = sub.add_parser("cleanup", help="Remove old markers")
    cl.add_argument("--age", type=float, default=24.0,
                    help="Max age in hours (default 24)")

    args = parser.parse_args()

    if args.command == "detect":
        changes = detect_changes(args.since)
        if not changes:
            print(f"No changes in the last {args.since}s.")
        else:
            print(f"Found {len(changes)} change(s) in the last {args.since}s:")
            for c in changes:
                print(
                    f"  [{c['change_type']}] {c['file']} "
                    f"by {c['author']} — {c['message'][:60]}"
                )

    elif args.command == "tag":
        marker = tag_change(args.file, args.change_type, args.worker, args.note)
        print(
            f"Tagged: {marker['worker']} {marker['change_type']} "
            f"{marker['file']}"
            + (f" — {marker['note']}" if marker["note"] else "")
        )

    elif args.command == "read":
        markers = read_markers(args.file, max_age_seconds=args.age)
        if not markers:
            print(f"No markers for {args.file}.")
        else:
            print(f"{len(markers)} marker(s) for {_normalize_path(args.file)}:")
            for m in markers:
                age = time.time() - m.get("epoch", 0)
                print(
                    f"  [{m['change_type']}] by {m['worker']} "
                    f"({age / 60:.0f}m ago)"
                    + (f" — {m['note']}" if m.get("note") else "")
                )

    elif args.command == "coordinate":
        warnings = auto_coordinate(args.file, args.worker, args.lookback)
        if not warnings:
            print(f"CLEAR: No recent changes to {args.file} by other workers. Safe to edit.")
        else:
            print(f"⚠ {len(warnings)} coordination warning(s) for {args.file}:")
            for w in warnings:
                print(f"  {w['message']}")

    elif args.command == "hot":
        hotspots = get_hotspots(top_n=args.top, max_age_seconds=args.age)
        if not hotspots:
            print("No hotspots found.")
        else:
            print(f"Top {len(hotspots)} coordination hotspots:")
            for h in hotspots:
                multi = " ⚠ MULTI-WORKER" if h["multi_worker"] else ""
                print(
                    f"  {h['file']}: {h['marker_count']} markers "
                    f"by {', '.join(h['workers'])} "
                    f"({', '.join(h['change_types'])}){multi}"
                )

    elif args.command == "cleanup":
        removed = cleanup(max_age_hours=args.age)
        print(f"Removed {removed} old marker(s).")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: delta
