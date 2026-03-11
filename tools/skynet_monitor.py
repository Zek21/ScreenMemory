#!/usr/bin/env python3
"""
Skynet Monitor Daemon — real window health monitoring for worker chat windows.

Runs as a detached background process. Every cycle it:
  1. Checks each worker HWND: IsWindow + IsVisible  (every 10s)
  2. Checks model via UIA button text — must be Opus 4.6 fast (every 60s)
  3. POSTs heartbeat to /worker/{name}/heartbeat with real health data
  4. On model drift: auto-corrects (type 'fast' -> Down+Enter)
  5. On dead window: posts CRITICAL alert to bus
  6. Writes data/worker_health.json for snapshot visibility

Usage:
    python tools/skynet_monitor.py            # start monitor (blocking)
    python tools/skynet_monitor.py --once     # single health check and exit
    python tools/skynet_monitor.py --status   # print current health.json
"""

import argparse
import ctypes
import ctypes.wintypes
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from tools.skynet_metrics import SkynetMetrics
DATA_DIR = ROOT / "data"
WORKERS_FILE = DATA_DIR / "workers.json"
HEALTH_FILE = DATA_DIR / "worker_health.json"
PID_FILE = DATA_DIR / "monitor.pid"
SKYNET_URL = "http://localhost:8420"

# Resolve real Python interpreter to avoid venv trampoline double-process.
def _resolve_real_python():
    """Return (real_python_path, env_dict) bypassing the venv trampoline."""
    venv_dir = ROOT.parent / "env"
    cfg = venv_dir / "pyvenv.cfg"
    base_python = None
    if cfg.exists():
        for line in cfg.read_text().splitlines():
            if line.strip().startswith("executable"):
                _, _, val = line.partition("=")
                candidate = val.strip()
                if Path(candidate).exists():
                    base_python = candidate
                    break
    if not base_python:
        base_python = str(Path(sys.executable))
    import os as _os
    env = _os.environ.copy()
    site_packages = str(venv_dir / "Lib" / "site-packages")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{site_packages};{existing}" if existing else site_packages
    env["VIRTUAL_ENV"] = str(venv_dir)
    return base_python, env

_REAL_PYTHON, _DAEMON_ENV = _resolve_real_python()

_metrics = None
def metrics():
    global _metrics
    _metrics = _metrics or SkynetMetrics()
    return _metrics

HWND_CHECK_INTERVAL = 10    # seconds between window alive checks
MODEL_CHECK_INTERVAL = 60   # seconds between model checks
ORCH_MODEL_CHECK_INTERVAL = 30  # orchestrator checked more frequently (security-critical)
STUCK_PROCESSING_THRESHOLD = 180  # seconds in PROCESSING before auto-recovery attempt
STUCK_DEDUP_WINDOW = 300          # suppress duplicate stuck alerts for 5 minutes

user32 = ctypes.windll.user32

# ─── Lessons learned: known failure modes ───────────────────────────────────
# MISTAKE 1: Using PostMessage for model picker click WITHOUT SetForegroundWindow first
#   -> SendKeys goes to wrong window. FIX: SetForegroundWindow before click+SendKeys
# MISTAKE 2: Searching UIA list items for 'Opus.*fast' — VS Code uses different text per session
#   -> FIX: type 'fast' in quickpick search box then Down+Enter (keyboard-driven, reliable)
# MISTAKE 3: Using bottom row y=550 with h=520 -> y+h=1070 overlaps taskbar
#   -> FIX: bottom row y=540, h=500 -> y+h=1040, 40px taskbar clearance
# MISTAKE 4: workers.json flat dict format breaks skynet_dispatch.py which expects {"workers":[...]}
#   -> FIX: always use {"workers": [...], "orchestrator_hwnd": N} format
# MISTAKE 5: Closing existing sessions to "clean up" — destroys chat context permanently
#   -> FIX: NEVER close worker windows. Only restore via SESSIONS panel right-click.
# MISTAKE 6: em-dash (—) in PS1 string literals without UTF-8 BOM causes parser errors
#   -> FIX: use -- (double hyphen) in PS1 strings instead of — (em-dash)
# ─────────────────────────────────────────────────────────────────────────────


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"INFO": "[INFO]", "OK": "[OK]  ", "WARN": "[WARN]", "ERR": "[ERR] ", "CRIT": "[CRIT]", "FIX": "[FIX] "}.get(level, "     ")
    line = f"[{ts}] {prefix} {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode(), flush=True)


def load_workers():
    with open(WORKERS_FILE) as f:
        data = json.load(f)
    return data.get("workers", []), data.get("orchestrator_hwnd", 0)


def skynet_post(path: str, body: dict) -> dict | None:
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{SKYNET_URL}{path}", data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"POST {path} failed: {e}", "WARN")
        return None


def check_window(hwnd: int) -> tuple[bool, bool]:
    """Returns (is_window, is_visible)."""
    return bool(user32.IsWindow(hwnd)), bool(user32.IsWindowVisible(hwnd))


# Debounce state: track consecutive DEAD checks per worker to suppress transient false positives
_dead_consecutive: dict[str, int] = {}
DEAD_DEBOUNCE_THRESHOLD = 3  # must fail 3 consecutive checks before reporting DEAD

