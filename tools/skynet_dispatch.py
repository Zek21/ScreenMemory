#!/usr/bin/env python3
"""
Skynet Dispatch — Send tasks to worker chat windows via ghost automation.

This is the bridge: Orchestrator → ghost-type prompt into worker CLI window.
Uses clipboard paste via PostMessage — zero cursor movement.

Usage:
    python tools/skynet_dispatch.py --worker alpha --task "List all Python files in core/"
    python tools/skynet_dispatch.py --worker orchestrator --task "Review the elevated convene report"
    python tools/skynet_dispatch.py --worker consultant --task "Advisory request: analyze the queue design"
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
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            print(f"[dispatch] Failed to load critical_processes.json: {e}", file=sys.stderr)  # signed: beta
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
        msg = {"sender": "process_guard", "topic": "orchestrator",
               "type": "alert", "content": alert}
        try:
            from tools.skynet_spam_guard import guarded_publish
            guarded_publish(msg)
        except Exception:
            # Raw fallback for when SpamGuard is unavailable
            try:
                import urllib.request
                payload = json.dumps(msg).encode()
                req = urllib.request.Request(
                    "http://localhost:8420/bus/publish", payload,
                    {"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=3)
            except Exception as e:
                print(f"[GUARD] Bus alert failed: {e}", file=sys.stderr)
        # signed: alpha
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
        except (ImportError, OSError, RuntimeError) as e:
            print(f"[dispatch] SkynetMetrics init failed: {e}", file=sys.stderr)  # signed: beta
    return _metrics

# DEPRECATED: Use build_preamble(worker_name) instead for full worker awareness.
NO_STEERING_PREAMBLE = (
    "IMPORTANT: Execute all steps directly. "
    "Do NOT show steering options, draft choices, or ask clarifying questions. "
    "If uncertain, pick the most direct approach and proceed. "
    "Post results to bus when done. "
)


DISPATCH_LOG = DATA_DIR / "dispatch_log.json"

# ── Tuning Constants ────────────────────────────────────────────────────────
# signed: gamma
DISPATCH_LOG_MAX_ENTRIES = 200        # max entries kept in dispatch_log.json
DELIVERY_VERIFY_CONSECUTIVE_UNKNOWN_LIMIT = 3  # UIA UNKNOWN readings before FAILED
DELIVERY_RETRY_MAX = 2                # max auto-retries on unverified delivery
DELIVERY_RETRY_BACKOFF_BASE = 2.0     # seconds; exponential: 2s, 4s, ...

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
    """Append dispatch event to dispatch_log.json (atomic)."""
    try:
        try:
            from tools.skynet_atomic import atomic_update_json
        except ModuleNotFoundError:
            from skynet_atomic import atomic_update_json
        def _append_entry(log_data):
            if not isinstance(log_data, list):
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
                "strategy_id": os.environ.get("SKYNET_STRATEGY_ID", ""),
            })
            if len(log_data) > DISPATCH_LOG_MAX_ENTRIES:  # signed: gamma
                log_data = log_data[-DISPATCH_LOG_MAX_ENTRIES:]
            return log_data
        atomic_update_json(DISPATCH_LOG, _append_entry, default=[])
    except Exception as e:
        print(f"[dispatch] _log_dispatch failed for {worker_name}: {e}", file=sys.stderr)  # signed: beta


def mark_dispatch_received(worker_name):
    """Mark the most recent pending dispatch for a worker as received.
    Called when a bus result arrives from that worker."""
    try:
        try:
            from tools.skynet_atomic import atomic_update_json
        except ModuleNotFoundError:
            from skynet_atomic import atomic_update_json
        def _mark_received(log_data):
            if not isinstance(log_data, list):
                return log_data
            for entry in reversed(log_data):
                if entry.get("worker") == worker_name and not entry.get("result_received"):
                    entry["result_received"] = True
                    entry["result_received_at"] = datetime.now().isoformat()  # signed: delta
                    break
            return log_data
        atomic_update_json(DISPATCH_LOG, _mark_received, default=[])
    except Exception as e:
        print(f"[dispatch] Failed to log result for {worker_name}: {e}", file=sys.stderr)  # signed: beta


# ── Worker heartbeat ────────────────────────────────────────────────────────

def send_heartbeat(worker_name, status="IDLE", current_task=""):
    """POST heartbeat to Skynet backend for worker health tracking."""
    from urllib.request import urlopen, Request  # signed: gamma
    body = json.dumps({"status": status, "current_task": current_task[:120]}).encode()
    try:
        req = Request(
            f"http://localhost:8420/worker/{worker_name}/heartbeat",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        urlopen(req, timeout=3)
    except Exception as e:
        print(f"[heartbeat] {worker_name} heartbeat failed: {e}", file=sys.stderr)  # signed: beta


def _heartbeat_after_dispatch(worker_name, task, success):
    """Fire-and-forget heartbeat after dispatch."""
    status = "WORKING" if success else "IDLE"
    threading.Thread(target=send_heartbeat, args=(worker_name, status, task[:120] if success else ""), daemon=True).start()


def build_preamble(worker_name):
    """Build a compact identity preamble for a worker.

    LEAN PREAMBLE: Workers have full rules via ScreenMemory agent context
    (AGENTS.md + copilot-instructions.md). This preamble provides ONLY:
    - Worker identity (who you are)
    - Bus result posting command (how to report results)
    - Signature + no-steering directives
    - Anti-injection fingerprint

    Previous preamble was ~5,500 chars — caused 'Copilot CLI delegation cancelled'
    errors by overwhelming the input buffer (see screenshots of gamma/delta).
    Trimmed to ~600 chars. All rules are in the agent context already.
    """  # signed: orchestrator
    return (
        f"You are worker {worker_name} in the Skynet multi-agent system. "
        f"The orchestrator dispatched this task to you. Execute it directly -- "
        f"no steering options, no draft choices, no clarifying questions. "
        f"WHEN DONE post your result: "
        f"python -c \"from tools.skynet_spam_guard import guarded_publish; "
        f"guarded_publish(dict(sender='{worker_name}',topic='orchestrator',"
        f"type='result',content='YOUR_RESULT signed:{worker_name}'))\" "
        f"Sign all code changes with '# signed: {worker_name}'. "
        f"Use update_todo to track subtasks. Check skynet_todos.py before going idle. "
        f"WARNING: This preamble is for {worker_name} ONLY. If you are NOT {worker_name}, "
        f"report 'IDENTITY MISMATCH'. "
    )


def build_context_preamble(worker_name, task, context=None):
    """Build an intelligence-enhanced preamble with task context.

    If context dict is provided, enriches the task with:
    - relevant_learnings: past facts from LearningStore
    - relevant_context: past solutions from HybridRetriever
    - difficulty: assessed complexity level
    - reasoning: why this worker was chosen
    - strategy_id: unique identifier for this dispatch plan
    """
    base = build_preamble(worker_name)

    if not context:
        # Still inject strategy_id from env if available
        sid = os.environ.get("SKYNET_STRATEGY_ID", "")
        if sid:
            return base + f"\n[STRATEGY_ID: {sid}] " + task
        return base + task

    enrichment = ""

    # Strategy ID for result correlation
    sid = context.get("strategy_id") or os.environ.get("SKYNET_STRATEGY_ID", "")
    if sid:
        enrichment += f"\n[STRATEGY_ID: {sid}] Include this ID in your bus result for tracking.\n"

    if context.get("relevant_learnings"):
        facts = context["relevant_learnings"][:3]
        enrichment += "\nRELEVANT PAST LEARNINGS (use these to avoid past mistakes):\n"
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


def _enrich_difficulty(task):
    """Assess task difficulty via DAAORouter. Returns section string or None."""
    try:
        from core.difficulty_router import DifficultyEstimator
        estimator = DifficultyEstimator()
        signal = estimator.estimate(task)
        level = signal.level.name if hasattr(signal.level, 'name') else str(signal.level).upper()
        domains = ", ".join(signal.domain_tags) if signal.domain_tags else "general"
        return (
            f"[DIFFICULTY] {level} (score={signal.complexity_score:.2f}, "
            f"domains={domains}, confidence={signal.confidence:.2f})"
        )
    except (ImportError, OSError, AttributeError, TypeError, ValueError):
        return None


def _enrich_learnings(task):
    """Recall relevant learnings from LearningStore. Returns section string or None.

    Content per entry capped at 100 chars to keep total enrichment under budget.
    """  # signed: beta
    try:
        from core.learning_store import LearningStore
        store = LearningStore()
        facts = store.recall(task, top_k=3)
        if facts:
            lines = []
            for i, f in enumerate(facts, 1):
                content = f.content if hasattr(f, 'content') else str(f)
                conf = f.confidence if hasattr(f, 'confidence') else 0
                lines.append(f"{i}. {content[:100]} (confidence: {conf:.2f})")
            return "[LEARNINGS] " + "; ".join(lines)
    except (ImportError, OSError, AttributeError, TypeError, ValueError):
        pass
    return None


def _enrich_context(task):
    """Retrieve relevant context from HybridRetriever. Returns section string or None.

    Content per entry capped at 100 chars to keep total enrichment under budget.
    """  # signed: beta
    try:
        from core.hybrid_retrieval import HybridRetriever
        retriever = HybridRetriever()
        results = retriever.search(task, limit=3)
        if results:
            lines = []
            for i, r in enumerate(results, 1):
                content = r.content if hasattr(r, 'content') else str(r)
                score = r.score if hasattr(r, 'score') else 0
                lines.append(f"{i}. {content[:100]} (relevance: {score:.2f})")
            return "[CONTEXT] " + "; ".join(lines)
    except (ImportError, OSError, AttributeError, TypeError, ValueError):
        pass
    return None


def _enrich_worker_states(worker_name):
    """Fetch other worker states from /status. Returns compact section string or None.

    Shows only name=status (no task excerpts) to keep enrichment lean.
    """  # signed: beta
    try:
        status = _fetch_json_quiet(f"{BUS_URL}/status")
        if not status or not isinstance(status, dict):
            return None
        agents = status.get("agents", {})
        states = []
        if isinstance(agents, dict):
            for name, info in agents.items():
                if name.lower() != worker_name.lower():
                    st = info.get("status", "?") if isinstance(info, dict) else "?"
                    states.append(f"{name}={st}")
        elif isinstance(agents, list):
            for a in agents:
                name = a.get("name", "?")
                if name.lower() != worker_name.lower():
                    st = a.get("status", "?")
                    states.append(f"{name}={st}")
        if states:
            return f"[WORKERS] {', '.join(states)}"
    except (ImportError, OSError, AttributeError, TypeError, ValueError, KeyError):
        pass
    return None


def _enrich_last_result(worker_name):
    """Fetch worker's last bus result. Returns section string or None."""
    try:
        msgs = _fetch_json_quiet(f"{BUS_URL}/bus/messages?limit=20")
        if msgs and isinstance(msgs, list):
            for m in msgs:
                if m.get("sender") == worker_name and m.get("type") == "result":
                    content = str(m.get("content", ""))[:100]
                    return f"[LAST_RESULT] {content}"
    except (ImportError, OSError, AttributeError, TypeError, ValueError, KeyError):  # signed: beta
        pass
    return None


