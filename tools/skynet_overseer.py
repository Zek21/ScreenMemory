#!/usr/bin/env python3
"""
skynet_overseer.py -- Autonomous monitoring daemon for Skynet.

The EYES of the orchestrator. Runs continuously, monitoring workers,
verifying task delivery, checking service health, and auto-reporting.
Never sleeps. When GOD returns, the orchestrator reads overseer reports.

Subsystems:
  1. Worker Monitoring  (every 30s) -- UIA state scan, stall/stuck detection
  2. Task Delivery      (every 60s) -- dispatch_log vs bus results
  3. Service Health     (every 60s) -- backend, god_console, watchdog
  4. Bus Activity       (every 30s) -- per-worker message counts
  5. Auto-Reporting     (every 5m)  -- full health summary to bus

Usage:
    python tools/skynet_overseer.py start    # run as daemon
    python tools/skynet_overseer.py status   # show last report
    python tools/skynet_overseer.py once     # single scan, print, exit
"""

import argparse
import ctypes
import ctypes.wintypes
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error
from collections import deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

DATA_DIR = ROOT / "data"
WORKERS_FILE = DATA_DIR / "workers.json"
PID_FILE = DATA_DIR / "overseer.pid"
STATUS_FILE = DATA_DIR / "overseer_status.json"
DISPATCH_LOG = DATA_DIR / "dispatch_log.json"
ORCH_FILE = DATA_DIR / "orchestrator.json"
BUS_URL = "http://localhost:8420"
GOD_URL = "http://localhost:8421"

# Thresholds
IDLE_STALL_S = 600       # IDLE >10min with pending TODOs = STALLED (was 120, caused spam)
PROCESSING_STUCK_S = 300 # PROCESSING >5min = POTENTIALLY_STUCK
DELIVERY_TIMEOUT_S = 180 # dispatched >3min with no result = UNDELIVERED
BUS_SILENCE_S = 300      # 0 messages in 5min while PROCESSING = suspicious

# Intervals
WORKER_SCAN_INTERVAL = 30
TASK_VERIFY_INTERVAL = 60
SERVICE_CHECK_INTERVAL = 60
BUS_SCAN_INTERVAL = 30
REPORT_INTERVAL = 300  # 5 minutes

user32 = ctypes.windll.user32

# Ensure UTF-8 output
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [OVERSEER] [{level}] {msg}", flush=True)