# Alert dedup: suppress repeated identical alerts per worker
_last_alert: dict[str, float] = {}  # worker_name -> last alert timestamp
ALERT_DEDUP_WINDOW = 300  # suppress same DEAD alert for 5 minutes after first post

# Dispatch lock file path — suppress DEAD alerts during active dispatch
_DISPATCH_LOCK_FILE = DATA_DIR / "dispatch_active.lock"

def _is_dispatch_active() -> bool:
    """Check if a dispatch is currently in progress (lock file exists and is fresh)."""
    try:
        if _DISPATCH_LOCK_FILE.exists():
            import json as _json
            data = _json.loads(_DISPATCH_LOCK_FILE.read_text(encoding="utf-8"))
            ts = data.get("timestamp", "")
            if ts:
                from datetime import datetime as _dt
                lock_time = _dt.fromisoformat(ts)
                age = (_dt.now() - lock_time).total_seconds()
                return age < 15  # dispatch lock valid for 15 seconds
    except Exception:
        pass
    return False


def get_model_via_uia(hwnd: int) -> str:
    """Read the Pick Model button text via COM-based UIA engine. Returns model name or ''."""
    from tools.uia_engine import get_engine
    return get_engine().scan(hwnd).model


def get_model_and_agent_uia(hwnd: int) -> tuple:
    """Read both model and delegate/agent label from a window. Returns (model_str, agent_str)."""
    from tools.uia_engine import get_engine
    r = get_engine().scan(hwnd)
    return (r.model, r.agent)


def is_model_correct(model_str: str) -> bool:
    """Check if model string indicates Claude Opus 4.6 (fast mode)."""
    lower = model_str.lower()
    return "opus" in lower and "fast" in lower


def is_agent_cli(agent_str: str) -> bool:
    """Check if delegate/agent label is Copilot CLI."""
    return "copilot cli" in agent_str.lower()


def fix_model_via_uia(hwnd: int, render_hwnd: int) -> bool:
    """Fix model drift using the standalone model guard tool.
    render_hwnd is ignored (kept for backward compatibility).
    """
    try:
        from tools.skynet_model_guard import fix_model
        result = fix_model(hwnd)
        if "FIXED" in result or "GUARD_OK" in result:
            return True
        log(f"fix_model_via_uia: model_guard returned '{result}'", "WARN")
        return False
    except Exception as e:
        log(f"fix_model_via_uia failed: {e}", "ERR")
        return False


def restore_orchestrator_focus(orch_hwnd: int):
    if orch_hwnd:
        user32.SetForegroundWindow(orch_hwnd)


def write_health(health: dict):
    health["updated"] = datetime.now().isoformat()
    DATA_DIR.mkdir(exist_ok=True)
    with open(HEALTH_FILE, "w") as f:
        json.dump(health, f, indent=2)
    try: metrics().record_worker_health({k: v.get("status", "?") for k, v in health.items() if isinstance(v, dict)})
    except Exception as e: log(f"Failed to record worker health metrics: {e}", "WARN")


