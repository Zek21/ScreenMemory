#!/usr/bin/env python3
"""
skynet_health_report.py -- Generate a one-page health report of the entire Skynet system.

Usage:
    python tools/skynet_health_report.py           # Print report
    python tools/skynet_health_report.py --json     # Output as JSON
    python tools/skynet_health_report.py --save     # Save to data/health_report.txt
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "chrome_bridge"))
DATA = ROOT / "data"


def http_get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def collect_report() -> dict:
    report = {"timestamp": datetime.now().isoformat(), "sections": {}}

    # Backend
    health = http_get("http://localhost:8420/health")
    metrics = http_get("http://localhost:8420/metrics")
    status = http_get("http://localhost:8420/status")
    report["sections"]["backend"] = {
        "status": "UP" if health else "DOWN",
        "uptime_s": health.get("uptime_s", 0) if health else 0,
        "uptime_h": round(health["uptime_s"] / 3600, 1) if health and health.get("uptime_s") else 0,
        "workers_alive": health.get("workers_alive", 0) if health else 0,
        "bus_depth": health.get("bus_depth", 0) if health else 0,
        "total_requests": metrics.get("total_requests", 0) if metrics else 0,
        "goroutines": metrics.get("goroutine_count", 0) if metrics else 0,
        "mem_mb": round(metrics.get("mem_alloc_mb", 0), 1) if metrics else 0,
    }

    # Workers
    agents = status.get("agents", {}) if status else {}
    worker_summary = {}
    for name, info in agents.items():
        worker_summary[name] = {
            "status": info.get("status", "UNKNOWN"),
            "model": info.get("model", "unknown"),
            "tasks": info.get("tasks_completed", 0),
            "errors": info.get("total_errors", 0),
            "last_hb": info.get("last_heartbeat", "N/A"),
        }
    report["sections"]["workers"] = worker_summary

    # GOD Console
    god_health = http_get("http://localhost:8421/health")
    god_engines = http_get("http://localhost:8421/engines")
    engine_summary = god_engines.get("summary", {}) if god_engines else {}
    report["sections"]["god_console"] = {
        "status": "UP" if god_health else "DOWN",
        "engines_online": engine_summary.get("online", 0),
        "engines_available": engine_summary.get("available", 0),
        "engines_offline": engine_summary.get("offline", 0),
        "engines_total": engine_summary.get("total", 0),
        "health_pct": engine_summary.get("health_pct", 0),
    }

    # IQ
    try:
        from tools.skynet_self import SkynetSelf
        s = SkynetSelf()
        iq = s.compute_iq()
        report["sections"]["iq"] = iq
    except Exception as e:
        report["sections"]["iq"] = {"score": 0, "trend": "unknown", "error": str(e)[:80]}

    # Collective Intelligence
    try:
        from tools.skynet_collective import intelligence_score
        ci = intelligence_score()
        report["sections"]["collective"] = {
            "score": ci.get("intelligence_score", 0),
            "components": ci.get("components", {}),
        }
    except Exception as e:
        report["sections"]["collective"] = {"score": 0, "error": str(e)[:80]}

    # Version
    try:
        from tools.skynet_version import current_version
        report["sections"]["version"] = current_version() or {}
    except Exception:
        report["sections"]["version"] = {}

    # Watchdog
    wdog = DATA / "watchdog_status.json"
    if wdog.exists():
        report["sections"]["watchdog"] = json.loads(wdog.read_text())
    else:
        report["sections"]["watchdog"] = {"status": "NOT_RUNNING"}

    # Task Queue
    tasks = http_get("http://localhost:8420/bus/tasks")
    if tasks is not None:
        task_list = tasks if isinstance(tasks, list) else []
        pending = sum(1 for t in task_list if t.get("status") == "pending")
        claimed = sum(1 for t in task_list if t.get("status") == "claimed")
        completed = sum(1 for t in task_list if t.get("status") == "completed")
        report["sections"]["task_queue"] = {
            "status": "UP",
            "total": len(task_list),
            "pending": pending,
            "claimed": claimed,
            "completed": completed,
        }
    else:
        report["sections"]["task_queue"] = {"status": "DOWN"}

    # Bus stress test (quick 10-message probe)
    import time
    stress_ok = 0
    stress_start = time.time()
    for i in range(10):
        try:
            data = json.dumps({"sender": "health_check", "topic": "health_probe", "type": "probe", "content": f"probe_{i}"}).encode()
            req = urllib.request.Request("http://localhost:8420/bus/publish", data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=3)
            stress_ok += 1
        except Exception:
            pass
    stress_elapsed = time.time() - stress_start
    report["sections"]["bus_stress"] = {
        "messages_sent": 10,
        "messages_ok": stress_ok,
        "elapsed_s": round(stress_elapsed, 3),
        "throughput_msg_s": round(stress_ok / stress_elapsed, 1) if stress_elapsed > 0 else 0,
        "status": "HEALTHY" if stress_ok == 10 else "DEGRADED",
    }

    # E2E test results
    e2e = DATA / "e2e_results.json"
    if e2e.exists():
        report["sections"]["e2e_tests"] = json.loads(e2e.read_text())
    else:
        report["sections"]["e2e_tests"] = {"status": "NOT_RUN"}

    return report


def format_report(report: dict) -> str:
    lines = []
    lines.append("=" * 55)
    lines.append("  SKYNET SYSTEM HEALTH REPORT")
    lines.append(f"  Generated: {report['timestamp']}")
    lines.append("=" * 55)

    # Backend
    b = report["sections"]["backend"]
    lines.append(f"\n── Backend ({'UP' if b['status'] == 'UP' else 'DOWN'}) ──")
    lines.append(f"  Uptime: {b['uptime_h']}h | Requests: {b['total_requests']} | Goroutines: {b['goroutines']} | Mem: {b['mem_mb']}MB")
    lines.append(f"  Workers alive: {b['workers_alive']} | Bus depth: {b['bus_depth']}")

    # Workers
    lines.append(f"\n── Workers ──")
    for name, w in report["sections"]["workers"].items():
        lines.append(f"  {name:13s} | {w['status']:10s} | model={w['model']} | tasks={w['tasks']} errors={w['errors']} | hb={w['last_hb']}")

    # GOD Console
    g = report["sections"]["god_console"]
    lines.append(f"\n── GOD Console ({g['status']}) ──")
    lines.append(f"  Engines: {g['engines_online']} online, {g['engines_available']} available, {g['engines_offline']} offline ({g['engines_total']} total, {g['health_pct']}%)")

    # IQ
    iq = report["sections"]["iq"]
    lines.append(f"\n── Intelligence ──")
    lines.append(f"  IQ Score: {iq.get('score', 0):.4f} | Trend: {iq.get('trend', 'unknown')}")

    # Collective
    ci = report["sections"]["collective"]
    comps = ci.get("components", {})
    comp_str = " | ".join(f"{k}={v:.3f}" for k, v in comps.items()) if comps else "N/A"
    lines.append(f"  Collective: {ci.get('score', 0):.3f} ({comp_str})")

    # Version
    v = report["sections"]["version"]
    lines.append(f"\n── Version ──")
    lines.append(f"  v{v.get('version', '?')} Level {v.get('level', '?')} ({v.get('timestamp', 'N/A')})")

    # Watchdog
    wd = report["sections"]["watchdog"]
    lines.append(f"\n── Watchdog ──")
    lines.append(f"  GOD Console: {wd.get('god_console', '?')} | Skynet: {wd.get('skynet', '?')} | Updated: {wd.get('updated', 'N/A')}")

    # Task Queue
    tq = report["sections"].get("task_queue", {})
    lines.append(f"\n── Task Queue ({tq.get('status', '?')}) ──")
    lines.append(f"  Total: {tq.get('total', 0)} | Pending: {tq.get('pending', 0)} | Claimed: {tq.get('claimed', 0)} | Completed: {tq.get('completed', 0)}")

    # Bus stress
    bs = report["sections"].get("bus_stress", {})
    lines.append(f"\n── Bus Stress Probe ──")
    lines.append(f"  {bs.get('messages_ok', 0)}/{bs.get('messages_sent', 0)} OK in {bs.get('elapsed_s', 0)}s | Throughput: {bs.get('throughput_msg_s', 0)} msg/s | {bs.get('status', '?')}")

    # E2E
    e2e = report["sections"]["e2e_tests"]
    lines.append(f"\n── E2E Tests ──")
    lines.append(f"  {e2e.get('passed', 0)} passed, {e2e.get('failed', 0)} failed ({e2e.get('status', 'NOT_RUN')})")

    lines.append(f"\n{'=' * 55}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Skynet Health Report")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--save", action="store_true", help="Save to data/health_report.txt")
    args = parser.parse_args()

    report = collect_report()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        text = format_report(report)
        print(text)
        if args.save:
            DATA.mkdir(exist_ok=True)
            (DATA / "health_report.txt").write_text(text, encoding="utf-8")
            print(f"\nSaved to data/health_report.txt")


if __name__ == "__main__":
    main()
