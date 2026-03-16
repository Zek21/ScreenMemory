"""Complex Event Processing (CEP) engine for Skynet.

Detects temporal patterns across the worker swarm by maintaining sliding
windows of events and evaluating configurable rules against them.

Built-in pattern rules:
  1. WorkerStuck   — PROCESSING >180s with stale heartbeat
  2. SystemUnstable — >5 errors across workers in 1-minute window
  3. CascadeFailure — >=2 workers DEAD within 30s
  4. IdleSwarm      — all workers IDLE >5 minutes, triggers self-improvement

Event sources:
  - Bus messages  (GET /bus/messages)
  - Worker state  (data/realtime.json)
  - Heartbeats    (metadata in bus heartbeat messages)

Usage:
    python tools/skynet_cep.py scan               # one-shot pattern scan
    python tools/skynet_cep.py daemon              # continuous monitoring
    python tools/skynet_cep.py rules               # show loaded rules
    python tools/skynet_cep.py history [--limit N]  # recent detections
    python tools/skynet_cep.py inject <type> [--worker NAME]  # inject test event

Python API:
    from tools.skynet_cep import CEPEngine
    engine = CEPEngine()
    engine.ingest_from_live()
    alerts = engine.detect_patterns()
"""
# signed: gamma

import json
import os
import sys
import time
import argparse
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
RULES_PATH = DATA_DIR / "cep_rules.json"
HISTORY_PATH = DATA_DIR / "cep_history.json"
REALTIME_PATH = DATA_DIR / "realtime.json"
BUS_URL = "http://localhost:8420"

WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

# ── Event types ingested by the CEP engine ───────────────────────
EVENT_WORKER_STATE = "worker_state"       # periodic worker status snapshot
EVENT_BUS_MESSAGE = "bus_message"         # bus message received
EVENT_HEARTBEAT = "heartbeat"             # worker/monitor heartbeat
EVENT_ERROR = "error"                     # error event (from bus alerts)
EVENT_DEAD = "dead"                       # worker DEAD detection
EVENT_STUCK = "stuck"                     # worker stuck in PROCESSING
EVENT_IDLE = "idle"                       # worker went IDLE

# Alert severities
SEV_CRITICAL = "CRITICAL"
SEV_HIGH = "HIGH"
SEV_MEDIUM = "MEDIUM"
SEV_LOW = "LOW"
# signed: gamma


class SlidingWindow:
    """Time-bounded deque of events with automatic expiry.

    Events are dicts with at least an ``epoch`` float field.
    """

    def __init__(self, window_s: float = 300.0):
        self._window_s = window_s
        self._events: deque = deque()
        self._lock = threading.Lock()

    @property
    def window_s(self) -> float:
        return self._window_s

    def add(self, event: dict) -> None:
        if "epoch" not in event:
            event["epoch"] = time.time()
        with self._lock:
            self._events.append(event)
            self._expire()

    def _expire(self) -> None:
        cutoff = time.time() - self._window_s
        while self._events and self._events[0].get("epoch", 0) < cutoff:
            self._events.popleft()

    def get_all(self) -> list[dict]:
        with self._lock:
            self._expire()
            return list(self._events)

    def get_by_type(self, event_type: str) -> list[dict]:
        return [e for e in self.get_all() if e.get("type") == event_type]

    def get_by_worker(self, worker: str) -> list[dict]:
        return [e for e in self.get_all() if e.get("worker") == worker]

    def count_by_type(self, event_type: str) -> int:
        return len(self.get_by_type(event_type))

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def __len__(self) -> int:
        with self._lock:
            self._expire()
            return len(self._events)
    # signed: gamma


