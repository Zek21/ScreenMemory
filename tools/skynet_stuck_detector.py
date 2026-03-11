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
ORCH_FILE = DATA_DIR / "orchestrator.json"

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


def _get_orch_hwnd():
    """Load orchestrator HWND from data/orchestrator.json."""
    try:
        data = json.loads(ORCH_FILE.read_text(encoding="utf-8"))
        return data.get("hwnd", 0)
    except Exception:
        return 0


def prompt_orchestrator(message):
    """Type a prompt into the orchestrator's SIDEBAR chat input via UIA.

    The orchestrator window has multiple Edit controls:
      - Terminal input (y=922, x=543) -- DO NOT target
      - Terminal input (y=835, x=347) -- DO NOT target
      - Sidebar chat input (y=844, x=24, w=265) -- TARGET THIS ONE

    ghost_type_to_worker targets the bottommost Edit (highest Y) which hits the
    terminal. This function instead targets the sidebar Edit (x < 320) which is
    the Copilot CLI chat input where the orchestrator conversation lives.

    Layout reference: data/orch_layout.json
    """
    orch_hwnd = _get_orch_hwnd()
    if not orch_hwnd:
        log("Cannot prompt orchestrator: no HWND found", "ERROR")
        return False

    # Flatten message to single line for clipboard paste
    flat_msg = message.replace("\n", " ").replace("\r", " ")

    # Write to temp file to avoid escaping issues in PowerShell
    dispatch_file = DATA_DIR / ".orch_wake_dispatch.txt"
    dispatch_file.write_text(flat_msg, encoding="utf-8")
    dispatch_path = str(dispatch_file).replace("\\", "\\\\")

    # PowerShell script that targets the SIDEBAR Edit (x < 320)
    ps = f'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms
Add-Type @"
using System; using System.Runtime.InteropServices;
public class WakeHelper {{
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool f);
    [DllImport("user32.dll")] public static extern IntPtr SetFocus(IntPtr h);
    public static bool FocusViaAttach(IntPtr target) {{
        uint pid; uint tid = GetWindowThreadProcessId(target, out pid);
        uint myTid = GetCurrentThreadId();
        if (tid == 0) return false;
        AttachThreadInput(myTid, tid, true);
        SetFocus(target);
        return true;
    }}
    public static void Detach(IntPtr target) {{
        uint pid; uint tid = GetWindowThreadProcessId(target, out pid);
        uint myTid = GetCurrentThreadId();
        AttachThreadInput(myTid, tid, false);
    }}
}}
"@

$hwnd = [IntPtr]{orch_hwnd}
$dispatchText = [System.IO.File]::ReadAllText("{dispatch_path}", [System.Text.Encoding]::UTF8)

# Find the SIDEBAR chat Edit (x < 320, highest Y)
$wnd = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
$allEdits = $wnd.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Edit
    ))
)
$sidebarEdit = $null
$maxY = -1
foreach ($e in $allEdits) {{
    try {{
        $r = $e.Current.BoundingRectangle
        # Sidebar is x < 320; skip terminal/editor Edits (x >= 320)
        if ($r.X -lt 320 -and $r.Y -gt $maxY -and $r.Width -gt 50) {{
            $maxY = $r.Y
            $sidebarEdit = $e
        }}
    }} catch {{}}
}}

if (-not $sidebarEdit) {{
    Write-Host "NO_SIDEBAR_EDIT"
    exit 1
}}

$savedClip = $null
try {{ $savedClip = [System.Windows.Forms.Clipboard]::GetText() }} catch {{}}

[System.Windows.Forms.Clipboard]::SetText($dispatchText)
Start-Sleep -Milliseconds 100

$attached = [WakeHelper]::FocusViaAttach($hwnd)
try {{ $sidebarEdit.SetFocus() }} catch {{}}
Start-Sleep -Milliseconds 200

[System.Windows.Forms.SendKeys]::SendWait("^a")
Start-Sleep -Milliseconds 50
[System.Windows.Forms.SendKeys]::SendWait("{{DELETE}}")
Start-Sleep -Milliseconds 50
[System.Windows.Forms.SendKeys]::SendWait("^v")
Start-Sleep -Milliseconds 200
[System.Windows.Forms.SendKeys]::SendWait("{{ENTER}}")

if ($attached) {{ [WakeHelper]::Detach($hwnd) }}

if ($savedClip -and $savedClip.Length -gt 0) {{
    Start-Sleep -Milliseconds 100
    try {{ [System.Windows.Forms.Clipboard]::SetText($savedClip) }} catch {{}}
}}

