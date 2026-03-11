"""Skynet Self-Healing Dispatch -- detects stuck tasks and auto-recovers.

Monitors worker state, detects stuck/failed dispatches, auto-heals by
re-dispatching or cancelling, and produces health reports.

Usage:
    python tools/skynet_self_heal.py detect     # Show stuck tasks
    python tools/skynet_self_heal.py heal       # Auto-heal stuck tasks
    python tools/skynet_self_heal.py report     # Full health report
    python tools/skynet_self_heal.py run        # Continuous monitoring loop
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

DISPATCH_LOG = ROOT / "data" / "dispatch_log.json"
WORKER_PERF = ROOT / "data" / "worker_performance.json"
REALTIME_FILE = ROOT / "data" / "realtime.json"
HEAL_LOG = ROOT / "data" / "heal_log.json"

STUCK_THRESHOLDS = {
    "simple": 120,    # 2 min
    "standard": 180,  # 3 min
    "complex": 300,   # 5 min
}

ALL_WORKERS = ["alpha", "beta", "gamma", "delta"]


def _load_json(path: Path) -> dict | list:
    """Load JSON file, return empty dict/list on failure."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json(path: Path, data) -> None:
    """Atomically save JSON data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _get_worker_states() -> dict[str, dict]:
    """Read worker states from realtime.json (zero-network)."""
    data = _load_json(REALTIME_FILE)
    if not data:
        return {}
    workers = data.get("workers", {})
    result = {}
    for name, info in workers.items():
        if isinstance(info, dict):
            result[name] = {
                "state": info.get("state", "UNKNOWN"),
                "since": info.get("since", 0),
                "task": info.get("task", ""),
            }
        else:
            result[name] = {"state": str(info), "since": 0, "task": ""}
    return result


def _get_dispatch_log() -> list[dict]:
    """Read dispatch log entries."""
    data = _load_json(DISPATCH_LOG)
    if isinstance(data, list):
        return data
    return data.get("dispatches", data.get("log", []))


def detect_stuck_tasks(threshold_s: Optional[float] = None) -> list[dict]:
    """Detect tasks that appear stuck based on worker state and timing.

    Returns list of {worker, state, stuck_seconds, task, severity}.
    """
    threshold = threshold_s or STUCK_THRESHOLDS["standard"]
    workers = _get_worker_states()
    now = time.time()
    stuck = []

    for name, info in workers.items():
        state = info.get("state", "UNKNOWN")
        if state != "PROCESSING":
            continue

        since = info.get("since", 0)
        if since <= 0:
            continue

        elapsed = now - since
        if elapsed > threshold:
            severity = "critical" if elapsed > threshold * 2 else "warning"
            stuck.append({
                "worker": name,
                "state": state,
                "stuck_seconds": round(elapsed, 1),
                "task": info.get("task", "unknown"),
                "severity": severity,
                "threshold": threshold,
            })

    return stuck


def auto_heal(dry_run: bool = False) -> list[dict]:
    """Auto-heal stuck tasks by cancelling and optionally re-dispatching.

    Args:
        dry_run: If True, report what would be done without acting.

    Returns list of {worker, action, result} dicts.
    """
    stuck = detect_stuck_tasks()
    actions = []

    for task in stuck:
        worker = task["worker"]
        action = {
            "worker": worker,
            "stuck_seconds": task["stuck_seconds"],
            "severity": task["severity"],
            "task_text": task.get("task", ""),
        }

        if dry_run:
            action["action"] = "would_cancel"
            action["result"] = "dry_run"
            actions.append(action)
            continue

        # Attempt to cancel via UIA
        cancelled = False
        try:
            from tools.uia_engine import get_engine
            workers_json = ROOT / "data" / "workers.json"
            if workers_json.exists():
                wdata = json.loads(workers_json.read_text(encoding="utf-8"))
                hwnd = wdata.get(worker, {}).get("hwnd")
                if hwnd:
                    engine = get_engine()
                    engine.cancel_generation(hwnd)
                    cancelled = True
        except Exception as e:
            action["cancel_error"] = str(e)

        if cancelled:
            action["action"] = "cancelled"
            action["result"] = "success"
        else:
            action["action"] = "cancel_failed"
            action["result"] = "manual_intervention_needed"

        actions.append(action)

    # Log heal actions
    if actions and not dry_run:
        log = _load_json(HEAL_LOG)
        if not isinstance(log, list):
            log = []
        log.append({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "actions": actions,
        })
        _save_json(HEAL_LOG, log[-100:])  # Keep last 100

    # Post to bus if actions taken
    if actions and not dry_run:
        try:
            import requests
            summary = "; ".join(
                f"{a['worker']}:{a['action']}" for a in actions
            )
            requests.post(
                "http://localhost:8420/bus/publish",
                json={
                    "sender": "self_heal",
                    "topic": "orchestrator",
                    "type": "alert",
                    "content": f"AUTO-HEAL: {summary}",
                },
                timeout=3,
            )
        except Exception:
            pass

    return actions


def health_report() -> dict:
    """Generate comprehensive dispatch health report.

    Returns dict with worker_states, stuck_tasks, recent_heals,
    dispatch_stats, and recommendations.
    """
    workers = _get_worker_states()
    stuck = detect_stuck_tasks()
    heal_log = _load_json(HEAL_LOG)
    if not isinstance(heal_log, list):
        heal_log = []
    perf = _load_json(WORKER_PERF)

    # Worker state summary
    state_counts = {}
    for w in workers.values():
        st = w.get("state", "UNKNOWN")
        state_counts[st] = state_counts.get(st, 0) + 1

    # Dispatch stats
    perf_workers = perf.get("workers", {})
    total_completed = sum(w.get("tasks_completed", 0) for w in perf_workers.values())
    total_failed = sum(w.get("tasks_failed", 0) for w in perf_workers.values())

    # Recommendations
    recommendations = []
    if len(stuck) > 0:
        recommendations.append(f"URGENT: {len(stuck)} stuck task(s) detected -- run auto_heal()")
    if total_failed > total_completed * 0.3 and total_completed > 0:
        recommendations.append("HIGH FAILURE RATE: >30% tasks failing. Investigate root cause.")

    idle_count = state_counts.get("IDLE", 0)
    processing_count = state_counts.get("PROCESSING", 0)
    if idle_count > 2 and processing_count == 0:
        recommendations.append("UNDERUTILIZED: Multiple idle workers. Dispatch more tasks.")

    if not recommendations:
        recommendations.append("System healthy. No issues detected.")

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "worker_states": workers,
        "state_summary": state_counts,
        "stuck_tasks": stuck,
        "recent_heals": heal_log[-5:],
        "dispatch_stats": {
            "total_completed": total_completed,
            "total_failed": total_failed,
            "success_rate": round(
                total_completed / max(1, total_completed + total_failed) * 100, 1
            ),
        },
        "recommendations": recommendations,
    }


def run_continuous(interval_s: float = 30.0, max_iterations: int = 0):
    """Run continuous self-healing monitoring loop.

    Args:
        interval_s: Check interval in seconds.
        max_iterations: Max loops (0 = infinite).
    """
    iteration = 0
    print(f"[SELF-HEAL] Starting continuous monitor (interval={interval_s}s)")

    while max_iterations == 0 or iteration < max_iterations:
        iteration += 1
        try:
            stuck = detect_stuck_tasks()
            if stuck:
                print(f"[SELF-HEAL] Iteration {iteration}: {len(stuck)} stuck task(s) found")
                actions = auto_heal()
                for a in actions:
                    print(f"  {a['worker']}: {a['action']} ({a.get('result', 'unknown')})")
            else:
                if iteration % 10 == 0:
                    print(f"[SELF-HEAL] Iteration {iteration}: all clear")
        except Exception as e:
            print(f"[SELF-HEAL] Error in iteration {iteration}: {e}")

        time.sleep(interval_s)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: skynet_self_heal.py <detect|heal|report|run> [options]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "detect":
        stuck = detect_stuck_tasks()
        if stuck:
            print(f"Found {len(stuck)} stuck task(s):")
            for s in stuck:
                print(f"  {s['worker']}: {s['stuck_seconds']}s ({s['severity']})")
        else:
            print("No stuck tasks detected.")

    elif cmd == "heal":
        dry = "--dry-run" in sys.argv
        actions = auto_heal(dry_run=dry)
        if actions:
            for a in actions:
                print(f"  {a['worker']}: {a['action']} ({a.get('result', 'unknown')})")
        else:
            print("Nothing to heal.")

    elif cmd == "report":
        report = health_report()
        print(json.dumps(report, indent=2))

    elif cmd == "run":
        interval = 30.0
        for arg in sys.argv[2:]:
            if arg.startswith("--interval="):
                interval = float(arg.split("=")[1])
        run_continuous(interval_s=interval)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
