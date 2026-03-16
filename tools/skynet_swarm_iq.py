"""Skynet Swarm IQ Measurement Suite — P3.10

Measures collective intelligence of the Skynet multi-agent swarm across
8 orthogonal dimensions, computes a composite IQ score 0-100, and tracks
historical trends for regression detection.

Metrics:
    1. throughput   — tasks completed per minute
    2. quality      — cross-validation pass rate (0-1)
    3. latency      — average task completion time (seconds)
    4. utilization  — worker busy percentage (0-1)
    5. knowledge    — learnings broadcast per hour
    6. collaboration— cross-worker interactions per hour
    7. adaptability — parameter/strategy drift rate
    8. resilience   — mean recovery time from failures (seconds)

Usage:
    python tools/skynet_swarm_iq.py measure          # take a measurement
    python tools/skynet_swarm_iq.py report            # full report
    python tools/skynet_swarm_iq.py trend             # show IQ trend
    python tools/skynet_swarm_iq.py compare           # compare workers
    python tools/skynet_swarm_iq.py composite         # just the IQ score

# signed: beta
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
IQ_HISTORY_FILE = DATA_DIR / "swarm_iq_history.json"
WORKER_SCORES_FILE = DATA_DIR / "worker_scores.json"
DISPATCH_LOG_FILE = DATA_DIR / "dispatch_log.json"
REALTIME_FILE = DATA_DIR / "realtime.json"
TODOS_FILE = DATA_DIR / "todos.json"
LEARNINGS_FILE = DATA_DIR / "learnings.json"
BUS_ARCHIVE_FILE = DATA_DIR / "bus_archive.jsonl"
PHEROMONE_FILE = DATA_DIR / "pheromone_trails.json"
INCIDENTS_FILE = DATA_DIR / "incidents.json"

WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

# ── Metric Weights (sum = 1.0) ─────────────────────────────────────
# signed: beta
METRIC_WEIGHTS = {
    "throughput":    0.20,
    "quality":       0.20,
    "latency":       0.15,
    "utilization":   0.15,
    "knowledge":     0.10,
    "collaboration": 0.08,
    "adaptability":  0.07,
    "resilience":    0.05,
}

# ── Normalization References ───────────────────────────────────────
# These define what "100" looks like for each metric.
# Values are calibrated against expected Skynet operating ranges.
NORM_REFS = {
    "throughput":    2.0,    # 2 tasks/min = excellent
    "quality":       1.0,    # 100% CV pass rate = perfect
    "latency":       30.0,   # 30s avg = excellent (lower is better)
    "utilization":   0.85,   # 85% busy = excellent
    "knowledge":     10.0,   # 10 learnings/hour = excellent
    "collaboration": 8.0,    # 8 interactions/hour = excellent
    "adaptability":  0.5,    # 50% strategy drift = excellent
    "resilience":    15.0,   # 15s avg recovery = excellent (lower better)
}

MAX_HISTORY = 500  # max IQ measurements to keep
# signed: beta


# ── Data Classes ───────────────────────────────────────────────────

@dataclass
class MetricSnapshot:
    """Single measurement of one metric."""
    name: str
    raw_value: float
    normalized: float  # 0-100
    weight: float
    weighted_score: float  # normalized * weight
    detail: str = ""
    # signed: beta


@dataclass
class SwarmMeasurement:
    """Complete swarm IQ measurement at a point in time."""
    timestamp: float = field(default_factory=time.time)
    iso_time: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    metrics: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    composite_iq: float = 0.0
    worker_count: int = 4
    active_workers: int = 0
    measurement_ms: float = 0.0
    # signed: beta

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "iso_time": self.iso_time,
            "metrics": self.metrics,
            "composite_iq": round(self.composite_iq, 2),
            "worker_count": self.worker_count,
            "active_workers": self.active_workers,
            "measurement_ms": round(self.measurement_ms, 2),
        }


# ── Helper: Safe JSON Load ────────────────────────────────────────

def _load_json(path: Path, default: Any = None) -> Any:
    """Load JSON file safely, return default on failure."""
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _load_jsonl(path: Path, max_lines: int = 5000) -> List[Dict]:
    """Load last N lines from a JSONL file."""
    if not path.exists():
        return []
    results = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return results[-max_lines:]
    except OSError:
        return []
# signed: beta


# ── Metric Collectors ──────────────────────────────────────────────

def _measure_throughput(window_s: float = 3600.0) -> Tuple[float, str]:
    """Tasks completed per minute in the last window.

    Sources: dispatch_log.json, bus archive results.
    """
    cutoff = time.time() - window_s
    completed = 0

    # Count from dispatch log
    dispatch_log = _load_json(DISPATCH_LOG_FILE, [])
    if isinstance(dispatch_log, list):
        for entry in dispatch_log:
            ts = entry.get("timestamp", 0)
            if isinstance(ts, str):
                try:
                    ts = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
                except (ValueError, TypeError):
                    ts = 0
            if ts >= cutoff and entry.get("result_received"):
                completed += 1

    # Also count from bus archive results
    archive = _load_jsonl(BUS_ARCHIVE_FILE, 2000)
    for msg in archive:
        ts = msg.get("timestamp", 0)
        if isinstance(ts, str):
            try:
                ts = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, TypeError):
                ts = 0
        if ts >= cutoff and msg.get("type") == "result":
            completed += 1

    elapsed_min = window_s / 60.0
    rate = completed / elapsed_min if elapsed_min > 0 else 0
    return rate, f"{completed} tasks in {elapsed_min:.0f}min = {rate:.2f}/min"
# signed: beta


def _measure_quality() -> Tuple[float, str]:
    """Cross-validation pass rate from worker scores.

    Sources: worker_scores.json (task_awards vs failed_validations).
    """
    scores = _load_json(WORKER_SCORES_FILE, {})
    total_tasks = 0
    total_passes = 0

    for worker, data in scores.items():
        if worker in ("version", "updated_at", "schema"):
            continue
        if isinstance(data, dict):
            awards = data.get("task_awards", 0)
            failures = data.get("failed_validations", 0)
            if isinstance(awards, (int, float)):
                total_tasks += int(awards)
                total_passes += int(awards)
            if isinstance(failures, (int, float)):
                total_tasks += int(failures)

    if total_tasks == 0:
        # Fallback: estimate from score values
        for worker in WORKER_NAMES:
            wdata = scores.get(worker, {})
            if isinstance(wdata, dict):
                score = wdata.get("score", wdata.get("total", 0))
                if isinstance(score, (int, float)) and score > 0:
                    total_tasks += 1
                    total_passes += 1

    rate = total_passes / total_tasks if total_tasks > 0 else 0.0
    return rate, f"{total_passes}/{total_tasks} passed ({rate:.0%})"
# signed: beta


def _measure_latency(window_s: float = 3600.0) -> Tuple[float, str]:
    """Average task completion time in seconds.

    Sources: dispatch_log.json (dispatched_at -> result_at).
    """
    cutoff = time.time() - window_s
    durations = []

    dispatch_log = _load_json(DISPATCH_LOG_FILE, [])
    if isinstance(dispatch_log, list):
        for entry in dispatch_log:
            if not entry.get("result_received"):
                continue
            dispatched = entry.get("dispatched_at", entry.get("timestamp", 0))
            result_at = entry.get("result_at", 0)
            if isinstance(dispatched, str):
                try:
                    dispatched = time.mktime(time.strptime(dispatched[:19], "%Y-%m-%dT%H:%M:%S"))
                except (ValueError, TypeError):
                    continue
            if isinstance(result_at, str):
                try:
                    result_at = time.mktime(time.strptime(result_at[:19], "%Y-%m-%dT%H:%M:%S"))
                except (ValueError, TypeError):
                    continue
            if dispatched >= cutoff and result_at > dispatched:
                durations.append(result_at - dispatched)

    if not durations:
        return 60.0, "No recent dispatch data (default 60s)"

    avg = sum(durations) / len(durations)
    return avg, f"avg {avg:.1f}s over {len(durations)} tasks"
# signed: beta


def _measure_utilization() -> Tuple[float, str]:
    """Worker busy percentage from realtime state.

    Sources: realtime.json (worker states: PROCESSING=busy).
    """
    rt = _load_json(REALTIME_FILE, {})
    workers = rt.get("workers", rt.get("agents", {}))

    total = 0
    busy = 0
    details = []

    if isinstance(workers, dict):
        for name in WORKER_NAMES:
            wdata = workers.get(name, {})
            if isinstance(wdata, dict):
                total += 1
                state = wdata.get("state", wdata.get("status", "UNKNOWN"))
                if state == "PROCESSING":
                    busy += 1
                    details.append(f"{name}=BUSY")
                else:
                    details.append(f"{name}={state}")
    elif isinstance(workers, list):
        for w in workers:
            if isinstance(w, dict):
                name = w.get("name", "")
                if name in WORKER_NAMES:
                    total += 1
                    if w.get("state", w.get("status", "")) == "PROCESSING":
                        busy += 1

    rate = busy / total if total > 0 else 0.0
    detail_str = ", ".join(details[:4]) if details else f"{busy}/{total} busy"
    return rate, detail_str
# signed: beta


def _measure_knowledge(window_s: float = 3600.0) -> Tuple[float, str]:
    """Learnings broadcast per hour.

    Sources: bus archive (type=learning or topic=knowledge).
    """
    cutoff = time.time() - window_s
    count = 0

    archive = _load_jsonl(BUS_ARCHIVE_FILE, 2000)
    for msg in archive:
        ts = msg.get("timestamp", 0)
        if isinstance(ts, str):
            try:
                ts = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, TypeError):
                ts = 0
        if ts >= cutoff:
            if msg.get("type") == "learning" or msg.get("topic") == "knowledge":
                count += 1

    # Also check learnings.json size
    learnings = _load_json(LEARNINGS_FILE, [])
    if isinstance(learnings, list):
        recent = [l for l in learnings
                  if isinstance(l, dict) and l.get("timestamp", 0) >= cutoff]
        count = max(count, len(recent))

    hours = window_s / 3600.0
    rate = count / hours if hours > 0 else 0
    return rate, f"{count} learnings in {hours:.1f}h = {rate:.1f}/hr"
# signed: beta


def _measure_collaboration(window_s: float = 3600.0) -> Tuple[float, str]:
    """Cross-worker interactions per hour.

    Counts bus messages where sender != topic target worker,
    cross-validation dispatches, and convene sessions.
    """
    cutoff = time.time() - window_s
    interactions = 0

    archive = _load_jsonl(BUS_ARCHIVE_FILE, 2000)
    for msg in archive:
        ts = msg.get("timestamp", 0)
        if isinstance(ts, str):
            try:
                ts = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, TypeError):
                ts = 0
        if ts < cutoff:
            continue

        msg_type = msg.get("type", "")
        sender = msg.get("sender", "")

        # Cross-validation, convene, knowledge sharing
        if msg_type in ("cross_validation", "gate-proposal", "gate-vote",
                        "convene_start", "convene_join", "learning",
                        "crdt_sync", "strategy_sync"):
            interactions += 1
        # Result from worker to orchestrator
        elif msg_type == "result" and sender in WORKER_NAMES:
            interactions += 1

    hours = window_s / 3600.0
    rate = interactions / hours if hours > 0 else 0
    return rate, f"{interactions} interactions in {hours:.1f}h = {rate:.1f}/hr"
# signed: beta


def _measure_adaptability() -> Tuple[float, str]:
    """Parameter/strategy drift rate.

    Measures how much the system has adapted its strategies over time.
    Sources: pheromone_trails.json diversity, brain_config evolution.
    """
    pheromones = _load_json(PHEROMONE_FILE, {})

    categories_explored = 0
    total_deposits = 0
    worker_diversity = set()

    trails = pheromones.get("trails", pheromones)
    if isinstance(trails, dict):
        for category, workers in trails.items():
            if category in ("version", "updated_at", "metadata"):
                continue
            if isinstance(workers, dict):
                categories_explored += 1
                for worker, strength in workers.items():
                    if isinstance(strength, (int, float)) and strength > 0:
                        total_deposits += 1
                        worker_diversity.add(worker)

    # Normalize: more categories + more workers = more adaptive
    max_expected = len(WORKER_NAMES) * 8  # 4 workers × 8 categories
    score = min(total_deposits / max_expected, 1.0) if max_expected > 0 else 0.0

    # Boost for worker diversity
    diversity_bonus = len(worker_diversity) / len(WORKER_NAMES) if WORKER_NAMES else 0
    score = min((score + diversity_bonus) / 2, 1.0)

    return score, f"{categories_explored} categories, {total_deposits} deposits, {len(worker_diversity)} workers"
# signed: beta


def _measure_resilience(window_s: float = 3600.0) -> Tuple[float, str]:
    """Mean recovery time from failures in seconds.

    Sources: incidents.json, bus archive alerts/recoveries.
    """
    cutoff = time.time() - window_s
    recovery_times = []

    # Check incidents
    incidents = _load_json(INCIDENTS_FILE, [])
    if isinstance(incidents, list):
        for inc in incidents:
            if not isinstance(inc, dict):
                continue
            detected = inc.get("detected_at", 0)
            resolved = inc.get("resolved_at", 0)
            if isinstance(detected, str):
                try:
                    detected = time.mktime(time.strptime(detected[:19], "%Y-%m-%dT%H:%M:%S"))
                except (ValueError, TypeError):
                    detected = 0
            if isinstance(resolved, str):
                try:
                    resolved = time.mktime(time.strptime(resolved[:19], "%Y-%m-%dT%H:%M:%S"))
                except (ValueError, TypeError):
                    resolved = 0
            if detected >= cutoff and resolved > detected:
                recovery_times.append(resolved - detected)

    # Also check bus for STUCK_RECOVERED alerts
    archive = _load_jsonl(BUS_ARCHIVE_FILE, 2000)
    stuck_start = {}
    for msg in archive:
        ts = msg.get("timestamp", 0)
        if isinstance(ts, str):
            try:
                ts = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, TypeError):
                ts = 0
        if ts < cutoff:
            continue
        content = str(msg.get("content", ""))
        if "STUCK" in content or "DEAD" in content:
            worker = msg.get("sender", "unknown")
            if worker not in stuck_start:
                stuck_start[worker] = ts
        elif "RECOVERED" in content or "RESTORED" in content:
            worker = msg.get("sender", "unknown")
            if worker in stuck_start:
                recovery_times.append(ts - stuck_start[worker])
                del stuck_start[worker]

    if not recovery_times:
        # No failures = perfect resilience
        return 0.0, "No failures detected (perfect)"

    avg = sum(recovery_times) / len(recovery_times)
    return avg, f"avg {avg:.1f}s over {len(recovery_times)} recoveries"
# signed: beta


# ── Normalization ──────────────────────────────────────────────────

def _normalize(name: str, raw: float) -> float:
    """Normalize a raw metric value to 0-100 scale.

    For metrics where lower is better (latency, resilience),
    the score is inverted.
    """
    ref = NORM_REFS.get(name, 1.0)
    if ref <= 0:
        return 0.0

    # Inverse metrics: lower raw = better score
    if name in ("latency", "resilience"):
        if raw <= 0:
            return 100.0  # perfect (no latency / no failures)
        score = (ref / raw) * 100.0
    else:
        score = (raw / ref) * 100.0

    return max(0.0, min(100.0, score))
# signed: beta


# ── SwarmIQ Class ──────────────────────────────────────────────────

class SwarmIQ:
    """Swarm Intelligence Quotient measurement engine.

    Collects 8 metrics, normalizes each to 0-100, applies weights,
    and produces a composite IQ score.
    """

    METRIC_COLLECTORS = {
        "throughput":    _measure_throughput,
        "quality":       _measure_quality,
        "latency":       _measure_latency,
        "utilization":   _measure_utilization,
        "knowledge":     _measure_knowledge,
        "collaboration": _measure_collaboration,
        "adaptability":  _measure_adaptability,
        "resilience":    _measure_resilience,
    }
    # signed: beta

    def __init__(self, history_file: Optional[Path] = None):
        self.history_file = history_file or IQ_HISTORY_FILE
        self.history: List[Dict[str, Any]] = self._load_history()

    def _load_history(self) -> List[Dict[str, Any]]:
        data = _load_json(self.history_file, [])
        if isinstance(data, list):
            return data[-MAX_HISTORY:]
        return []

    def _save_history(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        trimmed = self.history[-MAX_HISTORY:]
        try:
            tmp = self.history_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(trimmed, f, indent=2)
            os.replace(str(tmp), str(self.history_file))
            self.history = trimmed
        except OSError as e:
            logger.warning("Failed to save IQ history: %s", e)
    # signed: beta

    def measure(self, window_s: float = 3600.0,
                save: bool = True) -> SwarmMeasurement:
        """Take a complete swarm IQ measurement.

        Args:
            window_s: Time window for rate-based metrics (default 1 hour).
            save: Whether to append to history file.

        Returns:
            SwarmMeasurement with all 8 metrics and composite IQ.
        """
        t0 = time.time()
        measurement = SwarmMeasurement()
        total_weighted = 0.0

        for name, collector in self.METRIC_COLLECTORS.items():
            try:
                # Some collectors take window_s, some don't
                if name in ("throughput", "latency", "knowledge",
                            "collaboration", "resilience"):
                    raw, detail = collector(window_s)
                else:
                    raw, detail = collector()
            except Exception as e:
                logger.warning("Metric %s failed: %s", name, e)
                raw, detail = 0.0, f"ERROR: {e}"

            normalized = _normalize(name, raw)
            weight = METRIC_WEIGHTS.get(name, 0.0)
            weighted = normalized * weight

            measurement.metrics[name] = {
                "raw": round(raw, 4),
                "normalized": round(normalized, 2),
                "weight": weight,
                "weighted": round(weighted, 2),
                "detail": detail,
            }
            total_weighted += weighted

        measurement.composite_iq = round(total_weighted, 2)
        measurement.measurement_ms = (time.time() - t0) * 1000

        # Count active workers
        rt = _load_json(REALTIME_FILE, {})
        workers = rt.get("workers", rt.get("agents", {}))
        if isinstance(workers, dict):
            measurement.active_workers = sum(
                1 for n in WORKER_NAMES
                if n in workers and isinstance(workers[n], dict)
                and workers[n].get("state", "") != "DEAD"
            )
        elif isinstance(workers, list):
            measurement.active_workers = sum(
                1 for w in workers
                if isinstance(w, dict) and w.get("name") in WORKER_NAMES
            )

        if save:
            self.history.append(measurement.to_dict())
            self._save_history()

        return measurement
    # signed: beta

    def composite_iq(self, window_s: float = 3600.0) -> float:
        """Get just the composite IQ score."""
        m = self.measure(window_s=window_s, save=False)
        return m.composite_iq

    def get_trend(self, last_n: int = 20) -> Dict[str, Any]:
        """Analyze IQ trend from history.

        Returns:
            Dict with current, average, min, max, trend direction,
            and per-metric trends.
        """
        recent = self.history[-last_n:] if self.history else []
        if not recent:
            return {
                "entries": 0,
                "current": 0.0,
                "average": 0.0,
                "min": 0.0,
                "max": 0.0,
                "trend": "unknown",
                "metric_trends": {},
            }

        iqs = [e.get("composite_iq", 0.0) for e in recent]
        current = iqs[-1] if iqs else 0.0
        avg = sum(iqs) / len(iqs) if iqs else 0.0

        # Trend: compare first half avg to second half avg
        if len(iqs) >= 4:
            mid = len(iqs) // 2
            first_half = sum(iqs[:mid]) / mid
            second_half = sum(iqs[mid:]) / (len(iqs) - mid)
            diff = second_half - first_half
            if diff > 2:
                trend = "improving"
            elif diff < -2:
                trend = "declining"
            else:
                trend = "stable"
        else:
            trend = "insufficient_data"

        # Per-metric trends
        metric_trends = {}
        for metric_name in METRIC_WEIGHTS:
            values = []
            for e in recent:
                m = e.get("metrics", {}).get(metric_name, {})
                values.append(m.get("normalized", 0.0))
            if values:
                metric_trends[metric_name] = {
                    "current": round(values[-1], 2),
                    "average": round(sum(values) / len(values), 2),
                    "min": round(min(values), 2),
                    "max": round(max(values), 2),
                }

        return {
            "entries": len(recent),
            "current": round(current, 2),
            "average": round(avg, 2),
            "min": round(min(iqs), 2) if iqs else 0.0,
            "max": round(max(iqs), 2) if iqs else 0.0,
            "trend": trend,
            "metric_trends": metric_trends,
        }
    # signed: beta

    def compare_workers(self) -> Dict[str, Any]:
        """Compare individual worker performance metrics.

        Returns worker-by-worker breakdown of dispatch success,
        scores, and task counts.
        """
        scores = _load_json(WORKER_SCORES_FILE, {})
        dispatch_log = _load_json(DISPATCH_LOG_FILE, [])

        worker_stats = {}
        for name in WORKER_NAMES:
            wdata = scores.get(name, {})
            if not isinstance(wdata, dict):
                wdata = {}

            # Count dispatches to this worker
            dispatched = 0
            completed = 0
            if isinstance(dispatch_log, list):
                for entry in dispatch_log:
                    if entry.get("worker") == name:
                        dispatched += 1
                        if entry.get("result_received"):
                            completed += 1

            score = wdata.get("score", wdata.get("total", 0.0))
            if not isinstance(score, (int, float)):
                score = 0.0

            worker_stats[name] = {
                "score": round(score, 3),
                "dispatched": dispatched,
                "completed": completed,
                "completion_rate": round(completed / dispatched, 2) if dispatched > 0 else 0.0,
                "task_awards": wdata.get("task_awards", 0),
                "failed_validations": wdata.get("failed_validations", 0),
            }

        return {
            "workers": worker_stats,
            "best_worker": max(worker_stats, key=lambda w: worker_stats[w]["score"])
                           if worker_stats else None,
            "total_dispatched": sum(w["dispatched"] for w in worker_stats.values()),
            "total_completed": sum(w["completed"] for w in worker_stats.values()),
        }
    # signed: beta

    def report(self, window_s: float = 3600.0) -> str:
        """Generate a full text report.

        Returns:
            Multi-line string with all metrics, IQ score, trends,
            and worker comparison.
        """
        m = self.measure(window_s=window_s, save=True)
        trend = self.get_trend()
        workers = self.compare_workers()

        lines = []
        lines.append("=" * 60)
        lines.append("  SKYNET SWARM IQ REPORT")
        lines.append("=" * 60)
        lines.append(f"  Composite IQ: {m.composite_iq:.1f} / 100")
        lines.append(f"  Trend:        {trend['trend']} "
                      f"(avg {trend['average']:.1f}, "
                      f"min {trend['min']:.1f}, max {trend['max']:.1f})")
        lines.append(f"  Workers:      {m.active_workers}/{m.worker_count} active")
        lines.append(f"  Measured:     {m.iso_time} ({m.measurement_ms:.0f}ms)")
        lines.append("")
        lines.append("  METRICS")
        lines.append("  " + "-" * 56)

        for name in METRIC_WEIGHTS:
            md = m.metrics.get(name, {})
            bar_len = int(md.get("normalized", 0) / 5)  # 0-20 chars
            bar = "█" * bar_len + "░" * (20 - bar_len)
            lines.append(f"  {name:15s} {bar} {md.get('normalized', 0):5.1f} "
                          f"(raw={md.get('raw', 0):.3f}, w={md.get('weight', 0):.2f})")
            lines.append(f"  {'':15s} {md.get('detail', '')}")

        lines.append("")
        lines.append("  WORKER COMPARISON")
        lines.append("  " + "-" * 56)
        for name in WORKER_NAMES:
            ws = workers["workers"].get(name, {})
            lines.append(f"  {name:8s} score={ws.get('score', 0):+.3f}  "
                          f"tasks={ws.get('completed', 0)}/{ws.get('dispatched', 0)}  "
                          f"rate={ws.get('completion_rate', 0):.0%}")

        if trend.get("entries", 0) > 1:
            lines.append("")
            lines.append(f"  HISTORY ({trend['entries']} measurements)")
            lines.append("  " + "-" * 56)
            for name in METRIC_WEIGHTS:
                mt = trend.get("metric_trends", {}).get(name, {})
                if mt:
                    lines.append(f"  {name:15s} cur={mt['current']:5.1f}  "
                                  f"avg={mt['average']:5.1f}  "
                                  f"range=[{mt['min']:.1f}-{mt['max']:.1f}]")

        lines.append("")
        lines.append("=" * 60)
        return "\n".join(lines)
    # signed: beta


# ── Module-Level Convenience Functions ─────────────────────────────

def swarm_measure(window_s: float = 3600.0) -> SwarmMeasurement:
    """Take a swarm IQ measurement."""
    return SwarmIQ().measure(window_s=window_s)


def swarm_iq(window_s: float = 3600.0) -> float:
    """Get composite IQ score."""
    return SwarmIQ().composite_iq(window_s=window_s)


def swarm_report(window_s: float = 3600.0) -> str:
    """Generate full report."""
    return SwarmIQ().report(window_s=window_s)


def swarm_trend(last_n: int = 20) -> Dict[str, Any]:
    """Get IQ trend."""
    return SwarmIQ().get_trend(last_n=last_n)


def swarm_compare() -> Dict[str, Any]:
    """Compare workers."""
    return SwarmIQ().compare_workers()
# signed: beta


# ── CLI ────────────────────────────────────────────────────────────

def _cli() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Skynet Swarm IQ Measurement Suite — P3.10",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/skynet_swarm_iq.py measure       # take measurement + save
  python tools/skynet_swarm_iq.py report         # full report
  python tools/skynet_swarm_iq.py trend          # show IQ trend
  python tools/skynet_swarm_iq.py compare        # compare workers
  python tools/skynet_swarm_iq.py composite      # just the IQ score
""",
    )
    sub = parser.add_subparsers(dest="command")

    m_p = sub.add_parser("measure", help="Take a measurement")
    m_p.add_argument("--window", type=float, default=3600.0,
                     help="Time window in seconds (default 3600)")

    r_p = sub.add_parser("report", help="Full report")
    r_p.add_argument("--window", type=float, default=3600.0,
                     help="Time window in seconds")

    t_p = sub.add_parser("trend", help="Show IQ trend")
    t_p.add_argument("--last", type=int, default=20,
                     help="Number of recent entries (default 20)")

    sub.add_parser("compare", help="Compare worker performance")

    c_p = sub.add_parser("composite", help="Just the IQ score")
    c_p.add_argument("--window", type=float, default=3600.0,
                     help="Time window in seconds")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    iq = SwarmIQ()

    if args.command == "measure":
        m = iq.measure(window_s=args.window)
        print(json.dumps(m.to_dict(), indent=2))

    elif args.command == "report":
        print(iq.report(window_s=args.window))

    elif args.command == "trend":
        t = iq.get_trend(last_n=args.last)
        print(json.dumps(t, indent=2))

    elif args.command == "compare":
        c = iq.compare_workers()
        print(json.dumps(c, indent=2))

    elif args.command == "composite":
        score = iq.composite_iq(window_s=args.window)
        print(f"Swarm IQ: {score:.1f} / 100")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: beta
