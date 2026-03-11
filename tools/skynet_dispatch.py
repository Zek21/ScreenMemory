#!/usr/bin/env python3
"""
Skynet Dispatch — Send tasks to worker chat windows via ghost automation.

This is the bridge: Orchestrator → ghost-type prompt into worker CLI window.
Uses clipboard paste via PostMessage — zero cursor movement.

Usage:
    python tools/skynet_dispatch.py --worker alpha --task "List all Python files in core/"
    python tools/skynet_dispatch.py --all --task "Run health check"
    python tools/skynet_dispatch.py --parallel --task "Run health check"   # all workers simultaneously
    python tools/skynet_dispatch.py --smart --task "Analyse D:\\ML"         # auto-route to best worker
    python tools/skynet_dispatch.py --fan-out --tasks tasks.json
    python tools/skynet_dispatch.py --fan-out-parallel --tasks tasks.json  # parallel fan-out
    python tools/skynet_dispatch.py --blast --task "quick ps cmd"           # no preamble, max speed
    python tools/skynet_dispatch.py --wait-result KEY --timeout 120         # wait for bus result
"""

import json
import os
import sys
import time
import ctypes
import ctypes.wintypes
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Global lock to serialize clipboard operations (SetText + Ctrl+V)
# Without this, parallel dispatch corrupts clipboard between threads
_dispatch_lock = threading.Lock()

# Ensure UTF-8 output on Windows (emojis in log messages)
if hasattr(sys.stdout, 'reconfigure'):
    try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception: pass
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "data"
WORKERS_FILE = DATA_DIR / "workers.json"
ORCH_FILE = DATA_DIR / "orchestrator.json"
CRITICAL_PROCS_FILE = DATA_DIR / "critical_processes.json"
DISPATCH_LOCK_FILE = DATA_DIR / "dispatch_active.lock"

user32 = ctypes.windll.user32


# ── Process Protection Guard ────────────────────────────────────────────────

def _load_critical_processes():
    """Load the protected process list from data/critical_processes.json."""
    if CRITICAL_PROCS_FILE.exists():
        try:
            return json.loads(CRITICAL_PROCS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"protected_names": [], "protected_roles": [], "processes": []}


def is_process_protected(pid=None, name=None):
    """Check if a process is protected (must never be killed).

    Args:
        pid: Process ID to check
        name: Process name to check (e.g. 'skynet.exe', 'god_console.py')

    Returns:
        (bool, str): (is_protected, reason)
    """
    data = _load_critical_processes()

    # Check by name against protected_names list
    if name:
        name_lower = name.lower()
        for pn in data.get("protected_names", []):
            if pn.lower() in name_lower or name_lower in pn.lower():
                return True, f"Protected service: {pn}"

    # Check by PID/HWND against registered processes
    if pid:
        for proc in data.get("processes", []):
            if proc.get("pid") == pid or proc.get("hwnd") == pid:
                return True, f"Protected {proc.get('role', '?')}: {proc.get('name', '?')}"

    return False, ""


