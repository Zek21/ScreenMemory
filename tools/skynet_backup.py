#!/usr/bin/env python3
"""Skynet Critical File Backup System.

Provides snapshot/restore/diff/prune for all critical runtime state and protocol files.
Auto-snapshots before dangerous writes via safe_write_json().

Usage:
    python tools/skynet_backup.py snapshot [--label LABEL]   # Take a snapshot
    python tools/skynet_backup.py restore SNAPSHOT_ID        # Restore from snapshot
    python tools/skynet_backup.py list                       # List snapshots
    python tools/skynet_backup.py diff [SNAPSHOT_ID]         # Diff current vs snapshot
    python tools/skynet_backup.py verify [SNAPSHOT_ID]       # Verify checksums
    python tools/skynet_backup.py prune [--keep N]           # Prune old snapshots
    python tools/skynet_backup.py status                     # Show protection status

# signed: orchestrator
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_INDEX = BACKUP_DIR / "index.json"
MAX_SNAPSHOTS = 20
MAX_AUTO_SNAPSHOTS = 50  # auto-snapshots have a higher limit

# Critical data files — runtime state that has NO other source of truth
CRITICAL_DATA_FILES = [
    "workers.json",
    "orchestrator.json",
    "brain_config.json",
    "agent_profiles.json",
    "todos.json",
    "incidents.json",
    "worker_scores.json",
    "critical_processes.json",
    "boot_config.json",
    "boot_protocol.json",
    "consultant_state.json",
    "gemini_consultant_state.json",
    "consultant_registry.json",
    "daemon_state.json",
    "dispatch_log.json",
    "realtime.json",
    "monitor_health.json",
    "spam_guard_state.json",
    "iq_history.json",
    "convene_gate.json",
    "learner_state.json",
    "version_history.json",
]

# Protocol files — code/config that governs all agent behavior
PROTOCOL_FILES = [
    "AGENTS.md",
    ".github/copilot-instructions.md",
    "Orch-Start.ps1",
    "CC-Start.ps1",
    "GC-Start.ps1",
    "tools/skynet_start.py",
    "tools/skynet_dispatch.py",
    "tools/skynet_monitor.py",
    "tools/new_chat.ps1",
    "config.json",
]

ALL_PROTECTED = CRITICAL_DATA_FILES + PROTOCOL_FILES


def _ensure_backup_dir():
    """Create backup directory if needed."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def _load_index() -> Dict:
    """Load backup index."""
    if BACKUP_INDEX.exists():
        try:
            with open(BACKUP_INDEX, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"snapshots": [], "auto_snapshots": [], "created": datetime.now().isoformat()}


def _save_index(index: Dict):
    """Save backup index atomically."""
    tmp = str(BACKUP_INDEX) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
    os.replace(tmp, str(BACKUP_INDEX))


def _sha256(filepath: Path) -> str:
    """Compute SHA-256 of a file."""
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return "FILE_MISSING"


def _resolve_file(name: str) -> Path:
    """Resolve a protected file name to its full path."""
    if name in CRITICAL_DATA_FILES:
        return DATA_DIR / name
    else:
        return REPO_ROOT / name


