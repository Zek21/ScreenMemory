#!/usr/bin/env python3
"""Skynet Health Dashboard — terminal-based system health overview.

Reads ONLY from local files (data/*.json) — zero HTTP calls.
Color-coded status with single-screen overview of all subsystems.

CLI:
    python tools/skynet_health_dashboard.py           # Single snapshot
    python tools/skynet_health_dashboard.py --once    # Same as default
    python tools/skynet_health_dashboard.py --watch   # Refresh every 5s
    python tools/skynet_health_dashboard.py --json    # JSON output
"""
# signed: delta

import ctypes
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

KNOWN_DAEMONS = [
    "sse_daemon", "monitor", "self_prompt", "self_improve", "bus_relay",
    "learner", "watchdog", "overseer", "god_console", "bus_persist",
    "idle_monitor", "consultant_consumer", "consultant_bridge",
    "gemini_consultant_bridge", "proactive_handler", "knowledge_distill",
    "self_heal",
]

# ANSI color codes
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _load_json(path: Path):
    """Load JSON file safely."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _is_pid_alive(pid: int) -> bool:
    """Check if process is alive (Windows)."""
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


def _color(text: str, color: str) -> str:
    """Apply ANSI color if stdout is a terminal."""
    if not sys.stdout.isatty():
        return text
    return f"{color}{text}{RESET}"


def _status_color(status: str) -> str:
    """Color-code a status string."""
    s = status.upper()
    if s in ("ALIVE", "IDLE", "ONLINE", "HEALTHY", "OK", "RISING"):
        return _color(status, GREEN)
    elif s in ("PROCESSING", "WORKING", "ACTIVE", "STABLE", "AVAILABLE"):
        return _color(status, CYAN)
    elif s in ("DEGRADED", "STALE", "FALLING", "WARNING", "SLOW"):
        return _color(status, YELLOW)
    elif s in ("DEAD", "CRITICAL", "OFFLINE", "ERROR", "CORRUPT_PID", "FAILED"):
        return _color(status, RED)
    return status


def _bar(value: float, width: int = 20) -> str:
    """Render a simple progress bar."""
    filled = int(value * width)
    bar = "=" * filled + "-" * (width - filled)
    pct = f"{value * 100:.0f}%"
    if value >= 0.8:
        return _color(f"[{bar}] {pct}", GREEN)
    elif value >= 0.5:
        return _color(f"[{bar}] {pct}", YELLOW)
    else:
        return _color(f"[{bar}] {pct}", RED)


# ── Data collectors ──────────────────────────────────────────────

def collect_workers() -> dict:
    """Collect worker states from local files."""
    health = _load_json(DATA / "worker_health.json")
    workers = {}
    alive = 0
    for name in WORKER_NAMES:
        w = health.get(name, {})
        hwnd = int(w.get("hwnd", 0))
        is_alive = bool(hwnd and ctypes.windll.user32.IsWindow(hwnd))
        if is_alive:
            alive += 1
        workers[name] = {
            "alive": is_alive,
            "status": w.get("status", "UNKNOWN"),
            "hwnd": hwnd,
            "model": w.get("model", "unknown"),
            "checked_at": w.get("checked_at", ""),
        }
    return {"workers": workers, "alive": alive, "total": len(WORKER_NAMES)}


def collect_daemons() -> dict:
    """Collect daemon health from PID files."""
    daemons = {}
    alive = 0
    stale = 0
    missing = 0
    for name in KNOWN_DAEMONS:
        pid_path = DATA / f"{name}.pid"
        if not pid_path.exists():
            daemons[name] = {"status": "NO_PID"}
            missing += 1
            continue
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            daemons[name] = {"status": "CORRUPT_PID"}
            stale += 1
            continue
        if _is_pid_alive(pid):
            daemons[name] = {"status": "ALIVE", "pid": pid}
            alive += 1
        else:
            daemons[name] = {"status": "STALE", "pid": pid}
            stale += 1
    return {"daemons": daemons, "alive": alive, "stale": stale, "missing": missing}


def collect_bus() -> dict:
    """Collect bus info from realtime.json."""
    rt = _load_json(DATA / "realtime.json")
    if not rt:
        return {"status": "UNKNOWN", "messages": 0}
    # Check freshness
    ts = rt.get("ts", 0)
    age = time.time() - ts if ts else 999
    status = "ALIVE" if age < 30 else ("STALE" if age < 120 else "DEAD")
    return {
        "status": status,
        "age_s": round(age, 0),
        "uptime_s": rt.get("uptime_s", rt.get("backend_uptime_s", 0)),
    }


def collect_iq() -> dict:
    """Collect IQ score from iq_history.json."""
    history = _load_json(DATA / "iq_history.json")
    if not history or not isinstance(history, list) or len(history) == 0:
        return {"iq": None, "trend": "unknown"}
    latest = history[-1]
    iq = latest.get("iq", 0)
    recent = history[-5:]
    if len(recent) >= 2:
        first_half = recent[:len(recent) // 2]
        second_half = recent[len(recent) // 2:]
        avg1 = sum(e.get("iq", 0) for e in first_half) / max(1, len(first_half))
        avg2 = sum(e.get("iq", 0) for e in second_half) / max(1, len(second_half))
        trend = "rising" if avg2 > avg1 + 0.02 else ("falling" if avg2 < avg1 - 0.02 else "stable")
    else:
        trend = "unknown"
    return {"iq": round(iq, 4), "trend": trend, "readings": len(history)}


def collect_dispatch() -> dict:
    """Collect dispatch stats from dispatch_log.json."""
    log = _load_json(DATA / "dispatch_log.json")
    entries = log if isinstance(log, list) else log.get("dispatches", log.get("log", []))
    if not entries:
        return {"total": 0, "success_rate": None}
    total = len(entries)
    success = sum(1 for e in entries if e.get("success") or e.get("result_received"))
    return {
        "total": total,
        "success": success,
        "failed": total - success,
        "success_rate": round(success / max(1, total) * 100, 1),
    }


def collect_todos() -> dict:
    """Collect TODO stats from todos.json."""
    todos = _load_json(DATA / "todos.json")
    items = todos.get("todos", []) if isinstance(todos, dict) else (todos if isinstance(todos, list) else [])
    pending = sum(1 for t in items if t.get("status") == "pending")
    active = sum(1 for t in items if t.get("status") == "active")
    done = sum(1 for t in items if t.get("status") == "done")
    return {"total": len(items), "pending": pending, "active": active, "done": done}


def collect_scores() -> dict:
    """Collect agent scores from worker_scores.json."""
    scores = _load_json(DATA / "worker_scores.json")
    result = {}
    for name in WORKER_NAMES + ["orchestrator", "consultant", "gemini_consultant"]:
        if name in scores:
            s = scores[name]
            result[name] = s.get("score", s) if isinstance(s, dict) else s
    return result


def collect_monitor_metrics() -> dict:
    """Collect monitor metrics from monitor_metrics.json."""
    return _load_json(DATA / "monitor_metrics.json")


def collect_tests() -> dict:
    """Collect test results from test_results.json."""
    results = _load_json(DATA / "test_results.json")
    if not results:
        return {"available": False}
    return {
        "available": True,
        "total": results.get("total", 0),
        "passed": results.get("passed", 0),
        "failed": results.get("failed", 0),
        "pass_rate": round(results.get("passed", 0) / max(1, results.get("total", 1)) * 100, 1),
    }


def collect_health_report() -> dict:
    """Read latest health report from health_report.json."""
    report = _load_json(DATA / "health_report.json")
    if not report:
        return {"available": False}
    return {
        "available": True,
        "timestamp": report.get("timestamp", ""),
        "issues": len(report.get("issues", [])),
        "fixes": len(report.get("fixes_applied", [])),
    }


# ── Dashboard rendering ─────────────────────────────────────────

def collect_all() -> dict:
    """Collect all dashboard data."""
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "workers": collect_workers(),
        "daemons": collect_daemons(),
        "bus": collect_bus(),
        "iq": collect_iq(),
        "dispatch": collect_dispatch(),
        "todos": collect_todos(),
        "scores": collect_scores(),
        "tests": collect_tests(),
        "monitor_metrics": collect_monitor_metrics(),
        "health_report": collect_health_report(),
    }


def render_dashboard(data: dict):
    """Render the full terminal dashboard."""
    # Clear screen
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")

    w = data["workers"]
    d = data["daemons"]
    b = data["bus"]
    iq = data["iq"]
    dp = data["dispatch"]
    td = data["todos"]
    sc = data["scores"]
    ts = data["tests"]
    hr = data["health_report"]
    mm = data["monitor_metrics"]

    print(_color("=" * 70, DIM))
    print(_color(f"  SKYNET HEALTH DASHBOARD  --  {data['timestamp']}", BOLD))
    print(_color("=" * 70, DIM))

    # ── Workers ──
    wa = w["alive"]
    wt = w["total"]
    w_label = _color(f"{wa}/{wt}", GREEN if wa == wt else (YELLOW if wa > 0 else RED))
    print(f"\n  {_color('WORKERS', BOLD)} {w_label}")
    for name, info in w["workers"].items():
        alive_icon = _color("+", GREEN) if info["alive"] else _color("X", RED)
        st = _status_color(info["status"])
        print(f"    [{alive_icon}] {name:8s}  {st:20s}  HWND={info['hwnd']}")

    # ── IQ ──
    if iq["iq"] is not None:
        iq_val = iq["iq"]
        iq_bar = _bar(iq_val)
        trend = _status_color(iq["trend"].upper())
        print(f"\n  {_color('IQ', BOLD)}  {iq_val:.4f}  {iq_bar}  {trend}")
    else:
        print(f"\n  {_color('IQ', BOLD)}  {_color('NO DATA', DIM)}")

    # ── Bus ──
    bus_st = _status_color(b["status"])
    uptime = b.get("uptime_s", 0)
    uptime_str = f"{uptime:.0f}s" if uptime else "?"
    print(f"\n  {_color('BUS', BOLD)}  {bus_st}  uptime={uptime_str}  age={b.get('age_s', '?')}s")

    # ── Dispatch ──
    if dp["total"] > 0:
        rate = dp.get("success_rate", 0)
        rate_bar = _bar(rate / 100)
        print(f"\n  {_color('DISPATCH', BOLD)}  {dp['total']} total  "
              f"{dp['success']} ok  {dp['failed']} fail  {rate_bar}")
    else:
        print(f"\n  {_color('DISPATCH', BOLD)}  {_color('NO DATA', DIM)}")

    # ── Monitor Metrics (dispatch latency + completion rate) ──
    if mm:
        lat = mm.get("dispatch_latency", {})
        comp = mm.get("completion_rates", {})
        if lat or comp:
            print(f"\n  {_color('MONITOR METRICS', BOLD)}")
            if lat:
                for wname, avg_ms in sorted(lat.items()):
                    if isinstance(avg_ms, (int, float)):
                        color = GREEN if avg_ms < 5000 else (YELLOW if avg_ms < 10000 else RED)
                        print(f"    {wname:8s}  avg latency: {_color(f'{avg_ms:.0f}ms', color)}")
            if comp:
                for wname, rate in sorted(comp.items()):
                    if isinstance(rate, (int, float)):
                        color = GREEN if rate > 0.8 else (YELLOW if rate > 0.5 else RED)
                        print(f"    {wname:8s}  completion:  {_color(f'{rate*100:.0f}%', color)}")

    # ── Daemons ──
    da = d["alive"]
    ds = d["stale"]
    dm = d["missing"]
    d_label = _color(f"{da} alive", GREEN if ds == 0 else YELLOW)
    stale_label = _color(f"{ds} stale", RED) if ds > 0 else ""
    miss_label = _color(f"{dm} missing", DIM) if dm > 0 else ""
    extras = "  ".join(filter(None, [stale_label, miss_label]))
    print(f"\n  {_color('DAEMONS', BOLD)}  {d_label}  {extras}")
    # Compact daemon list — only show alive and stale
    alive_list = [n for n, info in d["daemons"].items() if info["status"] == "ALIVE"]
    stale_list = [n for n, info in d["daemons"].items() if info["status"] in ("STALE", "CORRUPT_PID")]
    if alive_list:
        print(f"    {_color('+', GREEN)} {', '.join(alive_list)}")
    if stale_list:
        print(f"    {_color('X', RED)} {', '.join(stale_list)}")

    # ── TODOs ──
    print(f"\n  {_color('TODOs', BOLD)}  {td['total']} total  "
          f"{_color(str(td['pending']), YELLOW if td['pending'] > 0 else GREEN)} pending  "
          f"{td['active']} active  {td['done']} done")

    # ── Tests ──
    if ts.get("available"):
        tp = ts.get("pass_rate", 0)
        t_bar = _bar(tp / 100, width=15)
        print(f"\n  {_color('TESTS', BOLD)}  {ts['passed']}/{ts['total']} passed  {t_bar}")
    else:
        print(f"\n  {_color('TESTS', BOLD)}  {_color('no test_results.json', DIM)}")

    # ── Scores ──
    if sc:
        print(f"\n  {_color('SCORES', BOLD)}")
        for name, score in sorted(sc.items(), key=lambda x: x[1], reverse=True):
            color = GREEN if score > 0 else (YELLOW if score == 0 else RED)
            print(f"    {name:20s}  {_color(f'{score:+.2f}', color)}")

    # ── Health Report ──
    if hr.get("available"):
        print(f"\n  {_color('LAST HEALTH CHECK', BOLD)}  {hr['timestamp']}  "
              f"{hr['issues']} issues  {hr['fixes']} fixes")

    print(_color("\n" + "=" * 70, DIM))


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skynet Health Dashboard")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--once", action="store_true", help="Single snapshot (default)")
    parser.add_argument("--watch", action="store_true", help="Refresh every 5s")
    parser.add_argument("--interval", type=int, default=5, help="Watch interval (default 5s)")
    args = parser.parse_args()

    if args.json:
        data = collect_all()
        print(json.dumps(data, indent=2, default=str))
        return

    if args.watch:
        try:
            while True:
                data = collect_all()
                render_dashboard(data)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nDashboard stopped.")
    else:
        data = collect_all()
        render_dashboard(data)


if __name__ == "__main__":
    main()
