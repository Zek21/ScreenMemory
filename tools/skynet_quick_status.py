#!/usr/bin/env python3
"""
skynet_quick_status.py -- One-command Skynet system health overview.

Replaces running 5+ separate commands to check system state.
Shows workers, daemons, bus, IQ, and TODOs in a single formatted table.

Usage:
    python tools/skynet_quick_status.py          # Human-readable table
    python tools/skynet_quick_status.py --json    # Machine-readable JSON

# signed: delta
"""

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"

WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

DAEMON_PID_MAP = {
    "monitor":              "monitor.pid",
    "watchdog":             "watchdog.pid",
    "realtime":             "realtime.pid",
    "self_prompt":          "self_prompt.pid",
    "self_improve":         "self_improve.pid",
    "bus_relay":            "bus_relay.pid",
    "learner":              "learner.pid",
    "overseer":             "overseer.pid",
    "sse_daemon":           "sse_daemon.pid",
    "bus_watcher":          "bus_watcher.pid",
    "bus_persist":          "bus_persist.pid",
    "idle_monitor":         "idle_monitor.pid",
    "consultant_consumer":  "consultant_consumer_8422.pid",
}


def _check_workers():
    """Check worker states from local HWND + backend."""
    results = {}
    # Local HWND check (fast)
    health_file = DATA / "worker_health.json"
    if health_file.exists():
        try:
            import ctypes
            health = json.loads(health_file.read_text())
            for name in WORKER_NAMES:
                w = health.get(name, {})
                hwnd = int(w.get("hwnd", 0))
                alive = bool(hwnd and ctypes.windll.user32.IsWindow(hwnd))
                results[name] = {
                    "hwnd": hwnd,
                    "alive": alive,
                    "status": w.get("status", "UNKNOWN"),
                    "model_ok": "opus" in w.get("model", "").lower(),
                    "checked": w.get("checked_at", ""),
                }
        except Exception:
            pass

    # Try backend for live status (short timeout)
    try:
        from urllib.request import urlopen
        resp = urlopen("http://localhost:8420/status", timeout=1)
        status = json.loads(resp.read())
        agents = status.get("agents", {})
        for name in WORKER_NAMES:
            a = agents.get(name, {})
            if a:
                if name in results:
                    results[name]["status"] = a.get("status", results[name]["status"])
                else:
                    results[name] = {
                        "hwnd": 0,
                        "alive": a.get("status") != "DEAD",
                        "status": a.get("status", "UNKNOWN"),
                        "model_ok": True,
                        "checked": "",
                    }
    except Exception:
        pass

    # Fill missing
    for name in WORKER_NAMES:
        if name not in results:
            results[name] = {"hwnd": 0, "alive": False, "status": "UNKNOWN", "model_ok": False, "checked": ""}

    return results


def _check_daemons():
    """Check daemon PID file vs live process."""
    results = {}
    for name, pid_file in DAEMON_PID_MAP.items():
        pid_path = DATA / pid_file
        if not pid_path.exists():
            results[name] = {"pid": None, "alive": False, "status": "NO_PID"}
            continue
        try:
            pid = int(pid_path.read_text().strip())
            # Check if process is alive
            if sys.platform == "win32":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
                if handle:
                    kernel32.CloseHandle(handle)
                    results[name] = {"pid": pid, "alive": True, "status": "RUNNING"}
                else:
                    results[name] = {"pid": pid, "alive": False, "status": "STALE_PID"}
            else:
                os.kill(pid, 0)
                results[name] = {"pid": pid, "alive": True, "status": "RUNNING"}
        except (ProcessLookupError, PermissionError):
            results[name] = {"pid": pid, "alive": False, "status": "STALE_PID"}
        except Exception:
            results[name] = {"pid": None, "alive": False, "status": "ERROR"}
    return results


def _check_bus():
    """Get last 5 bus messages summary."""
    try:
        from urllib.request import urlopen
        resp = urlopen("http://localhost:8420/bus/messages?limit=5", timeout=2)
        messages = json.loads(resp.read())
        if isinstance(messages, dict):
            messages = messages.get("messages", [])
        summaries = []
        for m in messages[:5]:
            sender = m.get("sender", "?")
            topic = m.get("topic", "?")
            mtype = m.get("type", "?")
            content = str(m.get("content", ""))[:60]
            summaries.append({"sender": sender, "topic": topic, "type": mtype, "content": content})
        return {"status": "UP", "messages": summaries}
    except Exception:
        return {"status": "DOWN", "messages": []}


