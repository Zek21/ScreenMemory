#!/usr/bin/env python3
"""
skynet_activity_feed.py -- Real-time worker activity extraction daemon.

Scans all 4 worker windows every 3s via UIA, extracts conversation content,
diffs against previous snapshot, and posts NEW lines to the Skynet bus.

Usage:
    python tools/skynet_activity_feed.py start    # Run daemon (blocking)
    python tools/skynet_activity_feed.py status   # Show current activity
    python tools/skynet_activity_feed.py stop     # Stop running daemon
"""

import argparse
import atexit
import ctypes
import hashlib
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
PID_FILE = DATA_DIR / "activity_feed.pid"
ACTIVITY_FILE = DATA_DIR / "worker_activity.json"
WORKERS_FILE = DATA_DIR / "workers.json"
LOG_FILE = DATA_DIR / "activity_feed.log"

SCAN_INTERVAL = 3  # seconds between scans
MAX_RECENT = 20    # recent activities per worker
MAX_CONTENT = 500  # max chars per bus post
BUS_URL = "http://localhost:8420/bus/publish"

# Activity type detection patterns
TOOL_PATTERNS = ["Ran terminal", "Read file", "Searched", "Ran command", "Listed directory"]
EDIT_PATTERNS = ["Edited", "Created", "Deleted"]
RESULT_PATTERNS = ["Posted to bus", "COMPLETE", "DONE", "PASS", "FAIL"]


def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            if f.tell() > 500_000:
                f.seek(0)
                f.truncate()
    except Exception:
        pass


def _load_workers():
    """Load worker list from data/workers.json."""
    try:
        data = json.loads(WORKERS_FILE.read_text(encoding="utf-8"))
        workers = data.get("workers", [])
        return {w["name"]: w["hwnd"] for w in workers if "name" in w and "hwnd" in w}
    except Exception as e:
        log(f"Failed to load workers.json: {e}", "ERROR")
        return {}


def _get_listitem_snapshot(hwnd):
    """Get all ListItem (y, name) tuples from a window via COM UIA."""
    try:
        import comtypes
        import comtypes.client

        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass

        from comtypes.gen import UIAutomationClient as UIA

        uia = comtypes.CoCreateInstance(
            comtypes.GUID("{ff48dba4-60ef-4201-aa87-54103eef594e}"),
            interface=UIA.IUIAutomation,
            clsctx=comtypes.CLSCTX_INPROC_SERVER,
        )

        root = uia.ElementFromHandle(ctypes.c_void_p(hwnd))
        if not root:
            return []

        li_cond = uia.CreatePropertyCondition(30003, 50007)  # ControlType.ListItem
        li_els = root.FindAll(4, li_cond)  # TreeScope.Descendants

        items = []
        for i in range(li_els.Length):
            el = li_els.GetElement(i)
            name = el.CurrentName or ""
            if name.strip() and len(name) > 5:
                try:
                    rect = el.CurrentBoundingRectangle
                    y = rect.top
                except Exception:
                    y = 0
                items.append((y, name.strip()))

        items.sort(key=lambda x: x[0])
        return items

    except Exception as e:
        log(f"UIA scan failed HWND={hwnd}: {e}", "WARN")
        return []


def _snapshot_hash(items):
    """MD5 hash of all ListItem text for quick change detection."""
    text = "\n".join(t for _, t in items)
    return hashlib.md5(text.encode(errors="replace")).hexdigest()[:16]


def _extract_delta(old_items, new_items):
    """Return list of new text lines that weren't in the old snapshot."""
    old_set = {t for _, t in old_items}
    delta = []
    for _, text in new_items:
        if text not in old_set:
            delta.append(text)
    return delta


def classify_activity(text):
    """Classify a delta line into an activity type."""
    for pat in TOOL_PATTERNS:
        if pat in text:
            return "tool_call"
    for pat in EDIT_PATTERNS:
        if pat in text:
            return "edit"
    for pat in RESULT_PATTERNS:
        if pat in text:
            return "result"
    return "thinking"


def _extract_tool_info(text):
    """Extract tool name and file path from activity text."""
    tool = None
    filepath = None
    if "Ran terminal" in text or "Ran command" in text:
        tool = "terminal"
    elif "Read file" in text:
        tool = "read_file"
    elif "Searched" in text:
        tool = "search"
    elif "Edited" in text:
        tool = "edit"
    elif "Created" in text:
        tool = "create"
    elif "Listed directory" in text:
        tool = "list_dir"

    # Try to extract file paths (common patterns)
    for segment in text.split():
        if "/" in segment or "\\" in segment:
            if "." in segment.split("/")[-1].split("\\")[-1]:
                filepath = segment.strip("'\"(),")
                break
    return tool, filepath


def _post_to_bus(sender, activity_type, content):
    """Post activity to Skynet bus. Fire-and-forget."""
    try:
        import urllib.request
        data = json.dumps({
            "sender": sender,
            "topic": "activity",
            "type": activity_type,
            "content": content[:MAX_CONTENT],
        }).encode()
        req = urllib.request.Request(BUS_URL, data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass  # non-critical


def _save_activity(activity_data):
    """Atomically write worker_activity.json."""
    try:
        ACTIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ACTIVITY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(activity_data, indent=2, default=str), encoding="utf-8")
        tmp.replace(ACTIVITY_FILE)
    except Exception as e:
        log(f"Failed to save activity: {e}", "WARN")