def run_check(workers: list, orch_hwnd: int, check_model: bool = False, check_orch: bool = False) -> dict:
    health = {}
    any_bad = False

    # ── Check orchestrator model+agent (security-critical) ──────────────────
    if check_orch and orch_hwnd:
        model_str, agent_str = get_model_and_agent_uia(orch_hwnd)
        orch_ok = is_model_correct(model_str) and is_agent_cli(agent_str)
        health["orchestrator"] = {
            "hwnd": orch_hwnd, "model": model_str, "agent": agent_str,
            "ok": orch_ok, "checked_at": datetime.now().isoformat()
        }
        if not orch_ok:
            issues = []
            if not is_model_correct(model_str):
                issues.append(f"model='{model_str}' (expected Opus 4.6 fast)")
            if not is_agent_cli(agent_str):
                issues.append(f"agent='{agent_str}' (expected Copilot CLI)")
            issue_text = "; ".join(issues)
            log(f"ORCHESTRATOR DRIFT: {issue_text} -- auto-correcting", "CRIT")
            skynet_post("/bus/publish", {"sender": "monitor", "topic": "workers", "type": "alert",
                "content": f"SECURITY: Orchestrator drift detected: {issue_text}. Workers: verify and report."})
            # Auto-fix model
            if not is_model_correct(model_str):
                fixed = fix_model_via_uia(orch_hwnd, 0)
                if fixed:
                    time.sleep(1)
                    model_str2, _ = get_model_and_agent_uia(orch_hwnd)
                    log(f"Orchestrator model after fix: '{model_str2}'", "OK" if is_model_correct(model_str2) else "WARN")
                    health["orchestrator"]["model"] = model_str2
                    health["orchestrator"]["ok"] = is_model_correct(model_str2) and is_agent_cli(agent_str)
                    skynet_post("/bus/publish", {"sender": "monitor", "topic": "workers", "type": "report",
                        "content": f"Orchestrator model fixed: '{model_str2}'"})
        else:
            log(f"ORCHESTRATOR: OK (model=Opus fast, agent=CLI)", "OK")
            try: metrics().record_model_guard("orchestrator", model_str, agent_str, orch_ok)
            except Exception as e: log(f"Failed to record orchestrator model guard metrics: {e}", "WARN")

    # ── Check workers ───────────────────────────────────────────────────────

    for w in workers:
        name = w["name"]
        hwnd = w["hwnd"]
        h = {"name": name, "hwnd": hwnd, "slot": w.get("grid", "?"), "checked_at": datetime.now().isoformat()}

        if hwnd == 0:
            h.update({"alive": False, "visible": False, "model": "N/A", "status": "NO_HWND"})
            log(f"{name.upper()}: no HWND — needs new-chat", "CRIT")
            skynet_post(f"/worker/{name}/heartbeat", {"hwnd_alive": False, "visible": False, "model": ""})
            skynet_post("/bus/publish", {"sender": "monitor", "topic": "orchestrator", "type": "alert",
                "content": f"WORKER {name.upper()} has no HWND -- needs new-chat spawn"})
            health[name] = h
            any_bad = True
            continue

        alive, visible = check_window(hwnd)
        h["alive"] = alive
        h["visible"] = visible

        if not alive or not visible:
            # Debounce: suppress during active dispatch and require 3 consecutive failures
            if _is_dispatch_active():
                log(f"{name.upper()}: visibility check failed but dispatch active -- suppressed", "WARN")
                _dead_consecutive[name] = 0  # reset counter during dispatch
                h.update({"model": "CHECKING", "status": "DISPATCH_ACTIVE"})
                health[name] = h
                continue

            _dead_consecutive[name] = _dead_consecutive.get(name, 0) + 1
            consecutive = _dead_consecutive[name]

            if consecutive < DEAD_DEBOUNCE_THRESHOLD:
                log(f"{name.upper()}: visibility check failed ({consecutive}/{DEAD_DEBOUNCE_THRESHOLD}) -- debouncing", "WARN")
                h.update({"model": "CHECKING", "status": f"DEBOUNCE_{consecutive}"})
                health[name] = h
                continue

            # Confirmed DEAD after 3 consecutive failures
            h.update({"model": "UNKNOWN", "status": "DEAD"})
            log(f"{name.upper()}: DEAD (hwnd={hwnd} alive={alive} visible={visible}, consecutive={consecutive})", "CRIT")
            skynet_post(f"/worker/{name}/heartbeat", {"hwnd_alive": False, "visible": False, "model": ""})
            # Alert dedup: only post bus alert once per ALERT_DEDUP_WINDOW
            now_ts = time.time()
            last_ts = _last_alert.get(name, 0)
            if (now_ts - last_ts) >= ALERT_DEDUP_WINDOW:
                skynet_post("/bus/publish", {"sender": "monitor", "topic": "orchestrator", "type": "alert",
                    "content": f"WORKER {name.upper()} DEAD -- hwnd={hwnd} alive={alive} visible={visible} (confirmed {consecutive}x)"})
                _last_alert[name] = now_ts
            else:
                log(f"{name.upper()}: DEAD alert suppressed (dedup window {ALERT_DEDUP_WINDOW}s)", "WARN")
            health[name] = h
            any_bad = True
            continue

        # Window is alive -- reset debounce counter
        _dead_consecutive[name] = 0
        model_name = ""
        model_ok = True

        if check_model:
            model_name, agent_name = get_model_and_agent_uia(hwnd)
            model_ok = is_model_correct(model_name) and is_agent_cli(agent_name)
            h["model"] = model_name
            h["agent"] = agent_name

            if not model_ok:
                issues = []
                if not is_model_correct(model_name):
                    issues.append(f"model='{model_name}'")
                if not is_agent_cli(agent_name):
                    issues.append(f"agent='{agent_name}'")
                log(f"{name.upper()}: DRIFT detected {'; '.join(issues)} -- auto-correcting", "FIX")
                skynet_post("/bus/publish", {"sender": "monitor", "topic": "orchestrator", "type": "alert",
                    "content": f"WORKER {name.upper()} drift: {'; '.join(issues)} -- auto-correcting to Opus fast"})
                fixed = fix_model_via_uia(hwnd, 0)
                if fixed:
                    # Re-read
                    time.sleep(1)
                    model_name, agent_name = get_model_and_agent_uia(hwnd)
                    model_ok = is_model_correct(model_name)
                    log(f"{name.upper()}: after fix model='{model_name}' agent='{agent_name}'", "OK" if model_ok else "WARN")
                    h["model"] = model_name
                    h["agent"] = agent_name
                    skynet_post("/bus/publish", {"sender": "monitor", "topic": "orchestrator", "type": "report",
                        "content": f"WORKER {name.upper()} fixed: model='{model_name}' agent='{agent_name}'"})
        else:
            h["model"] = "unchecked"
            h["agent"] = "unchecked"

        h["status"] = "OK" if model_ok else "MODEL_WRONG"
        if model_ok:
            log(f"{name.upper()}: OK (hwnd={hwnd} model={'checked' if check_model else 'skip'})", "OK")
        else:
            any_bad = True

        if check_model:
            try: metrics().record_model_guard(name, model_name, agent_name, model_ok, fixed=bool(not model_ok and locals().get('fixed')))
            except Exception as e: log(f"Failed to record model guard metrics for {name}: {e}", "WARN")

        # POST heartbeat to Skynet — this keeps workers_alive accurate
        skynet_post(f"/worker/{name}/heartbeat", {
            "hwnd_alive": True, "visible": True,
            "model": model_name or "Claude Opus 4.6 (fast mode)", "grid_slot": w.get("grid", "")
        })

        health[name] = h

    # Restore orchestrator focus after UIA work
    if check_model and orch_hwnd:
        restore_orchestrator_focus(orch_hwnd)

    return health


