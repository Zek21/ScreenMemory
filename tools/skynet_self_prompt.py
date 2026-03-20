#!/usr/bin/env python3
"""
skynet_self_prompt.py -- Orchestrator heartbeat daemon (BUS-ONLY, v2.0).

Monitors workers, bus, and TODOs. When conditions are met (all workers IDLE
for the configured quiet window), posts a structured status summary to the
Skynet bus. The GOD Console dashboard and orchestrator pick this up via
normal bus polling -- NO window typing, NO ghost-type, NO UIA interaction.

INCIDENT 016 FIX: This rewrite removes ALL ghost-type / deliver_to_orchestrator /
type_into_window calls. The daemon communicates EXCLUSIVELY via the Skynet bus
using guarded_publish() from tools.skynet_spam_guard.

Usage:
    python tools/skynet_self_prompt.py start      # run as daemon
    python tools/skynet_self_prompt.py status      # show last prompt info
    python tools/skynet_self_prompt.py once        # single check, post if needed
    python tools/skynet_self_prompt.py stop        # show PID for manual stop
    python tools/skynet_self_prompt.py start --dry-run  # log but don't post to bus
"""

import json
import hashlib
import os
import signal
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

DATA_DIR = ROOT / "data"
PID_FILE = DATA_DIR / "self_prompt.pid"
LOG_FILE = DATA_DIR / "self_prompt_log.json"
LAST_ACTION_FILE = DATA_DIR / "last_orchestrator_action.json"
WORKERS_FILE = DATA_DIR / "workers.json"
TODOS_FILE = DATA_DIR / "todos.json"
BRAIN_CONFIG_FILE = DATA_DIR / "brain_config.json"
HEALTH_FILE = DATA_DIR / "self_prompt_health.json"
BUS_URL = "http://localhost:8420"
DAEMON_NAME = "self_prompt"
DAEMON_VERSION = "2.0.0"

# Defaults (overridden by brain_config.json -> self_prompt section)
LOOP_INTERVAL = 30          # poll interval seconds
MIN_PROMPT_GAP = 300        # min seconds between status posts (rate limit: 1 per 5 min)
ALL_IDLE_INTERVAL = 300     # all workers must be IDLE this long before posting
MAX_LOG_ENTRIES = 50
HEALTH_REPORT_INTERVAL = 300
MAX_CONSECUTIVE_PROMPTS = 3
REPEATED_STATE_SUPPRESSION_S = 900

REQUIRED_WORKERS = ("alpha", "beta", "gamma", "delta")

# Global dry-run flag
DRY_RUN = False


def _load_config_overrides():
    """Load self_prompt thresholds from brain_config.json. Called on startup AND each cycle."""
    global LOOP_INTERVAL, MIN_PROMPT_GAP, ALL_IDLE_INTERVAL
    global HEALTH_REPORT_INTERVAL, MAX_CONSECUTIVE_PROMPTS, REPEATED_STATE_SUPPRESSION_S
    try:
        cfg = json.loads(BRAIN_CONFIG_FILE.read_text(encoding="utf-8"))
        sp = cfg.get("self_prompt", {})
        if sp.get("loop_interval"):
            LOOP_INTERVAL = sp["loop_interval"]
        if sp.get("min_prompt_gap"):
            MIN_PROMPT_GAP = sp["min_prompt_gap"]
        if sp.get("all_idle_interval"):
            ALL_IDLE_INTERVAL = sp["all_idle_interval"]
        if sp.get("health_report_interval"):
            HEALTH_REPORT_INTERVAL = sp["health_report_interval"]
        if sp.get("max_consecutive"):
            MAX_CONSECUTIVE_PROMPTS = sp["max_consecutive"]
        if sp.get("repeated_state_suppression_s"):
            REPEATED_STATE_SUPPRESSION_S = sp["repeated_state_suppression_s"]
    except Exception:
        pass


_load_config_overrides()

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [SELF-PROMPT] [{level}] {msg}", flush=True)


def _fetch_json(url, timeout=5):
    try:
        return json.loads(urllib.request.urlopen(url, timeout=timeout).read())
    except Exception:
        return None


