#!/usr/bin/env python3
"""Skynet Dashboard Data — unified JSON blob for GOD Console from LOCAL files only.

Generates a comprehensive system snapshot using zero HTTP calls.
All data is read from local JSON/PID files for maximum speed and reliability.

CLI:
    python tools/skynet_dashboard_data.py             # Print JSON to stdout
    python tools/skynet_dashboard_data.py --output FILE  # Write to file
    python tools/skynet_dashboard_data.py --pretty    # Pretty-print
    python tools/skynet_dashboard_data.py --write     # Write to data/dashboard_data.json
"""
# signed: delta

import ctypes
import datetime
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

KNOWN_DAEMONS = {
    "sse_daemon": "sse_daemon.pid",
    "monitor": "monitor.pid",
    "self_prompt": "self_prompt.pid",
    "self_improve": "self_improve.pid",
    "bus_relay": "bus_relay.pid",
    "learner": "learner.pid",
    "watchdog": "watchdog.pid",
    "overseer": "overseer.pid",
    "god_console": "god_console.pid",
    "bus_persist": "bus_persist.pid",
    "idle_monitor": "idle_monitor.pid",
    "consultant_consumer": "consultant_consumer.pid",
    "consultant_bridge": "consultant_bridge.pid",
    "gemini_consultant_bridge": "gemini_consultant_bridge.pid",
    "proactive_handler": "proactive_handler.pid",
    "knowledge_distill": "knowledge_distill.pid",
    "self_heal": "self_heal.pid",
}