def snapshot(label: Optional[str] = None, auto: bool = False) -> str:
    """Take a snapshot of all critical files.

    Args:
        label: Human-readable label for this snapshot
        auto: If True, this is an auto-snapshot (before a write)

    Returns:
        Snapshot ID string
    """
    _ensure_backup_dir()
    ts = datetime.now()
    snap_id = ts.strftime("%Y%m%d_%H%M%S")
    if auto:
        snap_id = f"auto_{snap_id}"
    snap_dir = BACKUP_DIR / snap_id
    snap_dir.mkdir(parents=True, exist_ok=True)

    # Subdirs for organized storage
    (snap_dir / "data").mkdir(exist_ok=True)
    (snap_dir / "protocol").mkdir(exist_ok=True)

    manifest = {
        "id": snap_id,
        "timestamp": ts.isoformat(),
        "label": label or ("auto-snapshot" if auto else "manual"),
        "auto": auto,
        "files": {},
        "missing": [],
        "total_bytes": 0,
    }

    for name in ALL_PROTECTED:
        src = _resolve_file(name)
        if not src.exists():
            manifest["missing"].append(name)
            continue

        # Determine destination
        if name in CRITICAL_DATA_FILES:
            dst = snap_dir / "data" / name
        else:
            # Flatten protocol file paths
            safe_name = name.replace("/", "__").replace("\\", "__")
            dst = snap_dir / "protocol" / safe_name

        try:
            shutil.copy2(str(src), str(dst))
            size = src.stat().st_size
            checksum = _sha256(src)
            manifest["files"][name] = {
                "size": size,
                "sha256": checksum,
                "mtime": datetime.fromtimestamp(src.stat().st_mtime).isoformat(),
                "backup_path": str(dst.relative_to(snap_dir)),
            }
            manifest["total_bytes"] += size
        except OSError as e:
            manifest["missing"].append(f"{name} (error: {e})")

    # Write manifest
    with open(snap_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # Update index
    index = _load_index()
    entry = {
        "id": snap_id,
        "timestamp": ts.isoformat(),
        "label": manifest["label"],
        "file_count": len(manifest["files"]),
        "total_bytes": manifest["total_bytes"],
        "missing_count": len(manifest["missing"]),
    }

    key = "auto_snapshots" if auto else "snapshots"
    index[key].append(entry)
    _save_index(index)

    return snap_id


def restore(snap_id: str, files: Optional[List[str]] = None, dry_run: bool = False) -> Dict:
    """Restore files from a snapshot.

    Args:
        snap_id: Snapshot ID to restore from
        files: Optional list of specific files to restore (default: all)
        dry_run: If True, show what would be restored without doing it

    Returns:
        Dict with restore results
    """
    snap_dir = BACKUP_DIR / snap_id
    manifest_path = snap_dir / "manifest.json"

    if not manifest_path.exists():
        return {"error": f"Snapshot {snap_id} not found"}

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    # Auto-snapshot current state before restoring
    if not dry_run:
        pre_restore_id = snapshot(label=f"pre-restore-{snap_id}", auto=True)

    results = {"restored": [], "skipped": [], "errors": [], "pre_restore_snapshot": None}
    if not dry_run:
        results["pre_restore_snapshot"] = pre_restore_id

    targets = files if files else list(manifest["files"].keys())

    for name in targets:
        if name not in manifest["files"]:
            results["skipped"].append(f"{name} (not in snapshot)")
            continue

        info = manifest["files"][name]
        backup_path = snap_dir / info["backup_path"]
        dest = _resolve_file(name)

        if not backup_path.exists():
            results["errors"].append(f"{name} (backup file missing)")
            continue

        if dry_run:
            current_hash = _sha256(dest) if dest.exists() else "MISSING"
            changed = current_hash != info["sha256"]
            results["restored"].append({
                "file": name,
                "would_change": changed,
                "current_hash": current_hash[:12],
                "backup_hash": info["sha256"][:12],
                "backup_size": info["size"],
            })
        else:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(backup_path), str(dest))
                results["restored"].append(name)
            except OSError as e:
                results["errors"].append(f"{name} ({e})")

    return results


def list_snapshots() -> List[Dict]:
    """List all available snapshots."""
    index = _load_index()
    all_snaps = []
    for s in index.get("snapshots", []):
        s["type"] = "manual"
        all_snaps.append(s)
    for s in index.get("auto_snapshots", []):
        s["type"] = "auto"
        all_snaps.append(s)
    all_snaps.sort(key=lambda x: x["timestamp"], reverse=True)
    return all_snaps


