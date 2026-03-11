#!/usr/bin/env python3
"""Polls brain_inbox.json for pending GOD directives and delivers them
to the orchestrator via direct-prompt (UIA ghost-type).

Delivery Model:
  - Reads pending directives from brain_inbox.json (written by Go backend /directive)
  - Marks each as "received"
  - Delivers to orchestrator via skynet_delivery.deliver_to_orchestrator()
  - Falls back to stdout print if delivery module is unavailable
"""

import json
import time
import sys
import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "tools"))

INBOX = os.path.join(ROOT, "data", "brain", "brain_inbox.json")
POLL_INTERVAL = 2


def locked_read_write(path, transform_fn):
    """Read JSON, apply transform, write back -- with file locking."""
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


def _deliver_directive(rid, directive):
    """Deliver a directive to the orchestrator via direct-prompt.

    Falls back to stdout if the delivery module is unavailable.
    """
    formatted = f"[GOD DIRECTIVE] {rid}: {directive}"

    # Try direct-prompt delivery first (UIA ghost-type to orchestrator window)
    try:
        from skynet_delivery import deliver_to_orchestrator
        result = deliver_to_orchestrator(formatted, sender="god_bridge", also_bus=False)
        if result.get("success"):
            print(f"\033[1;32m[god_bridge]\033[0m Delivered {rid} via direct-prompt "
                  f"({result.get('latency_ms', 0):.0f}ms)", flush=True)
            return
    except Exception:
        pass

    # Fallback: print to stdout (original behavior)
    print(f"\033[1;33m[GOD DIRECTIVE]\033[0m {rid}: {directive}", flush=True)


def process_pending(entries):
    """Mark pending entries as received and deliver them."""
    changed = False
    for entry in entries:
        if entry.get("status") == "pending":
            rid = entry.get("request_id", "???")
            directive = entry.get("directive", "")
            _deliver_directive(rid, directive)
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