def guard_process_kill(pid=None, name=None, caller="unknown"):
    """Guard against killing protected processes. Returns True if SAFE to kill.

    If protected, posts alert to bus and returns False.
    """
    protected, reason = is_process_protected(pid=pid, name=name)
    if protected:
        alert = f"BLOCKED: {caller} tried to kill protected process (pid={pid} name={name}). {reason}"
        try:
            import urllib.request
            payload = json.dumps({
                "sender": "process_guard",
                "topic": "orchestrator",
                "type": "alert",
                "content": alert,
            }).encode()
            req = urllib.request.Request(
                "http://localhost:8420/bus/publish", payload,
                {"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass
        print(f"\033[91m[GUARD] {alert}\033[0m", file=sys.stderr)
        return False  # NOT safe to kill
    return True  # safe to kill


# ── Research metrics (lazy singleton) ───────────────────────────────────────
_metrics = None

def metrics():
    global _metrics
    if _metrics is None:
        try:
            from tools.skynet_metrics import SkynetMetrics
            _metrics = SkynetMetrics()
        except Exception:
            pass
    return _metrics

# DEPRECATED: Use build_preamble(worker_name) instead for full worker awareness.
NO_STEERING_PREAMBLE = (
    "IMPORTANT: Execute all steps directly. "
    "Do NOT show steering options, draft choices, or ask clarifying questions. "
    "If uncertain, pick the most direct approach and proceed. "
    "Post results to bus when done. "
)


DISPATCH_LOG = DATA_DIR / "dispatch_log.json"

# Self-dispatch identity: set via env var SKYNET_WORKER_NAME or marker file
_SELF_WORKER_NAME = os.environ.get("SKYNET_WORKER_NAME", "")


def _get_self_identity():
    """Get the identity of THIS process to prevent self-dispatch."""
    if _SELF_WORKER_NAME:
        return _SELF_WORKER_NAME
    marker = DATA_DIR / "self_identity.txt"
    if marker.exists():
        return marker.read_text().strip()
    return ""


def _log_dispatch(worker_name, task, state, success, target_hwnd=0):
    """Append dispatch event to dispatch_log.json."""
    try:
        if DISPATCH_LOG.exists():
            log_data = json.loads(DISPATCH_LOG.read_text(encoding="utf-8"))
        else:
            log_data = []
        log_data.append({
            "worker": worker_name,
            "task_summary": task[:100],
            "timestamp": datetime.now().isoformat(),
            "state_at_dispatch": state,
            "success": success,
            "target_hwnd": target_hwnd,
            "result_received": False,
            "strategy": os.environ.get("SKYNET_STRATEGY", "direct"),
        })
        # Keep last 200 entries
        if len(log_data) > 200:
            log_data = log_data[-200:]
        DISPATCH_LOG.write_text(json.dumps(log_data, indent=2), encoding="utf-8")
    except Exception:
        pass


def mark_dispatch_received(worker_name):
    """Mark the most recent pending dispatch for a worker as received.
    Called when a bus result arrives from that worker."""
    try:
        if not DISPATCH_LOG.exists():
            return
        log_data = json.loads(DISPATCH_LOG.read_text(encoding="utf-8"))
        # Find most recent unreceived entry for this worker (reverse scan)
        for entry in reversed(log_data):
            if entry.get("worker") == worker_name and not entry.get("result_received"):
                entry["result_received"] = True
                entry["received_at"] = datetime.now().isoformat()
                break
        DISPATCH_LOG.write_text(json.dumps(log_data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── Worker heartbeat ────────────────────────────────────────────────────────

def send_heartbeat(worker_name, status="IDLE", current_task=""):
    """POST heartbeat to Skynet backend for worker health tracking."""
    from urllib.request import urlopen, Request
    from urllib.error import URLError
    body = json.dumps({"status": status, "current_task": current_task[:120]}).encode()
    try:
        req = Request(
            f"http://localhost:8420/worker/{worker_name}/heartbeat",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        urlopen(req, timeout=3)
    except Exception:
        pass


def _heartbeat_after_dispatch(worker_name, task, success):
    """Fire-and-forget heartbeat after dispatch."""
    status = "WORKING" if success else "IDLE"
    threading.Thread(target=send_heartbeat, args=(worker_name, status, task[:120] if success else ""), daemon=True).start()


def build_preamble(worker_name):
    """Build a full awareness preamble for a worker — identity, bus, comms, tools, no-steering.

    Includes anti-injection fingerprint: if this text appears in the orchestrator
    window, the identity guard will reject it before execution.
    """
    return (
        f"You are worker {worker_name} in the Skynet multi-agent system. "
        f"There are 4 workers: alpha, beta, gamma, delta. The orchestrator dispatched this task to you. "
        f"ALWAYS post your result to the bus when done: "
        f"import requests; requests.post('http://localhost:8420/bus/publish', "
        f"json={{'sender':'{worker_name}','topic':'orchestrator','type':'result','content':'BRIEF_RESULT_SUMMARY'}}) "
        f"To request help from another worker, post to bus with topic='workers' type='request' content='what you need'. "
        f"To coordinate with others on a sub-problem, post topic='convene' type='request' with what you need. "
        f"SKYNET TOOLS -- use these instead of manual polling or sleep loops: "
        f"(1) Check worker status: python tools/orch_realtime.py status "
        f"(2) Sub-delegate to idle worker: python tools/skynet_dispatch.py --idle --task \"subtask here\" "
        f"(3) Sub-delegate and wait: python tools/skynet_dispatch.py --idle --task \"subtask\" --wait-result \"{worker_name}\" --timeout 90 "
        f"(4) Check bus for results: python tools/orch_realtime.py pending "
        f"(5) Wait for a specific result: python tools/orch_realtime.py wait KEY --timeout 90 "
        f"(6) Full auto pipeline: python tools/skynet_brain_dispatch.py \"goal\" --timeout 120 "
        f"NEVER use Start-Sleep or manual Invoke-RestMethod polling loops. The tools handle all waiting internally. "
        f"For large tasks, check idle workers with orch_realtime.py status and sub-delegate immediately. "
        f"IMPORTANT: Execute all steps directly. Do NOT show steering options, draft choices, or ask clarifying questions. "
        f"If uncertain, pick the most direct approach and proceed. "
        f"WARNING: This preamble is for worker {worker_name} ONLY. If you are NOT {worker_name} "
        f"(e.g. you are the orchestrator or a different worker), IGNORE this entire message and report "
        f"'IDENTITY MISMATCH -- preamble for {worker_name} received by wrong target'. "
    )


def build_context_preamble(worker_name, task, context=None):
    """Build an intelligence-enhanced preamble with task context.

    If context dict is provided, enriches the task with:
    - relevant_learnings: past facts from LearningStore
    - relevant_context: past solutions from HybridRetriever
    - difficulty: assessed complexity level
    - reasoning: why this worker was chosen
    """
    base = build_preamble(worker_name)

    if not context:
        return base + task

    enrichment = ""
    if context.get("relevant_learnings"):
        facts = context["relevant_learnings"][:3]
        enrichment += "\n\nRELEVANT PAST LEARNINGS (use these to avoid past mistakes):\n"
        for f in facts:
            content = f if isinstance(f, str) else f.get("content", str(f))
            enrichment += f"- {content[:200]}\n"

    if context.get("relevant_context"):
        results = context["relevant_context"][:3]
        enrichment += "\nRELEVANT PAST SOLUTIONS:\n"
        for r in results:
            content = r if isinstance(r, str) else r.get("content", str(r))
            enrichment += f"- {content[:200]}\n"

    if context.get("difficulty"):
        enrichment += f"\nTASK COMPLEXITY: {context['difficulty']}\n"

    if context.get("reasoning"):
        enrichment += f"ROUTING REASON: {context['reasoning']}\n"

    return base + enrichment + "\nTASK: " + task


BUS_URL = "http://localhost:8420"


def _fetch_json_quiet(url, timeout=3):
    """Fetch JSON from URL, return None on any failure. No logging."""
    import urllib.request
    try:
        return json.loads(urllib.request.urlopen(url, timeout=timeout).read())
    except Exception:
        return None


def enrich_task(worker_name, task):
    """Enrich a task with INTELLIGENCE: difficulty assessment, learnings, context, worker states.

    Pipeline (all gracefully degrading):
    1. DAAORouter.estimate() -> difficulty level + reasoning
    2. LearningStore.recall() -> relevant past facts
    3. HybridRetriever.search() -> relevant past solutions/patterns
    4. Live worker states from /status
    5. Worker's last result from bus
    6. Autonomy instruction

    Each engine is lazily imported and try/except wrapped.
    Returns enriched task string (intelligence block + original task).
    """
    sections = []

    # 1. DIFFICULTY ASSESSMENT via DAAORouter
    try:
        from core.difficulty_router import DifficultyEstimator
        estimator = DifficultyEstimator()
        signal = estimator.estimate(task)
        level = signal.level.name if hasattr(signal.level, 'name') else str(signal.level).upper()
        domains = ", ".join(signal.domain_tags) if signal.domain_tags else "general"
        sections.append(
            f"[DIFFICULTY] {level} (score={signal.complexity_score:.2f}, "
            f"domains={domains}, confidence={signal.confidence:.2f})"
        )
    except Exception:
        pass

    # 2. RECALL LEARNINGS from LearningStore
    try:
        from core.learning_store import LearningStore
        store = LearningStore()
        facts = store.recall(task, top_k=3)
        if facts:
            lines = []
            for i, f in enumerate(facts, 1):
                content = f.content if hasattr(f, 'content') else str(f)
                conf = f.confidence if hasattr(f, 'confidence') else 0
                lines.append(f"{i}. {content[:150]} (confidence: {conf:.2f})")
            sections.append("[LEARNINGS] " + "; ".join(lines))
    except Exception:
        pass

    # 3. RETRIEVE CONTEXT from HybridRetriever
    try:
        from core.hybrid_retrieval import HybridRetriever
        retriever = HybridRetriever()
        results = retriever.search(task, limit=3)
        if results:
            lines = []
            for i, r in enumerate(results, 1):
                content = r.content if hasattr(r, 'content') else str(r)
                score = r.score if hasattr(r, 'score') else 0
                lines.append(f"{i}. {content[:150]} (relevance: {score:.2f})")
            sections.append("[CONTEXT] " + "; ".join(lines))
    except Exception:
        pass

    # 4. OTHER WORKER STATES from /status
    try:
        status = _fetch_json_quiet(f"{BUS_URL}/status")
        if status and isinstance(status, dict):
            agents = status.get("agents", {})
            states = []
            if isinstance(agents, dict):
                for name, info in agents.items():
                    if name.lower() != worker_name.lower():
                        st = info.get("status", "?") if isinstance(info, dict) else "?"
                        task_short = str(info.get("current_task", ""))[:40] if isinstance(info, dict) else ""
                        if task_short:
                            states.append(f"{name}={st}({task_short})")
                        else:
                            states.append(f"{name}={st}")
            elif isinstance(agents, list):
                for a in agents:
                    name = a.get("name", "?")
                    if name.lower() != worker_name.lower():
                        st = a.get("status", "?")
                        task_short = str(a.get("current_task", ""))[:40]
                        if task_short:
                            states.append(f"{name}={st}({task_short})")
                        else:
                            states.append(f"{name}={st}")
            if states:
                sections.append(f"[WORKERS] {', '.join(states)}")
    except Exception:
        pass

    # 5. WORKER'S LAST RESULT from bus
    try:
        msgs = _fetch_json_quiet(f"{BUS_URL}/bus/messages?limit=20")
        if msgs and isinstance(msgs, list):
            for m in msgs:
                if m.get("sender") == worker_name and m.get("type") == "result":
                    content = str(m.get("content", ""))[:100]
                    sections.append(f"[LAST_RESULT] {content}")
                    break
    except Exception:
        pass

    # 6. AUTONOMY INSTRUCTION
    sections.append(
        "After this task: check your TODOs (skynet_todos.py), check bus for pending "
        "requests from other workers, and if idle propose your next improvement. "
        "You are autonomous -- do not wait to be told."
    )

    if not sections:
        return task

    context_block = "--- SKYNET INTELLIGENCE ---\n" + " | ".join(sections) + "\n---\n"
    return context_block + task


def detect_steering(hwnd):
    """Return True if the worker window is showing a STEERING panel — UIA-based, no screenshot."""
    state = get_worker_state_uia(hwnd)
    return state == "STEERING"


def get_worker_state_uia(hwnd):
    """Detect worker window state via COM UIA engine — no PowerShell spawn needed.

    Returns one of: IDLE, PROCESSING, STEERING, TYPING, UNKNOWN
    """
    from tools.uia_engine import get_engine
    return get_engine().get_state(hwnd)


def wait_for_idle_uia(hwnd, timeout=60, poll_interval=2.0):
    """Poll worker state via COM UIA engine until IDLE or timeout. Returns True if became IDLE."""
    from tools.uia_engine import get_engine
    engine = get_engine()
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = engine.get_state(hwnd)
        if state == "IDLE":
            return True
        if state == "STEERING":
            log(f"HWND={hwnd} STEERING detected during wait — auto-cancelling", "WARN")
            clear_steering_and_send(hwnd, "", load_orch_hwnd())
        time.sleep(poll_interval)  # UIA state poll — keep as-is (local COM, not network)
    return False


def confirm_typed_uia(hwnd):
    """Return True if worker input box has content — uses COM UIA engine."""
    from tools.uia_engine import get_engine
    return get_engine().get_state(hwnd) == "TYPING"


def clear_steering_and_send(hwnd, task, orch_hwnd):
    """Cancel STEERING panel via 'Cancel (Alt+Backspace)' UIA button, then dispatch task normally.

    Discovery: The correct STEERING resolution is invoking Button 'Cancel (Alt+Backspace)'
    via UIA InvokePattern — NOT 'Steer with Message', NOT clicking cards, NOT Enter key.
    After cancel, a 'pending requests' dialog may appear: click 'Remove Pending Requests'.
    """
    # Fast-path: COM UIA cancel (~50ms) before falling back to PowerShell (~500ms)
    try:
        from tools.uia_engine import get_engine
        engine = get_engine()
        if engine.cancel_generation(hwnd):
            log("STEERING cancelled via COM UIA", "OK")
            time.sleep(0.8)  # Post-cancel settle delay — keep as-is (UIA operation)
            user32.SetForegroundWindow(orch_hwnd)
            return True
    except Exception:
        pass

    ps = f'''
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type @"
using System.Runtime.InteropServices;
public class SteerCancel {{
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr h);
    [DllImport("user32.dll")] public static extern void SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint d, uint e);
}}
"@
$hwnd = [IntPtr]{hwnd}
$orch = [IntPtr]{orch_hwnd}
[SteerCancel]::SetForegroundWindow($hwnd)
Start-Sleep -Milliseconds 600

$wnd = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
# Step 1: Invoke Cancel (Alt+Backspace) to dismiss STEERING
$cancelBtn = $wnd.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, 'Cancel (Alt+Backspace)')))
if ($cancelBtn) {{
    $cancelBtn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    Write-Host "STEERING-CANCELLED"
    Start-Sleep -Milliseconds 800
}} else {{
    Write-Host "NO-CANCEL-BTN"
}}
# Step 2: Handle 'pending requests' dialog if it appears — click Remove Pending
$wnd2 = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
$allBtns = $wnd2.FindAll([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button)))
foreach ($b in $allBtns) {{
    if ($b.Current.Name -match 'Remove Pending') {{
        $r = $b.Current.BoundingRectangle
        $cx = [int]($r.Left + $r.Width/2); $cy = [int]($r.Top + $r.Height/2)
        [SteerCancel]::SetCursorPos($cx, $cy)
        Start-Sleep -Milliseconds 150
        [SteerCancel]::mouse_event(2,0,0,0,0)
        Start-Sleep -Milliseconds 80
        [SteerCancel]::mouse_event(4,0,0,0,0)
        Write-Host "REMOVED-PENDING"
        break
    }}
}}
Start-Sleep -Milliseconds 400
[SteerCancel]::SetForegroundWindow($orch)
Write-Host "OK-STEER-BYPASS"
'''
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=20,
            creationflags=0x08000000  # CREATE_NO_WINDOW
        )
        cancelled = "STEERING-CANCELLED" in r.stdout
        log(f"STEERING cancel result: {r.stdout.strip()}", "OK" if cancelled else "WARN")
        return "OK-STEER-BYPASS" in r.stdout
    except Exception as e:
        log(f"Steer-bypass failed: {e}", "ERR")
        return False


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"INFO": "🔵", "OK": "🟢", "WARN": "🟡", "ERR": "🔴", "SYS": "⚡"}.get(level, "  ")
    print(f"[{ts}] {prefix} {msg}", flush=True)


def load_workers():
    if not WORKERS_FILE.exists():
        log("No workers.json", "ERR")
        return []
    data = json.loads(WORKERS_FILE.read_text())
    return data.get("workers", [])


def load_orch_hwnd():
    if ORCH_FILE.exists():
        data = json.loads(ORCH_FILE.read_text())
        return data.get("orchestrator_hwnd")
    return None


def ghost_type_to_worker(hwnd, text, orch_hwnd):
    """Type text into a worker chat window via clipboard paste.

    Level 4.1 -- CMD glitch elimination:
    - Clipboard save/restore (user clipboard never lost)
    - AttachThreadInput for less-visible focus transfer (no Z-order flash)
    - Minimized sleep durations (~200ms vs old 500ms)
    - CREATE_NO_WINDOW flag on subprocess (no console flash)
    """
    safe_text = text.replace("'", "''").replace('"', '`"').replace("\n", " ")

    ps = f'''
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms
Add-Type @"
using System; using System.Runtime.InteropServices; using System.Text;
public class GhostType {{
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr FindWindowEx(IntPtr p, IntPtr c, string cls, string w);
    [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
    [DllImport("user32.dll")] public static extern IntPtr SetFocus(IntPtr h);
    public static IntPtr FindRender(IntPtr hwnd) {{
        var h = FindWindowEx(hwnd, IntPtr.Zero, null, null);
        while (h != IntPtr.Zero) {{
            var sb = new StringBuilder(256); GetClassName(h, sb, 256);
            if (sb.ToString() == "Chrome_RenderWidgetHostHWND") return h;
            var f = FindRender(h); if (f != IntPtr.Zero) return f;
            h = FindWindowEx(hwnd, h, null, null);
        }}
        return IntPtr.Zero;
    }}
    public static bool FocusViaAttach(IntPtr target) {{
        uint targetTid = GetWindowThreadProcessId(target, out _);
        uint myTid = GetCurrentThreadId();
        if (targetTid == 0) return false;
        AttachThreadInput(myTid, targetTid, true);
        SetFocus(target);
        return true;
    }}
    public static void DetachThread(IntPtr target) {{
        uint targetTid = GetWindowThreadProcessId(target, out _);
        uint myTid = GetCurrentThreadId();
        AttachThreadInput(myTid, targetTid, false);
    }}
}}
"@

$hwnd = [IntPtr]{hwnd}
$orchHwnd = [IntPtr]{orch_hwnd}

# Auto-cancel STEERING if present (UIA InvokePattern -- no focus needed)
$wnd = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
$cancelBtn = $wnd.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, 'Cancel (Alt+Backspace)')))
if ($cancelBtn) {{
    try {{
        $cancelBtn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
        Write-Host "DEBUG: STEERING cancelled"
        Start-Sleep -Milliseconds 800
    }} catch {{ Write-Host "DEBUG: Cancel invoke failed: $_" }}
}}

# Locate bottommost Edit (chat input box) via UIA
$wnd = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
$allEdits = $wnd.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Edit
    ))
)
$edit = $null
$maxY = -1
foreach ($e in $allEdits) {{
    try {{
        $r = $e.Current.BoundingRectangle
        if ($r.Y -gt $maxY) {{ $maxY = $r.Y; $edit = $e }}
    }} catch {{}}
}}
if ($edit) {{
    # Save user clipboard before overwriting
    $savedClip = $null
    try {{ $savedClip = [System.Windows.Forms.Clipboard]::GetText() }} catch {{}}

    # Set dispatch text to clipboard
    [System.Windows.Forms.Clipboard]::SetText("{safe_text}")
    Start-Sleep -Milliseconds 50

    # PRIMARY: AttachThreadInput -- transfers keyboard focus without changing
    # the foreground window (no Z-order change = no visible CMD blink)
    $attached = [GhostType]::FocusViaAttach($hwnd)
    if ($attached) {{
        try {{ $edit.SetFocus() }} catch {{}}
        Start-Sleep -Milliseconds 80
        [System.Windows.Forms.SendKeys]::SendWait("^v")
        Start-Sleep -Milliseconds 80
        [System.Windows.Forms.SendKeys]::SendWait("{{ENTER}}")
        [GhostType]::DetachThread($hwnd)
        Write-Host "OK_ATTACHED"
    }} else {{
        # FALLBACK: Brief SetForegroundWindow (minimized to ~160ms)
        try {{ $edit.SetFocus() }} catch {{}}
        [GhostType]::SetForegroundWindow($hwnd)
        Start-Sleep -Milliseconds 80
        [System.Windows.Forms.SendKeys]::SendWait("^v")
        Start-Sleep -Milliseconds 80
        [System.Windows.Forms.SendKeys]::SendWait("{{ENTER}}")
        [GhostType]::SetForegroundWindow($orchHwnd)
        Write-Host "OK_FALLBACK"
    }}

    # Restore user clipboard (don't leave dispatch text in clipboard)
    if ($savedClip -and $savedClip.Length -gt 0) {{
        Start-Sleep -Milliseconds 50
        try {{ [System.Windows.Forms.Clipboard]::SetText($savedClip) }} catch {{}}
    }}
}} else {{
    Write-Host "NO_EDIT"
}}
'''
    try:
        # Lock ensures only one thread uses clipboard+focus at a time
        with _dispatch_lock:
            # Create file-based lock so self-prompt daemon knows to back off
            try:
                DISPATCH_LOCK_FILE.write_text(json.dumps({
                    "hwnd": hwnd, "orch_hwnd": orch_hwnd,
                    "timestamp": datetime.now().isoformat()
                }), encoding="utf-8")
            except Exception:
                pass

            log(f"Ghost targeting HWND={hwnd} (orch={orch_hwnd})", "SYS")
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=20,
                creationflags=0x08000000  # CREATE_NO_WINDOW
            )

            # Post-dispatch: no foreground check needed (focusless dispatch)

            # Remove file lock
            try:
                DISPATCH_LOCK_FILE.unlink(missing_ok=True)
            except Exception:
                pass

            # Inter-dispatch cooldown — prevent clipboard races between workers
            time.sleep(0.5)

        ok = ("OK_ATTACHED" in r.stdout or "OK_FALLBACK" in r.stdout) and "NO_EDIT" not in r.stdout
        if not ok and r.stdout:
            log(f"Ghost output: {r.stdout.strip()[:200]}", "WARN")
        if r.stderr and r.stderr.strip():
            log(f"Ghost stderr: {r.stderr.strip()[:200]}", "WARN")
        return ok
    except Exception as e:
        log(f"Ghost type failed: {e}", "ERR")
        try:
            DISPATCH_LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def dispatch_to_worker(worker_name, task, workers=None, orch_hwnd=None, context=None):
    """Dispatch a single task to a specific worker. Always fires immediately.

    VS Code queues messages, so there is no reason to wait for IDLE state.
    Only STEERING is handled (auto-cancelled) before dispatch.
    """
    # Self-dispatch guard: refuse if target is this process
    self_id = _get_self_identity()
    if self_id and self_id.lower() == worker_name.lower():
        log(f"SELF-DISPATCH BLOCKED: {worker_name} tried to dispatch to itself!", "ERR")
        _log_dispatch(worker_name, task, "SELF_DISPATCH_BLOCKED", False)
        return False

    if not workers:
        workers = load_workers()
    if not orch_hwnd:
        orch_hwnd = load_orch_hwnd()

    t_start = time.time()
    target = None
    for w in workers:
        if w["name"] == worker_name:
            target = w
            break

    if not target:
        log(f"Worker '{worker_name}' not found", "ERR")
        return False

    hwnd = target["hwnd"]
    if not user32.IsWindowVisible(hwnd):
        log(f"Worker {worker_name.upper()} window not visible (HWND={hwnd})", "ERR")
        return False

    # Pre-dispatch UIA state check (no screenshot needed)
    pre_state = get_worker_state_uia(hwnd)
    log(f"→ {worker_name.upper()} [state={pre_state}] [HWND={hwnd}]: {task[:80]}{'...' if len(task) > 80 else ''}", "SYS")

    if pre_state == "STEERING":
        log(f"STEERING detected on {worker_name.upper()} -- auto-cancelling before dispatch", "WARN")
        clear_steering_and_send(hwnd, "", orch_hwnd)
        time.sleep(1.0)  # Post-steering-cancel settle delay
    elif pre_state == "PROCESSING":
        log(f"{worker_name.upper()} is PROCESSING -- dispatching immediately (VS Code queues)", "SYS")

    # Inject full awareness preamble so worker knows identity, bus, and no-steering
    # Enrich task with live system context (worker states, last result, goal, autonomy)
    enriched_task = enrich_task(worker_name, task)
    full_task = build_context_preamble(worker_name, enriched_task, context) if context else build_preamble(worker_name) + enriched_task
    ok = ghost_type_to_worker(hwnd, full_task, orch_hwnd)

    if ok:
        log(f"✓ Dispatched to {worker_name.upper()} [HWND={hwnd}]", "OK")
        _log_dispatch(worker_name, task, pre_state, True, hwnd)
        notify_backend_dispatch(worker_name, task, True)
        try: metrics() and metrics().record_dispatch(worker_name, task, True, (time.time() - t_start) * 1000)
        except Exception: pass
        _heartbeat_after_dispatch(worker_name, task, True)
    else:
        log(f"✗ Failed to dispatch to {worker_name.upper()} — trying steer-bypass", "WARN")
        ok = clear_steering_and_send(hwnd, full_task, orch_hwnd)
        if ok:
            log(f"✓ Steer-bypass dispatched to {worker_name.upper()} [HWND={hwnd}]", "OK")
            _log_dispatch(worker_name, task, pre_state, True, hwnd)
            notify_backend_dispatch(worker_name, task, True)
            try: metrics() and metrics().record_dispatch(worker_name, task, True, (time.time() - t_start) * 1000, 'steer-bypass')
            except Exception: pass
            _heartbeat_after_dispatch(worker_name, task, True)
        else:
            log(f"✗ Steer-bypass also failed for {worker_name.upper()}", "ERR")
            _log_dispatch(worker_name, task, pre_state, False, hwnd)
            try: metrics() and metrics().record_dispatch(worker_name, task, False, (time.time() - t_start) * 1000)
            except Exception: pass
            _heartbeat_after_dispatch(worker_name, task, False)
    return ok


def notify_backend_dispatch(worker_name, task, success=True):
    """Fire-and-forget notification to Go backend so atomic counters increment.

    The Go backend only increments tasksDispatched when tasks come through /directive.
    Since ghost-type dispatches bypass /directive, this notifies the backend after
    successful UIA delivery so the dashboard shows accurate metrics.
    """
    def _notify():
        try:
            import urllib.request
            summary = task[:200].replace('\n', ' ')
            payload = json.dumps({
                "goal": f"[UIA-dispatch] {worker_name}: {summary}",
                "route": worker_name,
                "priority": 1,
            }).encode('utf-8')
            req = urllib.request.Request(
                f"http://localhost:{SKYNET_PORT}/directive",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass  # Fire-and-forget — never block dispatch pipeline

    if success:
        t = threading.Thread(target=_notify, daemon=True)
        t.start()


def batch_dispatch(task_map, workers=None, orch_hwnd=None):
    """Smart batch dispatch — consolidates multiple tasks per worker into mega-prompts.

    task_map: dict of worker_name → list of task strings
        e.g. {"alpha": ["task1", "task2"], "beta": ["task3"]}
    Tasks for the same worker are merged into a single numbered mega-prompt,
    reducing focus-steal overhead from N dispatches to M workers (M <= N).
    Returns dict of worker_name → True/False.
    """
    if not workers:
        workers = load_workers()
    if not orch_hwnd:
        orch_hwnd = load_orch_hwnd()

    # Consolidate: merge multiple tasks for the same worker into one mega-prompt
    consolidated = {}
    for worker_name, tasks in task_map.items():
        if isinstance(tasks, str):
            tasks = [tasks]
        if len(tasks) == 1:
            consolidated[worker_name] = tasks[0]
        else:
            parts = [f"TASK {i+1}/{len(tasks)}: {t}" for i, t in enumerate(tasks)]
            mega = (
                f"MULTI-TASK DISPATCH ({len(tasks)} tasks). "
                f"Execute ALL tasks below in order and report consolidated results.\n\n"
                + "\n\n".join(parts)
            )
            consolidated[worker_name] = mega
            log(f"Consolidated {len(tasks)} tasks for {worker_name.upper()} into 1 mega-prompt", "SYS")

    # Dispatch consolidated tasks in parallel
    return dispatch_parallel(consolidated, workers, orch_hwnd)


def dispatch_to_all(task, workers=None, orch_hwnd=None, delay=2.0):
    """Dispatch same task to ALL workers sequentially (use dispatch_parallel for speed)."""
    if not workers:
        workers = load_workers()
    if not orch_hwnd:
        orch_hwnd = load_orch_hwnd()

    results = {}
    for w in workers:
        ok = dispatch_to_worker(w["name"], task, workers, orch_hwnd)
        results[w["name"]] = ok
        if delay > 0:
            time.sleep(delay)  # Inter-dispatch delay for sequential sends — keep as-is

    return results


def dispatch_parallel(tasks_by_worker, workers=None, orch_hwnd=None, max_workers=8):
    """Dispatch different tasks to different workers IN PARALLEL — no sequential delay.

    Uses ThreadPoolExecutor so all workers start simultaneously.
    tasks_by_worker: dict like {"alpha": "task1", "beta": "task2", ...}
    Returns dict of worker_name → True/False
    """
    if not workers:
        workers = load_workers()
    if not orch_hwnd:
        orch_hwnd = load_orch_hwnd()

    t_start = time.time()

    # Scan all worker UIA states in parallel first
    worker_map = {w["name"]: w for w in workers}

    def _dispatch_one(name_task):
        name, task = name_task
        return name, dispatch_to_worker(name, task, workers, orch_hwnd)

    n = min(max_workers, len(tasks_by_worker))
    results = {}
    log(f"Parallel dispatch → {list(tasks_by_worker.keys())} ({n} threads)", "SYS")
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = {pool.submit(dispatch_to_worker, name, task, workers, orch_hwnd): name
                   for name, task in tasks_by_worker.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results[name] = fut.result()
            except Exception as e:
                log(f"Parallel dispatch error for {name}: {e}", "ERR")
                results[name] = False

    ok_count = sum(1 for v in results.values() if v)
    log(f"Parallel dispatch complete: {ok_count}/{len(results)} succeeded", "OK" if ok_count == len(results) else "WARN")
    try: metrics() and metrics().record_e2e_task(f"parallel_{int(time.time())}", list(tasks_by_worker.keys()), (time.time() - t_start) * 1000, ok_count, len(results) - ok_count)
    except Exception: pass
    return results


def blast_all(task, workers=None, orch_hwnd=None):
    """Fastest possible broadcast: parallel dispatch to ALL idle workers, no preamble overhead.

    Use for short commands where speed matters more than steering suppression.
    """
    if not workers:
        workers = load_workers()
    if not orch_hwnd:
        orch_hwnd = load_orch_hwnd()

    # Parallel UIA state scan
    states = scan_all_states(workers)
    targets = {w["name"]: task for w in workers if states.get(w["name"]) == "IDLE"}

    if not targets:
        log("blast_all: no IDLE workers available", "WARN")
        return {}

    log(f"BLAST → {list(targets.keys())} simultaneously", "SYS")
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {pool.submit(ghost_type_to_worker, w["hwnd"], task, orch_hwnd): w["name"]
                   for w in workers if w["name"] in targets}
        results = {}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results[name] = fut.result()
            except Exception as e:
                results[name] = False
    log(f"BLAST complete: {results}", "OK")
    return results


def scan_all_states(workers=None):
    """Parallel UIA state scan of all workers via COM engine. Returns dict name→state."""
    if not workers:
        workers = load_workers()
    from tools.uia_engine import get_engine
    engine = get_engine()
    hwnds = {w["name"]: w["hwnd"] for w in workers}
    t0 = time.perf_counter()
    results = engine.scan_all(hwnds)
    total_ms = (time.perf_counter() - t0) * 1000
    try: metrics() and metrics().record_uia_scan({n: r.to_dict() for n, r in results.items()}, total_ms, 'parallel')
    except Exception: pass
    return {name: r.state for name, r in results.items()}


def smart_dispatch(task, workers=None, orch_hwnd=None, n_workers=1):
    """Auto-route task to the best available worker(s).

    Uses expertise-aware routing: score = expertise_match * expertise_weight + inverse_load * load_weight.
    Falls back to load-only scoring if no expertise data available.
    """
    if not workers:
        workers = load_workers()
    if not orch_hwnd:
        orch_hwnd = load_orch_hwnd()

    # Parallel state scan
    states = scan_all_states(workers)

    # Get bus load per worker
    bus_statuses = get_worker_statuses()

    # Load expertise config and profiles
    expertise_weight = 0.6
    load_weight = 0.4
    try:
        bc = json.loads((DATA_DIR / "brain_config.json").read_text(encoding="utf-8"))
        routing = bc.get("routing", {})
        expertise_weight = routing.get("expertise_weight", 0.6)
        load_weight = routing.get("load_weight", 0.4)
    except Exception:
        pass

    profiles = {}
    try:
        pdata = json.loads((DATA_DIR / "agent_profiles.json").read_text(encoding="utf-8"))
        for k, v in pdata.items():
            if isinstance(v, dict) and k != "version" and k != "updated_at" and k != "updated_by":
                profiles[k] = v
    except Exception:
        pass

    # Extract task keywords for expertise matching
    task_lower = task.lower()
    task_words = set(task_lower.split())

    # Score: lower is better
    score_map = {"IDLE": 0, "TYPING": 1, "PROCESSING": 2, "STEERING": 3, "UNKNOWN": 4}

    def _expertise_score(worker_name):
        """Score 0.0 (no match) to 1.0 (perfect match) based on specializations."""
        profile = profiles.get(worker_name, {})
        specs = profile.get("specializations", [])
        if not specs:
            return 0.0
        matches = sum(1 for s in specs if s.lower() in task_lower or any(s.lower() in w for w in task_words))
        return min(1.0, matches / max(1, len(specs) * 0.3))

    def _load_score(worker_name):
        """Score 0.0 (idle, no pending) to 1.0 (busy, many pending)."""
        state = states.get(worker_name, "UNKNOWN")
        state_val = score_map.get(state, 4) / 4.0
        pending = bus_statuses.get(worker_name, {}).get("pending_tasks", 0)
        pending_val = min(1.0, pending / 5.0)
        return (state_val + pending_val) / 2.0

    def _combined_score(worker):
        """Combined score: higher is better (expertise up, load down)."""
        name = worker["name"]
        exp = _expertise_score(name)
        load = 1.0 - _load_score(name)  # invert: low load = high score
        return exp * expertise_weight + load * load_weight

    ranked = sorted(workers, key=lambda w: -_combined_score(w))

    # Log routing decision
    for w in ranked[:4]:
        name = w["name"]
        exp = _expertise_score(name)
        load = _load_score(name)
        combined = _combined_score(w)
        log(f"smart_route: {name} exp={exp:.2f} load={load:.2f} combined={combined:.2f} state={states.get(name, '?')}")

    selected = [w for w in ranked if states.get(w["name"]) == "IDLE"][:n_workers]
    if not selected:
        # Fall back to any non-STEERING worker
        selected = [w for w in ranked if states.get(w["name"]) not in ("STEERING", "UNKNOWN")][:n_workers]
    if not selected:
        log("smart_dispatch: no suitable workers", "ERR")
        return []

    if len(selected) == 1:
        ok = dispatch_to_worker(selected[0]["name"], task, workers, orch_hwnd)
        return [selected[0]["name"]] if ok else []
    else:
        tasks_map = {w["name"]: task for w in selected}
        results = dispatch_parallel(tasks_map, workers, orch_hwnd)
        return [name for name, ok in results.items() if ok]


def wait_for_bus_result(key, timeout=90, poll=2.0, skynet_url="http://localhost:8420",
                        auto_recover=True, _original_task=None):
    """Block until a bus message matching `key` (substring in content or sender) appears.

    Returns the matching message dict, or None on timeout.
    Tries file-based realtime wait first (0.5s resolution via data/realtime.json),
    falls back to HTTP polling (2.0s resolution) if realtime daemon is not running.

    If auto_recover=True and timeout is reached, attempts to cancel stuck PROCESSING
    workers via UIA and re-dispatch the original task ONE time.
    """
    import urllib.request
    deadline = time.time() + timeout
    seen_ids = set()
    key_lower = key.lower()

    # Try file-based realtime waiting (faster: 0.5s resolution, no network)
    realtime_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "realtime.json")
    use_realtime = os.path.exists(realtime_path)

    if use_realtime:
        log(f"Waiting via realtime file (0.5s resolution) for '{key}' (timeout={timeout}s)...", "INFO")
        while time.time() < deadline:
            try:
                with open(realtime_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                msgs = data if isinstance(data, list) else data.get("messages", data.get("results", []))
                for m in msgs:
                    if not isinstance(m, dict):
                        continue
                    mid = m.get("id", "")
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                    content = m.get("content", "")
                    sender = m.get("sender", "")
                    if key_lower in content.lower() or key_lower in sender.lower():
                        log(f"Result found from {sender}: {content[:100]}", "OK")
                        return m
            except (json.JSONDecodeError, OSError):
                pass
            time.sleep(0.5)
    else:
        log(f"Waiting via HTTP polling (2.0s resolution, realtime daemon not running) for '{key}' (timeout={timeout}s)...", "INFO")
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{skynet_url}/bus/messages?limit=50", timeout=3) as r:
                    msgs = json.loads(r.read())
                for m in msgs:
                    mid = m.get("id", "")
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                    content = m.get("content", "")
                    sender = m.get("sender", "")
                    if key_lower in content.lower() or key_lower in sender.lower():
                        log(f"Result found from {sender}: {content[:100]}", "OK")
                        return m
            except Exception:
                pass
            time.sleep(poll)

    # ── AUTO-RECOVERY: cancel stuck workers and retry once ──
    if auto_recover and _original_task:
        log(f"Timeout waiting for '{key}' -- attempting auto-recovery", "WARN")
        recovered = _auto_recover_stuck_workers(key, _original_task)
        if recovered:
            # Retry wait with same timeout, but disable auto_recover to prevent recursion
            return wait_for_bus_result(key, timeout=timeout, poll=poll,
                                       skynet_url=skynet_url, auto_recover=False)

    log(f"Timeout waiting for bus result matching '{key}'", "WARN")
    return None


def _auto_recover_stuck_workers(key, original_task):
    """Cancel PROCESSING workers that match `key` and re-dispatch the task.
    Returns True if at least one worker was recovered and re-dispatched."""
    try:
        from tools.uia_engine import get_engine
        engine = get_engine()
    except Exception as e:
        log(f"Auto-recover: UIA engine unavailable: {e}", "ERR")
        return False

    workers = load_workers()
    if not workers:
        log("Auto-recover: no workers loaded", "ERR")
        return False

    key_lower = key.lower()
    recovered_any = False

    for w in workers:
        wname = w.get("name", "")
        hwnd = w.get("hwnd", 0)
        if not hwnd:
            continue
        # Only target workers whose name matches the key we're waiting for
        if key_lower not in wname.lower():
            continue

        state = engine.get_state(int(hwnd))
        if state != "PROCESSING":
            log(f"Auto-recover: {wname.upper()} is {state}, not stuck", "INFO")
            continue

        log(f"Auto-recover: {wname.upper()} stuck PROCESSING -- cancelling generation", "WARN")
        try:
            engine.cancel_generation(int(hwnd))
        except Exception as e:
            log(f"Auto-recover: cancel failed for {wname.upper()}: {e}", "ERR")
            continue

        # Wait up to 3s for IDLE
        idle_deadline = time.time() + 3.0
        became_idle = False
        while time.time() < idle_deadline:
            s = engine.get_state(int(hwnd))
            if s == "IDLE":
                became_idle = True
                break
            time.sleep(0.5)

        if not became_idle:
            log(f"Auto-recover: {wname.upper()} did not become IDLE after cancel", "WARN")
            continue

        # Re-dispatch the original task
        log(f"Auto-recover: re-dispatching to {wname.upper()}", "SYS")
        orch_hwnd = load_orch_hwnd()
        ok = dispatch_to_worker(wname, original_task, workers, orch_hwnd)
        if ok:
            log(f"Auto-recover: re-dispatch to {wname.upper()} succeeded", "OK")
            recovered_any = True
        else:
            log(f"Auto-recover: re-dispatch to {wname.upper()} failed", "ERR")

    return recovered_any


def fan_out(tasks_by_worker, workers=None, orch_hwnd=None, delay=2.0):
    """Dispatch different tasks to different workers sequentially (use dispatch_parallel for speed)."""
    if not workers:
        workers = load_workers()
    if not orch_hwnd:
        orch_hwnd = load_orch_hwnd()

    results = {}
    for worker_name, task in tasks_by_worker.items():
        ok = dispatch_to_worker(worker_name, task, workers, orch_hwnd)
        results[worker_name] = ok
        if delay > 0:
            time.sleep(delay)  # Inter-dispatch delay for fan-out sends — keep as-is

    return results


def get_worker_statuses(skynet_url="http://localhost:8420"):
    """Query /worker/{name}/status + UIA state for all workers in parallel. No screenshots needed."""
    import urllib.request, urllib.error
    workers = load_workers()

    # Parallel UIA scan across all workers simultaneously
    uia_states = scan_all_states(workers)

    def _fetch_one(w):
        name = w["name"]
        try:
            with urllib.request.urlopen(f"{skynet_url}/worker/{name}/status", timeout=3) as r:
                s = json.loads(r.read())
        except Exception:
            s = {"worker": name, "alive": False, "pending_tasks": 0, "running_tasks": 0}
        s["uia_state"] = uia_states.get(name, "UNKNOWN")
        return name, s

    statuses = {}
    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        for name, s in pool.map(_fetch_one, workers):
            statuses[name] = s
    return statuses


def idle_workers(skynet_url="http://localhost:8420"):
    """Return list of worker names that are alive and have no pending/running tasks."""
    statuses = get_worker_statuses(skynet_url)
    return [
        name for name, s in statuses.items()
        if s.get("alive", False)
        and s.get("pending_tasks", 0) == 0
        and s.get("running_tasks", 0) == 0
    ]


def dispatch_to_idle(task, exclude=None, workers=None, orch_hwnd=None):
    """Dispatch a task to the first idle worker. Used for worker-to-worker sub-delegation.

    exclude: list of worker names to skip (e.g. the worker calling this)
    Returns the worker name it was dispatched to, or None.
    """
    if not workers:
        workers = load_workers()
    if not orch_hwnd:
        orch_hwnd = load_orch_hwnd()
    exclude = exclude or []

    idle = [w for w in idle_workers() if w not in exclude]
    if not idle:
        log("No idle workers available for sub-delegation", "WARN")
        return None

    target = idle[0]
    ok = dispatch_to_worker(target, task, workers, orch_hwnd)
    if ok:
        log(f"Sub-delegated to idle worker {target.upper()}", "OK")
        return target
    return None


def poll_bus(limit=20, skynet_url="http://localhost:8420"):
    """Poll bus messages. Returns list of recent messages."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{skynet_url}/bus/messages?limit={limit}", timeout=3) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"Bus poll failed: {e}", "WARN")
        return []


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Skynet Dispatch — Send tasks to workers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes (fastest first):
  --blast        Parallel broadcast to ALL idle workers, no preamble. Max speed.
  --parallel     Parallel broadcast to ALL workers with steering preamble.
  --smart        Auto-route to best idle worker(s). Use --n for multiple.
  --worker NAME  Target specific worker.
  --idle         Dispatch to first available idle worker.
  --all          Sequential broadcast (legacy, slower).
  --fan-out-parallel FILE  Parallel fan-out from JSON map (fastest for complex tasks).
  --fan-out FILE Sequential fan-out from JSON map.

Examples:
  python skynet_dispatch.py --smart --task "analyse D:\\ML" --wait-result "SMART-01"
  python skynet_dispatch.py --parallel --task "health check"
  python skynet_dispatch.py --blast --task "Write-Host hello"
  python skynet_dispatch.py --wait-result "ALPHA-" --timeout 60
""")
    parser.add_argument("--worker", type=str, help="Target worker name")
    parser.add_argument("--task", type=str, help="Task to dispatch")
    parser.add_argument("--all", action="store_true", help="Sequential broadcast to all workers")
    parser.add_argument("--parallel", action="store_true", help="PARALLEL broadcast to all workers simultaneously")
    parser.add_argument("--blast", action="store_true", help="FASTEST: parallel to all IDLE workers, no preamble")
    parser.add_argument("--smart", action="store_true", help="Auto-route to best idle worker(s)")
    parser.add_argument("--n", type=int, default=1, help="Number of workers for --smart (default 1)")
    parser.add_argument("--fan-out", type=str, help="JSON file with worker→task mapping (sequential)")
    parser.add_argument("--fan-out-parallel", type=str, help="JSON file with worker→task mapping (parallel, faster)")
    parser.add_argument("--batch", type=str, help="JSON file with worker→[task list] mapping (consolidates same-worker tasks)")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between dispatches for sequential modes (seconds)")
    parser.add_argument("--idle", action="store_true", help="Dispatch task to first idle worker")
    parser.add_argument("--exclude", type=str, help="Comma-separated worker names to exclude")
    parser.add_argument("--bus-status", action="store_true", help="Poll bus and print recent messages + worker statuses")
    parser.add_argument("--open-project", type=str, help="Open a project dir in VS Code Insiders with its venv")
    parser.add_argument("--state", type=str, help="Get UIA state of a specific worker (e.g. --state gamma)")
    parser.add_argument("--state-all", action="store_true", help="Get UIA state of ALL workers (parallel scan)")
    parser.add_argument("--wait-result", type=str, help="After dispatch, wait for bus result matching this key")
    parser.add_argument("--timeout", type=float, default=90, help="Timeout for --wait-result (default 90s)")
    args = parser.parse_args()

    workers = load_workers()
    orch_hwnd = load_orch_hwnd()

    # ── State query ──────────────────────────────────────────────────
    if args.state_all:
        states = scan_all_states(workers)
        log("=== UIA STATES (parallel scan) ===", "SYS")
        for name, state in states.items():
            icon = {"IDLE": "✅", "PROCESSING": "⏳", "STEERING": "⚠️", "TYPING": "✏️"}.get(state, "❓")
            print(f"  {name.upper():<8} {icon} {state}")
        return

    if args.state:
        target_workers = [w for w in workers if w["name"] == args.state]
        if not target_workers:
            log(f"Worker '{args.state}' not found", "ERR")
            return
        state = get_worker_state_uia(target_workers[0]["hwnd"])
        log(f"{args.state.upper()} UIA state: {state}", "INFO")
        return

    # ── Bus status ───────────────────────────────────────────────────
    if args.bus_status:
        log("=== BUS STATUS ===", "SYS")
        msgs = poll_bus(limit=15)
        for m in msgs[-10:]:
            ts = m.get("timestamp", "")[-8:]
            print(f"  [{ts}] {m.get('sender','?')}/{m.get('type','?')}: {m.get('content','')[:120]}")
        log("=== WORKER STATUS (parallel) ===", "SYS")
        statuses = get_worker_statuses()
        for name, s in statuses.items():
            alive = "ALIVE" if s.get("alive") else "DEAD"
            pending = s.get("pending_tasks", 0)
            running = s.get("running_tasks", 0)
            uia = s.get("uia_state", "?")
            icon = {"IDLE": "✅", "PROCESSING": "⏳", "STEERING": "⚠️", "TYPING": "✏️"}.get(uia, "❓")
            print(f"  {name.upper():<8} {alive:<6} pending={pending} running={running} {icon} {uia}")
        idle = idle_workers()
        log(f"Idle workers: {idle}", "INFO")
        return

    # ── Open project ─────────────────────────────────────────────────
    if args.open_project:
        project = args.open_project
        venv_candidates = [
            os.path.join(project, ".venv", "Scripts", "python.exe"),
            os.path.join(project, "env", "Scripts", "python.exe"),
            os.path.join(os.path.dirname(project), "env", "Scripts", "python.exe"),
        ]
        venv_py = next((v for v in venv_candidates if os.path.exists(v)), None)
        vscode_dir = os.path.join(project, ".vscode")
        os.makedirs(vscode_dir, exist_ok=True)
        settings_path = os.path.join(vscode_dir, "settings.json")
        settings = {}
        if os.path.exists(settings_path):
            with open(settings_path) as f:
                try: settings = json.load(f)
                except (json.JSONDecodeError, ValueError): settings = {}
        if venv_py:
            settings["python.defaultInterpreterPath"] = venv_py.replace("\\", "/")
            with open(settings_path, "w") as f:
                json.dump(settings, f, indent=2)
            log(f"Set python.defaultInterpreterPath → {venv_py}", "OK")
        else:
            log(f"No venv found in {project} — interpreter not set", "WARN")
        subprocess.Popen(["code-insiders", project], shell=True)
        log(f"Opened {project} in VS Code Insiders", "OK")
        return

    if not workers:
        log("No workers loaded", "ERR")
        return

    # ── Dispatch modes ───────────────────────────────────────────────
    t0 = time.time()
    result = None

    if args.blast and args.task:
        # FASTEST: parallel to idle workers, no preamble
        result = blast_all(args.task, workers, orch_hwnd)

    elif args.parallel and args.task:
        # Parallel broadcast to all workers with preamble
        tasks_map = {w["name"]: args.task for w in workers}
        result = dispatch_parallel(tasks_map, workers, orch_hwnd)

    elif args.smart and args.task:
        # Auto-route to best idle worker(s)
        routed = smart_dispatch(args.task, workers, orch_hwnd, n_workers=args.n)
        log(f"Smart-dispatched to: {routed}", "OK" if routed else "ERR")
        result = routed

    elif args.fan_out_parallel:
        with open(args.fan_out_parallel) as f:
            tasks = json.load(f)
        result = dispatch_parallel(tasks, workers, orch_hwnd)

    elif args.batch:
        with open(args.batch) as f:
            task_map = json.load(f)
        result = batch_dispatch(task_map, workers, orch_hwnd)

    elif args.fan_out:
        with open(args.fan_out) as f:
            tasks = json.load(f)
        result = fan_out(tasks, workers, orch_hwnd, args.delay)

    elif args.idle and args.task:
        exclude = [x.strip() for x in args.exclude.split(",")] if args.exclude else []
        target = dispatch_to_idle(args.task, exclude=exclude, workers=workers, orch_hwnd=orch_hwnd)
        log(f"Dispatched to idle worker: {target}" if target else "No idle worker available",
            "OK" if target else "ERR")
        result = target

    elif args.all and args.task:
        result = dispatch_to_all(args.task, workers, orch_hwnd, args.delay)

    elif args.worker and args.task:
        result = dispatch_to_worker(args.worker, args.task, workers, orch_hwnd)

    else:
        parser.print_help()
        return

    elapsed = time.time() - t0
    log(f"Dispatch took {elapsed:.2f}s", "INFO")

    # ── Optional result wait ─────────────────────────────────────────
    if args.wait_result:
        wait_for_bus_result(args.wait_result, timeout=args.timeout)


if __name__ == "__main__":
    main()
