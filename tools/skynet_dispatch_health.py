#!/usr/bin/env python3
"""Dispatch pipeline health monitor -- analyzes dispatch_log.json for reliability metrics.

Reads dispatch history and computes success rate, failure modes, clipboard failure rate,
focus-stolen rate, delivery verification rate, and average dispatch time. Generates
data/dispatch_health.json with metrics. Alerts on bus if success rate drops below 90%.

Usage:
    python tools/skynet_dispatch_health.py              # Human-readable report
    python tools/skynet_dispatch_health.py --json       # JSON output
    python tools/skynet_dispatch_health.py --since 24   # Only last 24 hours
    python tools/skynet_dispatch_health.py --alert      # Post alert if success < 90%

# signed: beta
"""

import json
import sys
import os
import argparse
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DISPATCH_LOG = DATA_DIR / "dispatch_log.json"
HEALTH_FILE = DATA_DIR / "dispatch_health.json"

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass


def load_dispatch_log(since_hours=None):
    """Load dispatch log entries, optionally filtered by time window."""  # signed: beta
    if not DISPATCH_LOG.exists():
        return []
    try:
        data = json.loads(DISPATCH_LOG.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []

    if since_hours is not None:
        cutoff = datetime.now() - timedelta(hours=since_hours)
        filtered = []
        for entry in data:
            try:
                ts = datetime.fromisoformat(entry.get("timestamp", ""))
                if ts >= cutoff:
                    filtered.append(entry)
            except (ValueError, TypeError):
                pass
        return filtered
    return data


def compute_metrics(entries):
    """Compute dispatch health metrics from log entries."""  # signed: beta
    if not entries:
        return {
            "total_dispatches": 0,
            "success_count": 0,
            "failure_count": 0,
            "success_rate": 0.0,
            "per_worker": {},
            "failure_modes": {},
            "clipboard_failures": 0,
            "clipboard_failure_rate": 0.0,
            "focus_stolen_count": 0,
            "focus_stolen_rate": 0.0,
            "delivery_verified_count": 0,
            "delivery_unverified_count": 0,
            "delivery_verification_rate": 0.0,
            "results_received": 0,
            "result_rate": 0.0,
            "avg_dispatch_time_ms": 0.0,
            "timestamp": datetime.now().isoformat(),
            "window_hours": 0,
        }

    total = len(entries)
    successes = sum(1 for e in entries if e.get("success"))
    failures = total - successes

    # Per-worker breakdown
    per_worker = {}
    for e in entries:
        w = e.get("worker", "unknown")
        if w not in per_worker:
            per_worker[w] = {"total": 0, "success": 0, "failure": 0, "results": 0}
        per_worker[w]["total"] += 1
        if e.get("success"):
            per_worker[w]["success"] += 1
        else:
            per_worker[w]["failure"] += 1
        if e.get("result_received"):
            per_worker[w]["results"] += 1

    # Failure mode analysis
    failure_modes = {}
    clipboard_failures = 0
    focus_stolen = 0
    for e in entries:
        ds = e.get("delivery_status", "")
        if not e.get("success"):
            state = e.get("state_at_dispatch", "UNKNOWN")
            mode = ds if ds else f"FAILED_{state}"
            failure_modes[mode] = failure_modes.get(mode, 0) + 1
        if ds in ("CLIPBOARD_VERIFY_FAILED", "CLIPBOARD_TAMPERED"):
            clipboard_failures += 1
        if ds == "FOCUS_STOLEN":
            focus_stolen += 1

    # Delivery verification tracking
    verified = sum(1 for e in entries if e.get("success") and
                   e.get("delivery_status", "").startswith("OK_"))
    unverified = successes - verified
    results_received = sum(1 for e in entries if e.get("result_received"))

    # Time window
    try:
        timestamps = []
        for e in entries:
            try:
                timestamps.append(datetime.fromisoformat(e.get("timestamp", "")))
            except (ValueError, TypeError):
                pass
        if len(timestamps) >= 2:
            window_hours = (max(timestamps) - min(timestamps)).total_seconds() / 3600
        else:
            window_hours = 0
    except Exception:
        window_hours = 0

    return {
        "total_dispatches": total,
        "success_count": successes,
        "failure_count": failures,
        "success_rate": round(successes / total * 100, 1) if total > 0 else 0.0,
        "per_worker": per_worker,
        "failure_modes": failure_modes,
        "clipboard_failures": clipboard_failures,
        "clipboard_failure_rate": round(clipboard_failures / total * 100, 1) if total > 0 else 0.0,
        "focus_stolen_count": focus_stolen,
        "focus_stolen_rate": round(focus_stolen / total * 100, 1) if total > 0 else 0.0,
        "delivery_verified_count": verified,
        "delivery_unverified_count": unverified,
        "delivery_verification_rate": round(verified / successes * 100, 1) if successes > 0 else 0.0,
        "results_received": results_received,
        "result_rate": round(results_received / total * 100, 1) if total > 0 else 0.0,
        "timestamp": datetime.now().isoformat(),
        "window_hours": round(window_hours, 1),
    }


def save_health(metrics_data):
    """Write metrics to data/dispatch_health.json."""  # signed: beta
    try:
        HEALTH_FILE.write_text(
            json.dumps(metrics_data, indent=2, default=str),
            encoding="utf-8"
        )
    except OSError as e:
        print(f"[dispatch_health] Failed to write {HEALTH_FILE}: {e}", file=sys.stderr)


def alert_if_degraded(metrics_data, threshold=90.0):
    """Post bus alert if success rate drops below threshold."""  # signed: beta
    rate = metrics_data.get("success_rate", 100.0)
    total = metrics_data.get("total_dispatches", 0)
    if total < 5:
        return  # Not enough data to alert
    if rate < threshold:
        try:
            from tools.skynet_spam_guard import guarded_publish
            guarded_publish({
                "sender": "dispatch_health",
                "topic": "orchestrator",
                "type": "alert",
                "content": (
                    f"DISPATCH_HEALTH_DEGRADED: success rate {rate}% "
                    f"(below {threshold}% threshold). "
                    f"{metrics_data['failure_count']}/{total} dispatches failed. "
                    f"Clipboard failures: {metrics_data['clipboard_failures']}, "
                    f"Focus stolen: {metrics_data['focus_stolen_count']}. "
                    f"signed:beta"
                ),
            })
            print(f"[ALERT] Bus alert posted: success rate {rate}% < {threshold}%")
        except Exception as e:
            print(f"[dispatch_health] Alert failed: {e}", file=sys.stderr)


def print_report(metrics_data):
    """Print human-readable health report."""  # signed: beta
    m = metrics_data
    print("=" * 60)
    print("  DISPATCH PIPELINE HEALTH REPORT")
    print("=" * 60)
    print(f"  Time window:     {m['window_hours']} hours")
    print(f"  Total dispatches: {m['total_dispatches']}")
    print(f"  Success rate:    {m['success_rate']}% ({m['success_count']}/{m['total_dispatches']})")
    print(f"  Failures:        {m['failure_count']}")
    print()

    print("  RELIABILITY METRICS:")
    print(f"    Clipboard failures:    {m['clipboard_failures']} ({m['clipboard_failure_rate']}%)")
    print(f"    Focus stolen:          {m['focus_stolen_count']} ({m['focus_stolen_rate']}%)")
    print(f"    Delivery verified:     {m['delivery_verified_count']} ({m['delivery_verification_rate']}%)")
    print(f"    Delivery unverified:   {m['delivery_unverified_count']}")
    print(f"    Results received:      {m['results_received']} ({m['result_rate']}%)")
    print()

    if m["per_worker"]:
        print("  PER-WORKER BREAKDOWN:")
        for w, stats in sorted(m["per_worker"].items()):
            rate = round(stats["success"] / stats["total"] * 100, 1) if stats["total"] > 0 else 0
            print(f"    {w:20s}  {stats['success']}/{stats['total']} ({rate}%)  results={stats['results']}")
        print()

    if m["failure_modes"]:
        print("  FAILURE MODES:")
        for mode, count in sorted(m["failure_modes"].items(), key=lambda x: -x[1]):
            print(f"    {mode:30s}  {count}")
        print()

    # Health assessment
    rate = m["success_rate"]
    if rate >= 95:
        status = "HEALTHY"
    elif rate >= 90:
        status = "DEGRADED"
    elif rate >= 75:
        status = "WARNING"
    else:
        status = "CRITICAL"
    print(f"  STATUS: {status}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Dispatch pipeline health monitor")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")
    parser.add_argument("--since", type=float, default=None, help="Only analyze last N hours")
    parser.add_argument("--alert", action="store_true", help="Post bus alert if success rate < 90%%")
    parser.add_argument("--threshold", type=float, default=90.0, help="Alert threshold (default: 90%%)")
    args = parser.parse_args()

    entries = load_dispatch_log(since_hours=args.since)
    metrics_data = compute_metrics(entries)
    save_health(metrics_data)

    if args.json:
        print(json.dumps(metrics_data, indent=2, default=str))
    else:
        print_report(metrics_data)

    if args.alert:
        alert_if_degraded(metrics_data, threshold=args.threshold)

    return 0


if __name__ == "__main__":
    sys.exit(main())