def diff(snap_id: Optional[str] = None) -> Dict:
    """Compare current files against a snapshot.

    Args:
        snap_id: Snapshot to compare against (default: latest manual snapshot)

    Returns:
        Dict with changed/added/removed/unchanged files
    """
    if snap_id is None:
        index = _load_index()
        if not index.get("snapshots"):
            return {"error": "No snapshots available. Run 'snapshot' first."}
        snap_id = index["snapshots"][-1]["id"]

    snap_dir = BACKUP_DIR / snap_id
    manifest_path = snap_dir / "manifest.json"
    if not manifest_path.exists():
        return {"error": f"Snapshot {snap_id} not found"}

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    result = {
        "snapshot": snap_id,
        "timestamp": manifest["timestamp"],
        "changed": [],
        "unchanged": [],
        "added": [],
        "removed": [],
    }

    snapshot_files = set(manifest["files"].keys())
    current_files = set()

    for name in ALL_PROTECTED:
        path = _resolve_file(name)
        if path.exists():
            current_files.add(name)

    # Files in both
    for name in snapshot_files & current_files:
        current_hash = _sha256(_resolve_file(name))
        snap_hash = manifest["files"][name]["sha256"]
        if current_hash != snap_hash:
            current_size = _resolve_file(name).stat().st_size
            result["changed"].append({
                "file": name,
                "snap_size": manifest["files"][name]["size"],
                "current_size": current_size,
                "size_delta": current_size - manifest["files"][name]["size"],
            })
        else:
            result["unchanged"].append(name)

    # New files not in snapshot
    for name in current_files - snapshot_files:
        result["added"].append(name)

    # Files removed since snapshot
    for name in snapshot_files - current_files:
        result["removed"].append(name)

    return result


def verify(snap_id: Optional[str] = None) -> Dict:
    """Verify integrity of a snapshot's backed up files.

    Returns:
        Dict with verified/corrupted/missing counts and details
    """
    if snap_id is None:
        index = _load_index()
        if not index.get("snapshots"):
            return {"error": "No snapshots available"}
        snap_id = index["snapshots"][-1]["id"]

    snap_dir = BACKUP_DIR / snap_id
    manifest_path = snap_dir / "manifest.json"
    if not manifest_path.exists():
        return {"error": f"Snapshot {snap_id} not found"}

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    result = {"snapshot": snap_id, "verified": 0, "corrupted": [], "missing": []}

    for name, info in manifest["files"].items():
        backup_path = snap_dir / info["backup_path"]
        if not backup_path.exists():
            result["missing"].append(name)
            continue

        actual_hash = _sha256(backup_path)
        if actual_hash != info["sha256"]:
            result["corrupted"].append({
                "file": name,
                "expected": info["sha256"][:16],
                "actual": actual_hash[:16],
            })
        else:
            result["verified"] += 1

    result["integrity"] = "PASS" if not result["corrupted"] and not result["missing"] else "FAIL"
    return result


def prune(keep: int = MAX_SNAPSHOTS, keep_auto: int = MAX_AUTO_SNAPSHOTS) -> Dict:
    """Remove old snapshots beyond the keep limit.

    Args:
        keep: Max manual snapshots to retain
        keep_auto: Max auto snapshots to retain

    Returns:
        Dict with pruned snapshot IDs
    """
    index = _load_index()
    pruned = []

    for key, limit in [("snapshots", keep), ("auto_snapshots", keep_auto)]:
        entries = index.get(key, [])
        if len(entries) > limit:
            to_remove = entries[:-limit]
            index[key] = entries[-limit:]
            for entry in to_remove:
                snap_dir = BACKUP_DIR / entry["id"]
                if snap_dir.exists():
                    shutil.rmtree(str(snap_dir), ignore_errors=True)
                pruned.append(entry["id"])

    _save_index(index)
    return {"pruned": pruned, "remaining_manual": len(index.get("snapshots", [])),
            "remaining_auto": len(index.get("auto_snapshots", []))}


def status() -> Dict:
    """Show current protection status."""
    index = _load_index()
    result = {
        "backup_dir": str(BACKUP_DIR),
        "manual_snapshots": len(index.get("snapshots", [])),
        "auto_snapshots": len(index.get("auto_snapshots", [])),
        "protected_files": {"total": len(ALL_PROTECTED), "data": 0, "protocol": 0,
                            "present": 0, "missing": 0},
        "latest_snapshot": None,
        "unprotected_changes": False,
    }

    for name in CRITICAL_DATA_FILES:
        if (_resolve_file(name)).exists():
            result["protected_files"]["data"] += 1
            result["protected_files"]["present"] += 1
        else:
            result["protected_files"]["missing"] += 1

    for name in PROTOCOL_FILES:
        if (_resolve_file(name)).exists():
            result["protected_files"]["protocol"] += 1
            result["protected_files"]["present"] += 1
        else:
            result["protected_files"]["missing"] += 1

    all_snaps = list_snapshots()
    if all_snaps:
        result["latest_snapshot"] = {
            "id": all_snaps[0]["id"],
            "timestamp": all_snaps[0]["timestamp"],
            "label": all_snaps[0].get("label", ""),
            "age_minutes": round((time.time() - datetime.fromisoformat(
                all_snaps[0]["timestamp"]).timestamp()) / 60, 1),
        }

    return result


