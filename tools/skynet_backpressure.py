"""Backpressure and flow-control for Skynet dispatch.

Monitors worker load, bus queue depth, and dispatch rate to prevent
overloading the swarm.  Exposes a simple gate — ``should_dispatch(worker)``
— that returns True/False plus the current pressure level.

Pressure levels:
    GREEN  — load < 70 %  → dispatch normally
    YELLOW — 70–90 %      → halve dispatch rate (insert cooldown)
    RED    — > 90 %       → stop dispatching, alert orchestrator

Data sources:
    • ``GET /status``       → per-worker queue_depth, status, consecutive_fails
    • ``data/realtime.json``→ cached worker state (zero-network)
    • ``data/dispatch_log.json`` → recent dispatch rate calculation

Usage:
    python tools/skynet_backpressure.py status          # system pressure
    python tools/skynet_backpressure.py check [WORKER]  # should_dispatch gate
    python tools/skynet_backpressure.py history          # pressure timeline
    python tools/skynet_backpressure.py daemon           # continuous monitor

Python API:
    from tools.skynet_backpressure import BackpressureMonitor
    bp = BackpressureMonitor()
    bp.refresh()
    if bp.should_dispatch("alpha"):
        dispatch_to_worker("alpha", task)
    print(bp.per_worker_load("beta"))   # 0.0 – 1.0
    print(bp.system_pressure())         # GREEN / YELLOW / RED
"""
# signed: gamma

import json
import os
import sys
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
REALTIME_PATH = DATA_DIR / "realtime.json"
DISPATCH_LOG_PATH = DATA_DIR / "dispatch_log.json"
PRESSURE_HISTORY_PATH = DATA_DIR / "backpressure_history.json"
BUS_URL = "http://localhost:8420"

WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

# ── Pressure levels ──────────────────────────────────────────────
PRESSURE_GREEN = "GREEN"
PRESSURE_YELLOW = "YELLOW"
PRESSURE_RED = "RED"

# ── Thresholds (fraction 0.0–1.0) ───────────────────────────────
THRESHOLD_YELLOW = 0.70   # 70 % capacity → slow down
THRESHOLD_RED = 0.90      # 90 % capacity → stop

# ── Dispatch rate limits ─────────────────────────────────────────
MAX_DISPATCHES_PER_MIN = 20          # absolute ceiling
YELLOW_DISPATCH_COOLDOWN_S = 4.0     # extra delay between dispatches
RATE_WINDOW_S = 60                   # sliding window for rate calc

# ── Worker load component weights ────────────────────────────────
WEIGHT_QUEUE_DEPTH = 0.35    # pending tasks in Go backend queue
WEIGHT_STATUS = 0.30         # PROCESSING=1.0, IDLE=0.0, DEAD=1.0
WEIGHT_CONSECUTIVE_FAILS = 0.20  # circuit-breaker stress
WEIGHT_DISPATCH_RATE = 0.15  # how fast we're feeding this worker

# Queue depth normalization — a worker with >=MAX_QUEUE_DEPTH is at 1.0
MAX_QUEUE_DEPTH = 5
MAX_CONSECUTIVE_FAILS = 5
# signed: gamma