def _post_bus(topic, msg_type, content):
    """Post a message to the Skynet bus."""
    try:
        payload = json.dumps({
            "sender": "overseer",
            "topic": topic,
            "type": msg_type,
            "content": content,
        }).encode()
        req = urllib.request.Request(
            f"{BUS_URL}/bus/publish", payload,
            {"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


def _fetch_json(url, timeout=5):
    """Fetch JSON from a URL, return None on failure."""
    try:
        data = urllib.request.urlopen(url, timeout=timeout).read()
        return json.loads(data)
    except Exception:
        return None


def _load_workers():
    """Load worker list from data/workers.json."""
    if not WORKERS_FILE.exists():
        return []
    try:
        data = json.loads(WORKERS_FILE.read_text(encoding="utf-8"))
        workers = data.get("workers", [])
        return workers if isinstance(workers, list) else []
    except Exception:
        return []


def _is_window_alive(hwnd):
    """Check if a window handle is still valid."""
    return bool(user32.IsWindow(int(hwnd)))


def _get_worker_state_uia(hwnd):
    """Get worker state via UIA engine. Returns state string."""
    try:
        from uia_engine import UIAEngine
        engine = UIAEngine()
        result = engine.scan(int(hwnd))
        return getattr(result, "state", "UNKNOWN")
    except Exception:
        return "UNKNOWN"


class OverseerDaemon:
    """The autonomous overseer — monitors everything, reports to bus."""

    def __init__(self, prod_mode=False):
        self.worker_states = {}  # name -> deque of {state, timestamp}
        self.last_bus_results = {}  # name -> last result timestamp
        self.alerts = []  # recent alerts
        self.services = {"backend": "unknown", "god_console": "unknown", "watchdog": "unknown"}
        self.bus_activity = {}  # name -> message count in last 5min
        self.scan_count = 0
        self.start_time = time.time()
        self._bus_cache = []  # cached bus messages
        self._bus_cache_t = 0
        self.prod_mode = prod_mode
        self._last_alert_times = {}  # "worker:issue" -> timestamp (dedup)

    def _get_bus_messages(self, limit=50):
        """Cached bus message fetch (refresh every 10s)."""
        now = time.time()
        if now - self._bus_cache_t > 10:
            msgs = _fetch_json(f"{BUS_URL}/bus/messages?limit={limit}")
            if msgs and isinstance(msgs, list):
                self._bus_cache = msgs
                self._bus_cache_t = now
        return self._bus_cache

    # ── 1. Worker Monitoring ────────────────────────────────────────────

    def scan_workers(self):
        """UIA scan all workers, track state history, detect issues."""
        workers = _load_workers()
        issues = []

        for w in workers:
            name = w.get("name", "?")
            hwnd = w.get("hwnd", 0)

            if not hwnd or not _is_window_alive(hwnd):
                state = "DEAD"
            else:
                state = _get_worker_state_uia(hwnd)

            now_iso = datetime.now().isoformat()

            # Initialize state history
            if name not in self.worker_states:
                self.worker_states[name] = deque(maxlen=10)
            self.worker_states[name].append({"state": state, "ts": now_iso, "t": time.time()})

            # Check for STALLED (IDLE too long WITH pending TODOs — idle without work is normal)
            history = list(self.worker_states[name])
            if len(history) >= 2 and state == "IDLE":
                idle_since = None
                for h in reversed(history):
                    if h["state"] != "IDLE":
                        break
                    idle_since = h["t"]
                if idle_since:
                    idle_duration = time.time() - idle_since
                    last_result = self.last_bus_results.get(name, 0)
                    # Only flag STALLED if worker has pending TODOs (idle without work is expected)
                    has_pending = False
                    try:
                        todos_file = DATA_DIR / "todos.json"
                        if todos_file.exists():
                            tdata = json.loads(todos_file.read_text(encoding="utf-8"))
                            has_pending = any(
                                t.get("worker") == name and t.get("status") in ("pending", "active")
                                for t in tdata.get("todos", [])
                            )
                    except Exception:
                        pass
                    if has_pending and idle_duration > IDLE_STALL_S and (time.time() - last_result) > IDLE_STALL_S:
                        issues.append({
                            "worker": name, "issue": "STALLED",
                            "detail": f"IDLE for {int(idle_duration)}s with pending TODOs and no bus result",
                            "severity": "warning"
                        })

            # Check for STUCK (PROCESSING >5min)
            if state == "PROCESSING":
                proc_since = None
                for h in reversed(history):
                    if h["state"] != "PROCESSING":
                        break
                    proc_since = h["t"]
                if proc_since:
                    proc_duration = time.time() - proc_since
                    if proc_duration > PROCESSING_STUCK_S:
                        issues.append({
                            "worker": name, "issue": "POTENTIALLY_STUCK",
                            "detail": f"PROCESSING for {int(proc_duration)}s",
                            "severity": "critical"
                        })

            # DEAD window
            if state == "DEAD":
                issues.append({
                    "worker": name, "issue": "WINDOW_DEAD",
                    "detail": f"HWND {hwnd} no longer valid",
                    "severity": "critical"
                })

            # Zero Ticket Stop Rule: IDLE worker with pending TODOs
            if state == "IDLE":
                try:
                    todos_file = DATA_DIR / "todos.json"
                    if todos_file.exists():
                        tdata = json.loads(todos_file.read_text(encoding="utf-8"))
                        pending = sum(
                            1 for t in tdata.get("todos", [])
                            if t.get("worker") == name and t.get("status") in ("pending", "active")
                        )
                        if pending > 0:
                            issues.append({
                                "worker": name, "issue": "IDLE_WITH_PENDING_TODOS",
                                "detail": f"IDLE but has {pending} pending/active TODO items",
                                "severity": "warning"
                            })
                except Exception:
                    pass

        # Post alerts for issues (deduplicated: same worker+issue suppressed for 5 min)
        DEDUP_WINDOW = 300  # 5 minutes
        now_t = time.time()
        for issue in issues:
            dedup_key = f"{issue['worker']}:{issue['issue']}"
            last_posted = self._last_alert_times.get(dedup_key, 0)
            if now_t - last_posted < DEDUP_WINDOW:
                continue  # suppress duplicate
            alert_msg = f"[{issue['worker'].upper()}] {issue['issue']}: {issue['detail']}"
            self.alerts.append({"msg": alert_msg, "ts": datetime.now().isoformat(), "severity": issue["severity"]})
            if len(self.alerts) > 50:
                self.alerts = self.alerts[-50:]
            _post_bus("orchestrator", "monitor_alert", alert_msg)
            log(alert_msg, level=issue["severity"].upper())
            self._last_alert_times[dedup_key] = now_t

        return issues

    # ── 2. Task Delivery Verification ───────────────────────────────────

    def verify_deliveries(self):
        """Check dispatch_log for undelivered tasks. Fixes false positives by:
        - Skipping entries older than 10 minutes (stale)
        - Cross-matching bus results by worker name + time proximity
        - Auto-marking matched entries as result_received=True
        """
        if not DISPATCH_LOG.exists():
            return []

        try:
            entries = json.loads(DISPATCH_LOG.read_text(encoding="utf-8"))
        except Exception:
            return []

        if not isinstance(entries, list):
            return []

        # Get bus results to match against
        bus_msgs = self._get_bus_messages(100)
        # Build per-worker result timestamps for proximity matching
        worker_result_times = {}  # worker -> [timestamp_epoch, ...]
        for m in bus_msgs:
            if m.get("type") == "result" and m.get("topic") == "orchestrator":
                sender = m.get("sender", "")
                ts_str = m.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    worker_result_times.setdefault(sender, []).append(ts.timestamp())
                except Exception:
                    worker_result_times.setdefault(sender, []).append(time.time())

        undelivered = []
        now = time.time()
        modified = False
        MAX_AGE = 600  # 10 minutes -- older entries are stale, skip them

        for entry in entries[-20:]:
            if not entry.get("success"):
                continue
            if entry.get("result_received"):
                continue

            ts_str = entry.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str)
                dispatch_epoch = ts.timestamp()
                age_s = now - dispatch_epoch
            except Exception:
                continue

            # Skip stale entries (>10 min old) -- auto-mark as expired
            if age_s > MAX_AGE:
                entry["result_received"] = True
                entry["received_at"] = "expired"
                modified = True
                continue

            worker = entry.get("worker", "?")

            # Check if worker posted a result AFTER this dispatch
            result_times = worker_result_times.get(worker, [])
            matched = any(rt > dispatch_epoch for rt in result_times)

            if matched:
                entry["result_received"] = True
                entry["received_at"] = datetime.now().isoformat()
                modified = True
                continue

            # Only flag if >3 min old and no matching result
            if age_s > DELIVERY_TIMEOUT_S:
                undelivered.append({
                    "worker": worker,
                    "task": str(entry.get("task_summary", "?"))[:60],
                    "age_min": round(age_s / 60, 1),
                })

        # Persist matched entries back to dispatch_log
        if modified:
            try:
                DISPATCH_LOG.write_text(json.dumps(entries, indent=2, default=str), encoding="utf-8")
            except Exception:
                pass

        if undelivered:
            names = [f"{u['worker']}({u['age_min']}m)" for u in undelivered]
            alert = f"UNDELIVERED: {len(undelivered)} task(s) without result: {', '.join(names)}"
            _post_bus("orchestrator", "delivery_alert", alert)
            log(alert, level="WARNING")
            self.alerts.append({"msg": alert, "ts": datetime.now().isoformat(), "severity": "warning"})

        return undelivered

    # ── 3. Service Health ───────────────────────────────────────────────

    def check_services(self):
        """Check all critical services are alive."""
        issues = []

        # Backend (port 8420)
        backend = _fetch_json(f"{BUS_URL}/health")
        if backend and backend.get("status") == "ok":
            self.services["backend"] = "ok"
        else:
            self.services["backend"] = "DOWN"
            issues.append("Skynet backend (8420) DOWN")

        # GOD Console (port 8421)
        god = _fetch_json(f"{GOD_URL}/health")
        if god and god.get("status") == "ok":
            self.services["god_console"] = "ok"
        else:
            self.services["god_console"] = "DOWN"
            issues.append("GOD Console (8421) DOWN")

        # Watchdog (PID file)
        watchdog_pid_file = DATA_DIR / "watchdog.pid"
        if watchdog_pid_file.exists():
            try:
                wpid = int(watchdog_pid_file.read_text().strip())
                # Check if PID is alive via tasklist
                out = subprocess.check_output(
                    ["tasklist", "/fi", f"pid eq {wpid}", "/fo", "csv", "/nh"],
                    text=True, timeout=5, stderr=subprocess.DEVNULL
                )
                if str(wpid) in out:
                    self.services["watchdog"] = "ok"
                else:
                    self.services["watchdog"] = "DOWN"
                    issues.append(f"Watchdog PID {wpid} not running")
            except Exception:
                self.services["watchdog"] = "unknown"
        else:
            self.services["watchdog"] = "no_pidfile"

        # Post critical alerts
        for issue in issues:
            alert = f"CRITICAL SERVICE: {issue}"
            _post_bus("orchestrator", "service_alert", alert)
            log(alert, level="CRITICAL")
            self.alerts.append({"msg": alert, "ts": datetime.now().isoformat(), "severity": "critical"})

        return issues

    # ── 4. Bus Activity ─────────────────────────────────────────────────

    def scan_bus_activity(self):
        """Count per-worker bus messages in last 5 minutes."""
        msgs = self._get_bus_messages(100)
        now = time.time()
        worker_names = {"alpha", "beta", "gamma", "delta"}
        counts = {w: 0 for w in worker_names}

        for m in msgs:
            sender = m.get("sender", "")
            if sender in worker_names:
                ts_str = m.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    age = now - ts.timestamp()
                    if age < BUS_SILENCE_S:
                        counts[sender] = counts.get(sender, 0) + 1
                except Exception:
                    pass
                # Track last result timestamp
                if m.get("type") == "result":
                    self.last_bus_results[sender] = now

        self.bus_activity = counts

        # Flag workers processing but silent on bus
        issues = []
        for name, count in counts.items():
            if name in self.worker_states:
                history = list(self.worker_states[name])
                if history and history[-1]["state"] == "PROCESSING" and count == 0:
                    issues.append(f"{name.upper()} is PROCESSING but 0 bus messages in 5min")

        for issue in issues:
            log(issue, level="WARNING")

        return counts

    # ── 5. Auto-Reporting ───────────────────────────────────────────────

    def generate_report(self):
        """Generate and post a full health summary."""
        self.scan_count += 1
        uptime = int(time.time() - self.start_time)

        # Worker states summary
        worker_summary = {}
        for name, history in self.worker_states.items():
            h = list(history)
            worker_summary[name] = {
                "current_state": h[-1]["state"] if h else "UNKNOWN",
                "last_seen": h[-1]["ts"] if h else None,
                "state_changes": len(set(x["state"] for x in h)),
            }

        # Recent alerts
        recent_alerts = [a["msg"] for a in self.alerts[-5:]]

        report = {
            "timestamp": datetime.now().isoformat(),
            "uptime_s": uptime,
            "scan_count": self.scan_count,
            "workers": worker_summary,
            "services": self.services.copy(),
            "bus_activity": self.bus_activity.copy(),
            "recent_alerts": recent_alerts,
            "alert_count": len(self.alerts),
        }

        # Save to disk
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATUS_FILE.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

        # Post to bus
        summary_parts = []
        for name, info in worker_summary.items():
            summary_parts.append(f"{name}={info['current_state']}")
        svc_parts = [f"{k}={v}" for k, v in self.services.items()]
        content = (
            f"HEALTH_REPORT: Workers: {', '.join(summary_parts)}. "
            f"Services: {', '.join(svc_parts)}. "
            f"Alerts: {len(self.alerts)}. "
            f"Bus activity: {self.bus_activity}. "
            f"Uptime: {uptime}s, Scans: {self.scan_count}"
        )
        _post_bus("orchestrator", "health_report", content)
        log(f"Health report posted (scan #{self.scan_count})")

        return report

    # ── Main Loop ───────────────────────────────────────────────────────

    def run(self):
        """Main daemon loop. Never returns."""
        log("Overseer daemon starting")
        _post_bus("orchestrator", "monitor_alert", "OVERSEER_ONLINE: Autonomous monitoring daemon started")

        last_worker_scan = 0.0
        last_task_verify = 0.0
        last_service_check = 0.0
        last_bus_scan = 0.0
        last_report = 0.0
        last_heartbeat = 0.0
        HEARTBEAT_INTERVAL = 60

        try:
            while True:
                now = time.time()

                # Worker monitoring (30s)
                if now - last_worker_scan >= WORKER_SCAN_INTERVAL:
                    try:
                        self.scan_workers()
                    except Exception as e:
                        log(f"Worker scan failed: {e}", level="ERROR")
                    last_worker_scan = now

                # Task delivery verification (60s)
                if now - last_task_verify >= TASK_VERIFY_INTERVAL:
                    try:
                        self.verify_deliveries()
                    except Exception as e:
                        log(f"Delivery verify failed: {e}", level="ERROR")
                    last_task_verify = now

                # Service health (60s)
                if now - last_service_check >= SERVICE_CHECK_INTERVAL:
                    try:
                        self.check_services()
                    except Exception as e:
                        log(f"Service check failed: {e}", level="ERROR")
                    last_service_check = now

                # Bus activity (30s)
                if now - last_bus_scan >= BUS_SCAN_INTERVAL:
                    try:
                        self.scan_bus_activity()
                    except Exception as e:
                        log(f"Bus scan failed: {e}", level="ERROR")
                    last_bus_scan = now

                # Auto-report (5 minutes)
                if now - last_report >= REPORT_INTERVAL:
                    try:
                        self.generate_report()
                    except Exception as e:
                        log(f"Report failed: {e}", level="ERROR")
                    last_report = now

                # Heartbeat (60s)
                if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                    uptime = int(now - self.start_time)
                    _post_bus("overseer", "heartbeat",
                              f"ALIVE pid={os.getpid()} uptime={uptime}s scans={self.scan_count} alerts={len(self.alerts)}")
                    last_heartbeat = now

                time.sleep(5)  # base tick

        except KeyboardInterrupt:
            log("Overseer shutting down (Ctrl+C)")
        finally:
            _post_bus("orchestrator", "monitor_alert", "OVERSEER_OFFLINE: Daemon stopped")
            if PID_FILE.exists():
                try:
                    PID_FILE.unlink()
                except Exception:
                    pass

    def run_once(self):
        """Single scan cycle, print results, exit."""
        log("Running single scan...")
        self.scan_workers()
        self.check_services()
        self.scan_bus_activity()
        self.verify_deliveries()
        report = self.generate_report()
        print(json.dumps(report, indent=2, default=str))
        return report


def _check_existing():
    """Check if overseer is already running."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            out = subprocess.check_output(
                ["tasklist", "/fi", f"pid eq {old_pid}", "/fo", "csv", "/nh"],
                text=True, timeout=5, stderr=subprocess.DEVNULL
            )
            if str(old_pid) in out and "python" in out.lower():
                return old_pid
        except Exception:
            pass
    return None


def main():
    parser = argparse.ArgumentParser(description="Skynet Overseer -- Autonomous Monitor Daemon")
    parser.add_argument("action", nargs="?", default="status",
                        choices=["start", "status", "once", "stop", "guardian"],
                        help="start=daemon, status=show last report, once=single scan, guardian=auto-restart wrapper")
    parser.add_argument("--prod", action="store_true",
                        help="Production mode: nudge orchestrator when workers idle with pending results")
    args = parser.parse_args()

    if args.action == "status":
        if STATUS_FILE.exists():
            data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            print(json.dumps(data, indent=2))
        else:
            print("No overseer status file found. Run 'start' or 'once' first.")
        return

    if args.action == "once":
        daemon = OverseerDaemon()
        daemon.run_once()
        return

    if args.action == "stop":
        pid = _check_existing()
        if pid:
            print(f"Overseer running as PID {pid}. Post to bus to request shutdown.")
        else:
            print("Overseer not running.")
        return

    if args.action == "guardian":
        # Auto-restart wrapper: runs overseer, restarts on crash with backoff
        log("Guardian mode: will restart overseer on crash")
        backoff = 5
        max_backoff = 120
        while True:
            existing = _check_existing()
            if existing:
                log(f"Overseer running (PID {existing}), guardian watching...")
                while _check_existing():
                    time.sleep(30)
                log("Overseer process died! Restarting...", level="WARNING")
                _post_bus("orchestrator", "monitor_alert",
                          f"OVERSEER_CRASHED: Guardian restarting after {backoff}s backoff")

            time.sleep(backoff)
            log(f"Starting overseer (backoff={backoff}s)")
            start_args = [sys.executable, __file__, "start"]
            if args.prod:
                start_args.append("--prod")
            proc = subprocess.Popen(
                start_args,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x00000008  # DETACHED_PROCESS
            )
            log(f"Overseer started as PID {proc.pid}")
            backoff = min(backoff * 2, max_backoff)
            time.sleep(10)  # let it initialize
            if _check_existing():
                backoff = 5  # reset on successful start
        return

    if args.action == "start":
        existing = _check_existing()
        if existing:
            print(f"Overseer already running (PID {existing}). Use 'status' to check.")
            return

        # Write PID file
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))

        log(f"Overseer daemon PID {os.getpid()}" + (" [PROD MODE]" if args.prod else ""))
        daemon = OverseerDaemon(prod_mode=args.prod)
        daemon.run()


if __name__ == "__main__":
    main()