def _collect_intelligence_metrics() -> dict:
    """Collect intelligence health metrics: knowledge flow and convene sessions."""
    knowledge_count = 0
    active_convenes = 0
    try:
        req = urllib.request.Request(f"{SKYNET_URL}/bus/messages?topic=knowledge&limit=100")
        with urllib.request.urlopen(req, timeout=5) as r:
            msgs = json.loads(r.read())
            knowledge_count = len(msgs) if isinstance(msgs, list) else 0
    except Exception:
        pass
    try:
        req = urllib.request.Request(f"{SKYNET_URL}/bus/messages?topic=convene&limit=100")
        with urllib.request.urlopen(req, timeout=5) as r:
            msgs = json.loads(r.read())
            active_convenes = sum(1 for m in msgs if isinstance(m, dict) and m.get("type") == "request") if isinstance(msgs, list) else 0
    except Exception:
        pass
    return {
        "knowledge_messages": knowledge_count,
        "active_convenes": active_convenes,
        "timestamp": datetime.now().isoformat(),
    }


# ─── Stuck worker detection state ───────────────────────────────────────────
_processing_since: dict[str, float] = {}   # worker_name -> epoch when PROCESSING started
_stuck_alert_last: dict[str, float] = {}   # worker_name -> epoch of last stuck alert (dedup)


def _get_worker_state(hwnd: int) -> str:
    """Get worker UIA state (IDLE/PROCESSING/STEERING/TYPING/UNKNOWN)."""
    try:
        from tools.uia_engine import get_engine
        return get_engine().get_state(hwnd)
    except Exception:
        return "UNKNOWN"


def _cancel_generation(hwnd: int) -> bool:
    """Cancel a stuck generation via UIA engine cancel_generation."""
    try:
        from tools.uia_engine import get_engine
        get_engine().cancel_generation(hwnd)
        return True
    except Exception as e:
        log(f"cancel_generation failed: {e}", "ERR")
        return False


def _check_stuck_workers(workers: list):
    """Detect workers stuck in PROCESSING > threshold and attempt auto-recovery.

    Logic:
      1. If PROCESSING > 180s: cancel generation, wait 3s, verify IDLE.
         On success: post AUTO_RECOVERED alert.
         On failure: post POTENTIALLY_STUCK alert (existing behavior).
      2. UNRESPONSIVE (dead window) detection is handled in run_check() -- untouched.
    """
    now = time.time()
    for w in workers:
        name = w["name"]
        hwnd = w["hwnd"]
        if hwnd == 0 or not user32.IsWindow(hwnd):
            _processing_since.pop(name, None)
            continue

        state = _get_worker_state(hwnd)

        # Track PROCESSING start time
        if state == "PROCESSING":
            if name not in _processing_since:
                _processing_since[name] = now
        else:
            _processing_since.pop(name, None)
            continue

        # Check if over threshold
        proc_start = _processing_since.get(name)
        if proc_start is None:
            continue
        duration = now - proc_start
        if duration < STUCK_PROCESSING_THRESHOLD:
            continue

        # Dedup: skip if already alerted within window
        last_alert = _stuck_alert_last.get(name, 0)
        if now - last_alert < STUCK_DEDUP_WINDOW:
            continue

        duration_s = int(duration)
        log(f"{name.upper()}: PROCESSING for {duration_s}s (>{STUCK_PROCESSING_THRESHOLD}s) -- attempting auto-recovery", "WARN")

        # Attempt cancel_generation
        cancelled = _cancel_generation(hwnd)
        if cancelled:
            time.sleep(3)
            new_state = _get_worker_state(hwnd)
            if new_state == "IDLE":
                # Success -- worker recovered
                _processing_since.pop(name, None)
                _stuck_alert_last[name] = now
                log(f"{name.upper()}: AUTO_RECOVERED -- was stuck PROCESSING for {duration_s}s, now IDLE", "OK")
                skynet_post("/bus/publish", {
                    "sender": "monitor", "topic": "orchestrator", "type": "alert",
                    "content": f"AUTO_RECOVERED: {name.upper()} was stuck PROCESSING for {duration_s}s, "
                               f"cancelled generation. Worker is now IDLE."
                })
                continue

        # Cancel failed or worker still not IDLE -- fall back to existing alert
        _stuck_alert_last[name] = now
        log(f"{name.upper()}: POTENTIALLY_STUCK -- cancel {'succeeded' if cancelled else 'failed'} but worker still not IDLE", "CRIT")
        skynet_post("/bus/publish", {
            "sender": "monitor", "topic": "orchestrator", "type": "alert",
            "content": f"POTENTIALLY_STUCK: {name.upper()} has been PROCESSING for {duration_s}s. "
                       f"Auto-recovery {'attempted but worker not IDLE' if cancelled else 'failed (cancel_generation error)'}. "
                       f"Manual intervention may be needed."
        })