def _read_json(path: Path) -> Any:
    """Read a JSON file, returning None on any error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_json(path: Path, data: Any) -> None:
    """Atomically write JSON."""
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(path))


class BackpressureMonitor:
    """Tracks system pressure and gates dispatch decisions.

    Call ``refresh()`` to pull latest state, then query with
    ``per_worker_load()``, ``should_dispatch()``, ``system_pressure()``.
    """

    def __init__(self):
        # Per-worker state cache (populated by refresh)
        self._worker_state: Dict[str, Dict] = {}
        # Dispatch timestamps for rate calculation
        self._dispatch_times: List[float] = []
        # Last refresh epoch
        self._last_refresh: float = 0.0
        # Pressure history (bounded)
        self._history: List[Dict] = []
        self._load_history()
    # signed: gamma

    def _load_history(self) -> None:
        data = _read_json(PRESSURE_HISTORY_PATH)
        if isinstance(data, list):
            self._history = data[-200:]

    def _save_history(self) -> None:
        if len(self._history) > 200:
            self._history = self._history[-200:]
        _write_json(PRESSURE_HISTORY_PATH, self._history)

    # ── Data collection ──────────────────────────────────────────

    def refresh(self) -> None:
        """Pull latest worker state from realtime.json and dispatch log."""
        self._refresh_from_realtime()
        self._refresh_from_backend()
        self._refresh_dispatch_rate()
        self._last_refresh = time.time()
    # signed: gamma

    def _refresh_from_realtime(self) -> None:
        """Read data/realtime.json (zero-network, updated every 1s)."""
        rt = _read_json(REALTIME_PATH)
        if not rt:
            return
        workers = rt.get("workers", {})
        for name in WORKER_NAMES:
            w = workers.get(name, {})
            self._worker_state.setdefault(name, {}).update({
                "status": w.get("status", "UNKNOWN").upper(),
                "queue_depth": w.get("queue_depth", 0),
                "consecutive_fails": w.get("consecutive_fails", 0),
                "tasks_completed": w.get("tasks_completed", 0),
                "total_errors": w.get("total_errors", 0),
                "current_task": w.get("current_task", ""),
                "avg_task_ms": w.get("avg_task_ms", 0),
                "circuit_state": w.get("circuit_state", "CLOSED"),
                "source": "realtime",
            })

    def _refresh_from_backend(self) -> None:
        """Try GET /status for live data (falls back silently)."""
        try:
            import urllib.request
            resp = urllib.request.urlopen(f"{BUS_URL}/status", timeout=3)
            data = json.loads(resp.read().decode())
            resp.close()
            agents = data.get("agents", {})
            for name in WORKER_NAMES:
                a = agents.get(name, {})
                if not a:
                    continue
                self._worker_state.setdefault(name, {}).update({
                    "status": a.get("status", "UNKNOWN").upper(),
                    "queue_depth": a.get("queue_depth", 0),
                    "consecutive_fails": a.get("consecutive_fails", 0),
                    "tasks_completed": a.get("tasks_completed", 0),
                    "total_errors": a.get("total_errors", 0),
                    "avg_task_ms": a.get("avg_task_ms", 0),
                    "circuit_state": a.get("circuit_state", "CLOSED"),
                    "source": "backend",
                })
        except Exception:
            pass  # backend unreachable — rely on realtime.json

    def _refresh_dispatch_rate(self) -> None:
        """Calculate recent dispatch rate from dispatch_log.json."""
        log = _read_json(DISPATCH_LOG_PATH)
        if not isinstance(log, list):
            return
        now = time.time()
        cutoff = now - RATE_WINDOW_S
        self._dispatch_times = []
        for entry in log:
            ts_str = entry.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str).timestamp()
                if ts > cutoff:
                    self._dispatch_times.append(ts)
            except (ValueError, TypeError):
                pass
    # signed: gamma

    # ── Per-worker load ──────────────────────────────────────────

    def per_worker_load(self, worker: str) -> float:
        """Return load factor 0.0–1.0 for a single worker.

        Components (weighted):
            35 %  queue_depth    — pending tasks normalized to MAX_QUEUE_DEPTH
            30 %  status         — PROCESSING/BUSY=1.0, IDLE=0.0, DEAD=1.0
            20 %  consec. fails  — circuit-breaker stress
            15 %  dispatch rate  — how fast tasks are being sent to this worker

        Returns:
            Float clamped to [0.0, 1.0].
        """
        ws = self._worker_state.get(worker, {})
        if not ws:
            return 0.0  # unknown worker treated as idle

        # Queue depth component
        qd = ws.get("queue_depth", 0)
        q_load = min(qd / MAX_QUEUE_DEPTH, 1.0)

        # Status component
        status = ws.get("status", "UNKNOWN")
        if status in ("PROCESSING", "BUSY", "ACTIVE", "WORKING"):
            s_load = 1.0
        elif status in ("DEAD", "FAILED"):
            s_load = 1.0  # dead workers are at full load (unusable)
        elif status == "IDLE":
            s_load = 0.0
        else:
            s_load = 0.5  # UNKNOWN

        # Consecutive fails component
        cf = ws.get("consecutive_fails", 0)
        cf_load = min(cf / MAX_CONSECUTIVE_FAILS, 1.0)

        # Dispatch rate component (per-worker: count recent dispatches to this worker)
        worker_dispatches = 0
        log = _read_json(DISPATCH_LOG_PATH)
        if isinstance(log, list):
            now = time.time()
            cutoff = now - RATE_WINDOW_S
            for entry in log:
                if entry.get("worker") != worker:
                    continue
                try:
                    ts = datetime.fromisoformat(entry["timestamp"]).timestamp()
                    if ts > cutoff:
                        worker_dispatches += 1
                except (ValueError, TypeError, KeyError):
                    pass
        # Normalize: >5 dispatches/min to one worker is heavy
        dr_load = min(worker_dispatches / 5.0, 1.0)

        # Weighted composite
        load = (
            WEIGHT_QUEUE_DEPTH * q_load
            + WEIGHT_STATUS * s_load
            + WEIGHT_CONSECUTIVE_FAILS * cf_load
            + WEIGHT_DISPATCH_RATE * dr_load
        )
        return round(min(max(load, 0.0), 1.0), 3)
    # signed: gamma

    # ── System-wide pressure ─────────────────────────────────────

    def system_pressure(self) -> str:
        """Return aggregate pressure level: GREEN, YELLOW, or RED.

        Computed from the average load across all workers plus
        the global dispatch rate factor.  Hard overrides:
        - ALL workers DEAD/FAILED → automatic RED
        - Any worker with circuit OPEN → at least YELLOW
        """
        if not self._worker_state:
            return PRESSURE_GREEN

        loads = [self.per_worker_load(w) for w in WORKER_NAMES]
        avg_load = sum(loads) / len(loads) if loads else 0.0

        # Hard override: all workers non-functional → RED
        statuses = [
            self._worker_state.get(w, {}).get("status", "UNKNOWN")
            for w in WORKER_NAMES
        ]
        alive_count = sum(
            1 for s in statuses if s in ("IDLE", "PROCESSING", "BUSY", "ACTIVE", "WORKING")
        )
        if alive_count == 0 and self._worker_state:
            return PRESSURE_RED

        # Hard override: any circuit breaker open → at least YELLOW
        any_circuit_open = any(
            self._worker_state.get(w, {}).get("circuit_state") == "CIRCUIT_OPEN"
            for w in WORKER_NAMES
        )

        # Global dispatch rate factor
        rate = len(self._dispatch_times)  # dispatches in last RATE_WINDOW_S
        rate_factor = min(rate / MAX_DISPATCHES_PER_MIN, 1.0)

        # Blend: 70 % worker load + 30 % dispatch rate pressure
        composite = 0.70 * avg_load + 0.30 * rate_factor

        if composite >= THRESHOLD_RED:
            return PRESSURE_RED
        elif composite >= THRESHOLD_YELLOW or any_circuit_open:
            return PRESSURE_YELLOW
        return PRESSURE_GREEN
    # signed: gamma

    def system_snapshot(self) -> Dict[str, Any]:
        """Return full pressure snapshot for display or logging."""
        loads = {w: self.per_worker_load(w) for w in WORKER_NAMES}
        avg_load = sum(loads.values()) / len(loads) if loads else 0.0
        rate = len(self._dispatch_times)
        pressure = self.system_pressure()

        # Per-worker details
        worker_details = {}
        for w in WORKER_NAMES:
            ws = self._worker_state.get(w, {})
            worker_details[w] = {
                "load": loads[w],
                "status": ws.get("status", "UNKNOWN"),
                "queue_depth": ws.get("queue_depth", 0),
                "consecutive_fails": ws.get("consecutive_fails", 0),
                "circuit_state": ws.get("circuit_state", "CLOSED"),
            }

        snap = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "pressure": pressure,
            "avg_load": round(avg_load, 3),
            "dispatch_rate_per_min": rate,
            "max_dispatches_per_min": MAX_DISPATCHES_PER_MIN,
            "workers": worker_details,
            "thresholds": {
                "yellow": THRESHOLD_YELLOW,
                "red": THRESHOLD_RED,
            },
        }
        return snap
    # signed: gamma

    # ── Dispatch gate ────────────────────────────────────────────

    def should_dispatch(self, worker: str) -> bool:
        """Return True if dispatching to this worker is safe.

        Decision logic:
            - RED system pressure → always False (+ alert)
            - Worker DEAD/FAILED or circuit OPEN → False
            - Worker load >= RED threshold → False
            - YELLOW pressure → True but caller should add cooldown
            - GREEN → True

        Callers can also check ``dispatch_cooldown()`` for the recommended
        delay before the next dispatch.
        """
        pressure = self.system_pressure()

        # System RED → block all dispatches
        if pressure == PRESSURE_RED:
            self._publish_red_alert()
            return False

        ws = self._worker_state.get(worker, {})
        status = ws.get("status", "UNKNOWN")

        # Dead or circuit-broken workers are not dispatchable
        if status in ("DEAD", "FAILED"):
            return False
        if ws.get("circuit_state", "CLOSED") == "CIRCUIT_OPEN":
            return False

        # Per-worker overload
        load = self.per_worker_load(worker)
        if load >= THRESHOLD_RED:
            return False

        return True
    # signed: gamma

    def dispatch_cooldown(self) -> float:
        """Return recommended seconds to wait before next dispatch.

        GREEN  → 0.0 (dispatch immediately)
        YELLOW → YELLOW_DISPATCH_COOLDOWN_S (slow down)
        RED    → float('inf') (stop dispatching)
        """
        pressure = self.system_pressure()
        if pressure == PRESSURE_RED:
            return float("inf")
        elif pressure == PRESSURE_YELLOW:
            return YELLOW_DISPATCH_COOLDOWN_S
        return 0.0

    def record_dispatch(self, worker: str) -> None:
        """Record a dispatch event for rate tracking."""
        self._dispatch_times.append(time.time())

    # ── Alerting ─────────────────────────────────────────────────

    def _publish_red_alert(self) -> None:
        """Publish RED pressure alert to bus (rate-limited)."""
        try:
            from tools.skynet_spam_guard import guarded_publish
            snap = self.system_snapshot()
            overloaded = [
                w for w, d in snap["workers"].items() if d["load"] >= THRESHOLD_RED
            ]
            guarded_publish({
                "sender": "backpressure",
                "topic": "orchestrator",
                "type": "alert",
                "content": (
                    f"BACKPRESSURE RED: avg_load={snap['avg_load']:.0%}, "
                    f"rate={snap['dispatch_rate_per_min']}/min, "
                    f"overloaded={overloaded or 'system-wide'}. "
                    f"Dispatching HALTED until pressure drops."
                ),
            })
        except Exception:
            pass
    # signed: gamma

    # ── History ──────────────────────────────────────────────────

    def record_snapshot(self) -> Dict:
        """Take a pressure snapshot and append to history."""
        snap = self.system_snapshot()
        self._history.append(snap)
        self._save_history()
        return snap

    def get_history(self, limit: int = 20) -> List[Dict]:
        """Return recent pressure history."""
        return self._history[-limit:]

    # ── Daemon ───────────────────────────────────────────────────

    def run_daemon(self, interval: float = 10.0) -> None:
        """Continuous monitoring loop — logs pressure and alerts on RED."""
        print(f"Backpressure daemon started (interval={interval}s). Ctrl+C to stop.")
        cycle = 0
        try:
            while True:
                cycle += 1
                self.refresh()
                snap = self.record_snapshot()
                p = snap["pressure"]

                if p == PRESSURE_RED:
                    print(
                        f"[{snap['timestamp']}] *** RED *** "
                        f"avg={snap['avg_load']:.0%} "
                        f"rate={snap['dispatch_rate_per_min']}/min"
                    )
                    for w, d in snap["workers"].items():
                        print(f"  {w}: load={d['load']:.0%} status={d['status']} qd={d['queue_depth']}")
                elif p == PRESSURE_YELLOW:
                    print(
                        f"[{snap['timestamp']}] YELLOW "
                        f"avg={snap['avg_load']:.0%} "
                        f"rate={snap['dispatch_rate_per_min']}/min "
                        f"cooldown={YELLOW_DISPATCH_COOLDOWN_S}s"
                    )
                elif cycle % 6 == 0:  # periodic heartbeat every ~60s
                    print(
                        f"[{snap['timestamp']}] GREEN "
                        f"avg={snap['avg_load']:.0%} "
                        f"rate={snap['dispatch_rate_per_min']}/min"
                    )

                time.sleep(interval)
        except KeyboardInterrupt:
            print(f"\nBackpressure daemon stopped after {cycle} cycles.")
    # signed: gamma


# ── Convenience functions (importable API) ───────────────────────

_monitor: Optional[BackpressureMonitor] = None


def _get_monitor() -> BackpressureMonitor:
    """Return a module-level monitor singleton, refreshed if stale."""
    global _monitor
    if _monitor is None:
        _monitor = BackpressureMonitor()
    if time.time() - _monitor._last_refresh > 3.0:
        _monitor.refresh()
    return _monitor


def per_worker_load(worker: str) -> float:
    """Return load factor 0.0–1.0 for a worker (auto-refreshes)."""
    return _get_monitor().per_worker_load(worker)


def should_dispatch(worker: str) -> bool:
    """Return True if dispatching to this worker is safe (auto-refreshes)."""
    return _get_monitor().should_dispatch(worker)


def dispatch_cooldown() -> float:
    """Recommended delay in seconds before next dispatch."""
    return _get_monitor().dispatch_cooldown()


def system_pressure() -> str:
    """Current system pressure: GREEN, YELLOW, or RED."""
    m = _get_monitor()
    return m.system_pressure()


def system_snapshot() -> Dict[str, Any]:
    """Full pressure snapshot dict."""
    m = _get_monitor()
    m.refresh()
    return m.system_snapshot()
# signed: gamma


# ── CLI ──────────────────────────────────────────────────────────

def _cli():
    parser = argparse.ArgumentParser(
        description="Skynet backpressure and flow-control monitor"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show current system pressure")

    ck = sub.add_parser("check", help="Check if dispatch is safe")
    ck.add_argument("worker", nargs="?", default=None,
                    help="Worker name (default: all workers)")

    hi = sub.add_parser("history", help="Show pressure history")
    hi.add_argument("--limit", type=int, default=10)

    dm = sub.add_parser("daemon", help="Run continuous pressure monitor")
    dm.add_argument("--interval", type=float, default=10.0)

    args = parser.parse_args()

    if args.command == "status":
        bp = BackpressureMonitor()
        bp.refresh()
        snap = bp.system_snapshot()
        p = snap["pressure"]
        color = {"GREEN": "OK", "YELLOW": "CAUTION", "RED": "CRITICAL"}[p]
        print(f"System Pressure: {p} ({color})")
        print(f"  Avg load: {snap['avg_load']:.1%}")
        print(f"  Dispatch rate: {snap['dispatch_rate_per_min']}/min "
              f"(max {snap['max_dispatches_per_min']})")
        print(f"  Cooldown: {bp.dispatch_cooldown()}s")
        print()
        print("Per-worker:")
        for w in WORKER_NAMES:
            d = snap["workers"][w]
            print(
                f"  {w:8s}  load={d['load']:.1%}  status={d['status']:10s} "
                f"qd={d['queue_depth']}  fails={d['consecutive_fails']}  "
                f"circuit={d['circuit_state']}"
            )

    elif args.command == "check":
        bp = BackpressureMonitor()
        bp.refresh()
        targets = [args.worker] if args.worker else WORKER_NAMES
        for w in targets:
            ok = bp.should_dispatch(w)
            load = bp.per_worker_load(w)
            verdict = "DISPATCH_OK" if ok else "BLOCKED"
            print(f"  {w}: {verdict}  (load={load:.1%}, pressure={bp.system_pressure()})")

    elif args.command == "history":
        bp = BackpressureMonitor()
        hist = bp.get_history(args.limit)
        if not hist:
            print("No pressure history recorded.")
        else:
            print(f"Last {len(hist)} snapshots:")
            for h in hist:
                print(
                    f"  [{h.get('timestamp','?')}] {h.get('pressure','?')} "
                    f"avg={h.get('avg_load',0):.1%} "
                    f"rate={h.get('dispatch_rate_per_min',0)}/min"
                )

    elif args.command == "daemon":
        bp = BackpressureMonitor()
        bp.run_daemon(args.interval)

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: gamma