try {{ Remove-Item "{dispatch_path}" -Force -ErrorAction SilentlyContinue }} catch {{}}
Write-Host "OK_SIDEBAR"
exit 0
'''

    try:
        import subprocess
        log(f"Sidebar-targeting HWND={orch_hwnd}")
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=20,
            creationflags=0x08000000  # CREATE_NO_WINDOW
        )
        ok = r.returncode == 0 and "OK_SIDEBAR" in r.stdout
        if not ok:
            out = (r.stdout or "").strip()[:200]
            err = (r.stderr or "").strip()[:200]
            log(f"Sidebar delivery result: {out} | {err}", "WARN")
        else:
            log("Prompt delivered to orchestrator sidebar chat")
        return ok
    except Exception as e:
        log(f"Prompt delivery failed: {e}", "ERROR")
        return False


def _gather_system_state():
    """Gather full Skynet system state for rich orchestrator prompts."""
    state = {
        "workers": {},
        "pending_todos": 0,
        "todo_items": [],
        "bus_results": [],
        "bus_alerts": [],
        "engines": {},
        "iq": None,
    }

    # Worker states from /status
    try:
        import urllib.request
        data = json.loads(urllib.request.urlopen(f"{SKYNET_URL}/status", timeout=3).read())
        agents = data.get("agents", {})
        for name in ("alpha", "beta", "gamma", "delta"):
            agent = agents.get(name, {})
            state["workers"][name] = {
                "status": agent.get("status", "UNKNOWN"),
                "tasks_completed": agent.get("tasks_completed", 0),
                "current_task": agent.get("current_task", ""),
                "queue_depth": agent.get("queue_depth", 0),
            }
    except Exception:
        pass

    # Pending TODOs
    try:
        todos_file = DATA_DIR / "todos.json"
        if todos_file.exists():
            td = json.loads(todos_file.read_text(encoding="utf-8"))
            todo_list = td if isinstance(td, list) else td.get("todos", [])
            pending = [t for t in todo_list
                       if isinstance(t, dict) and t.get("status") in ("pending", "active")]
            state["pending_todos"] = len(pending)
            # Top 5 by priority
            priority_rank = {"critical": 0, "high": 1, "medium": 2, "normal": 3, "low": 4}
            pending.sort(key=lambda t: priority_rank.get(
                str(t.get("priority", "normal")).lower(), 9))
            state["todo_items"] = [
                f"[{t.get('priority','normal')}] {t.get('task','?')[:80]}"
                for t in pending[:5]
            ]
    except Exception:
        pass

    # Recent bus messages (results and alerts for orchestrator)
    try:
        import urllib.request
        msgs = json.loads(urllib.request.urlopen(
            f"{SKYNET_URL}/bus/messages?limit=20", timeout=3).read())
        if isinstance(msgs, list):
            for m in msgs:
                if m.get("topic") == "orchestrator":
                    if m.get("type") == "result":
                        state["bus_results"].append(
                            f"{m.get('sender','?')}: {str(m.get('content',''))[:80]}")
                    elif m.get("type") in ("alert", "urgent"):
                        state["bus_alerts"].append(
                            f"{m.get('sender','?')}: {str(m.get('content',''))[:80]}")
    except Exception:
        pass

    # Engine status
    try:
        import urllib.request
        engines = json.loads(urllib.request.urlopen(
            "http://localhost:8421/engines", timeout=3).read())
        if isinstance(engines, dict):
            for name, info in engines.get("engines", {}).items():
                if isinstance(info, dict):
                    state["engines"][name] = info.get("status", "unknown")
    except Exception:
        pass

    # IQ
    try:
        iq_file = DATA_DIR / "iq_history.json"
        if iq_file.exists():
            iq_data = json.loads(iq_file.read_text(encoding="utf-8"))
            h = iq_data.get("history", [])
            if h:
                latest = h[-1]
                state["iq"] = round(latest.get("composite", 0), 4)
    except Exception:
        pass

    return state


def _compose_wake_prompt(cycle_label, worker_states_line, system_state):
    """Compose a rich actionable prompt from gathered system state."""
    parts = [f"[SKYNET WAKE-UP {cycle_label}]"]

    # Worker overview
    parts.append(f"Workers: {worker_states_line}")

    # IQ
    if system_state.get("iq"):
        parts.append(f"System IQ: {system_state['iq']}")

    # Engines
    if system_state.get("engines"):
        online = sum(1 for s in system_state["engines"].values() if s == "online")
        total = len(system_state["engines"])
        parts.append(f"Engines: {online}/{total} online")

    # Pending TODOs
    n_todos = system_state.get("pending_todos", 0)
    if n_todos > 0:
        parts.append(f"PENDING TODOs: {n_todos}")
        for item in system_state.get("todo_items", []):
            parts.append(f"  - {item}")
    else:
        parts.append("No pending TODOs")

    # Bus alerts
    alerts = system_state.get("bus_alerts", [])
    if alerts:
        parts.append(f"ALERTS ({len(alerts)}):")
        for a in alerts[:3]:
            parts.append(f"  ! {a}")

    # Bus results
    results = system_state.get("bus_results", [])
    if results:
        parts.append(f"RESULTS ({len(results)}):")
        for r in results[:3]:
            parts.append(f"  > {r}")

    # Action directive
    if n_todos > 0:
        parts.append("ACTION: All workers are IDLE with pending work. Decompose and dispatch TODOs to workers NOW.")
    elif alerts:
        parts.append("ACTION: Address pending alerts. Check worker health and system integrity.")
    else:
        parts.append("ACTION: System healthy. All workers IDLE. Generate improvement tasks or dispatch pending work from the bus.")

    return " | ".join(parts)


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
        """Continuous monitoring loop.
        
        Qualification: all 4 workers IDLE.
        On each interval where all workers are IDLE, increment counter.
        On the Nth interval (max_cycles, default 3), force-type a rich
        wake-up prompt into the orchestrator window regardless of its state.
        Then stop.
        """
        ctrl_c = _load_ctrl_c_enabled()
        log(f"Stuck detector started (interval={interval}s, "
            f"wake_after={max_cycles or 'unlimited'} idle intervals)")
        log(f"Tracking {len(self.trackers)} workers: {list(self.trackers.keys())}")

        cycle = 0
        idle_count = 0
        bus_post("stuck-detector", "orchestrator", "monitor_alert",
                 f"WORKER_MONITOR_ONLINE: tracking {list(self.trackers.keys())}, "
                 f"interval={interval}s, wake_after={max_cycles or 'unlimited'} idle intervals")
        try:
            while True:
                cycle += 1
                issues = self.check_all()

                # Build per-worker status summary
                states = []
                all_idle = True
                for name, tracker in self.trackers.items():
                    st = tracker.last_state or "UNKNOWN"
                    states.append(f"{name.upper()}={st}")
                    if st != "IDLE":
                        all_idle = False
                status_line = ", ".join(states)

                if all_idle:
                    idle_count += 1
                    log(f"Cycle {cycle}: ALL WORKERS IDLE (consecutive idle: {idle_count})")
                else:
                    idle_count = 0
                    log(f"Cycle {cycle}: Workers busy ({status_line}) -- idle counter reset")

                if issues:
                    for issue in issues:
                        log(f"  {issue['worker'].upper()}: {issue['condition']} "
                            f"(severity={issue['severity']})", "WARN")

                # Check if we've hit the wake-up threshold
                if max_cycles is not None and idle_count >= max_cycles:
                    log(f"Workers IDLE for {idle_count} consecutive intervals -- WAKING ORCHESTRATOR")

                    system_state = _gather_system_state()
                    prompt = _compose_wake_prompt(
                        f"IDLE x{idle_count}", status_line, system_state)

                    log(f"Force-typing wake-up prompt ({len(prompt)} chars)")
                    delivered = prompt_orchestrator(prompt)
                    if delivered:
                        log(f"Wake-up prompt DELIVERED to orchestrator")
                        bus_post("stuck-detector", "orchestrator", "heartbeat",
                                 f"WAKE_UP_DELIVERED after {idle_count} idle intervals: {status_line}")
                    else:
                        log(f"Wake-up delivery FAILED", "ERROR")
                        bus_post("stuck-detector", "orchestrator", "alert",
                                 f"WAKE_UP_FAILED after {idle_count} idle intervals")

                    log(f"Mission complete -- stopping")
                    bus_post("stuck-detector", "orchestrator", "monitor_alert",
                             f"WORKER_MONITOR_STOPPED: woke orchestrator after {idle_count} idle intervals")
                    break

                self.save_history()
                time.sleep(interval)
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
    parser.add_argument("--interval", type=int, default=60, help="Monitor interval (seconds)")
    parser.add_argument("--max-cycles", type=int, default=None, help="Stop after N cycles (default: unlimited)")
    args = parser.parse_args()

    if args.check:
        one_shot_check()
    elif args.monitor:
        detector = StuckDetector()
        detector.monitor(interval=args.interval, max_cycles=args.max_cycles)
    elif args.history:
        show_history()
    elif args.health:
        print(json.dumps(get_worker_health_json(), indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