_AUTONOMY_INSTRUCTION = (
    "After this task: check your TODOs (skynet_todos.py), check bus for pending "
    "requests from other workers, and if idle propose your next improvement. "
    "You are autonomous -- do not wait to be told."
)  # signed: beta


def _build_result_posting_reminder(worker_name):
    """Build a compact reminder to post results via guarded_publish.

    Placed right before the task text so the worker sees it immediately.
    Kept short — full posting instructions are in the preamble and agent context.
    """  # signed: orchestrator
    return (
        f"REMINDER: Post result to bus when done (guarded_publish, sender='{worker_name}'). "
    )


def enrich_task(worker_name, task):
    """Enrich a task with INTELLIGENCE: difficulty, learnings, context, worker states.

    Each enrichment engine is lazily imported and try/except wrapped.
    Total enrichment block is capped at 1200 chars to keep dispatch payload lean.
    Returns enriched task string (intelligence block + result reminder + original task).
    """  # signed: beta
    sections = [s for s in (
        _enrich_difficulty(task),
        _enrich_learnings(task),
        _enrich_context(task),
        _enrich_worker_states(worker_name),
        _enrich_last_result(worker_name),
        _AUTONOMY_INSTRUCTION,
    ) if s]

    # Result posting reminder placed right before task text so worker sees it last  # signed: beta
    reminder = _build_result_posting_reminder(worker_name)

    if not sections:
        return reminder + " " + task

    context_block = "--- SKYNET INTELLIGENCE ---\n" + " | ".join(sections) + "\n---\n"
    # Cap enrichment block to keep total payload under 3000 chars  # signed: beta
    _MAX_ENRICHMENT = 1200
    if len(context_block) > _MAX_ENRICHMENT:
        context_block = context_block[:_MAX_ENRICHMENT - 4] + "...\n"
    return context_block + reminder + " " + task


def pre_dispatch_visual_check(hwnd, worker_name):
    """Screenshot worker window before dispatch — visual verification for debugging.
    
    Saves screenshot to data/dispatch_screenshots/{worker}_{timestamp}.png.
    Returns (ok: bool, state: str, screenshot_path: str|None).
    """
    screenshot_dir = DATA_DIR / "dispatch_screenshots"
    screenshot_dir.mkdir(exist_ok=True)
    
    try:
        from tools.uia_engine import get_engine
        engine = get_engine()
        scan = engine.scan(hwnd)
        state = scan.state
        model_ok = scan.model_ok
        agent_ok = scan.agent_ok
    except Exception as ex:
        log(f"UIA scan failed for {worker_name}: {ex}", "WARN")
        state, model_ok, agent_ok = "UNKNOWN", None, None

    # Take screenshot via Desktop
    ss_path = None
    try:
        from tools.chrome_bridge.winctl import Desktop
        d = Desktop()
        ts = datetime.now().strftime("%H%M%S")
        ss_path = str(screenshot_dir / f"{worker_name}_{ts}.png")
        d.screenshot(path=ss_path, window=hwnd)
    except Exception as ex:
        log(f"Screenshot failed for {worker_name}: {ex}", "WARN")
        ss_path = None

    # Log visual check results
    log(f"👁 VISUAL CHECK {worker_name.upper()}: state={state} model_ok={model_ok} agent_ok={agent_ok}" +
        (f" ss={ss_path}" if ss_path else ""), "SYS")
    
    # Block dispatch if model is wrong (security)
    if model_ok is False:
        log(f"✗ {worker_name.upper()} model_ok=False — blocking dispatch", "SECURITY")
        return False, state, ss_path
    
    # Cleanup old screenshots (keep last 20 per worker)
    try:
        existing = sorted(screenshot_dir.glob(f"{worker_name}_*.png"))
        for old in existing[:-20]:
            old.unlink(missing_ok=True)
    except Exception:
        pass
    
    return True, state, ss_path


def detect_steering(hwnd):
    """Return True if the worker window is showing a STEERING panel.

    Uses a two-tier detection strategy for defense-in-depth:
      1. Primary: COM UIA engine state check (fast, ~10ms)
      2. Secondary: UIA tree scan for Cancel button with 'Alt+Backspace' in name,
         which is the definitive indicator of the STEERING panel.

    See docs/DELIVERY_PIPELINE.md Section 5 (Pre-Dispatch Visual Check) for context.

    Args:
        hwnd: Target worker window HWND

    Returns:
        bool: True if STEERING panel is detected by either method
    """  # signed: alpha
    state = get_worker_state_uia(hwnd)
    if state == "STEERING":
        return True
    # Secondary STEERING check: scan UIA tree for Cancel (Alt+Backspace) button
    # This catches cases where the UIA engine state doesn't report STEERING but the
    # panel is actually present. Defense-in-depth per docs/DELIVERY_PIPELINE.md Section 9.  # signed: alpha
    try:
        import ctypes
        if not ctypes.windll.user32.IsWindow(hwnd):
            return False
        from System.Windows.Automation import AutomationElement, TreeScope, PropertyCondition  # type: ignore
        wnd = AutomationElement.FromHandle(hwnd)
        cancel_btn = wnd.FindFirst(
            TreeScope.Descendants,
            PropertyCondition(AutomationElement.NameProperty, 'Cancel (Alt+Backspace)')
        )
        if cancel_btn is not None:
            log(f"STEERING detected by secondary UIA button scan for HWND={hwnd}", "WARN")
            return True
    except Exception:
        pass  # .NET UIA not available or window gone — fall through
    return False


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


def _build_steering_cancel_ps(hwnd, orch_hwnd):
    """Build the PowerShell script for cancelling STEERING via UIA."""
    return f'''
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


def clear_steering_and_send(hwnd, task, orch_hwnd):
    """Cancel STEERING panel via 'Cancel (Alt+Backspace)' UIA button, then dispatch task normally.

    Discovery: The correct STEERING resolution is invoking Button 'Cancel (Alt+Backspace)'
    via UIA InvokePattern -- NOT 'Steer with Message', NOT clicking cards, NOT Enter key.
    After cancel, a 'pending requests' dialog may appear: click 'Remove Pending Requests'.
    """
    # Rule 0.015: Pre-fire visual proof before any corrective action  # signed: orchestrator
    vis_ok, pre_state, ss_path = pre_dispatch_visual_check(hwnd, "steering_cancel")
    if not vis_ok:
        log(f"STEERING cancel BLOCKED: visual check failed (state={pre_state})", "SECURITY")
        return False

    try:
        from tools.uia_engine import get_engine
        engine = get_engine()
        if engine.cancel_generation(hwnd):
            log("STEERING cancelled via COM UIA", "OK")
            time.sleep(0.8)
            user32.SetForegroundWindow(orch_hwnd)
            return True
    except Exception as e:
        log(f"UIA steering cancel failed, falling back to PS: {e}", "WARN")  # signed: beta

    ps = _build_steering_cancel_ps(hwnd, orch_hwnd)
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=20,
            creationflags=0x08000000
        )
        cancelled = "STEERING-CANCELLED" in r.stdout
        log(f"STEERING cancel result: {r.stdout.strip()}", "OK" if cancelled else "WARN")
        return "OK-STEER-BYPASS" in r.stdout
    except Exception as e:
        log(f"Steer-bypass failed: {e}", "ERR")
        return False


def log(msg, level="INFO"):
    """Print a timestamped, color-coded log message to stdout.

    Args:
        msg: Message text to log.
        level: One of INFO, OK, WARN, ERR, SYS (controls prefix emoji).
    """  # signed: gamma
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"INFO": "🔵", "OK": "🟢", "WARN": "🟡", "ERR": "🔴", "SYS": "⚡"}.get(level, "  ")
    print(f"[{ts}] {prefix} {msg}", flush=True)


def load_workers():
    """Load the worker registry from data/workers.json.

    Returns:
        list[dict]: List of worker dicts with keys: name, hwnd, model, etc.
                    Empty list if file is missing or unparseable.
    """  # signed: gamma
    if not WORKERS_FILE.exists():
        log("No workers.json", "ERR")
        return []
    try:
        data = json.loads(WORKERS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        log(f"Failed to parse workers.json: {e}", "ERR")  # signed: beta
        return []
    return data.get("workers", [])


def load_orch_hwnd():
    """Load the orchestrator window HWND from data/orchestrator.json.

    Returns:
        int or None: The orchestrator HWND, or None if unavailable.
    """  # signed: gamma
    if ORCH_FILE.exists():
        try:
            data = json.loads(ORCH_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            log(f"Failed to parse orchestrator.json: {e}", "ERR")  # signed: beta
            return None
        return data.get("orchestrator_hwnd") or data.get("hwnd")
    return None


def _build_ghost_type_ps(hwnd, orch_hwnd, dispatch_file_path, render_hwnd=None):
    """Build the PowerShell script for ghost-typing into a worker window.

    Generates an inline PowerShell script containing a C# GhostType class with Win32
    P/Invoke methods for cross-thread focus management and clipboard-based text delivery.

    Architecture (see docs/DELIVERY_PIPELINE.md Section 4):
        1. STEERING cancel -- find 'Cancel (Alt+Backspace)' UIA button and invoke it
        2. Input target resolution -- score UIA Edit controls by position heuristics,
           or fall back to FindRender() DFS for Chrome_RenderWidgetHostHWND
           **Fast-path**: when render_hwnd is provided, skip UIA Edit search entirely
           and go directly to Chrome_RenderWidgetHostHWND targeting (eliminates UIA
           tree traversal overhead)
        3. Multi-pane disambiguation -- when multiple Chrome render widgets exist,
           select the one with the largest bounding area in the bottom-right quadrant
           (chat panes are typically positioned there in VS Code)
        4. Focus race prevention -- verify foreground window hasn't changed between
           clipboard set and paste; abort with FOCUS_STOLEN if stolen
        5. Clipboard verification -- 3x SetText/GetText retry loop
        6. Focus + paste + enter -- AttachThreadInput preferred, SetForegroundWindow fallback
        7. Clipboard cleanup -- Clear() + restore saved clipboard

    Args:
        hwnd: Target worker/consultant window HWND (int cast to IntPtr in PS)
        orch_hwnd: Orchestrator window HWND for focus restore after delivery
        dispatch_file_path: Path to temp file containing dispatch text (double-escaped backslashes)
        render_hwnd: Optional pre-resolved Chrome_RenderWidgetHostHWND (int). When provided,
                     skips UIA Edit search and FindAllRender DFS, going directly to
                     CHROME_RENDER paste path. Set to None or 0 to use normal discovery.

    Returns:
        str: Complete PowerShell script ready for subprocess execution

    See Also:
        docs/DELIVERY_PIPELINE.md Section 4 (Ghost Type Mechanism)
        docs/DELIVERY_PIPELINE.md Section 7 (Clipboard Safety)
        docs/DELIVERY_PIPELINE.md Section 9 (False Positive Risks)
    """  # signed: beta
    render_hwnd_val = int(render_hwnd) if render_hwnd else 0
    return f'''
