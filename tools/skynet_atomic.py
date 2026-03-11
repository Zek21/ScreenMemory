#!/usr/bin/env python3
"""
skynet_atomic.py — Atomic JSON file operations for Skynet infrastructure.

Prevents data corruption from:
  - Process crashes mid-write (half-written JSON)
  - Concurrent readers seeing partial writes
  - Power loss during file operations

Uses write-to-temp + os.replace() pattern which is atomic on NTFS (Windows)
and most POSIX filesystems.

Usage:
    from tools.skynet_atomic import atomic_write_json, safe_read_json, atomic_update_json

    # Atomic write — safe against crashes
    atomic_write_json(Path("data/workers.json"), {"workers": [...]})

    # Safe read — returns default on corruption/missing
    data = safe_read_json(Path("data/workers.json"), default={"workers": []})

    # Atomic read-modify-write — combines safe read + transform + atomic write
    def add_entry(data):
        data["entries"].append(new_entry)
        data["entries"] = data["entries"][-200:]  # trim
        return data
    atomic_update_json(Path("data/log.json"), add_entry, default={"entries": []})
"""

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("skynet.atomic")

# Per-file locks to prevent concurrent writes to the same file from
# different threads in the same process. Does not protect cross-process
# writes — os.replace() handles that at the filesystem level.
_file_locks: dict[str, threading.Lock] = {}
_meta_lock = threading.Lock()


def _get_file_lock(path: Path) -> threading.Lock:
    """Get or create a per-file thread lock."""
    key = str(path.resolve())
    with _meta_lock:
        if key not in _file_locks:
            _file_locks[key] = threading.Lock()
        return _file_locks[key]


def atomic_write_json(path: Path, data, *, indent: int = 2, default=str,
                      encoding: str = "utf-8") -> bool:
    """Atomically write JSON data to a file.

    Writes to a temporary file first, then atomically replaces the target.
    This ensures the target file is always in a valid state — readers never
    see a half-written file.

    Args:
        path: Target file path.
        data: JSON-serializable data.
        indent: JSON indentation (default 2).
        default: JSON serializer default function (default str).
        encoding: File encoding (default utf-8).

    Returns:
        True on success, False on failure (logged).
    """
    lock = _get_file_lock(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data, indent=indent, default=default)
        with lock:
            tmp.write_text(content, encoding=encoding)
            os.replace(str(tmp), str(path))
        return True
    except Exception as e:
        logger.error("atomic_write_json failed for %s: %s", path, e)
        # Clean up temp file on failure
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def safe_read_json(path: Path, *, default=None, encoding: str = "utf-8"):
    """Safely read a JSON file with fallback on corruption or missing file.

    If the primary file is corrupt, attempts to read the .bak backup.

    Args:
        path: File path to read.
        default: Value returned if file is missing or corrupt.
        encoding: File encoding.

    Returns:
        Parsed JSON data, or default if unreadable.
    """
    for candidate in (path, path.with_suffix(path.suffix + ".bak")):
        if not candidate.exists():
            continue
        try:
            text = candidate.read_text(encoding=encoding)
            if not text.strip():
                continue
            return json.loads(text)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            logger.warning("safe_read_json: corrupt %s: %s", candidate, e)
            continue
    return default if default is not None else {}


def atomic_update_json(path: Path, update_fn, *, default=None,
                       indent: int = 2, encoding: str = "utf-8") -> bool:
    """Atomic read-modify-write cycle for a JSON file.

    Reads the file (or uses default if missing/corrupt), applies update_fn
    to the data, and atomically writes the result back.

    The update_fn receives the current data and must return the modified data.
    The entire read-modify-write is done under a per-file lock.

    Args:
        path: Target file path.
        update_fn: callable(data) -> modified_data
        default: Default data if file is missing/corrupt.
        indent: JSON indentation.
        encoding: File encoding.

    Returns:
        True on success, False on failure.
    """
    lock = _get_file_lock(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with lock:
            data = safe_read_json(path, default=default, encoding=encoding)
            data = update_fn(data)
            content = json.dumps(data, indent=indent, default=str)
            tmp.write_text(content, encoding=encoding)
            os.replace(str(tmp), str(path))
        return True
    except Exception as e:
        logger.error("atomic_update_json failed for %s: %s", path, e)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


# ── Convenience aliases used across the codebase ──────────────────────────

def write_json(path: Path, data, **kwargs) -> bool:
    """Alias for atomic_write_json — drop-in replacement for Path.write_text(json.dumps(...))."""
    return atomic_write_json(path, data, **kwargs)


def read_json(path: Path, **kwargs):
    """Alias for safe_read_json."""
    return safe_read_json(path, **kwargs)


def update_json(path: Path, fn, **kwargs) -> bool:
    """Alias for atomic_update_json."""
    return atomic_update_json(path, fn, **kwargs)