# --- Safe Write API (for use by other Skynet tools) ---

def safe_write_json(filepath: str, data: Any, label: Optional[str] = None) -> str:
    """Write JSON data with automatic pre-write snapshot.

    This is the SAFE replacement for direct json.dump() on critical files.
    Takes an auto-snapshot before writing, then writes atomically.

    Args:
        filepath: Path to the JSON file
        data: Data to write
        label: Optional label for the auto-snapshot

    Returns:
        Path to the written file
    """
    path = Path(filepath)
    filename = path.name

    # Only auto-snapshot if this is a critical file
    if filename in CRITICAL_DATA_FILES:
        auto_label = label or f"pre-write-{filename}"
        try:
            snapshot(label=auto_label, auto=True)
        except Exception:
            pass  # Don't block writes if backup fails

    # Atomic write via temp file
    tmp = str(path) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, str(path))
    except Exception:
        # Clean up temp file on failure
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

    return str(path)


def safe_write_text(filepath: str, content: str, label: Optional[str] = None) -> str:
    """Write text content with automatic pre-write snapshot.

    For protocol files (AGENTS.md, .ps1 scripts, etc.)
    """
    path = Path(filepath)
    rel = str(path.relative_to(REPO_ROOT)) if str(path).startswith(str(REPO_ROOT)) else path.name

    if rel.replace("\\", "/") in PROTOCOL_FILES or path.name in CRITICAL_DATA_FILES:
        try:
            snapshot(label=f"pre-write-{path.name}", auto=True)
        except Exception:
            pass

    tmp = str(path) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, str(path))
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

    return str(path)


# --- CLI ---

def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    elif n < 1024 * 1024:
        return f"{n/1024:.1f}KB"
    else:
        return f"{n/(1024*1024):.1f}MB"