def _post_bus(topic, msg_type, content):
    """Post to bus via guarded_publish. Returns True if allowed."""
    if DRY_RUN:
        log(f"DRY-RUN: would post {{topic={topic}, type={msg_type}, content={content[:120]}}}")
        return True
    msg = {"sender": "self_prompt", "topic": topic, "type": msg_type, "content": content}
    try:
        from tools.skynet_spam_guard import guarded_publish
        result = guarded_publish(msg)
        return result.get("allowed", False)
    except ImportError:
        log("SpamGuard import failed -- skipping bus publish", "ERROR")
        return False
    except Exception as e:
        log(f"Bus publish failed: {e}", "ERROR")
        return False


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _load_workers():
    data = _load_json(WORKERS_FILE)
    if not data:
        return []
    workers = data.get("workers", [])
    return workers if isinstance(workers, list) else []


def _get_pending_todos():
    """Count pending/active TODOs."""
    data = _load_json(TODOS_FILE)
    if not data:
        return 0
    todo_list = data if isinstance(data, list) else data.get("todos", [])
    return sum(1 for t in todo_list if isinstance(t, dict) and t.get("status") in ("pending", "active"))


def _get_pending_todo_items(limit=5):
    """Return top pending TODO items sorted by priority."""
    data = _load_json(TODOS_FILE)
    if not data:
        return []
    todo_list = data if isinstance(data, list) else data.get("todos", [])
    items = [t for t in todo_list if isinstance(t, dict) and t.get("status") in ("pending", "active")]
    priority_rank = {"critical": 0, "high": 1, "normal": 2, "low": 3}
    items.sort(key=lambda t: (
        priority_rank.get(str(t.get("priority", "normal")).lower(), 9),
        str(t.get("created_at", "")),
    ))
    return items[:limit]


def _append_log(entry):
    log_data = _load_json(LOG_FILE) or []
    if not isinstance(log_data, list):
        log_data = []
    log_data.append(entry)
    if len(log_data) > MAX_LOG_ENTRIES:
        log_data = log_data[-MAX_LOG_ENTRIES:]
    _save_json(LOG_FILE, log_data)


def _update_last_action(reason="prompt_sent"):
    _save_json(LAST_ACTION_FILE, {
        "timestamp": datetime.now().isoformat(),
        "reason": reason,
        "t": time.time(),
    })