REALTIME_FILE = DATA_DIR / "realtime.json"
REALTIME_PID_FILE = DATA_DIR / "realtime.pid"
REALTIME_STALE_THRESHOLD = 5  # seconds
MONITOR_HEALTH_FILE = DATA_DIR / "monitor_health.json"
TODOS_FILE = DATA_DIR / "todos.json"
TASK_QUEUE_FILE = DATA_DIR / "task_queue.json"
SSE_DAEMON_SCRIPT = ROOT / "tools" / "skynet_sse_daemon.py"
REALTIME_DAEMON_SCRIPT = ROOT / "tools" / "skynet_realtime.py"
BACKGROUND_SPAWN_FLAGS = (
    subprocess.CREATE_NEW_PROCESS_GROUP
    | subprocess.DETACHED_PROCESS
    | subprocess.CREATE_NO_WINDOW
)

# ─── Daemon restart cooldown tracking (prevents cascading duplicates) ────────
RESTART_COOLDOWN_SECONDS = 60  # refuse to restart same daemon within this window
_restart_cooldowns: dict = {}  # daemon_name -> last_restart_epoch

# ─── Workers.json auto-reload state ─────────────────────────────────────────
_workers_mtime: float = 0.0  # last known mtime of workers.json

# ─── Productivity tracking state ────────────────────────────────────────────
_worker_productivity: dict = {}  # name -> {tasks_completed, first_seen, last_result_time}
_idle_since: dict = {}           # name -> epoch when IDLE streak started
_health_trend: list = []         # list of periodic snapshots for trend analysis
_IDLE_UNPRODUCTIVE_THRESHOLD = 300  # 5 minutes idle with pending work = unproductive
_MAX_HEALTH_TREND = 200            # keep last 200 snapshots (~33 min at 10s intervals)


def _reload_workers_if_changed() -> tuple:
    """Check workers.json mtime and reload if changed. Returns (workers, orch_hwnd)."""
    global _workers_mtime
    try:
        current_mtime = WORKERS_FILE.stat().st_mtime
        if current_mtime != _workers_mtime:
            workers, orch_hwnd = load_workers()
            _workers_mtime = current_mtime
            if _workers_mtime > 0:
                log(f"workers.json reloaded (mtime changed): {len(workers)} workers", "INFO")
            return workers, orch_hwnd, True
    except Exception as e:
        log(f"workers.json reload check failed: {e}", "WARN")
    return None, None, False


def _get_pending_work_count() -> int:
    """Count total pending work items from todos.json and task_queue.json."""
    count = 0
    try:
        if TODOS_FILE.exists():
            data = json.loads(TODOS_FILE.read_text(encoding="utf-8"))
            items = data.get("todos", [])
            count += sum(1 for t in items if t.get("status") in ("pending", "active"))
    except Exception:
        pass
    try:
        if TASK_QUEUE_FILE.exists():
            data = json.loads(TASK_QUEUE_FILE.read_text(encoding="utf-8"))
            tasks = data.get("tasks", [])
            count += sum(1 for t in tasks if t.get("status") not in ("done", "failed", "cancelled"))
    except Exception:
        pass
    return count


def _track_productivity(workers: list, health: dict):
    """Track worker productivity: tasks completed per hour, idle-but-unproductive detection."""
    global _worker_productivity, _idle_since
    now = time.time()
    pending_work = _get_pending_work_count()

    for w in workers:
        name = w["name"]
        h = health.get(name, {})
        status = h.get("status", "UNKNOWN")
        alive = h.get("alive", False)

        # Initialize productivity tracking
        if name not in _worker_productivity:
            _worker_productivity[name] = {
                "tasks_completed": 0,
                "first_seen": now,
                "last_result_time": now,
                "results_this_hour": 0,
                "hour_start": now,
            }

        prod = _worker_productivity[name]

        # Reset hourly counter if hour rolled over
        if now - prod["hour_start"] >= 3600:
            prod["results_this_hour"] = 0
            prod["hour_start"] = now

        # Track idle streaks
        state = "UNKNOWN"
        if alive and w.get("hwnd", 0):
            try:
                state = _get_worker_state(w["hwnd"])
            except Exception:
                pass

        if state == "IDLE":
            if name not in _idle_since:
                _idle_since[name] = now
            else:
                idle_duration = now - _idle_since[name]
                if idle_duration > _IDLE_UNPRODUCTIVE_THRESHOLD and pending_work > 0:
                    log(f"{name.upper()}: IDLE for {int(idle_duration)}s with {pending_work} pending work items -- UNPRODUCTIVE", "WARN")
                    skynet_post("/bus/publish", {
                        "sender": "monitor", "topic": "orchestrator", "type": "alert",
                        "content": f"IDLE_UNPRODUCTIVE: {name.upper()} idle {int(idle_duration)}s with {pending_work} pending tasks. Dispatch work!"
                    })
                    # Reset to avoid spamming (re-alert after another threshold period)
                    _idle_since[name] = now
        else:
            _idle_since.pop(name, None)

    # Check bus for recent results to update productivity counters
    try:
        req = urllib.request.Request(f"{SKYNET_URL}/bus/messages?topic=orchestrator&limit=20")
        with urllib.request.urlopen(req, timeout=5) as r:
            msgs = json.loads(r.read())
            if isinstance(msgs, list):
                for m in msgs:
                    if m.get("type") == "result":
                        sender = m.get("sender", "")
                        if sender in _worker_productivity:
                            # Simple dedup: only count if timestamp is fresh (last 30s)
                            ts = m.get("timestamp", "")
                            try:
                                from datetime import timezone
                                msg_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                                if msg_time.tzinfo:
                                    age = (datetime.now(timezone.utc) - msg_time).total_seconds()
                                else:
                                    age = (datetime.now() - msg_time).total_seconds()
                                if age < 30:
                                    _worker_productivity[sender]["tasks_completed"] += 1
                                    _worker_productivity[sender]["results_this_hour"] += 1
                                    _worker_productivity[sender]["last_result_time"] = now
                            except Exception:
                                pass
    except Exception:
        pass