def _load_json(path: Path):
    """Load JSON file safely, return empty dict on failure."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _is_pid_alive(pid: int) -> bool:
    """Check if a process is alive (Windows kernel API)."""
    try:
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


def get_worker_states() -> dict:
    """Read worker states from worker_health.json (local, no HTTP)."""
    health = _load_json(DATA / "worker_health.json")
    workers = {}
    alive_count = 0
    for name in WORKER_NAMES:
        w = health.get(name, {})
        hwnd = int(w.get("hwnd", 0))
        is_alive = bool(hwnd and ctypes.windll.user32.IsWindow(hwnd))
        if is_alive:
            alive_count += 1
        workers[name] = {
            "hwnd": hwnd,
            "alive": is_alive,
            "status": w.get("status", "UNKNOWN"),
            "model": w.get("model", "unknown"),
            "agent": w.get("agent", "unknown"),
            "checked_at": w.get("checked_at", ""),
            "slot": w.get("slot", {}),
        }
    return {
        "workers": workers,
        "alive": alive_count,
        "total": len(WORKER_NAMES),
        "updated": health.get("updated", ""),
    }


def get_daemon_health() -> dict:
    """Check all known daemons via PID files (local, no HTTP)."""
    daemons = {}
    alive_count = 0
    stale_count = 0
    missing_count = 0

    for name, pid_filename in KNOWN_DAEMONS.items():
        pid_path = DATA / pid_filename
        if not pid_path.exists():
            daemons[name] = {"status": "NO_PID", "pid": None}
            missing_count += 1
            continue
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            daemons[name] = {"status": "CORRUPT_PID", "pid": None}
            stale_count += 1
            continue
        if _is_pid_alive(pid):
            daemons[name] = {"status": "ALIVE", "pid": pid}
            alive_count += 1
        else:
            daemons[name] = {"status": "STALE", "pid": pid}
            stale_count += 1

    # Unknown PID files
    for f in DATA.glob("*.pid"):
        daemon_name = f.stem
        if daemon_name not in KNOWN_DAEMONS:
            try:
                pid = int(f.read_text(encoding="utf-8").strip())
                alive = _is_pid_alive(pid)
            except (ValueError, OSError):
                pid, alive = None, False
            daemons[daemon_name] = {
                "status": "ALIVE" if alive else "STALE",
                "pid": pid, "unknown": True,
            }
            if alive:
                alive_count += 1
            else:
                stale_count += 1

    return {
        "daemons": daemons,
        "alive": alive_count,
        "stale": stale_count,
        "missing": missing_count,
        "total": len(KNOWN_DAEMONS),
    }


def get_test_results() -> dict:
    """Read test results from data/test_results.json if available."""
    results = _load_json(DATA / "test_results.json")
    if not results:
        return {"available": False, "pass_rate": None, "tests_run": 0}
    total = results.get("total", 0)
    passed = results.get("passed", 0)
    failed = results.get("failed", 0)
    return {
        "available": True,
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": round(passed / max(1, total) * 100, 1),
        "timestamp": results.get("timestamp", ""),
    }


def get_iq_score() -> dict:
    """Read IQ score from data/iq_history.json."""
    history = _load_json(DATA / "iq_history.json")
    if not history:
        return {"iq": None, "trend": "unknown", "history_count": 0}
    if isinstance(history, list) and len(history) > 0:
        latest = history[-1]
        iq = latest.get("iq", 0)
        # Determine trend from last 5 readings
        recent = history[-5:]
        if len(recent) >= 2:
            first_avg = sum(e.get("iq", 0) for e in recent[:len(recent)//2]) / max(1, len(recent)//2)
            last_avg = sum(e.get("iq", 0) for e in recent[len(recent)//2:]) / max(1, len(recent) - len(recent)//2)
            if last_avg > first_avg + 0.02:
                trend = "rising"
            elif last_avg < first_avg - 0.02:
                trend = "falling"
            else:
                trend = "stable"
        else:
            trend = "insufficient_data"
        return {
            "iq": round(iq, 4),
            "trend": trend,
            "history_count": len(history),
            "timestamp": latest.get("ts", 0),
        }
    return {"iq": None, "trend": "unknown", "history_count": 0}


def get_dispatch_stats() -> dict:
    """Read dispatch stats from data/dispatch_log.json."""
    log = _load_json(DATA / "dispatch_log.json")
    if isinstance(log, dict):
        entries = log.get("dispatches", log.get("log", []))
    elif isinstance(log, list):
        entries = log
    else:
        entries = []

    if not entries:
        return {"total": 0, "success": 0, "failed": 0, "success_rate": None,
                "pending": 0, "by_worker": {}}

    total = len(entries)
    success = sum(1 for e in entries if e.get("success") or e.get("result_received"))
    failed = sum(1 for e in entries if not e.get("success", True))
    pending = sum(1 for e in entries if e.get("success") and not e.get("result_received"))

    # Per-worker breakdown
    by_worker = {}
    for e in entries:
        w = e.get("worker", "unknown")
        if w not in by_worker:
            by_worker[w] = {"dispatched": 0, "success": 0, "failed": 0}
        by_worker[w]["dispatched"] += 1
        if e.get("success") or e.get("result_received"):
            by_worker[w]["success"] += 1
        else:
            by_worker[w]["failed"] += 1

    return {
        "total": total,
        "success": success,
        "failed": failed,
        "pending": pending,
        "success_rate": round(success / max(1, total) * 100, 1),
        "by_worker": by_worker,
    }


def get_uptime() -> dict:
    """Estimate uptime from backend start time or PID file ages."""
    result = {"backend_uptime_s": None, "oldest_pid_age_s": None}

    # Use realtime.json for backend uptime if available
    rt = _load_json(DATA / "realtime.json")
    if rt and rt.get("backend_uptime_s"):
        result["backend_uptime_s"] = rt["backend_uptime_s"]
    elif rt and rt.get("uptime_s"):
        result["backend_uptime_s"] = rt["uptime_s"]

    # Fallback: oldest PID file creation time
    oldest_age = 0
    for f in DATA.glob("*.pid"):
        try:
            age = time.time() - f.stat().st_ctime
            if age > oldest_age:
                oldest_age = age
        except OSError:
            pass
    if oldest_age > 0:
        result["oldest_pid_age_s"] = round(oldest_age, 0)

    return result


def get_todo_stats() -> dict:
    """Read TODO statistics from data/todos.json."""
    todos = _load_json(DATA / "todos.json")
    items = todos.get("todos", []) if isinstance(todos, dict) else todos if isinstance(todos, list) else []
    if not items:
        return {"total": 0, "pending": 0, "active": 0, "done": 0, "cancelled": 0}
    pending = sum(1 for t in items if t.get("status") == "pending")
    active = sum(1 for t in items if t.get("status") == "active")
    done = sum(1 for t in items if t.get("status") == "done")
    cancelled = sum(1 for t in items if t.get("status") == "cancelled")
    return {
        "total": len(items),
        "pending": pending,
        "active": active,
        "done": done,
        "cancelled": cancelled,
    }


def get_scores() -> dict:
    """Read worker scores from data/worker_scores.json."""
    scores = _load_json(DATA / "worker_scores.json")
    if not scores:
        return {}
    result = {}
    for name in WORKER_NAMES + ["orchestrator", "consultant", "gemini_consultant"]:
        if name in scores:
            s = scores[name]
            if isinstance(s, dict):
                result[name] = s.get("score", 0)
            elif isinstance(s, (int, float)):
                result[name] = s
    return result


def generate_dashboard_data() -> dict:
    """Generate unified JSON blob for GOD Console dashboard."""
    return {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "workers": get_worker_states(),
        "daemons": get_daemon_health(),
        "test_results": get_test_results(),
        "iq": get_iq_score(),
        "dispatch": get_dispatch_stats(),
        "uptime": get_uptime(),
        "todos": get_todo_stats(),
        "scores": get_scores(),
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skynet Dashboard Data -- unified JSON from local files")
    parser.add_argument("--output", "-o", type=str, help="Write to specific file")
    parser.add_argument("--write", "-w", action="store_true",
                        help="Write to data/dashboard_data.json")
    parser.add_argument("--pretty", "-p", action="store_true", help="Pretty-print JSON")
    args = parser.parse_args()

    data = generate_dashboard_data()
    indent = 2 if args.pretty else None

    if args.write:
        out = DATA / "dashboard_data.json"
        out.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        print(f"Written to {out}")
    elif args.output:
        out = Path(args.output)
        out.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        print(f"Written to {out}")
    else:
        print(json.dumps(data, indent=indent, default=str))


if __name__ == "__main__":
    main()