def _load_rules() -> dict:
    """Load CEP rules from data/cep_rules.json."""
    if RULES_PATH.exists():
        try:
            with open(RULES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    # Hardcoded defaults if file missing
    return {
        "rules": {
            "worker_stuck": {
                "enabled": True, "threshold_s": 180,
                "heartbeat_stale_s": 60, "severity": "HIGH",
                "cooldown_s": 300, "action": "alert_orchestrator",
            },
            "system_unstable": {
                "enabled": True, "error_threshold": 5,
                "window_s": 60, "severity": "HIGH",
                "cooldown_s": 120, "action": "alert_orchestrator",
            },
            "cascade_failure": {
                "enabled": True, "dead_threshold": 2,
                "window_s": 30, "severity": "CRITICAL",
                "cooldown_s": 60, "action": "alert_urgent",
            },
            "idle_swarm": {
                "enabled": True, "idle_threshold_s": 300,
                "severity": "LOW", "cooldown_s": 600,
                "action": "trigger_self_improve",
            },
        },
        "windows": {"short": 60, "medium": 300, "long": 900},
        "daemon": {"poll_interval_s": 5, "bus_poll_limit": 50},
    }
    # signed: gamma


class CEPEngine:
    """Complex Event Processing engine with sliding window pattern detection.

    Maintains three overlapping windows (short/medium/long) and evaluates
    configurable rules against the event stream on each scan cycle.
    """

    def __init__(self, rules: Optional[dict] = None):
        cfg = rules or _load_rules()
        win_cfg = cfg.get("windows", {})

        self.rules = cfg.get("rules", {})
        self.daemon_cfg = cfg.get("daemon", {})

        # Three overlapping sliding windows
        self.short_window = SlidingWindow(win_cfg.get("short", 60))
        self.medium_window = SlidingWindow(win_cfg.get("medium", 300))
        self.long_window = SlidingWindow(win_cfg.get("long", 900))

        # Per-worker state tracking
        self._worker_processing_since: dict[str, float] = {}
        self._worker_last_heartbeat: dict[str, float] = {}
        self._worker_idle_since: dict[str, float] = {}

        # Alert cooldown tracking (rule_name → last_fire_epoch)
        self._last_alert: dict[str, float] = {}

        # Detection history (bounded)
        self._history: list[dict] = []
        self._load_history()
    # signed: gamma

    def _load_history(self) -> None:
        if HISTORY_PATH.exists():
            try:
                with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                    self._history = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._history = []

    def _save_history(self) -> None:
        if len(self._history) > 500:
            self._history = self._history[-500:]
        tmp = str(HISTORY_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._history, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(HISTORY_PATH))
    # signed: gamma

    def ingest(self, event: dict) -> None:
        """Ingest a single event into all sliding windows.

        Args:
            event: Dict with at least ``type`` and ``epoch`` fields.
                   Optional: ``worker``, ``severity``, ``content``.
        """
        if "epoch" not in event:
            event["epoch"] = time.time()

        self.short_window.add(event)
        self.medium_window.add(dict(event))   # independent copies
        self.long_window.add(dict(event))

        # Update per-worker tracking
        worker = event.get("worker")
        etype = event.get("type", "")

        if worker and worker in WORKER_NAMES:
            if etype == EVENT_HEARTBEAT:
                self._worker_last_heartbeat[worker] = event["epoch"]

            status = event.get("status", "").upper()
            if status == "PROCESSING" and worker not in self._worker_processing_since:
                self._worker_processing_since[worker] = event["epoch"]
            elif status == "IDLE":
                self._worker_processing_since.pop(worker, None)
                if worker not in self._worker_idle_since:
                    self._worker_idle_since[worker] = event["epoch"]
            elif status in ("PROCESSING", "BUSY", "ACTIVE"):
                self._worker_idle_since.pop(worker, None)
    # signed: gamma

    def ingest_from_live(self) -> int:
        """Ingest events from live system state (realtime.json + bus).

        Returns:
            Number of events ingested.
        """
        count = 0
        now = time.time()

        # Source 1: realtime.json worker states
        if REALTIME_PATH.exists():
            try:
                with open(REALTIME_PATH, "r", encoding="utf-8") as f:
                    rt = json.load(f)
                workers = rt.get("workers", {})
                for name in WORKER_NAMES:
                    w = workers.get(name, {})
                    status = w.get("status", "IDLE").upper()
                    event = {
                        "type": EVENT_WORKER_STATE,
                        "worker": name,
                        "status": status,
                        "epoch": now,
                        "current_task": w.get("current_task", ""),
                        "consecutive_fails": w.get("consecutive_fails", 0),
                        "total_errors": w.get("total_errors", 0),
                    }
                    self.ingest(event)
                    count += 1

                    # Detect error events from consecutive_fails
                    if w.get("consecutive_fails", 0) > 0:
                        self.ingest({
                            "type": EVENT_ERROR,
                            "worker": name,
                            "epoch": now,
                            "content": f"{name} has {w['consecutive_fails']} consecutive fails",
                        })
                        count += 1

                    # Track DEAD
                    if status in ("DEAD", "FAILED"):
                        self.ingest({
                            "type": EVENT_DEAD,
                            "worker": name,
                            "epoch": now,
                        })
                        count += 1

            except (json.JSONDecodeError, OSError):
                pass

        # Source 2: bus messages (recent alerts/errors)
        try:
            import urllib.request
            resp = urllib.request.urlopen(
                f"{BUS_URL}/bus/messages?limit={self.daemon_cfg.get('bus_poll_limit', 50)}",
                timeout=5,
            )
            messages = json.loads(resp.read().decode())
            resp.close()

            for msg in messages:
                msg_type = msg.get("type", "")
                sender = msg.get("sender", "")
                content = msg.get("content", "")

                # Parse timestamp
                ts_str = msg.get("timestamp", "")
                try:
                    from datetime import datetime as dt
                    parsed = dt.fromisoformat(ts_str.replace("Z", "+00:00"))
                    epoch = parsed.timestamp()
                except (ValueError, TypeError):
                    epoch = now

                # Convert bus alerts to CEP events
                if msg_type == "alert" and "DEAD" in content.upper():
                    # Extract worker name from content
                    for wn in WORKER_NAMES:
                        if wn in content.lower():
                            self.ingest({
                                "type": EVENT_DEAD,
                                "worker": wn,
                                "epoch": epoch,
                                "content": content,
                                "source": "bus",
                            })
                            count += 1
                            break

                elif msg_type == "alert" and "STUCK" in content.upper():
                    for wn in WORKER_NAMES:
                        if wn in content.lower():
                            self.ingest({
                                "type": EVENT_STUCK,
                                "worker": wn,
                                "epoch": epoch,
                                "content": content,
                                "source": "bus",
                            })
                            count += 1
                            break

                elif msg_type == "heartbeat":
                    # Monitor heartbeats contain worker states in metadata
                    meta = msg.get("metadata", {})
                    for wn in WORKER_NAMES:
                        if wn in meta:
                            self.ingest({
                                "type": EVENT_HEARTBEAT,
                                "worker": wn,
                                "status": meta[wn],
                                "epoch": epoch,
                                "source": "bus",
                            })
                            count += 1

                elif msg_type == "alert":
                    # Generic error/alert event
                    self.ingest({
                        "type": EVENT_ERROR,
                        "epoch": epoch,
                        "content": content,
                        "sender": sender,
                        "source": "bus",
                    })
                    count += 1

        except Exception:
            pass  # Bus unavailable — rely on realtime.json only

        return count
    # signed: gamma

    def _check_cooldown(self, rule_name: str) -> bool:
        """Return True if rule is past its cooldown and can fire."""
        cooldown = self.rules.get(rule_name, {}).get("cooldown_s", 300)
        last = self._last_alert.get(rule_name, 0)
        return (time.time() - last) > cooldown

    def _fire_alert(self, rule_name: str, alert: dict) -> None:
        """Record alert firing and save to history."""
        self._last_alert[rule_name] = time.time()
        alert["fired_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        alert["fired_epoch"] = time.time()
        self._history.append(alert)
        self._save_history()
    # signed: gamma

    # ── Pattern rules ────────────────────────────────────────────

    def _rule_worker_stuck(self) -> list[dict]:
        """Rule 1: Worker in PROCESSING >threshold with stale heartbeat."""
        cfg = self.rules.get("worker_stuck", {})
        if not cfg.get("enabled", True):
            return []

        threshold = cfg.get("threshold_s", 180)
        hb_stale = cfg.get("heartbeat_stale_s", 60)
        severity = cfg.get("severity", SEV_HIGH)
        alerts = []
        now = time.time()

        for worker in WORKER_NAMES:
            proc_since = self._worker_processing_since.get(worker)
            if proc_since is None:
                continue

            duration = now - proc_since
            if duration < threshold:
                continue

            # Check heartbeat staleness
            last_hb = self._worker_last_heartbeat.get(worker, 0)
            hb_age = now - last_hb if last_hb > 0 else float("inf")

            if hb_age > hb_stale:
                rule_key = f"worker_stuck_{worker}"
                if self._check_cooldown(rule_key):
                    alert = {
                        "rule": "worker_stuck",
                        "severity": severity,
                        "worker": worker,
                        "processing_duration_s": round(duration, 1),
                        "heartbeat_age_s": round(hb_age, 1) if hb_age != float("inf") else None,
                        "message": (
                            f"WORKER_STUCK: {worker} has been PROCESSING for "
                            f"{duration:.0f}s with heartbeat stale for "
                            f"{hb_age:.0f}s (threshold: {threshold}s)"
                        ),
                    }
                    self._fire_alert(rule_key, alert)
                    alerts.append(alert)

        return alerts
    # signed: gamma

    def _rule_system_unstable(self) -> list[dict]:
        """Rule 2: Too many errors across workers in short window."""
        cfg = self.rules.get("system_unstable", {})
        if not cfg.get("enabled", True):
            return []

        error_threshold = cfg.get("error_threshold", 5)
        window_s = cfg.get("window_s", 60)
        severity = cfg.get("severity", SEV_HIGH)

        # Use the short window if it matches, otherwise filter by window_s
        if abs(self.short_window.window_s - window_s) < 1:
            error_count = self.short_window.count_by_type(EVENT_ERROR)
        else:
            now = time.time()
            cutoff = now - window_s
            errors = [
                e for e in self.medium_window.get_by_type(EVENT_ERROR)
                if e.get("epoch", 0) > cutoff
            ]
            error_count = len(errors)

        if error_count >= error_threshold:
            rule_key = "system_unstable"
            if self._check_cooldown(rule_key):
                alert = {
                    "rule": "system_unstable",
                    "severity": severity,
                    "error_count": error_count,
                    "window_s": window_s,
                    "message": (
                        f"SYSTEM_UNSTABLE: {error_count} errors detected in "
                        f"last {window_s}s (threshold: {error_threshold})"
                    ),
                }
                self._fire_alert(rule_key, alert)
                return [alert]

        return []
    # signed: gamma

    def _rule_cascade_failure(self) -> list[dict]:
        """Rule 3: Multiple workers go DEAD within a short window."""
        cfg = self.rules.get("cascade_failure", {})
        if not cfg.get("enabled", True):
            return []

        dead_threshold = cfg.get("dead_threshold", 2)
        window_s = cfg.get("window_s", 30)
        severity = cfg.get("severity", SEV_CRITICAL)

        now = time.time()
        cutoff = now - window_s

        # Check short window for DEAD events
        dead_events = self.short_window.get_by_type(EVENT_DEAD)
        recent_dead = [e for e in dead_events if e.get("epoch", 0) > cutoff]

        # Deduplicate by worker name
        dead_workers = {e.get("worker") for e in recent_dead if e.get("worker")}

        if len(dead_workers) >= dead_threshold:
            rule_key = "cascade_failure"
            if self._check_cooldown(rule_key):
                alert = {
                    "rule": "cascade_failure",
                    "severity": severity,
                    "dead_workers": sorted(dead_workers),
                    "dead_count": len(dead_workers),
                    "window_s": window_s,
                    "message": (
                        f"CASCADE_FAILURE: {len(dead_workers)} workers DEAD "
                        f"within {window_s}s — {', '.join(sorted(dead_workers))}"
                    ),
                }
                self._fire_alert(rule_key, alert)
                return [alert]

        return []
    # signed: gamma

    def _rule_idle_swarm(self) -> list[dict]:
        """Rule 4: All workers IDLE for extended period."""
        cfg = self.rules.get("idle_swarm", {})
        if not cfg.get("enabled", True):
            return []

        idle_threshold = cfg.get("idle_threshold_s", 300)
        severity = cfg.get("severity", SEV_LOW)
        now = time.time()

        # Check if ALL workers are currently idle
        all_idle = True
        min_idle_duration = float("inf")

        for worker in WORKER_NAMES:
            idle_since = self._worker_idle_since.get(worker)
            if idle_since is None:
                all_idle = False
                break
            duration = now - idle_since
            min_idle_duration = min(min_idle_duration, duration)
            if duration < idle_threshold:
                all_idle = False
                break

        if all_idle and min_idle_duration >= idle_threshold:
            rule_key = "idle_swarm"
            if self._check_cooldown(rule_key):
                alert = {
                    "rule": "idle_swarm",
                    "severity": severity,
                    "idle_duration_s": round(min_idle_duration, 1),
                    "message": (
                        f"IDLE_SWARM: All {len(WORKER_NAMES)} workers have been "
                        f"IDLE for {min_idle_duration:.0f}s — trigger "
                        f"self-improvement tasks"
                    ),
                }
                self._fire_alert(rule_key, alert)
                return [alert]

        return []
    # signed: gamma

    def detect_patterns(self, events: Optional[list[dict]] = None) -> list[dict]:
        """Evaluate all rules against the current event windows.

        If ``events`` is provided, they are ingested before detection.
        Otherwise, evaluates against already-ingested events.

        Args:
            events: Optional list of event dicts to ingest first.

        Returns:
            List of alert dicts from all rules that fired.
        """
        if events:
            for e in events:
                self.ingest(e)

        alerts = []
        alerts.extend(self._rule_worker_stuck())
        alerts.extend(self._rule_system_unstable())
        alerts.extend(self._rule_cascade_failure())
        alerts.extend(self._rule_idle_swarm())

        # Sort by severity
        sev_order = {SEV_CRITICAL: 0, SEV_HIGH: 1, SEV_MEDIUM: 2, SEV_LOW: 3}
        alerts.sort(key=lambda a: sev_order.get(a.get("severity", "LOW"), 9))

        return alerts
    # signed: gamma

    def scan_live(self) -> dict:
        """One-shot: ingest live data and detect patterns.

        Returns:
            Dict with ingested count, alerts, and summary.
        """
        ingested = self.ingest_from_live()
        alerts = self.detect_patterns()

        # Publish critical/high alerts to bus
        self._publish_alerts(alerts)

        return {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "events_ingested": ingested,
            "short_window_size": len(self.short_window),
            "medium_window_size": len(self.medium_window),
            "long_window_size": len(self.long_window),
            "alerts": alerts,
            "alert_count": len(alerts),
            "critical_count": sum(1 for a in alerts if a.get("severity") == SEV_CRITICAL),
        }
    # signed: gamma

    def _publish_alerts(self, alerts: list[dict]) -> None:
        """Publish significant alerts to the Skynet bus."""
        publishable = [
            a for a in alerts
            if a.get("severity") in (SEV_CRITICAL, SEV_HIGH)
        ]
        if not publishable:
            return

        try:
            from tools.skynet_spam_guard import guarded_publish
            for alert in publishable[:3]:  # cap to avoid spam
                msg_type = "urgent" if alert["severity"] == SEV_CRITICAL else "alert"
                guarded_publish({
                    "sender": "cep_engine",
                    "topic": "orchestrator",
                    "type": msg_type,
                    "content": f"CEP: {alert['message']}",
                })
        except Exception:
            pass
    # signed: gamma

    def run_daemon(self, poll_interval: Optional[float] = None) -> None:
        """Run continuous CEP monitoring loop.

        Polls live data at configured interval, evaluates patterns,
        and publishes alerts. Runs until interrupted.
        """
        interval = poll_interval or self.daemon_cfg.get("poll_interval_s", 5)
        print(f"CEP daemon started (poll every {interval}s). Ctrl+C to stop.")

        cycle = 0
        try:
            while True:
                cycle += 1
                result = self.scan_live()
                ac = result["alert_count"]
                cc = result["critical_count"]

                if ac > 0:
                    marker = " *** CRITICAL ***" if cc > 0 else ""
                    print(
                        f"[{result['timestamp']}] cycle={cycle} "
                        f"events={result['events_ingested']} "
                        f"alerts={ac}{marker}"
                    )
                    for a in result["alerts"]:
                        print(f"  [{a['severity']}] {a['message']}")
                elif cycle % 12 == 0:  # periodic heartbeat every ~60s
                    print(
                        f"[{result['timestamp']}] cycle={cycle} "
                        f"events={result['events_ingested']} "
                        f"windows=({len(self.short_window)}/"
                        f"{len(self.medium_window)}/{len(self.long_window)}) "
                        f"OK"
                    )

                time.sleep(interval)
        except KeyboardInterrupt:
            print(f"\nCEP daemon stopped after {cycle} cycles.")
    # signed: gamma

    def get_history(self, limit: int = 20) -> list[dict]:
        """Return recent detection history."""
        return self._history[-limit:]

    def get_window_stats(self) -> dict:
        """Return current window sizes and event type counts."""
        def _type_counts(window: SlidingWindow) -> dict:
            events = window.get_all()
            counts: dict[str, int] = {}
            for e in events:
                t = e.get("type", "unknown")
                counts[t] = counts.get(t, 0) + 1
            return counts

        return {
            "short": {
                "window_s": self.short_window.window_s,
                "size": len(self.short_window),
                "types": _type_counts(self.short_window),
            },
            "medium": {
                "window_s": self.medium_window.window_s,
                "size": len(self.medium_window),
                "types": _type_counts(self.medium_window),
            },
            "long": {
                "window_s": self.long_window.window_s,
                "size": len(self.long_window),
                "types": _type_counts(self.long_window),
            },
            "tracking": {
                "processing_workers": list(self._worker_processing_since.keys()),
                "idle_workers": list(self._worker_idle_since.keys()),
                "heartbeat_workers": list(self._worker_last_heartbeat.keys()),
            },
        }
    # signed: gamma


# ── CLI ──────────────────────────────────────────────────────────

def _cli():
    parser = argparse.ArgumentParser(
        description="Skynet Complex Event Processing engine"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("scan", help="One-shot pattern scan against live data")

    dm = sub.add_parser("daemon", help="Run continuous CEP monitoring")
    dm.add_argument("--interval", type=float, default=None,
                    help="Poll interval in seconds")

    sub.add_parser("rules", help="Show loaded CEP rules")

    hi = sub.add_parser("history", help="Show detection history")
    hi.add_argument("--limit", type=int, default=20)

    sub.add_parser("windows", help="Show window statistics")

    inj = sub.add_parser("inject", help="Inject a test event")
    inj.add_argument("event_type",
                     choices=["stuck", "dead", "error", "idle", "heartbeat"])
    inj.add_argument("--worker", default="alpha")

    args = parser.parse_args()

    if args.command == "scan":
        engine = CEPEngine()
        result = engine.scan_live()
        print(f"CEP Scan @ {result['timestamp']}")
        print(f"  Events ingested: {result['events_ingested']}")
        print(f"  Windows: short={result['short_window_size']} "
              f"medium={result['medium_window_size']} "
              f"long={result['long_window_size']}")
        print(f"  Alerts: {result['alert_count']} "
              f"({result['critical_count']} critical)")
        if result["alerts"]:
            for a in result["alerts"]:
                print(f"  [{a['severity']}] {a['message']}")
        else:
            print("  No patterns detected.")

    elif args.command == "daemon":
        engine = CEPEngine()
        engine.run_daemon(args.interval)

    elif args.command == "rules":
        rules = _load_rules()
        for name, cfg in rules.get("rules", {}).items():
            enabled = "ON" if cfg.get("enabled") else "OFF"
            sev = cfg.get("severity", "?")
            desc = cfg.get("description", "")
            cool = cfg.get("cooldown_s", "?")
            print(f"  [{enabled}] {name} (severity={sev}, cooldown={cool}s)")
            print(f"       {desc}")

    elif args.command == "history":
        engine = CEPEngine()
        hist = engine.get_history(args.limit)
        if not hist:
            print("No detection history.")
        else:
            print(f"Last {len(hist)} detections:")
            for h in hist:
                print(f"  [{h.get('severity','?')}] {h.get('fired_at','?')} "
                      f"— {h.get('message','?')[:100]}")

    elif args.command == "windows":
        engine = CEPEngine()
        engine.ingest_from_live()
        stats = engine.get_window_stats()
        for name in ("short", "medium", "long"):
            w = stats[name]
            print(f"  {name} ({w['window_s']}s): {w['size']} events")
            for t, c in sorted(w["types"].items()):
                print(f"    {t}: {c}")
        trk = stats["tracking"]
        print(f"  Processing: {trk['processing_workers'] or 'none'}")
        print(f"  Idle: {trk['idle_workers'] or 'none'}")
        print(f"  Heartbeat: {trk['heartbeat_workers'] or 'none'}")

    elif args.command == "inject":
        engine = CEPEngine()
        engine.ingest_from_live()
        now = time.time()
        if args.event_type == "stuck":
            engine._worker_processing_since[args.worker] = now - 200
            engine.ingest({"type": EVENT_STUCK, "worker": args.worker, "epoch": now,
                           "status": "PROCESSING"})
        elif args.event_type == "dead":
            engine.ingest({"type": EVENT_DEAD, "worker": args.worker, "epoch": now})
        elif args.event_type == "error":
            for i in range(6):
                engine.ingest({"type": EVENT_ERROR, "worker": args.worker,
                               "epoch": now - i, "content": f"test error {i}"})
        elif args.event_type == "idle":
            for w in WORKER_NAMES:
                engine._worker_idle_since[w] = now - 400
                engine.ingest({"type": EVENT_IDLE, "worker": w, "epoch": now - 400,
                               "status": "IDLE"})
        elif args.event_type == "heartbeat":
            engine.ingest({"type": EVENT_HEARTBEAT, "worker": args.worker,
                           "epoch": now, "status": "IDLE"})

        alerts = engine.detect_patterns()
        if alerts:
            for a in alerts:
                print(f"  [{a['severity']}] {a['message']}")
        else:
            print("  No patterns triggered.")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: gamma