def _get_productivity_summary() -> dict:
    """Build a productivity summary for all tracked workers."""
    now = time.time()
    summary = {}
    for name, prod in _worker_productivity.items():
        uptime_h = max((now - prod["first_seen"]) / 3600, 0.01)
        summary[name] = {
            "tasks_total": prod["tasks_completed"],
            "tasks_per_hour": round(prod["tasks_completed"] / uptime_h, 2),
            "results_this_hour": prod["results_this_hour"],
            "idle_since": _idle_since.get(name),
            "idle_duration_s": int(now - _idle_since[name]) if name in _idle_since else 0,
        }
    return summary


def _auto_restart_stale_daemons():
    """Check if realtime and SSE daemons are alive; restart if dead.

    Guards against cascading duplicates:
    - 60s cooldown per daemon after any restart
    - Double-check PID file after 2s sleep before spawning
    - Only restarts managed daemons with real PID files
      (god_console/bus_relay are orchestrator-managed)
    """
    global _restart_cooldowns
    now = time.time()

    def _is_on_cooldown(daemon_name: str) -> bool:
        last = _restart_cooldowns.get(daemon_name, 0)
        elapsed = now - last
        if elapsed < RESTART_COOLDOWN_SECONDS:
            log(f"COOLDOWN: {daemon_name} restarted {int(elapsed)}s ago (need {RESTART_COOLDOWN_SECONDS}s) -- skipping", "WARN")
            return True
        return False

    def _pid_file_is_alive(pid_file) -> bool:
        """Check if a PID file exists and its process is running."""
        try:
            if pid_file.exists():
                pid = int(pid_file.read_text().strip())
                import os
                os.kill(pid, 0)
                return True
        except (OSError, ValueError):
            pass
        return False

    # ── Realtime daemon ──
    rt_status = _check_realtime_daemon()
    if REALTIME_PID_FILE.exists() and not rt_status["alive"]:
        if _is_on_cooldown("realtime"):
            pass  # skip
        else:
            # Double-check: sleep 2s then re-check PID (daemon may be starting)
            import time as _t
            _t.sleep(2)
            if _pid_file_is_alive(REALTIME_PID_FILE):
                log("Realtime daemon PID appeared after 2s wait -- NOT restarting", "WARN")
            else:
                # Re-verify freshness one more time
                rt_status2 = _check_realtime_daemon()
                if rt_status2["alive"]:
                    log("Realtime daemon became alive during double-check -- NOT restarting", "WARN")
                else:
                    log("Realtime daemon confirmed dead after double-check -- restarting", "FIX")
                    try:
                        import subprocess as sp
                        sp.Popen(
                            [_REAL_PYTHON, str(REALTIME_DAEMON_SCRIPT)],
                            env=_DAEMON_ENV,
                            creationflags=BACKGROUND_SPAWN_FLAGS,
                            stdout=sp.DEVNULL, stderr=sp.DEVNULL,
                        )
                        _restart_cooldowns["realtime"] = time.time()
                        log("Realtime daemon restart initiated (cooldown set)", "OK")
                        skynet_post("/bus/publish", {
                            "sender": "monitor", "topic": "orchestrator", "type": "report",
                            "content": "AUTO_RESTART: Realtime daemon was dead, restarted automatically (60s cooldown active)"
                        })
                    except Exception as e:
                        log(f"Realtime daemon restart failed: {e}", "ERR")

    # ── SSE daemon ──
    sse_pid_file = DATA_DIR / "sse_daemon.pid"
    sse_alive = _pid_file_is_alive(sse_pid_file)
    sse_state = _check_realtime_daemon()

    if not sse_alive and sse_state.get("alive"):
        log("SSE state file is fresh -- NOT restarting despite missing/stale PID", "WARN")
    elif not sse_alive and SSE_DAEMON_SCRIPT.exists():
        if _is_on_cooldown("sse"):
            pass  # skip
        else:
            # Double-check: sleep 2s then re-check PID
            import time as _t
            _t.sleep(2)
            if _pid_file_is_alive(sse_pid_file):
                log("SSE daemon PID appeared after 2s wait -- NOT restarting", "WARN")
            elif _check_realtime_daemon().get("alive"):
                log("SSE state file became fresh during double-check -- NOT restarting", "WARN")
            else:
                log("SSE daemon confirmed dead after double-check -- restarting", "FIX")
                try:
                    import subprocess as sp
                    sp.Popen(
                        [_REAL_PYTHON, str(SSE_DAEMON_SCRIPT)],
                        env=_DAEMON_ENV,
                        creationflags=BACKGROUND_SPAWN_FLAGS,
                        stdout=sp.DEVNULL, stderr=sp.DEVNULL,
                    )
                    _restart_cooldowns["sse"] = time.time()
                    log("SSE daemon restart initiated (cooldown set)", "OK")
                    skynet_post("/bus/publish", {
                        "sender": "monitor", "topic": "orchestrator", "type": "report",
                        "content": "AUTO_RESTART: SSE daemon was dead, restarted automatically (60s cooldown active)"
                    })
                except Exception as e:
                    log(f"SSE daemon restart failed: {e}", "ERR")

    # NOTE: god_console.py and skynet_bus_relay.py are NOT auto-restarted here.
    # Those are managed exclusively by the orchestrator.


