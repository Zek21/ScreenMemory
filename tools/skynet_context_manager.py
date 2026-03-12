#!/usr/bin/env python3
"""
Skynet Context Manager — Monitors worker conversation length and handles context refresh.

When a worker's conversation grows too long (context window exhaustion), this tool:
1. Saves current task state to data/worker_state_{name}.json
2. Opens a fresh chat window via new_chat.ps1
3. Re-injects worker identity
4. Re-dispatches pending work with saved context

Usage:
    python tools/skynet_context_manager.py --check alpha        # Check context depth
    python tools/skynet_context_manager.py --check-all          # Check all workers
    python tools/skynet_context_manager.py --refresh alpha      # Force context refresh
    python tools/skynet_context_manager.py --monitor            # Daemon: auto-refresh when needed
"""

import json
import os
import sys
import time
import subprocess
import threading
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
WORKERS_FILE = DATA_DIR / "workers.json"
SKYNET_PORT = 8420

# Context thresholds — estimated from UIA element counts
# VS Code Copilot CLI creates ~2-4 list items per exchange (user + assistant)
CONTEXT_WARNING_THRESHOLD = 80   # ~20 exchanges, approaching limits
CONTEXT_CRITICAL_THRESHOLD = 120  # ~30 exchanges, likely degraded
CONTEXT_REFRESH_THRESHOLD = 150   # ~37 exchanges, must refresh

# Python interpreter
def _resolve_python():
    venv = ROOT.parent / "env" / "Scripts" / "python.exe"
    return str(venv) if venv.exists() else sys.executable

PYTHON = _resolve_python()


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"OK": "[+]", "WARN": "[!]", "ERR": "[X]", "SYS": "[*]", "INFO": "[i]"}.get(level, "[?]")
    print(f"{prefix} [{ts}] {msg}")


def load_workers():
    if not WORKERS_FILE.exists():
        return []
    data = json.loads(WORKERS_FILE.read_text(encoding="utf-8"))
    return data.get("workers", [])


