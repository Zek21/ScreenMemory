#!/usr/bin/env python3
"""
Skynet Research Metrics — Automated performance data collection for publication.

Collects and persists UIA scan times, dispatch latencies, bus results, worker health,
model guard checks, steering events, benchmarks, and end-to-end task metrics.

Usage:
    python tools/skynet_metrics.py --summary              # Generate research summary
    python tools/skynet_metrics.py --run-benchmarks       # Run and record all benchmarks
    python tools/skynet_metrics.py --export-csv out.csv   # Export metrics to CSV
"""

import json
import sys
import time
import os
import statistics
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

METRICS_DIR = ROOT / "data" / "metrics"


class SkynetMetrics:
    def __init__(self):
        METRICS_DIR.mkdir(parents=True, exist_ok=True)
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_file = METRICS_DIR / f"session_{self.session_id}.jsonl"
        self.summary_file = METRICS_DIR / "research_summary.json"

    def record(self, category, event, data):
        """Append a metric event as JSONL line."""
        entry = {
            "ts": datetime.now().isoformat(),
            "session": self.session_id,
            "category": category,
            "event": event,
            **data,
        }
        with open(self.session_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def record_uia_scan(self, results_dict, total_ms, mode="sequential"):
        """Record a UIA scan benchmark."""
        per_window = {name: r.get("scan_ms", 0) for name, r in results_dict.items()}
        self.record("uia", "scan", {
            "mode": mode,
            "total_ms": total_ms,
            "window_count": len(results_dict),
            "per_window_ms": per_window,
            "avg_ms": statistics.mean(per_window.values()) if per_window else 0,
        })

    def record_dispatch(self, worker, task_preview, success, duration_ms, mode="single"):
        """Record a dispatch event."""
        self.record("dispatch", "send", {
            "worker": worker,
            "task": task_preview[:100],
            "success": success,
            "duration_ms": duration_ms,
            "mode": mode,
        })

    def record_bus_result(self, sender, content_preview, latency_ms):
        """Record a bus result received."""
        self.record("bus", "result", {
            "sender": sender,
            "content": content_preview[:150],
            "latency_ms": latency_ms,
        })

    def record_worker_health(self, worker_states):
        """Record worker health snapshot."""
        self.record("health", "snapshot", {"states": worker_states})

    def record_model_guard(self, target, model_str, agent_str, correct, fixed=False):
        """Record model guard check."""
        self.record("security", "model_guard", {
            "target": target,
            "model": model_str,
            "agent": agent_str,
            "correct": correct,
            "fixed": fixed,
        })

    def record_steering(self, worker, detected, cancelled, method="com_uia"):
        """Record steering detection/cancellation."""
        self.record("steering", "event", {
            "worker": worker,
            "detected": detected,
            "cancelled": cancelled,
            "method": method,
        })

    def record_benchmark(self, name, iterations, times_ms, metadata=None):
        """Record a named benchmark."""
        self.record("benchmark", name, {
            "iterations": iterations,
            "times_ms": times_ms,
            "mean_ms": statistics.mean(times_ms),
            "median_ms": statistics.median(times_ms),
            "stdev_ms": statistics.stdev(times_ms) if len(times_ms) > 1 else 0,
            "min_ms": min(times_ms),
            "max_ms": max(times_ms),
            "p95_ms": sorted(times_ms)[int(len(times_ms) * 0.95)] if len(times_ms) >= 20 else max(times_ms),
            "metadata": metadata or {},
        })

    def record_e2e_task(self, task_id, workers_used, total_ms, subtasks_completed, subtasks_failed):
        """Record end-to-end orchestrated task."""
        total = subtasks_completed + subtasks_failed
        self.record("e2e", "task", {
            "task_id": task_id,
            "workers_used": workers_used,
            "total_ms": total_ms,
            "completed": subtasks_completed,
            "failed": subtasks_failed,
            "success_rate": subtasks_completed / total if total > 0 else 0,
        })

    def record_dispatch_latency(self, worker, task_preview, dispatch_ms, result_ms, success):
        """Record dispatch timing breakdown: dispatch vs result latency."""
        self.record("dispatch_latency", "timing", {
            "worker": worker,
            "task": task_preview[:100],
            "dispatch_ms": dispatch_ms,
            "result_ms": result_ms,
            "total_ms": dispatch_ms + result_ms,
            "success": success,
        })

    def record_worker_utilization(self, states_dict):
        """Record worker state snapshot for utilization tracking.

        states_dict: {"alpha": "IDLE", "beta": "PROCESSING", ...}
        """
        idle = sum(1 for s in states_dict.values() if s == "IDLE")
        busy = sum(1 for s in states_dict.values() if s in ("PROCESSING", "TYPING"))
        steering = sum(1 for s in states_dict.values() if s == "STEERING")
        total = len(states_dict)
        self.record("utilization", "snapshot", {
            "states": states_dict,
            "idle": idle,
            "busy": busy,
            "steering": steering,
            "total": total,
            "utilization_pct": round(busy / total * 100, 2) if total > 0 else 0,
        })

    def record_bus_roundtrip(self, sender, topic, publish_ms, poll_ms):
        """Record bus publish+poll roundtrip latency."""
        self.record("bus_latency", "roundtrip", {
            "sender": sender,
            "topic": topic,
            "publish_ms": publish_ms,
            "poll_ms": poll_ms,
            "total_ms": publish_ms + poll_ms,
        })

    @staticmethod
    def _stats(values):
        """Compute publication-quality descriptive statistics for a list of numbers."""
        if not values:
            return {"count": 0, "mean": 0, "median": 0, "stdev": 0, "min": 0,
                    "max": 0, "p95": 0, "p99": 0, "cv": 0}
        n = len(values)
        s = sorted(values)
        mean = round(statistics.mean(values), 2)
        med = round(statistics.median(values), 2)
        sd = round(statistics.stdev(values), 2) if n > 1 else 0.0
        cv = round(sd / mean * 100, 2) if mean else 0.0
        return {
            "count": n,
            "mean": mean,
            "median": med,
            "stdev": sd,
            "min": round(s[0], 2),
            "max": round(s[-1], 2),
            "p95": round(s[int(n * 0.95)] if n >= 20 else s[-1], 2),
            "p99": round(s[int(n * 0.99)] if n >= 100 else s[-1], 2),
            "cv": cv,
        }

    def generate_summary(self):
        """Read all JSONL files and generate research_summary.json with publication-quality stats."""
        all_events = []
        for f in METRICS_DIR.glob("session_*.jsonl"):
            for line in open(f):
                try:
                    all_events.append(json.loads(line))
                except Exception:
                    pass

        # --- Timeline ---
        timestamps = [e.get("ts", "") for e in all_events if e.get("ts")]
        timestamps.sort()
        first_ts = timestamps[0] if timestamps else None
        last_ts = timestamps[-1] if timestamps else None
        if first_ts and last_ts:
            try:
                t0 = datetime.fromisoformat(first_ts)
                t1 = datetime.fromisoformat(last_ts)
                duration_s = round((t1 - t0).total_seconds(), 2)
            except Exception:
                duration_s = 0
        else:
            duration_s = 0

        # --- System info ---
        try:
            import comtypes
            comtypes_ver = comtypes.__version__
        except Exception:
            comtypes_ver = "unavailable"
        import platform
        worker_count = 0
        try:
            wf = ROOT / "data" / "workers.json"
            if wf.exists():
                worker_count = len(json.loads(wf.read_text()).get("workers", []))
        except Exception:
            pass

        summary = {
            "generated_at": datetime.now().isoformat(),
            "total_events": len(all_events),
            "system_info": {
                "python_version": platform.python_version(),
                "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
                "comtypes_version": comtypes_ver,
                "worker_count": worker_count,
            },
            "timeline": {
                "first_event": first_ts,
                "last_event": last_ts,
                "total_duration_s": duration_s,
            },
            "categories": {},
        }
        for e in all_events:
            cat = e.get("category", "unknown")
            if cat not in summary["categories"]:
                summary["categories"][cat] = {"count": 0}
            summary["categories"][cat]["count"] += 1

        # --- UIA benchmarks (full descriptive stats) ---
        uia_scans = [e for e in all_events if e.get("category") == "uia"]
        if uia_scans:
            seq = [e for e in uia_scans if e.get("mode") == "sequential"]
            par = [e for e in uia_scans if e.get("mode") == "parallel"]
            seq_times = [e["total_ms"] for e in seq if "total_ms" in e]
            par_times = [e["total_ms"] for e in par if "total_ms" in e]
            summary["uia_performance"] = {
                "total_scans": len(uia_scans),
                "sequential": self._stats(seq_times),
                "parallel": self._stats(par_times),
            }

        # --- Benchmarks (named, with full stats) ---
        benchmarks = [e for e in all_events if e.get("category") == "benchmark"]
        if benchmarks:
            by_name = {}
            for b in benchmarks:
                name = b.get("event", "unknown")
                times = b.get("times_ms", [])
                if name not in by_name:
                    by_name[name] = []
                by_name[name].extend(times)
            summary["benchmarks"] = {
                name: self._stats(times) for name, times in by_name.items()
            }

        # --- Comparisons / speedup ratios ---
        uia_perf = summary.get("uia_performance", {})
        seq_mean = uia_perf.get("sequential", {}).get("mean", 0)
        par_mean = uia_perf.get("parallel", {}).get("mean", 0)
        bench = summary.get("benchmarks", {})
        seq_bench_mean = bench.get("uia_sequential_5win", {}).get("mean", 0)
        par_bench_mean = bench.get("uia_parallel_5win", {}).get("mean", 0)

        comparisons = {}
        if seq_mean and par_mean:
            comparisons["parallel_vs_sequential"] = {
                "speedup": round(seq_mean / par_mean, 2),
                "reduction_pct": round((1 - par_mean / seq_mean) * 100, 2),
            }
        if seq_bench_mean and par_bench_mean:
            comparisons["parallel_vs_sequential_bench"] = {
                "speedup": round(seq_bench_mean / par_bench_mean, 2),
                "reduction_pct": round((1 - par_bench_mean / seq_bench_mean) * 100, 2),
            }
        # COM vs PowerShell: check for powershell benchmarks
        ps_bench = bench.get("powershell_scan", {})
        com_bench = bench.get("uia_single_window", {})
        if ps_bench.get("mean") and com_bench.get("mean"):
            comparisons["com_vs_powershell"] = {
                "speedup": round(ps_bench["mean"] / com_bench["mean"], 2),
                "reduction_pct": round((1 - com_bench["mean"] / ps_bench["mean"]) * 100, 2),
            }
        if comparisons:
            summary["comparisons"] = comparisons

        # --- Dispatch stats ---
        dispatches = [e for e in all_events if e.get("category") == "dispatch"]
        if dispatches:
            successes = sum(1 for d in dispatches if d.get("success"))
            dur_times = [d.get("duration_ms", 0) for d in dispatches]
            summary["dispatch"] = {
                "total": len(dispatches),
                "success": successes,
                "failed": len(dispatches) - successes,
                "success_rate": round(successes / len(dispatches), 4),
                "duration": self._stats(dur_times),
            }

        # --- Security / model guard ---
        guards = [e for e in all_events if e.get("category") == "security"]
        if guards:
            drifts = [g for g in guards if not g.get("correct")]
            fixes = sum(1 for g in guards if g.get("fixed"))
            drift_timeline = [
                {"ts": g.get("ts"), "target": g.get("target", ""),
                 "model": g.get("model", ""), "fixed": g.get("fixed", False)}
                for g in drifts
            ]
            summary["model_guard"] = {
                "total_checks": len(guards),
                "drifts_detected": len(drifts),
                "auto_fixed": fixes,
                "events": drift_timeline,
            }

        # --- E2E tasks ---
        e2es = [e for e in all_events if e.get("category") == "e2e"]
        if e2es:
            dur_times = [e.get("total_ms", 0) for e in e2es]
            rates = [e.get("success_rate", 0) for e in e2es]
            worker_counts = [len(e.get("workers_used", [])) for e in e2es]
            task_list = [
                {"id": e.get("task_id", ""), "name": e.get("task_id", ""),
                 "duration_ms": round(e.get("total_ms", 0), 2),
                 "workers": len(e.get("workers_used", [])),
                 "success_rate": round(e.get("success_rate", 0), 4)}
                for e in e2es
            ]
            summary["e2e_tasks"] = task_list
            summary["e2e_performance"] = {
                "total": len(e2es),
                "duration": self._stats(dur_times),
                "avg_workers": round(statistics.mean(worker_counts), 2) if worker_counts else 0,
                "avg_success_rate": round(statistics.mean(rates), 4) if rates else 0,
            }

        # --- Worker utilization ---
        worker_tasks = {}
        for d in dispatches:
            w = d.get("worker", "unknown")
            worker_tasks[w] = worker_tasks.get(w, 0) + 1
        if worker_tasks:
            summary["worker_utilization"] = {
                w: {"tasks": c} for w, c in sorted(worker_tasks.items())
            }

        # --- Dispatch latency (timing breakdown) ---
        dispatch_lat = [e for e in all_events if e.get("category") == "dispatch_latency"]
        if dispatch_lat:
            d_ms = [e.get("dispatch_ms", 0) for e in dispatch_lat]
            r_ms = [e.get("result_ms", 0) for e in dispatch_lat]
            t_ms = [e.get("total_ms", 0) for e in dispatch_lat]
            successes = sum(1 for e in dispatch_lat if e.get("success"))
            summary["dispatch_latency"] = {
                "total": len(dispatch_lat),
                "success": successes,
                "success_rate": round(successes / len(dispatch_lat), 4) if dispatch_lat else 0,
                "dispatch_ms": self._stats(d_ms),
                "result_ms": self._stats(r_ms),
                "total_ms": self._stats(t_ms),
            }

        # --- Worker utilization snapshots ---
        util_snaps = [e for e in all_events if e.get("category") == "utilization"]
        if util_snaps:
            util_pcts = [e.get("utilization_pct", 0) for e in util_snaps]
            idle_counts = [e.get("idle", 0) for e in util_snaps]
            busy_counts = [e.get("busy", 0) for e in util_snaps]
            steering_counts = [e.get("steering", 0) for e in util_snaps]
            summary["utilization_snapshots"] = {
                "total_snapshots": len(util_snaps),
                "utilization_pct": self._stats(util_pcts),
                "avg_idle": round(statistics.mean(idle_counts), 2) if idle_counts else 0,
                "avg_busy": round(statistics.mean(busy_counts), 2) if busy_counts else 0,
                "avg_steering": round(statistics.mean(steering_counts), 2) if steering_counts else 0,
            }

        # --- Bus roundtrip latency ---
        bus_rt = [e for e in all_events if e.get("category") == "bus_latency"]
        if bus_rt:
            pub_ms = [e.get("publish_ms", 0) for e in bus_rt]
            poll_ms = [e.get("poll_ms", 0) for e in bus_rt]
            total_ms = [e.get("total_ms", 0) for e in bus_rt]
            summary["bus_latency"] = {
                "total_roundtrips": len(bus_rt),
                "publish_ms": self._stats(pub_ms),
                "poll_ms": self._stats(poll_ms),
                "total_ms": self._stats(total_ms),
            }

        with open(self.summary_file, "w") as f:
            json.dump(summary, f, indent=2)
        return summary


    def collect_live_snapshot(self):
        """Scan all worker windows via UIA, poll Skynet bus, record metrics, return snapshot."""
        import urllib.request
        from tools.uia_engine import get_engine

        engine = get_engine()
        workers_file = ROOT / "data" / "workers.json"
        if not workers_file.exists():
            return {"error": "data/workers.json not found"}

        data = json.loads(workers_file.read_text())
        workers = data.get("workers", [])
        orch_hwnd = data.get("orchestrator_hwnd", 0)

        snapshot = {"ts": datetime.now().isoformat(), "workers": {}, "orchestrator": None, "bus": [], "skynet": None}

        # Scan each worker via UIA
        worker_states = {}
        for w in workers:
            name, hwnd = w["name"], w["hwnd"]
            try:
                r = engine.scan(hwnd)
                worker_states[name] = r.model if hasattr(r, "model") else "?"
                snapshot["workers"][name] = {
                    "hwnd": hwnd, "model": getattr(r, "model", ""),
                    "agent": getattr(r, "agent", ""), "state": getattr(r, "state", ""),
                    "cancel_visible": getattr(r, "cancel_visible", False),
                    "scan_ms": getattr(r, "scan_ms", 0),
                }
            except Exception as e:
                worker_states[name] = "ERROR"
                snapshot["workers"][name] = {"hwnd": hwnd, "error": str(e)}

        # Scan orchestrator
        if orch_hwnd:
            try:
                r = engine.scan(orch_hwnd)
                snapshot["orchestrator"] = {
                    "hwnd": orch_hwnd, "model": getattr(r, "model", ""),
                    "agent": getattr(r, "agent", ""), "state": getattr(r, "state", ""),
                }
            except Exception as e:
                snapshot["orchestrator"] = {"hwnd": orch_hwnd, "error": str(e)}

        # Record worker health
        self.record_worker_health(worker_states)

        # Poll Skynet status + bus
        try:
            with urllib.request.urlopen("http://localhost:8420/health", timeout=3) as resp:
                snapshot["skynet"] = json.loads(resp.read())
        except Exception:
            snapshot["skynet"] = {"error": "unreachable"}

        try:
            with urllib.request.urlopen("http://localhost:8420/bus/messages?limit=10", timeout=3) as resp:
                snapshot["bus"] = json.loads(resp.read())
        except Exception:
            snapshot["bus"] = []

        # Record utilization snapshot
        self.record("utilization", "snapshot", {
            "worker_count": len(workers),
            "states": worker_states,
            "skynet_up": snapshot["skynet"] is not None and "error" not in snapshot.get("skynet", {}),
        })

        return snapshot


# ─── CSV Export ───────────────────────────────────────────────────────────────

# Column mappings per event category for clean CSV output
_CSV_COLUMNS = {
    "uia":       ["ts", "session", "category", "event", "mode", "total_ms", "window_count", "avg_ms"],
    "dispatch":  ["ts", "session", "category", "event", "worker", "task", "success", "duration_ms", "mode"],
    "bus":       ["ts", "session", "category", "event", "sender", "content", "latency_ms"],
    "health":    ["ts", "session", "category", "event", "states"],
    "security":  ["ts", "session", "category", "event", "target", "model", "agent", "correct", "fixed"],
    "steering":  ["ts", "session", "category", "event", "worker", "detected", "cancelled", "method"],
    "benchmark": ["ts", "session", "category", "event", "iterations", "mean_ms", "median_ms", "stdev_ms", "min_ms", "max_ms", "p95_ms"],
    "e2e":       ["ts", "session", "category", "event", "task_id", "workers_used", "total_ms", "completed", "failed", "success_rate"],
    "utilization": ["ts", "session", "category", "event", "worker_count", "states", "skynet_up"],
}

_BASE_COLUMNS = ["ts", "session", "category", "event"]


def export_csv(output_path):
    """Export all JSONL metrics to CSV with proper per-category column mapping."""
    import csv

    events = []
    for f in METRICS_DIR.glob("session_*.jsonl"):
        for line in open(f):
            try:
                events.append(json.loads(line))
            except Exception:
                pass

    if not events:
        print("No events found")
        return

    # Collect all columns needed, ordered: base cols first, then extras alphabetically
    all_cols = list(_BASE_COLUMNS)
    extra = set()
    for e in events:
        cat = e.get("category", "")
        cat_cols = _CSV_COLUMNS.get(cat, [])
        for c in cat_cols:
            if c not in all_cols:
                extra.add(c)
        for k in e.keys():
            if k not in all_cols:
                extra.add(k)
    all_cols.extend(sorted(extra))

    # Flatten complex values for CSV compatibility
    rows = []
    for e in events:
        row = {}
        for col in all_cols:
            val = e.get(col, "")
            if isinstance(val, (dict, list)):
                val = json.dumps(val, default=str)
            row[col] = val
        rows.append(row)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=all_cols)
        w.writeheader()
        w.writerows(rows)

    # Per-category breakdown
    cats = {}
    for e in events:
        c = e.get("category", "unknown")
        cats[c] = cats.get(c, 0) + 1
    breakdown = ", ".join(f"{c}={n}" for c, n in sorted(cats.items()))
    print(f"Exported {len(events)} events to {output_path} ({breakdown})")