$ErrorActionPreference = 'Stop'
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
        // MULTI-PANE FIX: Collect ALL Chrome_RenderWidgetHostHWND children,
        // then let PowerShell pick the best one by bounding rectangle.
        // See docs/DELIVERY_PIPELINE.md Section 9, Risk 4 (Chrome Render Ambiguity).  // signed: alpha
        var h = FindWindowEx(hwnd, IntPtr.Zero, null, null);
        while (h != IntPtr.Zero) {{
            var sb = new StringBuilder(256); GetClassName(h, sb, 256);
            if (sb.ToString().StartsWith("Chrome_RenderWidgetHost")) return h;  // signed: beta -- prefix match for Electron version resilience
            var f = FindRender(h); if (f != IntPtr.Zero) return f;
            h = FindWindowEx(hwnd, h, null, null);
        }}
        return IntPtr.Zero;
    }}
    // FindAllRender: collect ALL Chrome render widgets for multi-pane disambiguation  // signed: alpha
    public static System.Collections.Generic.List<IntPtr> FindAllRender(IntPtr hwnd) {{
        var results = new System.Collections.Generic.List<IntPtr>();
        FindAllRenderInner(hwnd, results);
        return results;
    }}
    private static void FindAllRenderInner(IntPtr hwnd, System.Collections.Generic.List<IntPtr> results) {{
        var h = FindWindowEx(hwnd, IntPtr.Zero, null, null);
        while (h != IntPtr.Zero) {{
            var sb = new StringBuilder(256); GetClassName(h, sb, 256);
            if (sb.ToString().StartsWith("Chrome_RenderWidgetHost")) results.Add(h);
            FindAllRenderInner(h, results);
            h = FindWindowEx(hwnd, h, null, null);
        }}
    }}
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [StructLayout(LayoutKind.Sequential)] public struct RECT {{
        public int Left, Top, Right, Bottom;
    }}
    public static bool FocusViaAttach(IntPtr target) {{
        uint targetPid;
        uint targetTid = GetWindowThreadProcessId(target, out targetPid);
        uint myTid = GetCurrentThreadId();
        if (targetTid == 0) return false;
        AttachThreadInput(myTid, targetTid, true);
        SetFocus(target);
        return true;
    }}
    public static void DetachThread(IntPtr target) {{
        uint targetPid;
        uint targetTid = GetWindowThreadProcessId(target, out targetPid);
        uint myTid = GetCurrentThreadId();
        AttachThreadInput(myTid, targetTid, false);
    }}
    // Hardware-level Enter key -- SendKeys ENTER fails on Chromium render widgets
    // because Chromium loses internal focus after paste. keybd_event sends through the
    // OS input queue like physical keyboard, which Chromium always receives. (INCIDENT 013 class)
    [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
    public static void HardwareEnter() {{
        keybd_event(0x0D, 0, 0, UIntPtr.Zero);          // VK_RETURN down
        System.Threading.Thread.Sleep(50);
        keybd_event(0x0D, 0, 2, UIntPtr.Zero);          // VK_RETURN up (KEYEVENTF_KEYUP=2)
    }}
}}
"@

$hwnd = [IntPtr]{hwnd}
$orchHwnd = [IntPtr]{orch_hwnd}

$dispatchText = [System.IO.File]::ReadAllText("{dispatch_file_path}", [System.Text.Encoding]::UTF8)

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

# Fast-path: when render_hwnd is pre-resolved, skip UIA Edit search entirely  # signed: beta
$fastRenderHwnd = [IntPtr]{render_hwnd_val}
$focusTarget = $null
$focusMethod = "NONE"
if ($fastRenderHwnd -ne [IntPtr]::Zero) {{
    $renderHwnd = $fastRenderHwnd
    $focusMethod = "CHROME_RENDER"
    Write-Host "DEBUG: Fast-path render_hwnd=$($renderHwnd.ToInt64()) -- skipped UIA Edit search"
}} else {{
$wnd = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
$allEdits = $wnd.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Edit
    ))
)
$edit = $null
$bestScore = -1
$wndRect = $wnd.Current.BoundingRectangle
$leftBandMaxX = $wndRect.X + [Math]::Min(340, ($wndRect.Width * 0.40))
foreach ($e in $allEdits) {{
    try {{
        $r = $e.Current.BoundingRectangle
        $name = ""
        try {{ $name = [string]$e.Current.Name }} catch {{}}
        if ($r.Width -lt 20 -or $r.Height -lt 10) {{ continue }}
        # Skip VS Code accessibility placeholder (not a real chat input)
        if ($name -match 'not accessible|screen reader') {{ continue }}
        $score = [int]$r.Y
        if ($r.X -lt $leftBandMaxX) {{ $score += 2000 }}
        if ($name -notmatch 'Terminal input') {{ $score += 500 }}
        if ($r.Width -gt 120) {{ $score += 50 }}
        if ($score -gt $bestScore) {{
            $bestScore = $score
            $edit = $e
        }}
    }} catch {{}}
}}
# Determine focus target: UIA Edit control, or Chrome render widget as fallback  # signed: orchestrator
if ($edit) {{
    $focusTarget = $edit
    $focusMethod = "EDIT"
}} else {{
    # No UIA Edit found -- VS Code chat input lives inside Chrome renderer
    # MULTI-PANE FIX: Collect ALL Chrome_RenderWidgetHostHWND children and pick the best one.
    # Chat input pane is typically the rightmost/bottom-most render widget in VS Code layout.
    # See docs/DELIVERY_PIPELINE.md Section 9, Risk 4 (Chrome Render Ambiguity).  # signed: alpha
    $allRenderWidgets = [GhostType]::FindAllRender($hwnd)
    if ($allRenderWidgets.Count -gt 0) {{
        if ($allRenderWidgets.Count -eq 1) {{
            $renderHwnd = $allRenderWidgets[0]
            $focusMethod = "CHROME_RENDER"
        }} else {{
            # Multiple render widgets found -- disambiguate by bounding rectangle.
            # The chat pane render widget is typically the one with the largest area
            # whose center is in the right half of the window (editor is left, chat is right).
            # If all widgets are in similar positions, fall back to the last one (highest Z-order).  # signed: alpha
            $wndMidX = $wndRect.X + ($wndRect.Width / 2)
            $bestRenderHwnd = [IntPtr]::Zero
            $bestRenderArea = 0
            $rightHalfFound = $false
            foreach ($rh in $allRenderWidgets) {{
                $rRect = New-Object GhostType+RECT
                [GhostType]::GetWindowRect($rh, [ref]$rRect) | Out-Null
                $rWidth = $rRect.Right - $rRect.Left
                $rHeight = $rRect.Bottom - $rRect.Top
                $rArea = $rWidth * $rHeight
                $rCenterX = $rRect.Left + ($rWidth / 2)
                # Prefer render widgets in the right half of the window (chat pane location)
                $inRightHalf = ($rCenterX -gt $wndMidX)
                if ($inRightHalf -and (-not $rightHalfFound -or $rArea -gt $bestRenderArea)) {{
                    $bestRenderHwnd = $rh
                    $bestRenderArea = $rArea
                    $rightHalfFound = $true
                }} elseif (-not $rightHalfFound -and $rArea -gt $bestRenderArea) {{
                    $bestRenderHwnd = $rh
                    $bestRenderArea = $rArea
                }}
            }}
            if ($bestRenderHwnd -ne [IntPtr]::Zero) {{
                $renderHwnd = $bestRenderHwnd
                $focusMethod = "CHROME_RENDER"
                Write-Host "DEBUG: Multi-pane disambiguation: $($allRenderWidgets.Count) widgets, selected area=$bestRenderArea rightHalf=$rightHalfFound"
            }} else {{
                # Fallback to first widget if scoring fails  # signed: alpha
                $renderHwnd = $allRenderWidgets[0]
                $focusMethod = "CHROME_RENDER"
            }}
        }}
    }}
}}
}}  # end fast-path else block  # signed: beta