def _record_health_trend(health: dict, productivity: dict, pending_work: int):
    """Append a health snapshot to the trend log and write to data/monitor_health.json."""
    global _health_trend
    now = time.time()

    snapshot = {
        "timestamp": datetime.now().isoformat(),
        "epoch": now,
        "workers": {},
        "pending_work": pending_work,
        "total_alive": 0,
        "total_ok": 0,
    }

    for name, h in health.items():
        if not isinstance(h, dict) or name in ("updated", "intelligence", "realtime_daemon"):
            continue
        alive = h.get("alive", False)
        status = h.get("status", "UNKNOWN")
        snapshot["workers"][name] = {
            "alive": alive,
            "status": status,
            "tasks_per_hour": productivity.get(name, {}).get("tasks_per_hour", 0),
            "idle_s": productivity.get(name, {}).get("idle_duration_s", 0),
        }
        if alive:
            snapshot["total_alive"] += 1
        if status == "OK":
            snapshot["total_ok"] += 1

    _health_trend.append(snapshot)
    if len(_health_trend) > _MAX_HEALTH_TREND:
        _health_trend = _health_trend[-_MAX_HEALTH_TREND:]

    # Write full trend to disk for dashboard consumption
    try:
        trend_data = {
            "updated": datetime.now().isoformat(),
            "trend_count": len(_health_trend),
            "latest": snapshot,
            "productivity": productivity,
            "trend": _health_trend[-20:],  # last 20 snapshots for dashboard
        }
        with open(MONITOR_HEALTH_FILE, "w") as f:
            json.dump(trend_data, f, indent=2, default=str)
    except Exception as e:
        log(f"Failed to write monitor_health.json: {e}", "WARN")