def main():
    parser = argparse.ArgumentParser(description="Skynet Critical File Backup System")
    sub = parser.add_subparsers(dest="command")

    # snapshot
    snap_p = sub.add_parser("snapshot", help="Take a snapshot of all critical files")
    snap_p.add_argument("--label", help="Human-readable label")

    # restore
    rest_p = sub.add_parser("restore", help="Restore from a snapshot")
    rest_p.add_argument("snapshot_id", help="Snapshot ID to restore from")
    rest_p.add_argument("--files", nargs="*", help="Specific files to restore")
    rest_p.add_argument("--dry-run", action="store_true", help="Show what would be restored")

    # list
    sub.add_parser("list", help="List available snapshots")

    # diff
    diff_p = sub.add_parser("diff", help="Compare current state against a snapshot")
    diff_p.add_argument("snapshot_id", nargs="?", help="Snapshot to compare (default: latest)")

    # verify
    ver_p = sub.add_parser("verify", help="Verify snapshot integrity")
    ver_p.add_argument("snapshot_id", nargs="?", help="Snapshot to verify (default: latest)")

    # prune
    prune_p = sub.add_parser("prune", help="Remove old snapshots")
    prune_p.add_argument("--keep", type=int, default=MAX_SNAPSHOTS)
    prune_p.add_argument("--keep-auto", type=int, default=MAX_AUTO_SNAPSHOTS)

    # status
    sub.add_parser("status", help="Show backup protection status")

    args = parser.parse_args()

    if args.command == "snapshot":
        snap_id = snapshot(label=args.label)
        idx = _load_index()
        entry = idx["snapshots"][-1]
        print(f"Snapshot created: {snap_id}")
        print(f"  Files: {entry['file_count']}, Size: {_format_bytes(entry['total_bytes'])}")
        if entry.get("missing_count", 0) > 0:
            print(f"  Missing: {entry['missing_count']} files")

    elif args.command == "restore":
        result = restore(args.snapshot_id, files=args.files, dry_run=args.dry_run)
        if "error" in result:
            print(f"ERROR: {result['error']}")
            sys.exit(1)
        if args.dry_run:
            print(f"Dry run - would restore from {args.snapshot_id}:")
            for item in result["restored"]:
                flag = "CHANGED" if item["would_change"] else "same"
                print(f"  [{flag}] {item['file']} ({_format_bytes(item['backup_size'])})")
        else:
            print(f"Restored {len(result['restored'])} files from {args.snapshot_id}")
            if result.get("pre_restore_snapshot"):
                print(f"  Pre-restore backup: {result['pre_restore_snapshot']}")
            if result["errors"]:
                print(f"  Errors: {result['errors']}")

    elif args.command == "list":
        snaps = list_snapshots()
        if not snaps:
            print("No snapshots found. Run: python tools/skynet_backup.py snapshot")
            return
        print(f"{'ID':<30} {'Type':<6} {'Files':<6} {'Size':<10} {'Label'}")
        print("-" * 80)
        for s in snaps[:30]:
            print(f"{s['id']:<30} {s.get('type','?'):<6} {s['file_count']:<6} "
                  f"{_format_bytes(s['total_bytes']):<10} {s.get('label','')}")

    elif args.command == "diff":
        result = diff(args.snapshot_id)
        if "error" in result:
            print(f"ERROR: {result['error']}")
            sys.exit(1)
        print(f"Diff against snapshot: {result['snapshot']} ({result['timestamp']})")
        if result["changed"]:
            print(f"\n  CHANGED ({len(result['changed'])}):")
            for c in result["changed"]:
                delta = c["size_delta"]
                sign = "+" if delta >= 0 else ""
                print(f"    {c['file']} ({sign}{_format_bytes(abs(delta))})")
        if result["added"]:
            print(f"\n  NEW ({len(result['added'])}):")
            for a in result["added"]:
                print(f"    + {a}")
        if result["removed"]:
            print(f"\n  REMOVED ({len(result['removed'])}):")
            for r in result["removed"]:
                print(f"    - {r}")
        unchanged = len(result["unchanged"])
        print(f"\n  Unchanged: {unchanged} files")

    elif args.command == "verify":
        result = verify(args.snapshot_id)
        if "error" in result:
            print(f"ERROR: {result['error']}")
            sys.exit(1)
        print(f"Snapshot: {result['snapshot']}")
        print(f"  Integrity: {result['integrity']}")
        print(f"  Verified: {result['verified']} files")
        if result["corrupted"]:
            print(f"  CORRUPTED: {len(result['corrupted'])} files!")
            for c in result["corrupted"]:
                print(f"    {c['file']}: expected {c['expected']}... got {c['actual']}...")
        if result["missing"]:
            print(f"  Missing: {len(result['missing'])} files")

    elif args.command == "prune":
        result = prune(keep=args.keep, keep_auto=args.keep_auto)
        print(f"Pruned {len(result['pruned'])} snapshots")
        print(f"  Remaining: {result['remaining_manual']} manual, {result['remaining_auto']} auto")

    elif args.command == "status":
        result = status()
        print("=== Skynet Backup Protection Status ===")
        pf = result["protected_files"]
        print(f"  Protected files: {pf['present']}/{pf['total']} present "
              f"({pf['data']} data, {pf['protocol']} protocol)")
        if pf["missing"] > 0:
            print(f"  WARNING: {pf['missing']} protected files missing!")
        print(f"  Snapshots: {result['manual_snapshots']} manual, "
              f"{result['auto_snapshots']} auto")
        if result["latest_snapshot"]:
            ls = result["latest_snapshot"]
            print(f"  Latest: {ls['id']} ({ls['age_minutes']} min ago) -- {ls['label']}")
        else:
            print("  WARNING: No snapshots exist! Run: python tools/skynet_backup.py snapshot")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