if ($focusMethod -ne "NONE") {{
    $savedClip = $null
    $deliveryStatus = "FAILED"
    try {{ $savedClip = [System.Windows.Forms.Clipboard]::GetText() }} catch {{}}
    # Clipboard verification: set text and confirm with exponential backoff  # signed: beta
    $clipRetries = 0
    $clipVerified = $false
    $clipBackoffMs = 100
    while ($clipRetries -lt 5 -and -not $clipVerified) {{
        try {{ [System.Windows.Forms.Clipboard]::Clear() }} catch {{}}
        Start-Sleep -Milliseconds 30
        [System.Windows.Forms.Clipboard]::SetText($dispatchText)
        Start-Sleep -Milliseconds $clipBackoffMs
        try {{
            $readBack = [System.Windows.Forms.Clipboard]::GetText()
            if ($readBack -eq $dispatchText) {{
                $clipVerified = $true
            }} else {{
                $clipRetries++
                Write-Host "DEBUG: Clipboard verify mismatch attempt $clipRetries (backoff $($clipBackoffMs)ms)"
                $clipBackoffMs = [Math]::Min($clipBackoffMs * 2, 800)
                Start-Sleep -Milliseconds $clipBackoffMs
            }}
        }} catch {{
            $clipRetries++
            $clipBackoffMs = [Math]::Min($clipBackoffMs * 2, 800)
            Start-Sleep -Milliseconds $clipBackoffMs
        }}
    }}
    if (-not $clipVerified) {{
        Write-Host "CLIPBOARD_VERIFY_FAILED"
        exit 1
    }}

    # FOCUS RACE PREVENTION: Verify foreground window hasn't been stolen between
    # clipboard set and paste. If another window grabbed focus, the paste would go
    # to the wrong target. See docs/DELIVERY_PIPELINE.md Section 9, Risk 2.  # signed: alpha
    $prePasteFgHwnd = [GhostType]::GetForegroundWindow()

    if ($focusMethod -eq "EDIT") {{
        $attached = [GhostType]::FocusViaAttach($hwnd)
        if ($attached) {{
            try {{ $edit.SetFocus() }} catch {{}}
            Start-Sleep -Milliseconds 80
            # Focus race check: verify foreground window is still ours before paste  # signed: alpha
            $postFocusFg = [GhostType]::GetForegroundWindow()
            if ($postFocusFg -ne $prePasteFgHwnd -and $postFocusFg -ne $hwnd) {{
                Write-Host "FOCUS_STOLEN"
                [GhostType]::DetachThread($hwnd)
                try {{ [System.Windows.Forms.Clipboard]::Clear() }} catch {{}}
                exit 1
            }}
            [System.Windows.Forms.SendKeys]::SendWait("^v")
            Start-Sleep -Milliseconds 300
            [GhostType]::HardwareEnter()
            [GhostType]::DetachThread($hwnd)
            $deliveryStatus = "OK_ATTACHED"
        }} else {{
            try {{ $edit.SetFocus() }} catch {{}}
            [GhostType]::SetForegroundWindow($hwnd)
            Start-Sleep -Milliseconds 80
            # Focus race check for fallback path  # signed: alpha
            $postFocusFg = [GhostType]::GetForegroundWindow()
            if ($postFocusFg -ne $hwnd) {{
                Write-Host "FOCUS_STOLEN"
                try {{ [System.Windows.Forms.Clipboard]::Clear() }} catch {{}}
                exit 1
            }}
            [System.Windows.Forms.SendKeys]::SendWait("^v")
            Start-Sleep -Milliseconds 300
            [GhostType]::HardwareEnter()
            [GhostType]::SetForegroundWindow($orchHwnd)
            $deliveryStatus = "OK_FALLBACK"
        }}
    }} else {{
        # CHROME_RENDER path: focus render widget, then paste  # signed: orchestrator
        $attached = [GhostType]::FocusViaAttach($hwnd)
        if ($attached) {{
            [GhostType]::SetFocus($renderHwnd)
            Start-Sleep -Milliseconds 120
            # Focus race check: verify foreground hasn't been stolen  # signed: alpha
            $postFocusFg = [GhostType]::GetForegroundWindow()
            if ($postFocusFg -ne $prePasteFgHwnd -and $postFocusFg -ne $hwnd) {{
                Write-Host "FOCUS_STOLEN"
                [GhostType]::DetachThread($hwnd)
                try {{ [System.Windows.Forms.Clipboard]::Clear() }} catch {{}}
                exit 1
            }}
            [System.Windows.Forms.SendKeys]::SendWait("^v")
            Start-Sleep -Milliseconds 300
            [GhostType]::HardwareEnter()
            [GhostType]::DetachThread($hwnd)
            $deliveryStatus = "OK_RENDER_ATTACHED"
        }} else {{
            [GhostType]::SetForegroundWindow($hwnd)
            Start-Sleep -Milliseconds 80
            [GhostType]::SetFocus($renderHwnd)
            Start-Sleep -Milliseconds 120
            # Focus race check for render fallback path  # signed: alpha
            $postFocusFg = [GhostType]::GetForegroundWindow()
            if ($postFocusFg -ne $hwnd) {{
                Write-Host "FOCUS_STOLEN"
                try {{ [System.Windows.Forms.Clipboard]::Clear() }} catch {{}}
                exit 1
            }}
            [System.Windows.Forms.SendKeys]::SendWait("^v")
            Start-Sleep -Milliseconds 300
            [GhostType]::HardwareEnter()
            [GhostType]::SetForegroundWindow($orchHwnd)
            $deliveryStatus = "OK_RENDER_FALLBACK"
        }}
    }}
    # Post-paste clipboard clear: prevent stale dispatch data from lingering  # signed: alpha
    Start-Sleep -Milliseconds 30
    try {{ [System.Windows.Forms.Clipboard]::Clear() }} catch {{}}
    if ($savedClip -and $savedClip.Length -gt 0) {{
        Start-Sleep -Milliseconds 50
        try {{ [System.Windows.Forms.Clipboard]::SetText($savedClip) }} catch {{}}
    }}
    try {{ Remove-Item "{dispatch_file_path}" -Force -ErrorAction SilentlyContinue }} catch {{}}
    Write-Host $deliveryStatus
    if ($deliveryStatus -like "OK_*") {{ exit 0 }}
    exit 1
}} else {{
    Write-Host "NO_EDIT_NO_RENDER"
    exit 1
}}
'''


def _execute_ghost_dispatch(ps, hwnd, orch_hwnd):
    """Execute the ghost-type PS script under dispatch lock and validate delivery.

    Runs the PowerShell script generated by _build_ghost_type_ps() as a subprocess
    with CREATE_NO_WINDOW flag. Validates success by checking stdout for OK_* prefix
    status codes. Handles failure codes: CLIPBOARD_VERIFY_FAILED, FOCUS_STOLEN,
    NO_EDIT_NO_RENDER.

    Architecture (see docs/DELIVERY_PIPELINE.md Section 4.3):
        - Acquires _dispatch_lock (threading.Lock) to prevent concurrent ghost-type ops
        - Writes dispatch lock file for external monitoring
        - Runs PS with 20s timeout, CREATE_NO_WINDOW (0x08000000) creation flag
        - CLIPBOARD_VERIFY_FAILED retry runs INSIDE the lock to prevent clipboard
          races with concurrent dispatch threads (fixed: was outside lock)
        - Validates: returncode==0, stdout contains OK_*, no stderr, no NO_EDIT

    Args:
        ps: Complete PowerShell script string from _build_ghost_type_ps()
        hwnd: Target window HWND (for logging)
        orch_hwnd: Orchestrator HWND (for logging)

    Returns:
        bool: True if PS reported successful delivery (OK_*), False otherwise
    """  # signed: beta
    try:
        with _dispatch_lock:
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
                creationflags=0x08000000
            )

            stderr = (r.stderr or "").strip()
            stdout = r.stdout or ""

            # Clipboard verify failed -- retry INSIDE the lock to prevent clipboard  # signed: beta
            # races with other threads. Moving this inside _dispatch_lock is critical:
            # if the retry ran outside the lock (as it previously did), another thread
            # could acquire the lock and start its own clipboard operation during the
            # 500ms cooldown, corrupting both dispatches.
            if "CLIPBOARD_VERIFY_FAILED" in stdout:
                log(f"Ghost CLIPBOARD_VERIFY_FAILED for HWND={hwnd} -- retrying once after 500ms cooldown (lock held)", "WARN")
                time.sleep(0.5)
                try:
                    r2 = subprocess.run(
                        ["powershell", "-NoProfile", "-Command", ps],
                        capture_output=True, text=True, timeout=20,
                        creationflags=0x08000000
                    )
                    stdout2 = r2.stdout or ""
                    if any(s in stdout2 for s in ("OK_ATTACHED", "OK_FALLBACK", "OK_RENDER_ATTACHED", "OK_RENDER_FALLBACK")):
                        log(f"Ghost CLIPBOARD retry succeeded for HWND={hwnd}", "OK")
                        try:
                            DISPATCH_LOCK_FILE.unlink(missing_ok=True)
                        except Exception:
                            pass
                        return True
                    log(f"Ghost CLIPBOARD retry also failed for HWND={hwnd}: {stdout2.strip()[:150]}", "ERR")
                except Exception as e2:
                    log(f"Ghost CLIPBOARD retry exception: {e2}", "ERR")
                try:
                    DISPATCH_LOCK_FILE.unlink(missing_ok=True)
                except Exception:
                    pass
                return False
            # signed: beta — end clipboard retry block (now inside lock)

            try:
                DISPATCH_LOCK_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            time.sleep(0.5)

        # Focus race detection: another window stole focus between clipboard set and paste  # signed: alpha
        if "FOCUS_STOLEN" in stdout:
            log(f"Ghost FOCUS_STOLEN for HWND={hwnd} -- focus race detected, paste aborted safely", "ERR")
            return False
        ok = (
            r.returncode == 0
            and any(s in stdout for s in ("OK_ATTACHED", "OK_FALLBACK", "OK_RENDER_ATTACHED", "OK_RENDER_FALLBACK"))
            and "NO_EDIT" not in stdout
            and not stderr
        )  # signed: orchestrator — accept Chrome render widget delivery as valid
        if not ok and r.stdout:
            log(f"Ghost output: {r.stdout.strip()[:200]}", "WARN")
        if stderr:
            log(f"Ghost stderr: {stderr[:200]}", "WARN")
        return ok
    except Exception as e:
        log(f"Ghost type failed: {e}", "ERR")
        try:
            DISPATCH_LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def ghost_type_to_worker(hwnd, text, orch_hwnd, render_hwnd=None):
    """Type text into a worker/consultant chat window via clipboard paste (Level 4.2 dispatch).

    This is the core delivery mechanism for all Skynet prompt dispatch. It writes the dispatch
    text to a temp file, builds an inline PowerShell script with C# Win32 interop, and executes
    it in a subprocess to paste the text into the target window's chat input.

    Architecture (see docs/DELIVERY_PIPELINE.md Section 4 for full details):
        1. Text written to temp file at data/.dispatch_tmp_{hwnd}.txt (no clipboard truncation)
        2. _build_ghost_type_ps() generates PS script with: STEERING cancel, Edit scoring,
           multi-pane Chrome render widget disambiguation, focus race prevention,
           clipboard verification (3x retry), AttachThreadInput paste, clipboard cleanup
           **Fast-path**: when render_hwnd is provided, skips UIA Edit search entirely
        3. _execute_ghost_dispatch() runs script under dispatch lock, validates OK_ stdout

    Safety features:
        - Clipboard save/restore (user clipboard never lost)
        - Clipboard verification (3x SetText/GetText readback loop)
        - Focus race prevention (GetForegroundWindow check before paste)
        - Multi-pane disambiguation (FindAllRender + right-half area scoring)
        - AttachThreadInput for less-visible focus transfer (no Z-order flash)
        - CREATE_NO_WINDOW flag on subprocess (no console flash)
        - Post-paste Clipboard.Clear() to prevent stale dispatch text lingering

    Args:
        hwnd: Target window HWND (int). Must be valid (IsWindow check).
        text: Dispatch text content. Newlines replaced with spaces before writing to temp file.
        orch_hwnd: Orchestrator HWND for focus restore after delivery. Can be invalid (warn only).
        render_hwnd: Optional pre-resolved Chrome_RenderWidgetHostHWND (int). When provided,
                     _build_ghost_type_ps() skips UIA Edit search and FindAllRender DFS,
                     going directly to CHROME_RENDER paste path. Eliminates UIA tree
                     traversal overhead for repeated dispatches to the same window.

    Returns:
        bool: True if delivery succeeded (PS reported OK_*), False on any failure.

    See Also:
        docs/DELIVERY_PIPELINE.md (full architecture reference)
        _build_ghost_type_ps() (PS script generation)
        _execute_ghost_dispatch() (subprocess execution and validation)
        _verify_delivery() (post-dispatch UIA state verification)
    """  # signed: beta
    if not hwnd or not user32.IsWindow(hwnd):
        log(f"ghost_type: invalid target HWND={hwnd}", "ERR")  # signed: beta
        return False
    if not orch_hwnd or not user32.IsWindow(orch_hwnd):
        log(f"ghost_type: invalid orchestrator HWND={orch_hwnd}, proceeding without focus restore", "WARN")  # signed: beta

    dispatch_file = Path(ROOT) / "data" / f".dispatch_tmp_{hwnd}.txt"
    try:
        dispatch_file.write_text(text.replace("\n", " "), encoding="utf-8")
    except OSError as e:
        log(f"ghost_type: failed to write dispatch temp file: {e}", "ERR")  # signed: beta
        return False
    dispatch_file_path = str(dispatch_file).replace("\\", "\\\\")
    ps = _build_ghost_type_ps(hwnd, orch_hwnd, dispatch_file_path, render_hwnd=render_hwnd)
    try:
        return _execute_ghost_dispatch(ps, hwnd, orch_hwnd)
    finally:
        # Always clean up temp file even if dispatch fails/times out (prevents resource leak)
        try:
            dispatch_file.unlink(missing_ok=True)
        except Exception:
            pass  # Best-effort cleanup; PS script may have already removed it
        # signed: alpha


def _dispatch_to_orchestrator(task, self_id, orch_hwnd):
    """Dispatch to orchestrator via direct-prompt delivery. Returns ok bool."""
    try:
        from tools.skynet_delivery import deliver_to_orchestrator
        log(f"→ ORCHESTRATOR [direct-prompt]: {task[:80]}{'...' if len(task) > 80 else ''}", "SYS")
        result = deliver_to_orchestrator(task, sender=self_id or "orchestrator", also_bus=True)
        ok = bool(result.get("success"))
        _log_dispatch("orchestrator", task, "DIRECT_PROMPT", ok, orch_hwnd or 0)
        log(f"{'✓' if ok else '✗'} Dispatched to ORCHESTRATOR [{result.get('detail', '')}]",
            "OK" if ok else "ERR")
        return ok
    except Exception as e:
        log(f"Orchestrator dispatch failed: {e}", "ERR")
        _log_dispatch("orchestrator", task, "DIRECT_PROMPT", False, orch_hwnd or 0)
        return False


def load_consultant_hwnd(consultant_id):
    """Load consultant HWND from state file. Returns int or 0."""  # signed: orchestrator
    state_files = {
        "consultant": ROOT / "data" / "consultant_state.json",
        "gemini_consultant": ROOT / "data" / "gemini_consultant_state.json",
    }
    path = state_files.get(consultant_id)
    if not path or not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("hwnd", 0))
    except (json.JSONDecodeError, OSError, ValueError):
        return 0


def _dispatch_to_consultant(target_name, task, self_id):
    """Dispatch to consultant via ghost_type (primary) or bridge-queue (fallback).

    Consultants ARE VS Code windows — they receive prompts via ghost_type
    to Chrome_RenderWidgetHostHWND, exactly like workers. Bridge-queue is
    kept as audit trail / fallback only.
    """  # signed: orchestrator — INCIDENT 012 fix
    orch_hwnd = load_orch_hwnd()

    # --- Phase 1: Try ghost_type (primary delivery) ---
    consultant_hwnd = load_consultant_hwnd(target_name)
    if consultant_hwnd and user32.IsWindow(consultant_hwnd):
        log(f"→ {target_name.upper()} [ghost_type HWND={consultant_hwnd}]: {task[:80]}{'...' if len(task) > 80 else ''}", "SYS")
        ok = ghost_type_to_worker(consultant_hwnd, task, orch_hwnd or consultant_hwnd)
        if ok:
            _log_dispatch(target_name, task, "GHOST_TYPE", True, 0)
            log(f"✓ Dispatched to {target_name.upper()} via ghost_type [HWND={consultant_hwnd}]", "OK")
            # Also post to bridge as audit trail (best-effort)
            try:
                from tools.skynet_delivery import deliver_to_consultant
                deliver_to_consultant(target_name, task, sender=self_id or "orchestrator", msg_type="directive")
            except Exception:
                pass  # audit trail is best-effort
            return True
        else:
            log(f"ghost_type failed for {target_name.upper()} HWND={consultant_hwnd}, falling back to bridge", "WARN")

    # --- Phase 2: Fallback to bridge-queue ---
    try:
        from tools.skynet_delivery import deliver_to_consultant
        method_note = "bridge-queue" if not consultant_hwnd else "bridge-queue (ghost_type fallback)"
        log(f"→ {target_name.upper()} [{method_note}]: {task[:80]}{'...' if len(task) > 80 else ''}", "SYS")
        result = deliver_to_consultant(target_name, task, sender=self_id or "orchestrator", msg_type="directive")
        ok = bool(result.get("success"))
        delivery_status = result.get("delivery_status", "unknown")
        status_note = f" (delivery_status={delivery_status})" if delivery_status == "queued" else ""
        _log_dispatch(target_name, task, "BRIDGE_QUEUE", ok, 0)
        log(f"{'✓' if ok else '✗'} Dispatched to {target_name.upper()}{status_note} [{result.get('detail', '')}]",
            "OK" if ok else "ERR")
        return ok  # signed: orchestrator
    except Exception as e:
        log(f"Consultant dispatch failed for {target_name}: {e}", "ERR")
        _log_dispatch(target_name, task, "BRIDGE_QUEUE", False, 0)
        return False


def _validate_target_hwnd(hwnd, worker_name):
    """Security-validate HWND before ghost-typing. Returns True if valid."""
    try:
        from skynet_delivery import validate_hwnd as _validate_hwnd
        validation = _validate_hwnd(hwnd, f"worker:{worker_name}")
        if not validation["valid"]:
            failed_checks = [k for k, v in validation["checks"].items() if not v]
            log(f"✗ HWND {hwnd} FAILED security validation for {worker_name}: "
                f"{failed_checks} pid={validation['pid']} proc={validation['process_name']}",
                "SECURITY")
            return False
    except ImportError:
        try:
            if not ctypes.windll.user32.IsWindow(hwnd):
                log(f"✗ HWND {hwnd} is not a valid window for {worker_name}", "SECURITY")
                return False
        except Exception as e:
            log(f"✗ HWND validation error for {worker_name}: {e}", "SECURITY")  # signed: beta
            return False
    return True


# ── Dispatch failure tracking (consecutive failures per worker) ──  # signed: beta
_dispatch_failure_counts = {}  # {worker_name: consecutive_failure_count}
UNRESPONSIVE_THRESHOLD = 5  # consecutive unverified dispatches before alert


def _track_dispatch_failure(worker_name):
    """Increment consecutive dispatch failure counter; alert at threshold."""
    _dispatch_failure_counts[worker_name] = _dispatch_failure_counts.get(worker_name, 0) + 1
    count = _dispatch_failure_counts[worker_name]
    log(f"[DISPATCH_FAILURES] {worker_name.upper()} consecutive failures: {count}/{UNRESPONSIVE_THRESHOLD}", "WARN")
    if count >= UNRESPONSIVE_THRESHOLD:
        alert_msg = f"WORKER_UNRESPONSIVE: {worker_name.upper()} failed {count} consecutive dispatches -- worker may be dead/stuck"
        log(alert_msg, "ERR")
        try:
            from tools.skynet_spam_guard import guarded_publish
            guarded_publish({
                "sender": "dispatch",
                "topic": "orchestrator",
                "type": "alert",
                "content": alert_msg,
            })
        except Exception:
            pass  # best-effort alert
    # signed: beta


def _reset_dispatch_failures(worker_name):
    """Reset consecutive failure counter on successful delivery."""
    if worker_name in _dispatch_failure_counts and _dispatch_failure_counts[worker_name] > 0:
        log(f"[DISPATCH_FAILURES] {worker_name.upper()} reset from {_dispatch_failure_counts[worker_name]} to 0", "OK")
        _dispatch_failure_counts[worker_name] = 0
    # signed: beta


def _record_dispatch_outcome(worker_name, task, pre_state, hwnd, t_start, ok, method=""):
    """Record dispatch metrics, log, heartbeat, and backend notification."""
    _log_dispatch(worker_name, task, pre_state, ok, hwnd)
    if ok:
        label = f"Steer-bypass dispatched" if method else "Dispatched"
        log(f"✓ {label} to {worker_name.upper()} [HWND={hwnd}]", "OK")
        notify_backend_dispatch(worker_name, task, True)
    else:
        log(f"✗ Steer-bypass also failed for {worker_name.upper()}", "ERR")
    try:
        m = metrics()
        if m:
            args = [worker_name, task, ok, (time.time() - t_start) * 1000]
            if method:
                args.append(method)
            m.record_dispatch(*args)
    except Exception:
        pass
    _heartbeat_after_dispatch(worker_name, task, ok)


def dispatch_to_worker(worker_name, task, workers=None, orch_hwnd=None, context=None):
    """Dispatch a single task to a specific routable identity. Always fires immediately.

    VS Code queues messages, so there is no reason to wait for IDLE state.
    Only STEERING is handled (auto-cancelled) before dispatch.
    """
    self_id = _get_self_identity()
    if self_id and self_id.lower() == worker_name.lower():
        log(f"SELF-DISPATCH BLOCKED: {worker_name} tried to dispatch to itself!", "ERR")
        _log_dispatch(worker_name, task, "SELF_DISPATCH_BLOCKED", False)
        return False

    if not workers:
        workers = load_workers()
    if not orch_hwnd:
        orch_hwnd = load_orch_hwnd()

    target_name = str(worker_name).lower()
    if target_name == "orchestrator":
        return _dispatch_to_orchestrator(task, self_id, orch_hwnd)
    if target_name in ("consultant", "gemini_consultant"):
        return _dispatch_to_consultant(target_name, task, self_id)

    t_start = time.time()
    target = next((w for w in workers if w["name"] == worker_name), None)
    if not target:
        log(f"Target '{worker_name}' not found", "ERR")
        return False

    hwnd = target["hwnd"]
    if not user32.IsWindowVisible(hwnd):
        # HWND may be stale -- re-read workers.json in case discovery updated it
        fresh_workers = load_workers()
        fresh_target = next((w for w in fresh_workers if w["name"] == worker_name), None)
        if fresh_target and fresh_target["hwnd"] != hwnd:
            hwnd = fresh_target["hwnd"]
            target["hwnd"] = hwnd
            log(f"Refreshed stale HWND for {worker_name.upper()} -> HWND={hwnd}", "SYS")
        if not user32.IsWindowVisible(hwnd):
            log(f"Worker {worker_name.upper()} window not visible (HWND={hwnd})", "ERR")
            return False

    vis_ok, pre_state, ss_path = pre_dispatch_visual_check(hwnd, worker_name)
    if not vis_ok:
        log(f"✗ Visual check FAILED for {worker_name.upper()} -- aborting dispatch", "ERR")
        return False

    log(f"→ {worker_name.upper()} [state={pre_state}] [HWND={hwnd}]: {task[:80]}{'...' if len(task) > 80 else ''}", "SYS")

    if pre_state == "STEERING":
        log(f"STEERING detected on {worker_name.upper()} -- auto-cancelling before dispatch", "WARN")
        clear_steering_and_send(hwnd, "", orch_hwnd)
        time.sleep(1.0)
    elif pre_state == "PROCESSING":
        log(f"{worker_name.upper()} is PROCESSING -- dispatching immediately (VS Code queues)", "SYS")

    try:
        enriched_task = enrich_task(worker_name, task)
    except Exception as e:
        log(f"Enrichment failed for {worker_name.upper()}: {e} -- dispatching raw task", "WARN")
        enriched_task = task  # Fall back to unenriched task
    try:
        full_task = build_context_preamble(worker_name, enriched_task, context) if context else build_preamble(worker_name) + enriched_task
    except Exception as e:
        log(f"Preamble build failed for {worker_name.upper()}: {e} -- dispatching raw task", "WARN")
        full_task = enriched_task  # Fall back to task without preamble  # signed: alpha

    # Dispatch payload size logging + safeguard against oversized payloads  # signed: orchestrator
    MAX_DISPATCH_LENGTH = 12000  # chars — beyond this, Copilot CLI may reject with "delegation cancelled"
    payload_len = len(full_task)
    if payload_len > MAX_DISPATCH_LENGTH:
        log(f"⚠ {worker_name.upper()} payload {payload_len} chars exceeds {MAX_DISPATCH_LENGTH} limit -- trimming preamble", "WARN")
        # Keep only task text (no preamble/enrichment) to stay under limit
        full_task = build_preamble(worker_name) + task
        payload_len = len(full_task)
    log(f"📦 {worker_name.upper()} dispatch payload: {payload_len} chars", "SYS")

    if not _validate_target_hwnd(hwnd, worker_name):
        _log_dispatch(worker_name, task, pre_state, False, hwnd)
        return False

    ok = ghost_type_to_worker(hwnd, full_task, orch_hwnd)
    if not ok:
        log(f"✗ Failed to dispatch to {worker_name.upper()} -- trying steer-bypass", "WARN")
        ok = clear_steering_and_send(hwnd, full_task, orch_hwnd)
        _record_dispatch_outcome(worker_name, task, pre_state, hwnd, t_start, ok, 'steer-bypass' if ok else '')
    else:
        _record_dispatch_outcome(worker_name, task, pre_state, hwnd, t_start, True)

    # Delivery verification: confirm worker state changed after dispatch  # signed: orchestrator
    if ok:
        verified = _verify_delivery(hwnd, worker_name, pre_state)
        if not verified and pre_state == "IDLE":
            # Auto-retry with exponential backoff  # signed: beta
            for attempt in range(2, DELIVERY_RETRY_MAX + 2):  # signed: gamma — use named constant
                delay = DELIVERY_RETRY_BACKOFF_BASE * (2 ** (attempt - 2))
                log(f"[RETRY] {worker_name.upper()} attempt {attempt}/3 -- delivery unverified, retrying in {delay:.0f}s (exp backoff)", "WARN")
                time.sleep(delay)
                # Re-check state before retry -- abort if worker moved on its own
                try:
                    from tools.uia_engine import get_engine
                    current_state = get_engine().get_state(hwnd)
                except Exception:
                    current_state = "UNKNOWN"
                if current_state not in ("IDLE", "UNKNOWN"):  # UNKNOWN = UIA failed, not confirmed  # signed: alpha
                    log(f"✓ {worker_name.upper()} now {current_state} before retry -- delivery confirmed", "OK")
                    verified = True
                    _reset_dispatch_failures(worker_name)  # signed: beta
                    break
                # Retry ghost_type
                retry_ok = ghost_type_to_worker(hwnd, full_task, orch_hwnd)
                if retry_ok:
                    verified = _verify_delivery(hwnd, worker_name, "IDLE")
                    if verified:
                        log(f"✓ {worker_name.upper()} delivery VERIFIED on attempt {attempt}/3", "OK")
                        _reset_dispatch_failures(worker_name)  # signed: beta
                        break
                else:
                    log(f"[RETRY] {worker_name.upper()} ghost_type failed on attempt {attempt}/3", "WARN")
            if not verified:
                log(f"⚠ {worker_name.upper()} delivery UNVERIFIED after 3 attempts", "WARN")
                _track_dispatch_failure(worker_name)  # signed: beta
        elif not verified:
            log(f"⚠ {worker_name.upper()} delivery UNVERIFIED (state did not change from {pre_state})", "WARN")
            _track_dispatch_failure(worker_name)  # signed: beta
        else:
            _reset_dispatch_failures(worker_name)  # signed: beta

    return ok


def _verify_delivery(hwnd, worker_name, pre_state, timeout_s=8):
    """Verify dispatch delivery by polling UIA for worker state transitions.

    After ghost_type_to_worker() reports PS-level success (OK_* stdout), this function
    provides a secondary verification layer by checking whether the worker's UIA state
    actually changed. This catches silent delivery failures where the PS script thinks
    it pasted successfully but the text went to the wrong target or was swallowed.

    Architecture (see docs/DELIVERY_PIPELINE.md Section 6):
        - Polls engine.get_state(hwnd) every 0.5s for up to timeout_s seconds
        - Success = state changed from pre_state to any non-UNKNOWN state
        - If pre_state was already PROCESSING, returns True immediately (queued dispatch)
        - UNKNOWN handling: 3+ consecutive UNKNOWN readings = FAILED (UIA is broken)
        - This is INFORMATIONAL only -- a False return does NOT mean delivery failed,
          just that it couldn't be verified. See Risk 6 in docs/DELIVERY_PIPELINE.md.

    Args:
        hwnd: Target worker window HWND
        worker_name: Worker name for logging (e.g., 'alpha')
        pre_state: Worker UIA state captured BEFORE dispatch (typically 'IDLE')
        timeout_s: Maximum seconds to poll for state transition (default: 8)

    Returns:
        bool: True if state transition detected (delivery verified),
              False if state unchanged or UIA unavailable (delivery unverified)

    See Also:
        docs/DELIVERY_PIPELINE.md Section 6 (Delivery Verification)
        docs/DELIVERY_PIPELINE.md Section 9, Risk 6 (Verify is Informational)
        docs/DELIVERY_PIPELINE.md Section 9, Risk 9 (UNKNOWN State Handling)
    """  # signed: alpha
    if pre_state == "PROCESSING":
        return True  # was already processing, dispatch queued in VS Code

    try:
        from tools.uia_engine import get_engine
        engine = get_engine()
        consecutive_unknown = 0  # track consecutive UNKNOWN readings  # signed: alpha
        for i in range(timeout_s * 2):  # poll every 0.5s
            time.sleep(0.5)
            try:
                post_state = engine.get_state(hwnd)
                if post_state == "UNKNOWN":
                    consecutive_unknown += 1
                    if consecutive_unknown >= DELIVERY_VERIFY_CONSECUTIVE_UNKNOWN_LIMIT:  # signed: gamma
                        log(f"✗ {worker_name.upper()} delivery FAILED: {consecutive_unknown} consecutive UNKNOWN states (UIA broken)", "WARN")
                        return False
                else:
                    consecutive_unknown = 0  # reset on any real state  # signed: alpha
                if post_state != pre_state and post_state != "UNKNOWN":  # UNKNOWN = UIA error, not a real transition  # signed: alpha
                    log(f"✓ {worker_name.upper()} delivery VERIFIED: {pre_state} -> {post_state}", "OK")
                    return True
            except Exception:
                consecutive_unknown += 1  # exceptions also count as UNKNOWN  # signed: alpha
                if consecutive_unknown >= DELIVERY_VERIFY_CONSECUTIVE_UNKNOWN_LIMIT:  # signed: gamma
                    log(f"✗ {worker_name.upper()} delivery FAILED: {consecutive_unknown} consecutive UIA exceptions", "WARN")
                    return False
        # State didn't change — with HardwareEnter the text was likely submitted and
        # the CLI processed it fast enough to return to IDLE before verification.
        # HardwareEnter (keybd_event) is reliable for Chromium, so if the PS script
        # reported OK_*, trust the delivery. Only use pyautogui fallback if needed.
        # Check if we can detect that text was processed (scan for new chat content).
        try:
            # Secondary verification: re-scan UIA. If still IDLE and pre was IDLE,
            # the CLI likely processed the message instantly. With HardwareEnter this
            # is the expected case — report as verified with a note.
            final_state = engine.get_state(hwnd)
            if final_state == "PROCESSING":
                log(f"✓ {worker_name.upper()} delivery VERIFIED (late transition): IDLE -> PROCESSING", "OK")
                return True
            # If pre_state was IDLE and we're still IDLE, the message was likely
            # submitted and processed fast. Trust the delivery from the PS script.
            if pre_state == "IDLE" and final_state == "IDLE":
                log(f"✓ {worker_name.upper()} delivery ASSUMED OK: CLI processed fast (IDLE->IDLE). HardwareEnter is reliable.", "OK")
                return True
        except Exception:
            pass
        # Last resort: pyautogui Enter fallback (shouldn't be needed with HardwareEnter)
        try:
            import pyautogui
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            input_x = (rect.left + rect.right) // 2
            input_y = rect.bottom - 80
            pyautogui.click(input_x, input_y)
            time.sleep(0.2)
            pyautogui.press('enter')
            log(f"⚡ {worker_name.upper()} pyautogui Enter fallback fired at ({input_x},{input_y})", "WARN")
            time.sleep(1.5)
            try:
                post = engine.get_state(hwnd)
                if post != pre_state and post != "UNKNOWN":
                    log(f"✓ {worker_name.upper()} delivery VERIFIED after pyautogui fallback: {pre_state} -> {post}", "OK")
                    return True
                # Same fast-processing logic for pyautogui fallback
                if pre_state == "IDLE" and post == "IDLE":
                    log(f"✓ {worker_name.upper()} delivery ASSUMED OK after pyautogui fallback (fast processing)", "OK")
                    return True
            except Exception:
                pass
        except Exception as e:
            log(f"pyautogui Enter fallback failed for {worker_name}: {e}", "WARN")
        return False
    except Exception as e:
        log(f"Delivery verify error for {worker_name}: {e}", "WARN")
        return False  # UIA engine import failed = cannot verify = FAILED, not assumed success  # signed: alpha


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
                f"http://localhost:8420/directive",  # signed: gamma
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

    # Rule 0.015: Pre-fire visual checks on MAIN thread (COM UIA requires STA)  # signed: orchestrator
    worker_map = {w["name"]: w for w in workers}
    verified = {}
    for name, task in tasks_by_worker.items():
        w = worker_map.get(name)
        if not w:
            log(f"Parallel dispatch: worker '{name}' not found in registry", "ERR")
            continue
        vis_ok, pre_state, ss_path = pre_dispatch_visual_check(w["hwnd"], name)
        if vis_ok:
            verified[name] = task
        else:
            log(f"Parallel dispatch: skipping {name} -- visual check failed (state={pre_state})", "SECURITY")

    if not verified:
        log("Parallel dispatch: no workers passed visual check", "WARN")
        return {name: False for name in tasks_by_worker}

    n = min(max_workers, len(verified))
    results = {}
    log(f"Parallel dispatch → {list(verified.keys())} ({n} threads, {len(tasks_by_worker) - len(verified)} skipped)", "SYS")

    def _dispatch_one_no_visual(name, task):
        """Dispatch without re-running visual check (already done on main thread)."""
        target = worker_map.get(name)
        if not target:
            return False
        hwnd = target["hwnd"]
        enriched_task = enrich_task(name, task)
        full_task = build_preamble(name) + enriched_task
        ok = ghost_type_to_worker(hwnd, full_task, orch_hwnd)
        if not ok:
            ok = clear_steering_and_send(hwnd, full_task, orch_hwnd)
        return ok

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = {pool.submit(_dispatch_one_no_visual, name, task): name
                   for name, task in verified.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results[name] = fut.result()
            except Exception as e:
                log(f"Parallel dispatch error for {name}: {e}", "ERR")
                results[name] = False

    # Mark skipped workers as failed
    for name in tasks_by_worker:
        if name not in results:
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

    # Rule 0.015: Pre-fire visual proof before blast  # signed: orchestrator
    verified_workers = []
    for w in workers:
        if w["name"] in targets:
            vis_ok, pre_state, ss_path = pre_dispatch_visual_check(w["hwnd"], w["name"])
            if vis_ok:
                verified_workers.append(w)
            else:
                log(f"BLAST: skipping {w['name']} -- visual check failed (state={pre_state})", "SECURITY")

    if not verified_workers:
        log("blast_all: no workers passed visual check", "WARN")
        return {}

    log(f"BLAST → {[w['name'] for w in verified_workers]} simultaneously (visual-verified)", "SYS")
    with ThreadPoolExecutor(max_workers=len(verified_workers)) as pool:
        futures = {pool.submit(ghost_type_to_worker, w["hwnd"], task, orch_hwnd): w["name"]
                   for w in verified_workers}
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


def _load_routing_config():
    """Load expertise_weight and load_weight from brain_config.json."""
    try:
        bc = json.loads((DATA_DIR / "brain_config.json").read_text(encoding="utf-8"))
        routing = bc.get("routing", {})
        return routing.get("expertise_weight", 0.6), routing.get("load_weight", 0.4)
    except (json.JSONDecodeError, OSError, KeyError, UnicodeDecodeError):  # signed: beta
        return 0.6, 0.4


def _load_worker_profiles():
    """Load worker profiles for expertise matching."""
    try:
        pdata = json.loads((DATA_DIR / "agent_profiles.json").read_text(encoding="utf-8"))
        return {k: v for k, v in pdata.items()
                if isinstance(v, dict) and k not in ("version", "updated_at", "updated_by")}
    except (json.JSONDecodeError, OSError, KeyError, UnicodeDecodeError):  # signed: beta
        return {}


def _expertise_score(worker_name, task_lower, task_words, profiles):
    """Score 0.0 (no match) to 1.0 (perfect match) based on specializations."""
    profile = profiles.get(worker_name, {})
    specs = profile.get("specializations", [])
    if not specs:
        return 0.0
    matches = sum(1 for s in specs if s.lower() in task_lower or any(s.lower() in w for w in task_words))
    return min(1.0, matches / max(1, len(specs) * 0.3))


def _load_score(worker_name, states, bus_statuses):
    """Score 0.0 (idle, no pending) to 1.0 (busy, many pending)."""
    score_map = {"IDLE": 0, "TYPING": 1, "PROCESSING": 2, "STEERING": 3, "UNKNOWN": 4}
    state = states.get(worker_name, "UNKNOWN")
    state_val = score_map.get(state, 4) / 4.0
    pending = bus_statuses.get(worker_name, {}).get("pending_tasks", 0)
    pending_val = min(1.0, pending / 5.0)
    return (state_val + pending_val) / 2.0


def smart_dispatch(task, workers=None, orch_hwnd=None, n_workers=1):
    """Auto-route task to the best available worker(s).

    Uses expertise-aware routing: score = expertise_match * expertise_weight + inverse_load * load_weight.
    """
    if not workers:
        workers = load_workers()
    if not orch_hwnd:
        orch_hwnd = load_orch_hwnd()

    states = scan_all_states(workers)
    bus_statuses = get_worker_statuses()
    expertise_weight, load_weight = _load_routing_config()
    profiles = _load_worker_profiles()
    task_lower = task.lower()
    task_words = set(task_lower.split())

    def _combined_score(worker):
        name = worker["name"]
        exp = _expertise_score(name, task_lower, task_words, profiles)
        load = 1.0 - _load_score(name, states, bus_statuses)
        return exp * expertise_weight + load * load_weight

    ranked = sorted(workers, key=lambda w: -_combined_score(w))

    for w in ranked[:4]:
        name = w["name"]
        exp = _expertise_score(name, task_lower, task_words, profiles)
        load = _load_score(name, states, bus_statuses)
        log(f"smart_route: {name} exp={exp:.2f} load={load:.2f} combined={_combined_score(w):.2f} state={states.get(name, '?')}")

    selected = [w for w in ranked if states.get(w["name"]) == "IDLE"][:n_workers]
    if not selected:
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


def _scan_bus_messages_for_key(msgs, key_lower, seen_ids):
    """Scan a list of bus messages for one matching key. Returns match or None."""
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
    return None


def _wait_via_realtime_file(key_lower, seen_ids, deadline, realtime_path):
    """Poll data/realtime.json at 0.5s resolution for a matching message."""
    while time.time() < deadline:
        try:
            with open(realtime_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            msgs = data if isinstance(data, list) else data.get("messages", data.get("results", []))
            match = _scan_bus_messages_for_key(msgs, key_lower, seen_ids)
            if match:
                return match
        except (json.JSONDecodeError, OSError, ValueError, TypeError) as e:
            pass  # Transient file read errors during polling — retry next cycle  # signed: beta
        time.sleep(0.5)
    return None


def _wait_via_http_polling(key_lower, seen_ids, deadline, poll, skynet_url):
    """Poll bus via HTTP at configurable interval for a matching message."""
    import urllib.request
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{skynet_url}/bus/messages?limit=50", timeout=3) as r:
                msgs = json.loads(r.read())
            match = _scan_bus_messages_for_key(msgs, key_lower, seen_ids)
            if match:
                return match
        except (OSError, json.JSONDecodeError, ValueError) as e:
            log(f"HTTP poll error (will retry): {e}", "WARN")  # signed: beta
        time.sleep(poll)
    return None


def wait_for_bus_result(key, timeout=90, poll=2.0, skynet_url="http://localhost:8420",
                        auto_recover=True, _original_task=None):
    """Block until a bus message matching `key` (substring in content or sender) appears.

    Returns the matching message dict, or None on timeout.
    Tries file-based realtime wait first (0.5s resolution via data/realtime.json),
    falls back to HTTP polling (2.0s resolution) if realtime daemon is not running.
    If auto_recover=True and timeout is reached, cancels stuck workers and retries once.
    """
    if not key or not key.strip():
        log("wait_for_bus_result called with empty key — returning None", "ERR")  # signed: beta
        return None

    deadline = time.time() + timeout
    seen_ids = set()
    key_lower = key.lower()

    realtime_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "realtime.json")

    if os.path.exists(realtime_path):
        log(f"Waiting via realtime file (0.5s resolution) for '{key}' (timeout={timeout}s)...", "INFO")
        result = _wait_via_realtime_file(key_lower, seen_ids, deadline, realtime_path)
    else:
        log(f"Waiting via HTTP polling ({poll}s resolution) for '{key}' (timeout={timeout}s)...", "INFO")
        result = _wait_via_http_polling(key_lower, seen_ids, deadline, poll, skynet_url)

    if result:
        # Mark dispatch as received in dispatch_log.json  # signed: alpha
        sender = (result.get("sender") or "").strip()
        if sender:
            mark_dispatch_received(sender)  # signed: alpha
        return result

    if auto_recover and _original_task:
        log(f"Timeout waiting for '{key}' -- attempting auto-recovery", "WARN")
        if _auto_recover_stuck_workers(key, _original_task):
            return wait_for_bus_result(key, timeout=timeout, poll=poll,
                                       skynet_url=skynet_url, auto_recover=False)

    log(f"Timeout waiting for bus result matching '{key}'", "WARN")
    return None


def _try_cancel_and_wait_idle(engine, hwnd, wname):
    """Cancel generation on a worker and wait up to 3s for IDLE. Returns True if worker became IDLE."""
    try:
        engine.cancel_generation(int(hwnd))
    except Exception as e:
        log(f"Auto-recover: cancel failed for {wname.upper()}: {e}", "ERR")
        return False
    idle_deadline = time.time() + 3.0
    while time.time() < idle_deadline:
        if engine.get_state(int(hwnd)) == "IDLE":
            return True
        time.sleep(0.5)
    log(f"Auto-recover: {wname.upper()} did not become IDLE after cancel", "WARN")
    return False


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
        if not hwnd or key_lower not in wname.lower():
            continue
        state = engine.get_state(int(hwnd))
        if state != "PROCESSING":
            log(f"Auto-recover: {wname.upper()} is {state}, not stuck", "INFO")
            continue
        log(f"Auto-recover: {wname.upper()} stuck PROCESSING -- cancelling generation", "WARN")
        if not _try_cancel_and_wait_idle(engine, hwnd, wname):
            continue
        log(f"Auto-recover: re-dispatching to {wname.upper()}", "SYS")
        ok = dispatch_to_worker(wname, original_task, workers, load_orch_hwnd())
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
    import urllib.request  # signed: gamma
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


def _handle_state_query(args, workers):
    """Handle --state and --state-all CLI queries. Returns True if handled."""
    if args.state_all:
        states = scan_all_states(workers)
        log("=== UIA STATES (parallel scan) ===", "SYS")
        for name, state in states.items():
            icon = {"IDLE": "✅", "PROCESSING": "⏳", "STEERING": "⚠️", "TYPING": "✏️"}.get(state, "❓")
            print(f"  {name.upper():<8} {icon} {state}")
        return True
    if args.state:
        target_workers = [w for w in workers if w["name"] == args.state]
        if not target_workers:
            log(f"Worker '{args.state}' not found", "ERR")
            return True
        state = get_worker_state_uia(target_workers[0]["hwnd"])
        log(f"{args.state.upper()} UIA state: {state}", "INFO")
        return True
    return False


def _handle_bus_status():
    """Handle --bus-status CLI query. Prints bus messages and worker statuses."""
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


def _handle_open_project(project):
    """Handle --open-project: configure venv and launch VS Code Insiders."""
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
        with open(settings_path, encoding="utf-8") as f:  # signed: gamma
            try: settings = json.load(f)
            except (json.JSONDecodeError, ValueError): settings = {}
    if venv_py:
        settings["python.defaultInterpreterPath"] = venv_py.replace("\\", "/")
        with open(settings_path, "w", encoding="utf-8") as f:  # signed: gamma
            json.dump(settings, f, indent=2)
        log(f"Set python.defaultInterpreterPath -> {venv_py}", "OK")
    else:
        log(f"No venv found in {project} -- interpreter not set", "WARN")
    subprocess.Popen(["code-insiders", project])
    log(f"Opened {project} in VS Code Insiders", "OK")


def _execute_dispatch_mode(args, workers, orch_hwnd):
    """Execute the selected dispatch mode. Returns dispatch result or None."""
    if args.blast and args.task:
        return blast_all(args.task, workers, orch_hwnd)
    if args.parallel and args.task:
        return dispatch_parallel({w["name"]: args.task for w in workers}, workers, orch_hwnd)
    if args.smart and args.task:
        routed = smart_dispatch(args.task, workers, orch_hwnd, n_workers=args.n)
        log(f"Smart-dispatched to: {routed}", "OK" if routed else "ERR")
        return routed
    if args.fan_out_parallel:
        with open(args.fan_out_parallel, encoding="utf-8") as f:  # signed: gamma
            return dispatch_parallel(json.load(f), workers, orch_hwnd)
    if args.batch:
        with open(args.batch, encoding="utf-8") as f:  # signed: gamma
            return batch_dispatch(json.load(f), workers, orch_hwnd)
    if args.fan_out:
        with open(args.fan_out, encoding="utf-8") as f:  # signed: gamma
            return fan_out(json.load(f), workers, orch_hwnd, args.delay)
    if args.idle and args.task:
        exclude = [x.strip() for x in args.exclude.split(",")] if args.exclude else []
        target = dispatch_to_idle(args.task, exclude=exclude, workers=workers, orch_hwnd=orch_hwnd)
        log(f"Dispatched to idle worker: {target}" if target else "No idle worker available",
            "OK" if target else "ERR")
        return target
    if getattr(args, "moa", False) and args.task:
        # P2.05: Mixture of Agents dispatch — signed: alpha
        from tools.skynet_moa import MoADispatch
        moa = MoADispatch()
        n = getattr(args, "moa_n", 3)
        result = moa.dispatch_moa(args.task, n_workers=n, timeout=args.timeout)
        log(f"MoA dispatched to {result.get('n_workers', 0)} workers, "
            f"state={result.get('state')}", "OK")
        return result
    if getattr(args, "debate", False) and args.task:
        # P2.06: Red Team / Blue Team adversarial debate — signed: beta
        from tools.skynet_debate import dispatch_debate
        debate_rounds = getattr(args, "debate_rounds", 3)
        result = dispatch_debate(args.task, rounds=debate_rounds)
        log(f"Debate {result['session_id']} dispatched: "
            f"{len(result['dispatched'])} rounds to "
            f"{len(set(d['worker'] for d in result['dispatched']))} workers",
            "OK")
        return result
    if args.all and args.task:
        return dispatch_to_all(args.task, workers, orch_hwnd, args.delay)
    if args.worker and args.task:
        return dispatch_to_worker(args.worker, args.task, workers, orch_hwnd)
    return None


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Skynet Dispatch -- Send tasks to workers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes (fastest first):
  --blast        Parallel broadcast to ALL idle workers, no preamble. Max speed.
  --parallel     Parallel broadcast to ALL workers with steering preamble.
  --smart        Auto-route to best idle worker(s). Use --n for multiple.
  --moa          Mixture of Agents: same task to N workers with different personas,
                 collect responses, synthesize. Use --moa-n (default 3).
  --worker NAME  Target specific identity (worker, orchestrator, consultant).
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
    parser.add_argument("--worker", type=str, help="Target identity name")
    parser.add_argument("--task", type=str, help="Task to dispatch")
    parser.add_argument("--all", action="store_true", help="Sequential broadcast to all workers")
    parser.add_argument("--parallel", action="store_true", help="PARALLEL broadcast to all workers simultaneously")
    parser.add_argument("--blast", action="store_true", help="FASTEST: parallel to all IDLE workers, no preamble")
    parser.add_argument("--smart", action="store_true", help="Auto-route to best idle worker(s)")
    parser.add_argument("--n", type=int, default=1, help="Number of workers for --smart (default 1)")
    parser.add_argument("--fan-out", type=str, help="JSON file with worker->task mapping (sequential)")
    parser.add_argument("--fan-out-parallel", type=str, help="JSON file with worker->task mapping (parallel, faster)")
    parser.add_argument("--batch", type=str, help="JSON file with worker->[task list] mapping (consolidates same-worker tasks)")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between dispatches for sequential modes (seconds)")
    parser.add_argument("--idle", action="store_true", help="Dispatch task to first idle worker")
    parser.add_argument("--moa", action="store_true", help="Mixture of Agents: dispatch to N workers with different personas, collect+synthesize")
    parser.add_argument("--moa-n", type=int, default=3, help="Number of MoA workers/personas (1-4, default 3)")
    parser.add_argument("--exclude", type=str, help="Comma-separated worker names to exclude")
    parser.add_argument("--bus-status", action="store_true", help="Poll bus and print recent messages + worker statuses")
    parser.add_argument("--open-project", type=str, help="Open a project dir in VS Code Insiders with its venv")
    parser.add_argument("--state", type=str, help="Get UIA state of a specific worker (e.g. --state gamma)")
    parser.add_argument("--state-all", action="store_true", help="Get UIA state of ALL workers (parallel scan)")
    parser.add_argument("--debate", action="store_true", help="Run task through Red Team/Blue Team adversarial debate")
    parser.add_argument("--debate-rounds", type=int, default=3, help="Number of debate rounds (default 3)")
    parser.add_argument("--wait-result", type=str, help="After dispatch, wait for bus result matching this key")
    parser.add_argument("--timeout", type=float, default=90, help="Timeout for --wait-result (default 90s)")  # signed: beta
    args = parser.parse_args()

    workers = load_workers()
    orch_hwnd = load_orch_hwnd()

    if _handle_state_query(args, workers):
        return
    if args.bus_status:
        _handle_bus_status()
        return
    if args.open_project:
        _handle_open_project(args.open_project)
        return
    if not workers:
        log("No workers loaded", "ERR")
        return

    t0 = time.time()
    result = _execute_dispatch_mode(args, workers, orch_hwnd)
    if result is None:
        parser.print_help()
        return
    log(f"Dispatch took {time.time() - t0:.2f}s", "INFO")
    if args.wait_result:
        wait_for_bus_result(args.wait_result, timeout=args.timeout)


if __name__ == "__main__":
    main()
