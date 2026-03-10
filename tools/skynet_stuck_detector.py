#!/usr/bin/env python3
"""
skynet_stuck_detector.py -- Detects and intervenes when workers get stuck.

Monitors worker state via UIA, tracks state history, and alerts the
orchestrator when workers may need attention.

PHILOSOPHY: Workers in PROCESSING are THINKING, not stuck. Never interrupt
a thinking worker. Only the orchestrator may decide to intervene.

Detection rules:
  - IDLE worker          -> do NOTHING (waiting for tasks is normal)
  - PROCESSING < 15 min  -> do NOTHING (worker is thinking)
  - PROCESSING > 15 min  -> post INFO alert, do NOT interrupt
  - STEERING detected    -> auto-cancel via UIA (this is always a bug)
  - Self-dispatch loop   -> Ctrl+C (only if kill switch enabled)

Kill switch: data/brain_config.json -> stuck_detector.ctrl_c_enabled
  Default: false. Only the orchestrator enables this for emergencies.

Usage:
    python tools/skynet_stuck_detector.py --check       # One-shot check all workers
    python tools/skynet_stuck_detector.py --monitor     # Continuous monitoring (15s)
    python tools/skynet_stuck_detector.py --history     # Show state history
    python tools/skynet_stuck_detector.py --health      # Per-worker health JSON
"""

import argparse
import ctypes
import json
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
HISTORY_FILE = DATA_DIR / "worker_stuck_history.json"
WORKERS_FILE = DATA_DIR / "workers.json"
BRAIN_CONFIG = DATA_DIR / "brain_config.json"
SKYNET_URL = "http://localhost:8420"

# Thresholds
PROCESSING_LONG_S = 900    # 15 minutes in PROCESSING = worth alerting (INFO only)
PROCESSING_INFO_S = 600    # 10 minutes = first INFO alert (no intervention)
MAX_HISTORY = 20            # state entries per worker


def _load_ctrl_c_enabled():
    """Check brain_config.json for stuck_detector.ctrl_c_enabled (default: False)."""
    try:
        cfg = json.loads(BRAIN_CONFIG.read_text(encoding="utf-8"))
        return bool(cfg.get("stuck_detector", {}).get("ctrl_c_enabled", False))
    except Exception:
        return False

user32 = ctypes.windll.user32


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def bus_post(sender, topic, msg_type, content):
    """Post to Skynet bus."""
    try:
        import requests
        requests.post(f"{SKYNET_URL}/bus/publish", json={
            "sender": sender, "topic": topic,
            "type": msg_type, "content": content,
        }, timeout=3)
    except Exception:
        pass


def load_workers():
    """Load registered workers from workers.json."""
    if not WORKERS_FILE.exists():
        return []
    try:
        data = json.loads(WORKERS_FILE.read_text(encoding="utf-8"))
        return data.get("workers", [])
    except Exception:
        return []


def get_worker_state(hwnd):
    """Get worker state via UIA engine."""
    try:
        from tools.uia_engine import get_engine
        return get_engine().get_state(hwnd)
    except Exception:
        return "UNKNOWN"


def cancel_steering(hwnd):
    """Cancel STEERING panel via UIA InvokePattern."""
    try:
        from tools.skynet_dispatch import clear_steering_and_send, load_orch_hwnd
        clear_steering_and_send(hwnd, "", load_orch_hwnd())
        return True
    except Exception as e:
        log(f"Cancel steering failed: {e}", "ERR")
        return False


def send_ctrl_c(hwnd):
    """Send Ctrl+C to a worker window to interrupt stuck commands."""
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    VK_CONTROL = 0x11
    VK_C = 0x43
    try:
        user32.PostMessageW(hwnd, WM_KEYDOWN, VK_CONTROL, 0)
        user32.PostMessageW(hwnd, WM_KEYDOWN, VK_C, 0)
        time.sleep(0.05)
        user32.PostMessageW(hwnd, WM_KEYUP, VK_C, 0)
        user32.PostMessageW(hwnd, WM_KEYUP, VK_CONTROL, 0)
        return True
    except Exception:
        return False


