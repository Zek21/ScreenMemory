#!/usr/bin/env python3
"""Observability module for Skynet system health tracking.

Collects system snapshots (worker states, bus stats, engine status, process
info), persists them to data/observability/, and provides comparison and
trend analysis functions.

CLI:
  python tools/skynet_observability.py snapshot          # Take a snapshot now
  python tools/skynet_observability.py compare A B       # Compare two snapshots
  python tools/skynet_observability.py trends [--hours 1] # Show trends
  python tools/skynet_observability.py health            # System health summary
  python tools/skynet_observability.py throughput         # Dispatch throughput
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OBS_DIR = DATA_DIR / "observability"

# Snapshot sources
WORKERS_FILE = DATA_DIR / "workers.json"
REALTIME_FILE = DATA_DIR / "realtime.json"
DISPATCH_LOG = DATA_DIR / "dispatch_log.json"
EPISODES_DIR = DATA_DIR / "episodes"
MISSIONS_FILE = DATA_DIR / "missions.json"
TODOS_FILE = DATA_DIR / "todos.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _worker_states() -> dict:
    """Read worker states from realtime.json or workers.json."""
    rt = _read_json(REALTIME_FILE)
    if isinstance(rt, dict) and "workers" in rt:
        return rt["workers"]
    wj = _read_json(WORKERS_FILE)
    if isinstance(wj, dict) and "workers" in wj:
        return {w["name"]: w.get("status", "unknown") for w in wj["workers"]}
    return {}


def _bus_stats() -> dict:
    """Get bus message counts from backend."""
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:8420/bus/stats", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return {"error": "unreachable", "total": 0}


def _dispatch_stats() -> dict:
    """Analyze dispatch_log.json for throughput metrics."""
    data = _read_json(DISPATCH_LOG)
    if not isinstance(data, list):
        return {"total": 0, "success": 0, "failure": 0, "rate": 0.0}
    total = len(data)
    success = sum(1 for d in data if d.get("success"))
    now = time.time()
    # Count dispatches in last hour
    recent = 0
    for d in data:
        ts = d.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts)
            if (now - dt.timestamp()) < 3600:
                recent += 1
        except (ValueError, TypeError):
            pass
    return {
        "total": total,
        "success": success,
        "failure": total - success,
        "success_rate": round(success / total, 3) if total else 0.0,
        "last_hour": recent,
        "throughput_per_min": round(recent / 60, 2) if recent else 0.0,
    }


def _episode_stats() -> dict:
    """Count episodes by outcome."""
    if not EPISODES_DIR.exists():
        return {"total": 0, "by_outcome": {}}
    by_outcome: dict[str, int] = {}
    total = 0
    for fp in EPISODES_DIR.glob("*.json"):
        try:
            ep = json.loads(fp.read_text(encoding="utf-8"))
            outcome = ep.get("outcome", "unknown")
            by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
            total += 1
        except (json.JSONDecodeError, OSError):
            pass
    return {"total": total, "by_outcome": by_outcome}


def _mission_stats() -> dict:
    """Mission counts by status."""
    data = _read_json(MISSIONS_FILE)
    if not isinstance(data, dict):
        return {"total": 0, "by_status": {}}
    missions = data.get("missions", [])
    by_status: dict[str, int] = {}
    for m in missions:
        s = m.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
    return {"total": len(missions), "by_status": by_status}


def _todo_stats() -> dict:
    """Todo counts by status."""
    data = _read_json(TODOS_FILE)
    if not isinstance(data, dict):
        return {"total": 0, "by_status": {}}
    todos = data.get("todos", [])
    by_status: dict[str, int] = {}
    for t in todos:
        s = t.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
    return {"total": len(todos), "by_status": by_status}


def _process_count() -> int:
    """Count running Python processes (lightweight)."""
    try:
        import subprocess
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process python* -ErrorAction SilentlyContinue).Count"],
            capture_output=True, text=True, timeout=5,
            creationflags=0x08000000,
        )
        return int(r.stdout.strip()) if r.stdout.strip().isdigit() else 0
    except Exception:
        return -1


def collect_system_snapshot() -> dict:
    """Collect a complete system state snapshot."""
    snapshot = {
        "snapshot_id": f"snap-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "timestamp": _now_iso(),
        "epoch": time.time(),
        "workers": _worker_states(),
        "bus": _bus_stats(),
        "dispatch": _dispatch_stats(),
        "episodes": _episode_stats(),
        "missions": _mission_stats(),
        "todos": _todo_stats(),
        "python_processes": _process_count(),
    }
    return snapshot


def save_snapshot(snapshot: dict) -> Path:
    """Persist snapshot to data/observability/."""
    OBS_DIR.mkdir(parents=True, exist_ok=True)
    path = OBS_DIR / f"{snapshot['snapshot_id']}.json"
    path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_snapshot(snapshot_id: str) -> Optional[dict]:
    """Load a snapshot by ID."""
    path = OBS_DIR / f"{snapshot_id}.json"
    return _read_json(path)


def list_snapshots(limit: int = 20) -> list[dict]:
    """List recent snapshots (newest first)."""
    if not OBS_DIR.exists():
        return []
    files = sorted(OBS_DIR.glob("snap-*.json"), reverse=True)
    result = []
    for fp in files[:limit]:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            result.append({
                "snapshot_id": data.get("snapshot_id", fp.stem),
                "timestamp": data.get("timestamp", ""),
                "workers": len(data.get("workers", {})),
                "dispatch_total": data.get("dispatch", {}).get("total", 0),
            })
        except (json.JSONDecodeError, OSError):
            pass
    return result


def compare_snapshots(snap_a: dict, snap_b: dict) -> dict:
    """Compare two snapshots and return deltas."""
    deltas = {
        "time_delta_s": round(snap_b.get("epoch", 0) - snap_a.get("epoch", 0), 1),
        "dispatch_delta": {
            "total": (snap_b.get("dispatch", {}).get("total", 0)
                      - snap_a.get("dispatch", {}).get("total", 0)),
            "success": (snap_b.get("dispatch", {}).get("success", 0)
                        - snap_a.get("dispatch", {}).get("success", 0)),
            "failure": (snap_b.get("dispatch", {}).get("failure", 0)
                        - snap_a.get("dispatch", {}).get("failure", 0)),
        },
        "episode_delta": {
            "total": (snap_b.get("episodes", {}).get("total", 0)
                      - snap_a.get("episodes", {}).get("total", 0)),
        },
        "worker_changes": {},
    }
    wa = snap_a.get("workers", {})
    wb = snap_b.get("workers", {})
    all_workers = set(list(wa.keys()) + list(wb.keys()))
    for w in all_workers:
        sa = wa.get(w, "absent")
        sb = wb.get(w, "absent")
        if sa != sb:
            deltas["worker_changes"][w] = {"from": sa, "to": sb}
    return deltas


def trend_analysis(hours: float = 1.0) -> dict:
    """Analyze trends from recent snapshots within the given time window."""
    if not OBS_DIR.exists():
        return {"snapshots": 0, "window_hours": hours}
    cutoff = time.time() - (hours * 3600)
    snapshots = []
    for fp in sorted(OBS_DIR.glob("snap-*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if data.get("epoch", 0) >= cutoff:
                snapshots.append(data)
        except (json.JSONDecodeError, OSError):
            pass

    if not snapshots:
        return {"snapshots": 0, "window_hours": hours}

    dispatch_totals = [s.get("dispatch", {}).get("total", 0) for s in snapshots]
    success_rates = [s.get("dispatch", {}).get("success_rate", 0) for s in snapshots]
    episode_totals = [s.get("episodes", {}).get("total", 0) for s in snapshots]

    return {
        "snapshots": len(snapshots),
        "window_hours": hours,
        "dispatch": {
            "min": min(dispatch_totals),
            "max": max(dispatch_totals),
            "growth": dispatch_totals[-1] - dispatch_totals[0] if len(dispatch_totals) > 1 else 0,
        },
        "success_rate": {
            "min": min(success_rates),
            "max": max(success_rates),
            "latest": success_rates[-1] if success_rates else 0,
        },
        "episodes": {
            "min": min(episode_totals),
            "max": max(episode_totals),
            "growth": episode_totals[-1] - episode_totals[0] if len(episode_totals) > 1 else 0,
        },
        "first_snapshot": snapshots[0].get("timestamp", ""),
        "last_snapshot": snapshots[-1].get("timestamp", ""),
    }


def system_health() -> dict:
    """Quick system health assessment based on latest data."""
    workers = _worker_states()
    dispatch = _dispatch_stats()
    episodes = _episode_stats()
    bus = _bus_stats()

    issues = []
    worker_count = len(workers)
    if worker_count == 0:
        issues.append("No workers detected")
    idle_count = sum(1 for s in workers.values()
                     if (s if isinstance(s, str) else str(s)).upper() in ("IDLE", ""))
    if worker_count > 0 and idle_count == worker_count:
        issues.append("All workers idle")

    if dispatch.get("success_rate", 1.0) < 0.5 and dispatch.get("total", 0) > 5:
        issues.append(f"Low dispatch success rate: {dispatch['success_rate']}")

    if bus.get("error"):
        issues.append(f"Bus: {bus['error']}")

    status = "healthy" if not issues else ("degraded" if len(issues) <= 2 else "unhealthy")
    return {
        "status": status,
        "timestamp": _now_iso(),
        "workers": {"total": worker_count, "idle": idle_count},
        "dispatch": dispatch,
        "episodes": episodes,
        "bus_ok": "error" not in bus,
        "issues": issues,
    }


def throughput_metrics() -> dict:
    """Dispatch throughput metrics for dashboard consumption."""
    dispatch = _dispatch_stats()
    return {
        "timestamp": _now_iso(),
        "total_dispatches": dispatch.get("total", 0),
        "successful": dispatch.get("success", 0),
        "failed": dispatch.get("failure", 0),
        "success_rate": dispatch.get("success_rate", 0.0),
        "last_hour": dispatch.get("last_hour", 0),
        "throughput_per_min": dispatch.get("throughput_per_min", 0.0),
    }


def _build_observability_parser():
    """Build the observability CLI parser."""
    parser = argparse.ArgumentParser(description="Skynet Observability")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("snapshot", help="Take a system snapshot")
    sub.add_parser("health", help="System health summary")
    sub.add_parser("throughput", help="Dispatch throughput metrics")
    p_compare = sub.add_parser("compare", help="Compare two snapshots")
    p_compare.add_argument("snap_a")
    p_compare.add_argument("snap_b")
    p_trends = sub.add_parser("trends", help="Trend analysis")
    p_trends.add_argument("--hours", type=float, default=1.0)
    p_list = sub.add_parser("list", help="List snapshots")
    p_list.add_argument("--limit", type=int, default=20)
    return parser


def _dispatch_observability_command(args) -> int:
    """Dispatch parsed CLI command."""
    if args.command == "snapshot":
        snap = collect_system_snapshot()
        path = save_snapshot(snap)
        print(f"Snapshot saved: {path}")
        print(json.dumps(snap, indent=2))
        return 0
    if args.command == "health":
        print(json.dumps(system_health(), indent=2))
        return 0
    if args.command == "throughput":
        print(json.dumps(throughput_metrics(), indent=2))
        return 0
    if args.command == "compare":
        a, b = load_snapshot(args.snap_a), load_snapshot(args.snap_b)
        if not a or not b:
            print("Snapshot(s) not found")
            return 1
        print(json.dumps(compare_snapshots(a, b), indent=2))
        return 0
    if args.command == "trends":
        print(json.dumps(trend_analysis(hours=args.hours), indent=2))
        return 0
    if args.command == "list":
        for s in list_snapshots(limit=args.limit):
            print(f"  {s['snapshot_id']}  {s['timestamp']}  dispatches={s['dispatch_total']}")
        return 0
    return -1


def main() -> int:
    parser = _build_observability_parser()
    args = parser.parse_args()
    result = _dispatch_observability_command(args)
    if result == -1:
        parser.print_help()
        return 0
    return result


if __name__ == "__main__":
    raise SystemExit(main())