class SelfPromptDaemon:
    """BUS-ONLY Self-Prompt Daemon.

    Monitors workers, bus, and TODOs. When all workers are IDLE for the
    configured quiet window, posts a status summary to the bus. The
    orchestrator and GOD Console dashboard pick this up via normal polling.

    NEVER imports or calls ghost_type, deliver_to_orchestrator, or any
    function that types into VS Code windows.
    """

    def __init__(self):
        self.last_prompt_time = 0.0
        self.last_health_report = 0.0
        self.last_prompt_state_hash = ""
        self.last_prompt_state_hash_time = 0.0
        self.prompts_sent = 0
        self.prompts_failed = 0
        self.cycles = 0
        self.consecutive_prompts = 0
        self.all_idle_since = 0.0
        self._start_time = time.time()
        self._consecutive_loop_errors = 0
        self._DEGRADED_THRESHOLD = 10

    def _fetch_worker_states(self):
        """Get worker states from backend /status endpoint."""
        status = _fetch_json(f"{BUS_URL}/status")
        if not status:
            return {}
        agents = status.get("agents", {})
        states = {}
        for name in REQUIRED_WORKERS:
            agent = agents.get(name, {})
            states[name] = str(agent.get("status", "UNKNOWN")).upper()
        return states

    def _fetch_bus_summary(self, limit=30):
        """Fetch recent bus messages and extract key info."""
        msgs = _fetch_json(f"{BUS_URL}/bus/messages?limit={limit}")
        if not msgs or not isinstance(msgs, list):
            return {"total": 0, "results": 0, "alerts": 0, "recent_senders": []}
        results = [m for m in msgs if m.get("type") == "result" and m.get("topic") == "orchestrator"]
        alerts = [m for m in msgs
                  if m.get("type") in ("alert", "monitor_alert", "service_alert", "urgent")
                  and m.get("sender") != "self_prompt"]
        senders = list({m.get("sender", "?") for m in msgs if m.get("sender") != "self_prompt"})[:5]
        return {
            "total": len(msgs),
            "results": len(results),
            "alerts": len(alerts),
            "recent_senders": senders,
        }

    def _all_workers_idle(self, states):
        """Check if all required workers are IDLE."""
        if not states:
            return False
        return all(states.get(name, "UNKNOWN") == "IDLE" for name in REQUIRED_WORKERS)

    def _refresh_all_idle_window(self, now, states):
        """Track all-idle window. Returns True if all workers currently IDLE."""
        if self._all_workers_idle(states):
            if not self.all_idle_since:
                self.all_idle_since = now
                log(f"All workers IDLE -- starting quiet window ({int(ALL_IDLE_INTERVAL)}s)")
            return True
        if self.all_idle_since:
            quiet_s = int(now - self.all_idle_since)
            state_str = " ".join(f"{n[0].upper()}={states.get(n, '?')}" for n in sorted(REQUIRED_WORKERS))
            log(f"All-idle window reset after {quiet_s}s ({state_str})")
        self.all_idle_since = 0.0
        return False

    def _all_idle_window_ready(self, now):
        if not self.all_idle_since:
            return False
        return (now - self.all_idle_since) >= max(1.0, float(ALL_IDLE_INTERVAL))

    def _build_status_summary(self, worker_states, bus_summary, pending_todos, todo_items):
        """Build a structured status summary string."""
        parts = []

        # Worker states
        worker_parts = []
        for name in sorted(REQUIRED_WORKERS):
            state = worker_states.get(name, "UNKNOWN")
            worker_parts.append(f"{name[0].upper()}={state}")
        parts.append("Workers: " + " ".join(worker_parts))

        # Bus activity
        parts.append(
            f"Bus: {bus_summary['total']} msgs, "
            f"{bus_summary['results']} results, "
            f"{bus_summary['alerts']} alerts"
        )

        # Pending TODOs
        parts.append(f"TODOs: {pending_todos} pending")

        # Top TODO items
        if todo_items:
            top = todo_items[0]
            pri = str(top.get("priority", "normal")).upper()
            title = str(top.get("title") or top.get("task", "untitled"))[:80]
            parts.append(f"Top TODO [{pri}]: {title}")

        # Dead workers
        dead = [n for n in REQUIRED_WORKERS if worker_states.get(n) == "DEAD"]
        if dead:
            parts.append(f"DEAD WORKERS: {', '.join(d.upper() for d in dead)}")

        # Alerts
        if bus_summary["alerts"] > 0:
            parts.append(f"ACTIVE ALERTS: {bus_summary['alerts']}")

        return " | ".join(parts)

    def _build_state_hash(self, worker_states, bus_summary, pending_todos):
        """Hash the current state to detect changes and suppress duplicates."""
        payload = {
            "workers": {n: worker_states.get(n, "UNKNOWN") for n in sorted(REQUIRED_WORKERS)},
            "results": bus_summary.get("results", 0),
            "alerts": bus_summary.get("alerts", 0),
            "pending_todos": pending_todos,
        }
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hashlib.md5(encoded).hexdigest()[:12]

    def _is_duplicate_state(self, state_hash, now):
        """Suppress posting when the system state hasn't changed."""
        if (
            state_hash == self.last_prompt_state_hash
            and (now - self.last_prompt_state_hash_time) < REPEATED_STATE_SUPPRESSION_S
        ):
            remaining = int(REPEATED_STATE_SUPPRESSION_S - (now - self.last_prompt_state_hash_time))
            log(f"Repeated state suppressed (hash={state_hash}, remaining={remaining}s)")
            return True
        return False

    def _write_health_file(self):
        """Write daemon health to persistent file every cycle."""
        now = time.time()
        health = {
            "daemon": DAEMON_NAME,
            "daemon_version": DAEMON_VERSION,
            "delivery_mode": "bus_only",
            "cycles": self.cycles,
            "sent": self.prompts_sent,
            "failed": self.prompts_failed,
            "last_sent_timestamp": self.last_prompt_time,
            "min_prompt_gap_s": int(MIN_PROMPT_GAP),
            "all_idle_interval_s": int(ALL_IDLE_INTERVAL),
            "all_idle_since_timestamp": float(self.all_idle_since or 0.0),
            "consecutive_prompts": self.consecutive_prompts,
            "max_consecutive": MAX_CONSECUTIVE_PROMPTS,
            "dry_run": DRY_RUN,
            "pid": os.getpid(),
            "uptime_s": int(now - self._start_time),
            "timestamp": datetime.now().isoformat(),
        }
        try:
            _save_json(HEALTH_FILE, health)
        except Exception:
            pass

    def _report_health_to_bus(self):
        """Post health status to bus periodically."""
        now = time.time()
        if now - self.last_health_report < HEALTH_REPORT_INTERVAL:
            return
        self.last_health_report = now
        _post_bus("orchestrator", "daemon_health",
                  f"SELF_PROMPT_HEALTH v{DAEMON_VERSION}: "
                  f"cycles={self.cycles} sent={self.prompts_sent} failed={self.prompts_failed} "
                  f"mode=bus_only dry_run={DRY_RUN}")
        log(f"Health report posted (cycles={self.cycles}, sent={self.prompts_sent})")

    def _orchestrator_took_action(self):
        """Detect if orchestrator dispatched something since last prompt."""
        try:
            dispatch_log = _load_json(DATA_DIR / "dispatch_log.json")
            if dispatch_log:
                entries = dispatch_log if isinstance(dispatch_log, list) else dispatch_log.get("log", [])
                for entry in reversed(entries[-10:]):
                    ts = entry.get("timestamp", "")
                    if ts:
                        try:
                            entry_time = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                            if entry_time > self.last_prompt_time:
                                return True
                        except Exception:
                            pass
        except Exception:
            pass
        return False

    def check_and_post(self):
        """Single check cycle. Posts status to bus if conditions are met. Returns True if posted."""
        self.cycles += 1
        now = time.time()

        self._report_health_to_bus()

        # 1. Fetch live worker states from backend
        worker_states = self._fetch_worker_states()
        if not worker_states:
            log("Cannot reach backend /status -- skipping cycle", "WARN")
            return False

        # 2. All-idle gate: only post when all workers have been IDLE for quiet window
        if not self._refresh_all_idle_window(now, worker_states):
            return False
        if not self._all_idle_window_ready(now):
            return False

        # 3. Rate limiting: max 1 post per MIN_PROMPT_GAP (default 300s = 5 min)
        effective_gap = max(1.0, float(MIN_PROMPT_GAP))
        if now - self.last_prompt_time < effective_gap:
            return False

        # 4. Consecutive prompt limiter
        if self._orchestrator_took_action():
            if self.consecutive_prompts > 0:
                log(f"Orchestrator acted -- resetting consecutive counter ({self.consecutive_prompts} -> 0)")
            self.consecutive_prompts = 0

        if self.consecutive_prompts >= MAX_CONSECUTIVE_PROMPTS:
            cooldown_remaining = effective_gap - (now - self.last_prompt_time)
            if cooldown_remaining > 0:
                log(f"Consecutive limit ({self.consecutive_prompts}/{MAX_CONSECUTIVE_PROMPTS})"
                    f" -- cooldown {int(cooldown_remaining)}s")
                return False
            else:
                self.consecutive_prompts = 0

        # 5. Gather system state
        bus_summary = self._fetch_bus_summary()
        pending_todos = _get_pending_todos()
        todo_items = _get_pending_todo_items(limit=3)

        # 6. Build state hash and check for duplicate state
        state_hash = self._build_state_hash(worker_states, bus_summary, pending_todos)
        if self._is_duplicate_state(state_hash, now):
            return False

        # 7. Build status summary
        summary = self._build_status_summary(worker_states, bus_summary, pending_todos, todo_items)

        # 8. Post to bus
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        content = f"[SELF-PROMPT {stamp}] {summary}"

        log(f"Posting status: {content[:200]}")
        ok = _post_bus("orchestrator", "self_prompt_status", content)

        # 9. Record outcome
        self.last_prompt_time = now
        self.last_prompt_state_hash = state_hash
        self.last_prompt_state_hash_time = now

        _append_log({
            "timestamp": datetime.now().isoformat(),
            "summary": summary[:300],
            "posted": ok,
            "delivery_mode": "bus_only",
            "dry_run": DRY_RUN,
            "state_hash": state_hash,
        })
        _update_last_action("self_prompt_bus_post")

        if ok:
            self.prompts_sent += 1
            self.consecutive_prompts += 1
            self.all_idle_since = now  # reset quiet window after posting
            log(f"Status POSTED to bus (consecutive {self.consecutive_prompts}/{MAX_CONSECUTIVE_PROMPTS})")
        else:
            self.prompts_failed += 1
            log("Status post to bus FAILED", "ERROR")

        return ok

    def run(self):
        """Main daemon loop."""
        # Kill switch check on entry
        try:
            cfg = json.loads(BRAIN_CONFIG_FILE.read_text(encoding="utf-8"))
            if cfg.get("self_prompt", {}).get("enabled") is False:
                log("DAEMON BLOCKED -- self_prompt.enabled=false in brain_config.json")
                return
        except Exception:
            pass

        self._start_time = time.time()
        log(f"Self-prompt daemon v{DAEMON_VERSION} starting (BUS-ONLY, {LOOP_INTERVAL}s poll, dry_run={DRY_RUN})")
        log(f"Config: poll={LOOP_INTERVAL}s gap={MIN_PROMPT_GAP}s idle_window={ALL_IDLE_INTERVAL}s")

        _post_bus("orchestrator", "monitor_alert",
                  f"SELF_PROMPT_ONLINE v{DAEMON_VERSION}: "
                  f"BUS-ONLY heartbeat daemon started (no ghost-type, no window typing)")

        # Prime quiet period
        self.last_prompt_time = time.time()
        log(f"Priming quiet period for {int(MIN_PROMPT_GAP)}s")

        try:
            while True:
                self._main_loop_cycle()
                time.sleep(max(1, float(LOOP_INTERVAL)))
        except KeyboardInterrupt:
            log("Shutting down (Ctrl+C)")
        finally:
            _post_bus("orchestrator", "monitor_alert",
                      f"SELF_PROMPT_OFFLINE v{DAEMON_VERSION}: Heartbeat daemon stopped")
            if PID_FILE.exists():
                try:
                    PID_FILE.unlink()
                except Exception:
                    pass

    def _main_loop_cycle(self):
        """Single iteration of the main daemon loop."""
        # Kill switch: re-check self_prompt.enabled EVERY iteration
        try:
            _kill_cfg = json.loads(BRAIN_CONFIG_FILE.read_text(encoding="utf-8"))
            if _kill_cfg.get("self_prompt", {}).get("enabled") is False:
                return  # silently skip -- daemon stays alive but dormant
        except Exception:
            pass

        try:
            _load_config_overrides()
            self._write_health_file()
            self.check_and_post()
            self._write_health_file()
            self._consecutive_loop_errors = 0
        except (ConnectionError, TimeoutError, OSError) as e:
            self._consecutive_loop_errors += 1
            log(f"Check failed (network, {self._consecutive_loop_errors}x): {e}", "ERROR")
            if self._consecutive_loop_errors % self._DEGRADED_THRESHOLD == 0:
                _post_bus("orchestrator", "alert",
                          f"DAEMON_DEGRADED self_prompt {self._consecutive_loop_errors} consecutive errors: {e}")
        except Exception as e:
            self._consecutive_loop_errors += 1
            log(f"Check failed ({self._consecutive_loop_errors}x): {e}", "ERROR")
            if self._consecutive_loop_errors % self._DEGRADED_THRESHOLD == 0:
                _post_bus("orchestrator", "alert",
                          f"DAEMON_DEGRADED self_prompt {self._consecutive_loop_errors} consecutive errors: {e}")
        finally:
            try:
                self._write_health_file()
            except Exception:
                pass