class WorkerTracker:
    """Tracks per-worker state history and detects stuck conditions."""

    def __init__(self, name, hwnd):
        self.name = name
        self.hwnd = hwnd
        self.history = deque(maxlen=MAX_HISTORY)
        self.processing_since = None
        self.last_state_change = time.time()
        self.last_state = None
        self.stuck_count = 0
        self.interventions = []

    def update(self, state):
        """Record a new state observation."""
        now = time.time()
        self.history.append({
            "state": state,
            "ts": datetime.now().isoformat(),
            "epoch": now,
        })

        if state != self.last_state:
            self.last_state_change = now
            if state == "PROCESSING":
                self.processing_since = now
            else:
                self.processing_since = None
            self.last_state = state
        elif state == "PROCESSING" and self.processing_since is None:
            self.processing_since = now

    def diagnose(self):
        """Check for stuck conditions. Returns diagnosis dict or None.

        Rules:
          - IDLE/TYPING/UNKNOWN/None: do NOTHING (valid stable states)
          - PROCESSING < 10 min: do NOTHING (worker is thinking)
          - PROCESSING 10-15 min: INFO alert (no intervention)
          - PROCESSING > 15 min: WARN alert (no intervention, orchestrator decides)
          - STEERING: auto-cancel (always a bug)
        """
        now = time.time()
        state = self.last_state

        # STEERING detection — always actionable
        if state == "STEERING":
            return {
                "condition": "STEERING",
                "severity": "high",
                "duration_s": int(now - self.last_state_change),
                "action": "cancel_steering",
            }

        # IDLE, TYPING, UNKNOWN, None — these are all valid stable states
        if state in ("IDLE", "TYPING", "UNKNOWN", None):
            return None

        # PROCESSING — worker is thinking. Alert after thresholds but NEVER interrupt.
        if state == "PROCESSING" and self.processing_since:
            duration = now - self.processing_since
            if duration > PROCESSING_LONG_S:
                return {
                    "condition": "LONG_TASK",
                    "severity": "info",
                    "duration_s": int(duration),
                    "action": "info_alert",  # NEVER Ctrl+C
                }
            elif duration > PROCESSING_INFO_S:
                return {
                    "condition": "EXTENDED_PROCESSING",
                    "severity": "info",
                    "duration_s": int(duration),
                    "action": "info_alert",  # NEVER Ctrl+C
                }

        return None

    def record_intervention(self, intervention):
        """Record that an intervention was performed."""
        self.interventions.append({
            "type": intervention,
            "ts": datetime.now().isoformat(),
        })
        self.stuck_count += 1

    def to_dict(self):
        """Export tracker state for persistence/API."""
        return {
            "name": self.name,
            "hwnd": self.hwnd,
            "current_state": self.last_state,
            "processing_since": datetime.fromtimestamp(self.processing_since).isoformat() if self.processing_since else None,
            "last_state_change": datetime.fromtimestamp(self.last_state_change).isoformat(),
            "stuck_count": self.stuck_count,
            "history": list(self.history)[-5:],
            "interventions": self.interventions[-5:],
        }