def bus_post(sender, topic, msg_type, content):
    """Fire-and-forget bus post via SpamGuard."""
    bus_msg = {
        "sender": sender,
        "topic": topic,
        "type": msg_type,
        "content": content,
    }
    try:
        from tools.skynet_spam_guard import guarded_publish
        guarded_publish(bus_msg)
    except ImportError:
        # Fallback: raw urllib when SpamGuard unavailable
        try:
            import urllib.request
            payload = json.dumps(bus_msg).encode("utf-8")
            req = urllib.request.Request(
                f"http://localhost:{SKYNET_PORT}/bus/publish",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass
    # signed: beta


def get_context_depth(hwnd):
    """Count conversation elements in a worker window via UIA engine.

    Returns estimated message count based on ListItem elements visible in the chat.
    Each user/assistant exchange creates multiple ListItems in the chat view.
    """
    try:
        from tools.uia_engine import get_engine
        engine = get_engine()
        scan = engine.scan(hwnd)
        # element_count includes buttons, list items, edits — rough proxy for context depth
        # list_items more directly correlates with conversation turns
        list_count = len(scan.list_items) if hasattr(scan, "list_items") else 0
        element_count = scan.element_count if hasattr(scan, "element_count") else 0
        return {
            "list_items": list_count,
            "element_count": element_count,
            "state": scan.state,
            "estimated_turns": max(list_count // 3, 0),
            "scan_ms": scan.scan_ms,
        }
    except Exception as e:
        return {"list_items": 0, "element_count": 0, "state": "UNKNOWN", "error": str(e)}


def check_worker(name, hwnd):
    """Check a single worker's context depth and return status."""
    depth = get_context_depth(hwnd)
    elements = depth.get("element_count", 0)
    turns = depth.get("estimated_turns", 0)

    if elements >= CONTEXT_REFRESH_THRESHOLD:
        status = "CRITICAL"
    elif elements >= CONTEXT_CRITICAL_THRESHOLD:
        status = "WARNING"
    elif elements >= CONTEXT_WARNING_THRESHOLD:
        status = "ELEVATED"
    else:
        status = "OK"

    return {
        "name": name,
        "hwnd": hwnd,
        "status": status,
        "elements": elements,
        "estimated_turns": turns,
        "worker_state": depth.get("state", "UNKNOWN"),
        "scan_ms": depth.get("scan_ms", 0),
        "timestamp": datetime.now().isoformat(),
    }


def check_all_workers():
    """Check context depth for all workers."""
    workers = load_workers()
    results = {}
    for w in workers:
        name = w.get("name", "unknown")
        hwnd = w.get("hwnd", 0)
        results[name] = check_worker(name, hwnd)
    return results


def save_worker_state(name, hwnd, pending_task=None, context_summary=None):
    """Save worker state before context refresh."""
    state = {
        "name": name,
        "hwnd": hwnd,
        "saved_at": datetime.now().isoformat(),
        "pending_task": pending_task,
        "context_summary": context_summary,
        "reason": "context_exhaustion",
    }

    # Try to read current task from bus
    try:
        import urllib.request
        url = f"http://localhost:{SKYNET_PORT}/bus/messages?limit=50"
        resp = urllib.request.urlopen(url, timeout=5)
        messages = json.loads(resp.read().decode())
        if isinstance(messages, list):
            # Find last task dispatched to this worker
            for msg in reversed(messages):
                if msg.get("topic") == name and msg.get("type") in ("directive", "sub-task"):
                    state["last_directive"] = msg.get("content", "")[:500]
                    break
            # Find last result from this worker
            for msg in reversed(messages):
                if msg.get("sender") == name and msg.get("type") == "result":
                    state["last_result"] = msg.get("content", "")[:500]
                    break
    except Exception:
        pass

    state_file = DATA_DIR / f"worker_state_{name}.json"
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    log(f"Saved state for {name.upper()} to {state_file}", "OK")
    return state


def _open_fresh_chat() -> bool:
    """Open a fresh chat window via new_chat.ps1. Returns True on success."""
    new_chat_script = ROOT / "tools" / "new_chat.ps1"
    if not new_chat_script.exists():
        log(f"new_chat.ps1 not found at {new_chat_script}", "ERR")
        return False
    try:
        result = subprocess.run(
            ["pwsh", "-ExecutionPolicy", "Bypass", "-File", str(new_chat_script)],
            cwd=str(ROOT), timeout=30, capture_output=True, text=True,
        )
        if result.returncode != 0:
            log(f"new_chat.ps1 failed: {result.stderr[:200]}", "ERR")
            return False
        log("Fresh chat window opened", "OK")
        return True
    except Exception as e:
        log(f"Failed to open fresh chat: {e}", "ERR")
        return False


def _find_new_hwnd(name: str, old_hwnd: int) -> int | None:
    """Find the new HWND for a worker after refresh. Returns None if not found."""
    time.sleep(2)
    for w in load_workers():
        if w.get("name") == name:
            new_hwnd = w.get("hwnd", 0)
            if new_hwnd != old_hwnd:
                return new_hwnd
    return None


def _reinject_identity(name: str, new_hwnd: int, state: dict):
    """Re-inject worker identity from agent_profiles.json into new window."""
    profiles_file = DATA_DIR / "agent_profiles.json"
    if not profiles_file.exists():
        return
    try:
        profiles = json.loads(profiles_file.read_text(encoding="utf-8"))
        profile = profiles.get(name, {})
        role = profile.get("role", "worker")
        specs = ", ".join(profile.get("specializations", [])) or "general"
        prev_ctx = state.get("last_result", "none available")[:300]
        identity = (
            f"You are {name.upper()} -- {role} in the Skynet multi-agent network. "
            f"Your HWND is {new_hwnd}. Claude Opus 4.6 fast, Copilot CLI mode. "
            f"Connected to Skynet on port {SKYNET_PORT}. "
            f"Your specializations: {specs}. "
            f"CONTEXT WAS REFRESHED -- you are in a fresh conversation window. "
            f"Previous context summary: {prev_ctx}"
        )
        subprocess.run(
            [PYTHON, str(ROOT / "tools" / "skynet_dispatch.py"),
             "--worker", name, "--task", identity],
            cwd=str(ROOT), timeout=30, capture_output=True, text=True,
        )
        log(f"Identity re-injected for {name.upper()}", "OK")
    except Exception as e:
        log(f"Identity re-injection failed: {e}", "WARN")


def _redispatch_pending(name: str, state: dict):
    """Re-dispatch pending task to refreshed worker, if any."""
    pending = state.get("pending_task") or state.get("last_directive")
    if not pending:
        return
    time.sleep(3)
    try:
        subprocess.run(
            [PYTHON, str(ROOT / "tools" / "skynet_dispatch.py"),
             "--worker", name, "--task", f"RESUMED TASK (after context refresh): {pending}"],
            cwd=str(ROOT), timeout=30, capture_output=True, text=True,
        )
        log(f"Pending task re-dispatched to {name.upper()}", "OK")
    except Exception as e:
        log(f"Failed to re-dispatch pending task: {e}", "WARN")


def refresh_worker(name, hwnd):
    """Refresh a worker's context by opening a fresh chat window."""
    log(f"Starting context refresh for {name.upper()} (HWND={hwnd})", "SYS")
    state = save_worker_state(name, hwnd)
    bus_post("context_manager", "orchestrator", "alert",
             f"CONTEXT_REFRESH: {name.upper()} starting context refresh -- conversation too long")

    if not _open_fresh_chat():
        return False

    new_hwnd = _find_new_hwnd(name, hwnd)
    if new_hwnd is None:
        log(f"Could not find new HWND for {name.upper()} after refresh", "WARN")
        bus_post("context_manager", "orchestrator", "alert",
                 f"CONTEXT_REFRESH_PARTIAL: {name.upper()} new window opened but HWND not updated. "
                 f"Run skynet_start.py --reconnect to re-map.")
        return True

    log(f"New HWND for {name.upper()}: {new_hwnd} (was {hwnd})", "OK")
    _reinject_identity(name, new_hwnd, state)
    _redispatch_pending(name, state)

    bus_post("context_manager", "orchestrator", "alert",
             f"CONTEXT_REFRESH_COMPLETE: {name.upper()} refreshed. "
             f"Old HWND={hwnd}, New HWND={new_hwnd}. Identity + pending work re-injected.")
    return True


def monitor_daemon(check_interval=60):
    """Background daemon that monitors all workers and auto-refreshes when needed."""
    log("Context monitor daemon started", "SYS")
    log(f"Thresholds: warning={CONTEXT_WARNING_THRESHOLD}, "
        f"critical={CONTEXT_CRITICAL_THRESHOLD}, refresh={CONTEXT_REFRESH_THRESHOLD}", "INFO")

    while True:
        try:
            results = check_all_workers()
            for name, info in results.items():
                status = info["status"]
                elements = info["elements"]

                if status == "CRITICAL":
                    log(f"{name.upper()}: CRITICAL ({elements} elements) -- triggering refresh", "WARN")
                    bus_post("context_manager", "orchestrator", "alert",
                             f"CONTEXT_CRITICAL: {name.upper()} at {elements} elements -- auto-refreshing")
                    # Only refresh if worker is IDLE (don't interrupt active work)
                    if info["worker_state"] == "IDLE":
                        refresh_worker(name, info["hwnd"])
                    else:
                        log(f"{name.upper()} is {info['worker_state']} -- deferring refresh", "INFO")
                elif status == "WARNING":
                    log(f"{name.upper()}: WARNING ({elements} elements) -- approaching limit", "WARN")
                elif status == "ELEVATED":
                    log(f"{name.upper()}: ELEVATED ({elements} elements)", "INFO")

        except Exception as e:
            log(f"Monitor loop error: {e}", "ERR")

        time.sleep(check_interval)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skynet Context Manager")
    parser.add_argument("--check", type=str, help="Check context depth for a specific worker")
    parser.add_argument("--check-all", action="store_true", help="Check all workers")
    parser.add_argument("--refresh", type=str, help="Force context refresh for a worker")
    parser.add_argument("--monitor", action="store_true", help="Run as background daemon")
    parser.add_argument("--interval", type=int, default=60, help="Check interval for --monitor (seconds)")
    args = parser.parse_args()

    workers = load_workers()
    worker_map = {w["name"]: w for w in workers}

    if args.check:
        name = args.check.lower()
        if name not in worker_map:
            log(f"Worker '{name}' not found", "ERR")
            return
        info = check_worker(name, worker_map[name]["hwnd"])
        print(json.dumps(info, indent=2))

    elif args.check_all:
        results = check_all_workers()
        for name, info in sorted(results.items()):
            icon = {"OK": "OK", "ELEVATED": "!!", "WARNING": "!!", "CRITICAL": "XX"}.get(info["status"], "??")
            print(f"  {name.upper():<8} [{icon}] {info['status']:<10} "
                  f"elements={info['elements']:<4} turns~{info['estimated_turns']:<3} "
                  f"state={info['worker_state']}")

    elif args.refresh:
        name = args.refresh.lower()
        if name not in worker_map:
            log(f"Worker '{name}' not found", "ERR")
            return
        ok = refresh_worker(name, worker_map[name]["hwnd"])
        log(f"Refresh {'succeeded' if ok else 'failed'} for {name.upper()}", "OK" if ok else "ERR")

    elif args.monitor:
        monitor_daemon(check_interval=args.interval)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