def _get_worker_state(hwnd):
    """Quick state check via uia_engine if available."""
    try:
        from tools.uia_engine import get_engine
        engine = get_engine()
        scan = engine.scan(hwnd)
        return scan.state
    except Exception:
        return "UNKNOWN"


def run_daemon():
    """Main daemon loop: scan workers, diff, post deltas."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            os.kill(old_pid, 0)
            log(f"Activity feed already running (PID {old_pid}) -- exiting")
            return
        except (OSError, ValueError):
            pass  # stale PID

    PID_FILE.write_text(str(os.getpid()))

    def _cleanup_pid():
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    atexit.register(_cleanup_pid)

    workers = _load_workers()
    if not workers:
        log("No workers found in workers.json -- exiting", "ERROR")
        return

    log(f"Activity feed daemon started (PID {os.getpid()}), tracking {len(workers)} workers")

    # Per-worker state
    snapshots = {name: [] for name in workers}
    hashes = {name: "" for name in workers}
    activity_data = {}

    # Initialize activity_data structure
    for name in workers:
        activity_data[name] = {
            "state": "UNKNOWN",
            "current_activity": None,
            "last_tool": None,
            "last_file": None,
            "timestamp": None,
            "recent_activities": [],
        }

    cycle = 0
    try:
        while True:
            cycle += 1
            now = datetime.now(timezone.utc).isoformat()

            for name, hwnd in workers.items():
                try:
                    items = _get_listitem_snapshot(hwnd)
                    if not items:
                        continue

                    new_hash = _snapshot_hash(items)
                    if new_hash == hashes[name]:
                        continue  # no change

                    # Content changed -- extract delta
                    delta = _extract_delta(snapshots[name], items)
                    snapshots[name] = items
                    hashes[name] = new_hash

                    if not delta:
                        continue

                    # Get worker state
                    state = _get_worker_state(hwnd)
                    activity_data[name]["state"] = state
                    activity_data[name]["timestamp"] = now

                    # Process each delta line
                    for line in delta[-5:]:  # cap to last 5 new lines per cycle
                        atype = classify_activity(line)
                        tool, filepath = _extract_tool_info(line)

                        activity_data[name]["current_activity"] = line[:200]
                        if tool:
                            activity_data[name]["last_tool"] = tool
                        if filepath:
                            activity_data[name]["last_file"] = filepath

                        # Append to recent (capped)
                        activity_data[name]["recent_activities"].append({
                            "type": atype,
                            "text": line[:200],
                            "tool": tool,
                            "file": filepath,
                            "timestamp": now,
                        })
                        activity_data[name]["recent_activities"] = \
                            activity_data[name]["recent_activities"][-MAX_RECENT:]

                        # Post to bus
                        _post_to_bus(name, atype, line)

                    if cycle % 10 == 0 or delta:
                        log(f"{name}: {len(delta)} new lines, state={state}")

                except Exception as e:
                    if cycle <= 3:
                        log(f"Error scanning {name}: {e}", "WARN")

            # Save activity file periodically
            if cycle % 5 == 0:
                _save_activity(activity_data)

            time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        log("Activity feed shutting down (Ctrl+C)")
    finally:
        _save_activity(activity_data)
        _cleanup_pid()


def show_status():
    """Display current worker activity from saved state."""
    if not ACTIVITY_FILE.exists():
        print("No activity data found. Is the daemon running?")
        return

    data = json.loads(ACTIVITY_FILE.read_text(encoding="utf-8"))
    for name, info in sorted(data.items()):
        state = info.get("state", "UNKNOWN")
        activity = info.get("current_activity", "none")
        tool = info.get("last_tool", "-")
        fpath = info.get("last_file", "-")
        ts = info.get("timestamp", "-")
        recent_count = len(info.get("recent_activities", []))
        print(f"\n  {name.upper()}: [{state}]")
        print(f"    Activity: {(activity or 'none')[:80]}")
        print(f"    Last tool: {tool} | Last file: {fpath}")
        print(f"    Updated: {ts} | Recent: {recent_count} entries")

    # PID check
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            print(f"\n  Daemon: RUNNING (PID {pid})")
        except (OSError, ValueError):
            print("\n  Daemon: STOPPED (stale PID file)")
    else:
        print("\n  Daemon: NOT RUNNING")


def stop_daemon():
    """Stop the running daemon via PID file."""
    if not PID_FILE.exists():
        print("No PID file found -- daemon not running")
        return

    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to PID {pid}")
        time.sleep(1)
        try:
            os.kill(pid, 0)
            # Still alive, force kill
            os.kill(pid, signal.SIGTERM)
            print(f"Force-killed PID {pid}")
        except OSError:
            pass  # already dead
    except (OSError, ValueError) as e:
        print(f"Could not stop daemon: {e}")
    finally:
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    print("Activity feed daemon stopped")


def main():
    parser = argparse.ArgumentParser(description="Skynet Activity Feed Daemon")
    parser.add_argument("command", choices=["start", "status", "stop"],
                        help="start=run daemon, status=show current, stop=kill daemon")
    args = parser.parse_args()

    if args.command == "start":
        run_daemon()
    elif args.command == "status":
        show_status()
    elif args.command == "stop":
        stop_daemon()


if __name__ == "__main__":
    main()
