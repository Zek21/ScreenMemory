#!/usr/bin/env python3
"""
skynet_version.py -- Skynet version tracking and upgrade history.

Tracks version history in data/version_history.json.
Each entry: version, level, timestamp, changes_summary.

Usage:
    python tools/skynet_version.py                # Show current version
    python tools/skynet_version.py --changelog    # Show all versions
    python tools/skynet_version.py --log VERSION LEVEL "changes summary"
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
VERSION_FILE = DATA_DIR / "version_history.json"


def _load() -> list:
    if VERSION_FILE.exists():
        return json.loads(VERSION_FILE.read_text())
    return []


def _save(history: list):
    DATA_DIR.mkdir(exist_ok=True)
    VERSION_FILE.write_text(json.dumps(history, indent=2))


def log_upgrade(version: str, level: int, changes: str) -> dict:
    """Log a new version upgrade to history."""
    history = _load()
    entry = {
        "version": version,
        "level": level,
        "timestamp": datetime.now().isoformat(),
        "changes_summary": changes,
    }
    history.append(entry)
    _save(history)
    return entry


def current_version() -> dict | None:
    """Return the latest version entry, or None if no history."""
    history = _load()
    return history[-1] if history else None


def changelog() -> list:
    """Return all version entries."""
    return _load()


def main():
    parser = argparse.ArgumentParser(description="Skynet version tracker")
    parser.add_argument("--changelog", action="store_true", help="Show all versions")
    parser.add_argument("--log", nargs=3, metavar=("VERSION", "LEVEL", "CHANGES"),
                        help="Log a new upgrade: VERSION LEVEL 'changes summary'")
    args = parser.parse_args()

    if args.log:
        version, level, changes = args.log
        entry = log_upgrade(version, int(level), changes)
        print(f"Logged: v{entry['version']} (Level {entry['level']}) at {entry['timestamp']}")
    elif args.changelog:
        for entry in changelog():
            print(f"  v{entry['version']} | Level {entry['level']} | {entry['timestamp']}")
            print(f"    {entry['changes_summary']}")
    else:
        cur = current_version()
        if cur:
            print(f"Skynet v{cur['version']} (Level {cur['level']})")
        else:
            print("No version history yet")


if __name__ == "__main__":
    main()
