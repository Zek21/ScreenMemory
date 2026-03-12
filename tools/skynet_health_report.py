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


def _collect_core_services():
    """Collect backend status and worker summary from Skynet backend."""
    health = http_get("http://localhost:8420/health")
    metrics = http_get("http://localhost:8420/metrics")
    status = http_get("http://localhost:8420/status")
    backend = {
        "status": "UP" if health else "DOWN",
        "uptime_s": health.get("uptime_s", 0) if health else 0,
        "uptime_h": round(health["uptime_s"] / 3600, 1) if health and health.get("uptime_s") else 0,
        "workers_alive": health.get("workers_alive", 0) if health else 0,
        "bus_depth": health.get("bus_depth", 0) if health else 0,
        "total_requests": metrics.get("total_requests", 0) if metrics else 0,
        "goroutines": metrics.get("goroutine_count", 0) if metrics else 0,
        "mem_mb": round(metrics.get("mem_alloc_mb", 0), 1) if metrics else 0,
    }
    agents = status.get("agents", {}) if status else {}
    workers = {}
    for name, info in agents.items():
        workers[name] = {
            "status": info.get("status", "UNKNOWN"),
            "model": info.get("model", "unknown"),
            "tasks": info.get("tasks_completed", 0),
            "errors": info.get("total_errors", 0),
            "last_hb": info.get("last_heartbeat", "N/A"),
        }
    return backend, workers


def _collect_engines_and_intel():
    """Collect GOD Console engine status, IQ score, and collective intelligence."""
    god_health = http_get("http://localhost:8421/health")
    god_engines = http_get("http://localhost:8421/engines")
    engine_summary = god_engines.get("summary", {}) if god_engines else {}
    god_console = {
        "status": "UP" if god_health else "DOWN",
        "engines_online": engine_summary.get("online", 0),
        "engines_available": engine_summary.get("available", 0),
        "engines_offline": engine_summary.get("offline", 0),
        "engines_total": engine_summary.get("total", 0),
        "health_pct": engine_summary.get("health_pct", 0),
    }
    try:
        from tools.skynet_self import SkynetSelf
        iq = SkynetSelf().compute_iq()
    except Exception as e:
        iq = {"score": 0, "trend": "unknown", "error": str(e)[:80]}
    try:
        from tools.skynet_collective import intelligence_score
        ci = intelligence_score()
        collective = {"score": ci.get("intelligence_score", 0), "components": ci.get("components", {})}
    except Exception as e:
        collective = {"score": 0, "error": str(e)[:80]}
    return god_console, iq, collective


def _collect_ops_status():
    """Collect version, watchdog, task queue, and e2e test results."""
    try:
        from tools.skynet_version import current_version
        version = current_version() or {}
    except Exception:
        version = {}
    wdog = DATA / "watchdog_status.json"
    watchdog = json.loads(wdog.read_text()) if wdog.exists() else {"status": "NOT_RUNNING"}
    tasks = http_get("http://localhost:8420/bus/tasks")
    if tasks is not None:
        task_list = tasks if isinstance(tasks, list) else []
        pending = sum(1 for t in task_list if t.get("status") == "pending")
        claimed = sum(1 for t in task_list if t.get("status") == "claimed")
        completed = sum(1 for t in task_list if t.get("status") == "completed")
        task_queue = {"status": "UP", "total": len(task_list), "pending": pending,
                      "claimed": claimed, "completed": completed}
    else:
        task_queue = {"status": "DOWN"}
    e2e = DATA / "e2e_results.json"
    e2e_tests = json.loads(e2e.read_text()) if e2e.exists() else {"status": "NOT_RUN"}
    return version, watchdog, task_queue, e2e_tests


def _collect_bus_stress():
    """Run a 10-message bus probe and measure throughput."""
    import time
    stress_ok = 0
    stress_start = time.time()
    try:
        from tools.skynet_spam_guard import guarded_publish
    except Exception:
        guarded_publish = None
    probe_count = 5  # within SpamGuard rate limit (5/min/sender)
    for i in range(probe_count):
        try:
            payload = {"sender": "health_check", "topic": "health_probe",
                       "type": "probe", "content": f"probe_{i}_{int(time.time())}"}
            if guarded_publish:
                guarded_publish(payload)
            else:
                # Raw fallback only when SpamGuard is unavailable
                data = json.dumps(payload).encode()
                req = urllib.request.Request("http://localhost:8420/bus/publish", data=data,
                                            headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=3)
            stress_ok += 1
        except Exception:
            pass
    stress_elapsed = time.time() - stress_start
    # signed: alpha
    return {
        "messages_sent": probe_count, "messages_ok": stress_ok,
        "elapsed_s": round(stress_elapsed, 3),
        "throughput_msg_s": round(stress_ok / stress_elapsed, 1) if stress_elapsed > 0 else 0,
        "status": "HEALTHY" if stress_ok == probe_count else "DEGRADED",
    }