def _read_state_timestamp_age(path: Path, *keys: str) -> tuple[object | None, float | None]:
    """Read a JSON timestamp field and return (raw_value, age_seconds)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, None

    raw_value = None
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            raw_value = value
            break

    if raw_value in (None, ""):
        return None, None

    try:
        from datetime import timezone
        if isinstance(raw_value, str):
            dt = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            if dt.tzinfo:
                age = (datetime.now(timezone.utc) - dt).total_seconds()
            else:
                age = (datetime.now() - dt).total_seconds()
        else:
            age = time.time() - float(raw_value)
        return raw_value, max(age, 0.0)
    except Exception:
        return raw_value, None


def _check_realtime_daemon() -> dict:
    """Check freshness of data/realtime.json.

    This reflects live realtime state. A managed realtime process only exists when
    data/realtime.pid is present; otherwise the SSE daemon is the active writer.
    """
    try:
        if not REALTIME_FILE.exists():
            return {"alive": False, "last_update": None, "latency_ms": None, "managed_process": REALTIME_PID_FILE.exists()}
        last_update, age = _read_state_timestamp_age(REALTIME_FILE, "timestamp", "last_update")
        if last_update is None or age is None:
            return {"alive": False, "last_update": last_update, "latency_ms": None, "managed_process": REALTIME_PID_FILE.exists()}
        alive = age < REALTIME_STALE_THRESHOLD
        return {
            "alive": alive,
            "last_update": str(last_update),
            "latency_ms": round(age * 1000, 1),
            "managed_process": REALTIME_PID_FILE.exists(),
        }
    except Exception as e:
        return {
            "alive": False,
            "last_update": None,
            "latency_ms": None,
            "managed_process": REALTIME_PID_FILE.exists(),
            "error": str(e)[:80],
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Single check and exit")
    parser.add_argument("--status", action="store_true", help="Print current health.json")
    parser.add_argument("--hwnd-interval", type=int, default=HWND_CHECK_INTERVAL)
    parser.add_argument("--model-interval", type=int, default=MODEL_CHECK_INTERVAL)
    parser.add_argument("--max-runtime", type=int, default=0,
                        help="Max runtime in seconds (0=unlimited). Daemon exits gracefully after this.")
    args = parser.parse_args()

    if args.status:
        if HEALTH_FILE.exists():
            print(HEALTH_FILE.read_text())
        else:
            print("No health.json yet")
        return

    # ── PID guard: prevent duplicate monitor daemons ─────────────────────────
    import os, signal
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            # Check if the process is still alive (Windows: OpenProcess trick via os.kill)
            os.kill(old_pid, 0)
            log(f"Monitor already running (PID {old_pid}) -- exiting to prevent duplicate", "WARN")
            return
        except (OSError, ValueError):
            pass  # Process dead or PID file stale -- proceed
    PID_FILE.write_text(str(os.getpid()))
    try:
        _run_monitor(args)
    finally:
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass


def _run_monitor(args):
    """Inner monitor loop, called from main() after PID guard.
    
    Level 4 upgrade: auto-reloads workers.json on change, tracks productivity,
    detects idle-but-unproductive workers, auto-restarts stale daemons,
    logs health trends to data/monitor_health.json.
    """
    global _workers_mtime

    workers, orch_hwnd = load_workers()
    _workers_mtime = WORKERS_FILE.stat().st_mtime if WORKERS_FILE.exists() else 0
    log(f"Skynet Monitor starting -- watching {len(workers)} workers", "INFO")
    log(f"HWND check every {args.hwnd_interval}s | Model check every {args.model_interval}s", "INFO")
    log(f"L4 features: auto-reload, productivity tracking, daemon auto-restart, health trends", "INFO")

    if args.once:
        health = run_check(workers, orch_hwnd, check_model=True)
        write_health(health)
        print(json.dumps(health, indent=2))
        return

    max_runtime = getattr(args, 'max_runtime', 0)
    start_time = time.time()
    last_model_check = 0.0
    last_orch_check = 0.0
    last_daemon_restart_check = 0.0
    cycle = 0
    DAEMON_CHECK_INTERVAL = 120  # check daemon health every 2 minutes

    try:
        while True:
            # Max runtime guard
            if max_runtime and (time.time() - start_time) >= max_runtime:
                log(f"Max runtime {max_runtime}s reached -- shutting down gracefully", "INFO")
                skynet_post("/bus/publish", {"sender": "monitor", "topic": "orchestrator", "type": "lifecycle",
                    "content": f"Monitor shutdown: max_runtime={max_runtime}s reached after {cycle} cycles"})
                break
            try:
                cycle += 1
                now = time.time()

                # ── AUTO-RELOAD workers.json if changed ────────────────────
                new_workers, new_orch, changed = _reload_workers_if_changed()
                if changed and new_workers is not None:
                    workers = new_workers
                    orch_hwnd = new_orch
                    log(f"Workers reloaded: {len(workers)} workers, orch_hwnd={orch_hwnd}", "OK")

                do_model = (now - last_model_check) >= args.model_interval
                do_orch = (now - last_orch_check) >= ORCH_MODEL_CHECK_INTERVAL

                health = run_check(workers, orch_hwnd, check_model=do_model, check_orch=do_orch)
                write_health(health)

                # ── Stuck worker detection with auto-recovery ───────────────
                _check_stuck_workers(workers)

                # ── Productivity tracking (every cycle) ─────────────────────
                _track_productivity(workers, health)
                productivity = _get_productivity_summary()
                pending_work = _get_pending_work_count()

                # ── Health trend logging (every cycle) ──────────────────────
                _record_health_trend(health, productivity, pending_work)

                if do_model:
                    last_model_check = now
                if do_orch:
                    last_orch_check = now

                # Post periodic bus heartbeat from monitor (with productivity data)
                if cycle % 6 == 0:  # every ~60s
                    alive_count = sum(1 for h in health.values() if isinstance(h, dict) and h.get("alive"))
                    prod_summary = "; ".join(
                        f"{n}={p.get('tasks_per_hour', 0):.1f}t/h"
                        for n, p in productivity.items()
                    )
                    skynet_post("/bus/publish", {
                        "sender": "monitor", "topic": "orchestrator", "type": "heartbeat",
                        "content": f"Monitor cycle {cycle}: {alive_count}/{len(workers)} alive, pending={pending_work}. Productivity: {prod_summary}",
                        "metadata": {k: (v.get("status", "?") if isinstance(v, dict) else str(v)) for k, v in health.items()}
                    })

                # Collect intelligence metrics (knowledge flow + convene sessions)
                if cycle % 6 == 0:
                    intel = _collect_intelligence_metrics()
                    health["intelligence"] = intel

                    # Check realtime daemon liveness
                    rt_status = _check_realtime_daemon()
                    health["realtime_daemon"] = rt_status
                    if not rt_status["alive"]:
                        content = "REALTIME STATE STALE -- data/realtime.json stale or missing"
                        if rt_status.get("managed_process"):
                            content = "REALTIME DAEMON DOWN -- data/realtime.json stale or missing"
                        skynet_post("/bus/publish", {
                            "sender": "monitor", "topic": "orchestrator", "type": "alert",
                            "content": content
                        })

                    write_health(health)

                # ── Auto-restart stale daemons (every 2 minutes) ────────────
                if (now - last_daemon_restart_check) >= DAEMON_CHECK_INTERVAL:
                    last_daemon_restart_check = now
                    _auto_restart_stale_daemons()

            except Exception as e:
                log(f"Monitor cycle error: {e}", "ERR")

            time.sleep(args.hwnd_interval)
    except KeyboardInterrupt:
        log("Monitor shutting down (Ctrl+C)", "INFO")
        skynet_post("/bus/publish", {"sender": "monitor", "topic": "orchestrator", "type": "lifecycle",
            "content": f"Monitor shutdown: KeyboardInterrupt after {cycle} cycles"})


if __name__ == "__main__":
    main()
