#!/usr/bin/env python3
"""Polls brain_inbox.json for pending GOD directives and prints them to stdout."""

import json
import time
import sys
import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl

INBOX = os.path.join(os.path.dirname(__file__), "data", "brain", "brain_inbox.json")
POLL_INTERVAL = 2


def locked_read_write(path, transform_fn):
    """Read JSON, apply transform, write back — with file locking."""
    for attempt in range(5):
        try:
            with open(path, "r+", encoding="utf-8") as f:
                if os.name == "nt":
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                data = json.load(f)
                changed = transform_fn(data)
                if changed:
                    f.seek(0)
                    json.dump(data, f, indent=2)
                    f.truncate()
                if os.name == "nt":
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f, fcntl.LOCK_UN)
            return
        except (OSError, PermissionError):
            time.sleep(0.2)


def process_pending(entries):
    """Mark pending entries as received and print them."""
    changed = False
    for entry in entries:
        if entry.get("status") == "pending":
            rid = entry.get("request_id", "???")
            directive = entry.get("directive", "")
            print(f"\033[1;33m[GOD DIRECTIVE]\033[0m {rid}: {directive}", flush=True)
            entry["status"] = "received"
            changed = True
    return changed


def main():
    print(f"\033[1;36m[god_bridge]\033[0m watching {INBOX} (every {POLL_INTERVAL}s)", flush=True)
    while True:
        if os.path.exists(INBOX):
            locked_read_write(INBOX, process_pending)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