def _check_existing():
    """Check if daemon is already running."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            from tools.skynet_pid_guard import _pid_alive, _pid_matches_daemon
            if _pid_alive(old_pid) and _pid_matches_daemon(old_pid, "skynet_self_prompt"):
                return old_pid
        except Exception:
            pass
    return None


def _action_status():
    """Show last self-prompt log entry and daemon PID."""
    log_data = _load_json(LOG_FILE)
    health = _load_json(HEALTH_FILE) or {}
    print(f"{DAEMON_NAME} v{health.get('daemon_version', DAEMON_VERSION)} (BUS-ONLY)")
    print(f"Delivery mode: {health.get('delivery_mode', 'bus_only')}")
    if health:
        print(f"Health snapshot: {health.get('timestamp') or 'unknown'}")
        print(f"Cycles: {health.get('cycles', 0)}, Sent: {health.get('sent', 0)}, "
              f"Failed: {health.get('failed', 0)}")
        print(f"Dry run: {health.get('dry_run', False)}")
    if log_data and isinstance(log_data, list) and len(log_data) > 0:
        print(json.dumps(log_data[-1], indent=2))
        print(f"\nTotal entries logged: {len(log_data)}")
    else:
        print("No self-prompt log found.")
    pid = _check_existing()
    print(f"Daemon running: PID {pid}" if pid else "Daemon not running.")


def _action_start(dry_run=False):
    """Start the self-prompt daemon if not already running."""
    global DRY_RUN
    DRY_RUN = dry_run

    # Kill switch check
    try:
        cfg = json.loads(BRAIN_CONFIG_FILE.read_text(encoding="utf-8"))
        sp = cfg.get("self_prompt", {})
        if sp.get("enabled") is False:
            reason = sp.get("disabled_reason", "disabled in brain_config.json")
            print(f"SELF-PROMPT DAEMON DISABLED: {reason}")
            log(f"Daemon start BLOCKED -- enabled=false: {reason}")
            return
    except Exception:
        pass

    # PID guard
    from tools.skynet_pid_guard import acquire_pid_guard
    if not acquire_pid_guard(PID_FILE, "skynet_self_prompt", logger=log):
        existing = _check_existing()
        if existing:
            print(f"Already running (PID {existing}). Use 'status' to check.")
        return

    # Signal handlers for graceful shutdown
    def _sigterm_handler(signum, frame):
        log(f"Received signal {signum} -- requesting graceful shutdown")
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _sigterm_handler)
    try:
        signal.signal(signal.SIGBREAK, _sigterm_handler)
    except (AttributeError, OSError):
        pass

    log(f"Self-prompt daemon v{DAEMON_VERSION} PID {os.getpid()} (BUS-ONLY, dry_run={dry_run})")
    _update_last_action("daemon_start")
    SelfPromptDaemon().run()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skynet Self-Prompt -- BUS-ONLY Orchestrator Heartbeat")
    parser.add_argument("action", nargs="?", default="status",
                        choices=["start", "status", "once", "stop", "version"],
                        help="start=daemon, status=last prompt, once=single check, stop=show PID")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log status summaries but do not post to bus")
    args = parser.parse_args()

    if args.action == "start":
        _action_start(dry_run=args.dry_run)
    elif args.action == "status":
        _action_status()
    elif args.action == "version":
        print(f"{DAEMON_NAME} v{DAEMON_VERSION} (BUS-ONLY)")
    elif args.action == "once":
        global DRY_RUN
        DRY_RUN = args.dry_run
        daemon = SelfPromptDaemon()
        result = daemon.check_and_post()
        print(f"Posted: {result}")
    elif args.action == "stop":
        p = _check_existing()
        print(f"Self-prompt daemon running as PID {p}." if p else "Daemon not running.")


if __name__ == "__main__":
    main()