def collect_report() -> dict:
    report = {"timestamp": datetime.now().isoformat(), "sections": {}}
    backend, workers = _collect_core_services()
    god_console, iq, collective = _collect_engines_and_intel()
    version, watchdog, task_queue, e2e_tests = _collect_ops_status()
    report["sections"].update({
        "backend": backend, "workers": workers,
        "god_console": god_console, "iq": iq, "collective": collective,
        "version": version, "watchdog": watchdog,
        "task_queue": task_queue, "bus_stress": _collect_bus_stress(),
        "e2e_tests": e2e_tests,
    })
    return report


def _format_core_sections(sections):
    """Format backend, workers, and GOD Console sections."""
    lines = []
    b = sections["backend"]
    lines.append(f"\n── Backend ({'UP' if b['status'] == 'UP' else 'DOWN'}) ──")
    lines.append(f"  Uptime: {b['uptime_h']}h | Requests: {b['total_requests']} | Goroutines: {b['goroutines']} | Mem: {b['mem_mb']}MB")
    lines.append(f"  Workers alive: {b['workers_alive']} | Bus depth: {b['bus_depth']}")
    lines.append("\n── Workers ──")
    for name, w in sections["workers"].items():
        lines.append(f"  {name:13s} | {w['status']:10s} | model={w['model']} | tasks={w['tasks']} errors={w['errors']} | hb={w['last_hb']}")
    g = sections["god_console"]
    lines.append(f"\n── GOD Console ({g['status']}) ──")
    lines.append(f"  Engines: {g['engines_online']} online, {g['engines_available']} available, {g['engines_offline']} offline ({g['engines_total']} total, {g['health_pct']}%)")
    return lines


def _format_intel_and_ops(sections):
    """Format intelligence, version, infrastructure, and test sections."""
    lines = []
    iq = sections["iq"]
    lines.append("\n── Intelligence ──")
    lines.append(f"  IQ Score: {iq.get('score', 0):.4f} | Trend: {iq.get('trend', 'unknown')}")
    ci = sections["collective"]
    comps = ci.get("components", {})
    comp_str = " | ".join(f"{k}={v:.3f}" for k, v in comps.items()) if comps else "N/A"
    lines.append(f"  Collective: {ci.get('score', 0):.3f} ({comp_str})")
    v = sections["version"]
    lines.append("\n── Version ──")
    lines.append(f"  v{v.get('version', '?')} Level {v.get('level', '?')} ({v.get('timestamp', 'N/A')})")
    wd = sections["watchdog"]
    lines.append("\n── Watchdog ──")
    lines.append(f"  GOD Console: {wd.get('god_console', '?')} | Skynet: {wd.get('skynet', '?')} | Updated: {wd.get('updated', 'N/A')}")
    tq = sections.get("task_queue", {})
    lines.append(f"\n── Task Queue ({tq.get('status', '?')}) ──")
    lines.append(f"  Total: {tq.get('total', 0)} | Pending: {tq.get('pending', 0)} | Claimed: {tq.get('claimed', 0)} | Completed: {tq.get('completed', 0)}")
    bs = sections.get("bus_stress", {})
    lines.append("\n── Bus Stress Probe ──")
    lines.append(f"  {bs.get('messages_ok', 0)}/{bs.get('messages_sent', 0)} OK in {bs.get('elapsed_s', 0)}s | Throughput: {bs.get('throughput_msg_s', 0)} msg/s | {bs.get('status', '?')}")
    e2e = sections["e2e_tests"]
    lines.append("\n── E2E Tests ──")
    lines.append(f"  {e2e.get('passed', 0)} passed, {e2e.get('failed', 0)} failed ({e2e.get('status', 'NOT_RUN')})")
    return lines


def format_report(report: dict) -> str:
    lines = [
        "=" * 55,
        "  SKYNET SYSTEM HEALTH REPORT",
        f"  Generated: {report['timestamp']}",
        "=" * 55,
    ]
    sections = report["sections"]
    lines.extend(_format_core_sections(sections))
    lines.extend(_format_intel_and_ops(sections))
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