def run_powershell_benchmark(metrics, hwnd, iterations=20):
    """Time the OLD PowerShell UIA scan (subprocess spawn) as baseline comparison."""
    import subprocess

    ps_script = f"""Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$wnd = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
$cancelBtn = $wnd.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, 'Cancel (Alt+Backspace)')))
$btns = $wnd.FindAll([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button)))
$state = 'IDLE'
if ($cancelBtn) {{ $state = 'PROCESSING' }}
foreach ($b in $btns) {{ if ($b.Current.Name -match 'Pick Model') {{ break }} }}
Write-Output $state"""

    ps_times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=10,
        )
        ps_times.append((time.perf_counter() - t0) * 1000)

    metrics.record_benchmark("uia_powershell_baseline", iterations, ps_times, {"hwnd": hwnd})


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Skynet Research Metrics")
    parser.add_argument("--summary", action="store_true", help="Generate research summary")
    parser.add_argument("--run-benchmarks", action="store_true", help="Run and record all benchmarks")
    parser.add_argument("--export-csv", type=str, help="Export metrics to CSV file")
    parser.add_argument("--snapshot", action="store_true", help="Collect live worker snapshot via UIA and print")
    args = parser.parse_args()

    m = SkynetMetrics()

    if args.summary:
        print(json.dumps(m.generate_summary(), indent=2))

    elif args.run_benchmarks:
        from tools.uia_engine import get_engine
        engine = get_engine()

        with open("data/workers.json") as f:
            data = json.load(f)
        hwnds = {w["name"]: w["hwnd"] for w in data["workers"]}
        orch_hwnd = data.get("orchestrator_hwnd", 0)
        if orch_hwnd:
            hwnds["orchestrator"] = orch_hwnd

        # Sequential benchmark (20 iterations)
        seq_times = []
        for _ in range(20):
            t0 = time.perf_counter()
            for h in hwnds.values():
                engine.scan(h)
            seq_times.append((time.perf_counter() - t0) * 1000)
        m.record_benchmark("uia_sequential_5win", 20, seq_times, {"windows": len(hwnds)})

        # Parallel benchmark (20 iterations)
        par_times = []
        for _ in range(20):
            t0 = time.perf_counter()
            engine.scan_all(hwnds)
            par_times.append((time.perf_counter() - t0) * 1000)
        m.record_benchmark("uia_parallel_5win", 20, par_times, {"windows": len(hwnds)})

        # Single window benchmark (50 iterations)
        first_hwnd = list(hwnds.values())[0]
        single_times = []
        for _ in range(50):
            t0 = time.perf_counter()
            engine.scan(first_hwnd)
            single_times.append((time.perf_counter() - t0) * 1000)
        m.record_benchmark("uia_single_window", 50, single_times, {"hwnd": first_hwnd})

        # PowerShell baseline benchmark (20 iterations on first worker)
        print("Running PowerShell baseline benchmark...")
        run_powershell_benchmark(m, first_hwnd, iterations=20)

        summary = m.generate_summary()
        print(json.dumps(summary, indent=2))

    elif args.export_csv:
        export_csv(args.export_csv)

    elif args.snapshot:
        snap = m.collect_live_snapshot()
        print(json.dumps(snap, indent=2, default=str))