def _check_iq():
    """Get current IQ score and trend."""
    try:
        from tools.skynet_self import SkynetSelf
        SkynetSelf._pulse_cache = None
        SkynetSelf._pulse_cache_t = 0
        s = SkynetSelf()
        pulse = s.quick_pulse()
        return {"iq": pulse.get("iq", 0), "trend": pulse.get("iq_trend", "unknown"),
                "health": pulse.get("health", "UNKNOWN")}
    except Exception as e:
        # Fallback: read from iq_history.json
        try:
            history = json.loads((DATA / "iq_history.json").read_text())
            if history:
                last = history[-1]
                return {"iq": last.get("iq", 0), "trend": "unknown", "health": "UNKNOWN"}
        except Exception:
            pass
        return {"iq": 0, "trend": "unknown", "health": "ERROR"}


def _check_todos():
    """Count pending/active/done TODOs."""
    try:
        todos = json.loads((DATA / "todos.json").read_text())
        items = todos.get("todos", []) if isinstance(todos, dict) else todos
        pending = sum(1 for t in items if t.get("status") == "pending")
        active = sum(1 for t in items if t.get("status") == "active")
        done = sum(1 for t in items if t.get("status") == "done")
        return {"pending": pending, "active": active, "done": done, "total": len(items)}
    except Exception:
        return {"pending": 0, "active": 0, "done": 0, "total": 0}


def collect_status():
    """Collect all status data."""
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "workers": _check_workers(),
        "daemons": _check_daemons(),
        "bus": _check_bus(),
        "iq": _check_iq(),
        "todos": _check_todos(),
    }


def format_table(status):
    """Format status as a readable table."""
    lines = []
    lines.append("=" * 62)
    lines.append("  SKYNET QUICK STATUS")
    lines.append(f"  {status['timestamp']}")
    lines.append("=" * 62)

    # IQ
    iq = status["iq"]
    trend_sym = {"rising": "▲", "falling": "▼", "stable": "●"}.get(iq["trend"], "?")
    lines.append(f"\n  IQ: {iq['iq']:.4f} {trend_sym} ({iq['trend']})  Health: {iq['health']}")

    # Workers
    lines.append(f"\n  {'WORKERS':-<58}")
    lines.append(f"  {'Name':<8} {'Status':<12} {'Alive':<7} {'Model':<8} {'HWND':<12}")
    lines.append(f"  {'----':<8} {'------':<12} {'-----':<7} {'-----':<8} {'----':<12}")
    for name in WORKER_NAMES:
        w = status["workers"].get(name, {})
        st = w.get("status", "?")
        alive = "YES" if w.get("alive") else "NO"
        model = "OK" if w.get("model_ok") else "DRIFT"
        hwnd = str(w.get("hwnd", 0))
        lines.append(f"  {name:<8} {st:<12} {alive:<7} {model:<8} {hwnd:<12}")

    alive_count = sum(1 for w in status["workers"].values() if w.get("alive"))
    lines.append(f"  Total: {alive_count}/{len(WORKER_NAMES)} alive")

    # Daemons
    lines.append(f"\n  {'DAEMONS':-<58}")
    running = sum(1 for d in status["daemons"].values() if d.get("alive"))
    total_d = len(status["daemons"])
    lines.append(f"  {running}/{total_d} running")
    for name, d in sorted(status["daemons"].items()):
        st = d.get("status", "?")
        pid = d.get("pid", "-")
        marker = "●" if d.get("alive") else "○"
        lines.append(f"  {marker} {name:<24} PID: {str(pid):<8} {st}")

    # Bus
    bus = status["bus"]
    lines.append(f"\n  {'BUS':-<58}")
    lines.append(f"  Status: {bus['status']}")
    if bus["messages"]:
        lines.append(f"  Last {len(bus['messages'])} messages:")
        for m in bus["messages"]:
            lines.append(f"    [{m['sender']}] {m['type']}: {m['content']}")
    else:
        lines.append("  No recent messages")

    # TODOs
    todos = status["todos"]
    lines.append(f"\n  {'TODOS':-<58}")
    lines.append(f"  Pending: {todos['pending']}  Active: {todos['active']}  "
                 f"Done: {todos['done']}  Total: {todos['total']}")

    lines.append("\n" + "=" * 62)
    return "\n".join(lines)


def main():
    as_json = "--json" in sys.argv
    status = collect_status()
    if as_json:
        print(json.dumps(status, indent=2, default=str))
    else:
        print(format_table(status))


if __name__ == "__main__":
    main()