class StuckDetector:
    """Main detector that monitors all workers."""

    ALERT_DEDUP_WINDOW = 300  # suppress same alert for 5 minutes

    def __init__(self):
        self.trackers = {}
        self._last_alerts = {}  # key="worker:condition" -> epoch
        self._init_workers()

    def _init_workers(self):
        """Initialize trackers for all registered workers."""
        workers = load_workers()
        for w in workers:
            name = w["name"]
            hwnd = w["hwnd"]
            if name != "orchestrator" and user32.IsWindowVisible(hwnd):
                self.trackers[name] = WorkerTracker(name, hwnd)

    def check_all(self):
        """Check all workers once. Returns list of issues found."""
        issues = []
        for name, tracker in self.trackers.items():
            if not user32.IsWindowVisible(tracker.hwnd):
                issues.append({
                    "worker": name,
                    "condition": "WINDOW_DEAD",
                    "severity": "critical",
                })
                continue

            state = get_worker_state(tracker.hwnd)
            tracker.update(state)
            diagnosis = tracker.diagnose()

            if diagnosis:
                diagnosis["worker"] = name
                issues.append(diagnosis)
                self._auto_intervene(name, tracker, diagnosis)

        return issues

    def _auto_intervene(self, name, tracker, diagnosis):
        """Perform automatic intervention based on diagnosis.

        CRITICAL RULES:
          - NEVER send Ctrl+C to a PROCESSING worker (they are thinking)
          - STEERING auto-cancel is always allowed
          - DEADLOCKED (self-dispatch) Ctrl+C only if kill switch enabled
          - LONG_TASK / EXTENDED_PROCESSING: info alert only, never interrupt
        """
        condition = diagnosis["condition"]

        # Dedup: suppress same worker+condition alert for ALERT_DEDUP_WINDOW
        dedup_key = f"{name}:{condition}"
        now = time.time()
        last_alert = self._last_alerts.get(dedup_key, 0)
        if now - last_alert < self.ALERT_DEDUP_WINDOW:
            return  # already alerted recently

        if condition == "STEERING":
            log(f"INTERVENTION: {name.upper()} has STEERING -- auto-cancelling", "WARN")
            if cancel_steering(tracker.hwnd):
                tracker.record_intervention("steering_cancelled")
                bus_post("stuck-detector", "orchestrator", "alert",
                         f"STUCK_FIXED: {name.upper()} had STEERING panel -- auto-cancelled")
                self._last_alerts[dedup_key] = now
                time.sleep(3)  # wait before any further action

        elif condition == "LONG_TASK":
            duration = diagnosis["duration_s"]
            minutes = duration // 60
            log(f"INFO: {name.upper()} PROCESSING for {minutes}m -- worker is thinking (no intervention)", "INFO")
            bus_post("stuck-detector", "orchestrator", "info",
                     f"WORKER_LONG_TASK: {name.upper()} has been PROCESSING for {minutes}m -- may need attention")
            self._last_alerts[dedup_key] = now

        elif condition == "EXTENDED_PROCESSING":
            duration = diagnosis["duration_s"]
            minutes = duration // 60
            log(f"INFO: {name.upper()} PROCESSING for {minutes}m -- normal thinking (no intervention)", "INFO")
            # Light info post — not an alert, just awareness
            bus_post("stuck-detector", "orchestrator", "info",
                     f"WORKER_THINKING: {name.upper()} has been PROCESSING for {minutes}m -- this is normal")
            self._last_alerts[dedup_key] = now

        elif condition == "DEADLOCKED":
            ctrl_c_enabled = _load_ctrl_c_enabled()
            if ctrl_c_enabled:
                log(f"INTERVENTION: {name.upper()} DEADLOCKED (self-dispatch) -- breaking loop", "WARN")
                send_ctrl_c(tracker.hwnd)
                tracker.record_intervention("deadlock_broken")
                bus_post("stuck-detector", "orchestrator", "alert",
                         f"DEADLOCK_BROKEN: {name.upper()} was dispatching to itself. "
                         f"Sent Ctrl+C. Needs new task.")
            else:
                log(f"ALERT: {name.upper()} DEADLOCKED but kill switch is OFF -- alerting orchestrator", "WARN")
                bus_post("stuck-detector", "orchestrator", "alert",
                         f"DEADLOCK_DETECTED: {name.upper()} appears to be in a self-dispatch loop. "
                         f"Ctrl+C kill switch is OFF. Orchestrator must intervene manually.")
            self._last_alerts[dedup_key] = now

    def get_health(self):
        """Get per-worker health report."""
        return {name: t.to_dict() for name, t in self.trackers.items()}

    def save_history(self):
        """Persist state history to disk."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "timestamp": datetime.now().isoformat(),
            "workers": self.get_health(),
        }
        HISTORY_FILE.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def monitor(self, interval=15, max_cycles=None):
        """Continuous monitoring loop."""
        ctrl_c = _load_ctrl_c_enabled()
        log(f"Stuck detector started (interval={interval}s, "
            f"info_threshold={PROCESSING_INFO_S}s, "
            f"long_threshold={PROCESSING_LONG_S}s, "
            f"ctrl_c_enabled={ctrl_c})")
        log(f"Tracking {len(self.trackers)} workers: {list(self.trackers.keys())}")
        log(f"POLICY: NEVER interrupt PROCESSING workers. IDLE workers are ignored.")

        cycle = 0
        try:
            while max_cycles is None or cycle < max_cycles:
                issues = self.check_all()
                if issues:
                    for issue in issues:
                        log(f"  {issue['worker'].upper()}: {issue['condition']} "
                            f"(severity={issue['severity']})", "WARN")
                self.save_history()
                time.sleep(interval)
                cycle += 1
        except KeyboardInterrupt:
            log("Stuck detector stopped")
        self.save_history()


def check_self_dispatch(worker_name, target_name):
    """Check if a worker is trying to dispatch to itself. Returns True if self-dispatch."""
    if worker_name and target_name and worker_name.lower() == target_name.lower():
        log(f"SELF-DISPATCH BLOCKED: {worker_name} tried to dispatch to itself", "ERR")
        bus_post("stuck-detector", "orchestrator", "alert",
                 f"SELF_DISPATCH_BLOCKED: {worker_name} tried to dispatch to itself. "
                 f"This creates a deadlock. Task rejected.")
        return True
    return False


def one_shot_check():
    """Run a single check and print results."""
    detector = StuckDetector()
    if not detector.trackers:
        print("No workers found in workers.json")
        return

    issues = detector.check_all()
    health = detector.get_health()

    print(f"Workers checked: {len(detector.trackers)}")
    for name, h in health.items():
        state = h["current_state"] or "UNKNOWN"
        stuck = h["stuck_count"]
        interventions = len(h["interventions"])
        print(f"  {name.upper():12s} state={state:12s} stuck_count={stuck} interventions={interventions}")

    if issues:
        print(f"\nIssues found: {len(issues)}")
        for i in issues:
            print(f"  {i['worker'].upper()}: {i['condition']} (severity={i['severity']})")
    else:
        print("\nAll workers healthy")

    detector.save_history()


def show_history():
    """Show saved state history."""
    if not HISTORY_FILE.exists():
        print("No history yet. Run --check or --monitor first.")
        return
    data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    print(f"Last check: {data.get('timestamp')}")
    for name, h in data.get("workers", {}).items():
        print(f"\n  {name.upper()}:")
        print(f"    State: {h.get('current_state')}")
        print(f"    Stuck count: {h.get('stuck_count', 0)}")
        for entry in h.get("history", []):
            print(f"    [{entry['ts'][-8:]}] {entry['state']}")
        for iv in h.get("interventions", []):
            print(f"    INTERVENTION: {iv['type']} at {iv['ts'][-8:]}")


def get_worker_health_json():
    """Get worker health as JSON (for API endpoint)."""
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"workers": {}, "timestamp": None}


def main():
    parser = argparse.ArgumentParser(description="Skynet Stuck Worker Detector")
    parser.add_argument("--check", action="store_true", help="One-shot check all workers")
    parser.add_argument("--monitor", action="store_true", help="Continuous monitoring")
    parser.add_argument("--history", action="store_true", help="Show state history")
    parser.add_argument("--health", action="store_true", help="Output health JSON")
    parser.add_argument("--interval", type=int, default=15, help="Monitor interval (seconds)")
    args = parser.parse_args()

    if args.check:
        one_shot_check()
    elif args.monitor:
        detector = StuckDetector()
        detector.monitor(interval=args.interval)
    elif args.history:
        show_history()
    elif args.health:
        print(json.dumps(get_worker_health_json(), indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
