#!/usr/bin/env python3
"""
Skynet Proactive Handler Daemon — Auto-detects and clears blocking UI elements.

Runs every SCAN_INTERVAL seconds and checks ALL worker windows for:
  1. "Uncommitted Changes" dialog  → clicks "Copy Changes"
  2. Error / assertion dialogs     → clicks "OK" / "Close"
  3. Stuck Apply buttons           → F6×4 + Ctrl+L to dismiss
  4. STEERING state                → cancels via "Cancel (Alt+Backspace)"
  5. "Pending Requests" dialog     → clicks "Remove Pending Requests"

This daemon is PROACTIVE — it does not wait for the orchestrator to notice problems.
It runs alongside skynet_monitor.py and complements it (monitor checks health/model,
this handler clears UI blockages).

Usage:
  python tools/skynet_proactive_handler.py          # foreground
  python tools/skynet_proactive_handler.py start     # daemon mode
  python tools/skynet_proactive_handler.py stop      # stop daemon
  python tools/skynet_proactive_handler.py status    # check if running

# signed: orchestrator
"""

import json
import os
import signal
import subprocess
import sys
import time
import ctypes
import ctypes.wintypes
import logging
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PID_FILE = DATA / "proactive_handler.pid"
LOG_FILE = DATA / "proactive_handler.log"
SCAN_INTERVAL = 15  # seconds between scans
PYTHON = sys.executable

# ── Logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [PROACTIVE] %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("proactive")

# ── Singleton PID management ────────────────────────────────────────

def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        import psutil
        p = psutil.Process(pid)
        return p.is_running() and "proactive" in " ".join(p.cmdline()).lower()
    except Exception:
        return False


def _write_pid():
    PID_FILE.write_text(str(os.getpid()))


def _remove_pid():
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── Worker HWND loading ────────────────────────────────────────────

def _load_worker_hwnds() -> dict[str, int]:
    """Load worker name→HWND from workers.json."""
    wf = DATA / "workers.json"
    if not wf.exists():
        return {}
    try:
        raw = json.loads(wf.read_text(encoding="utf-8"))
        workers = raw.get("workers", raw) if isinstance(raw, dict) else raw
        return {w["name"]: w["hwnd"] for w in workers if w.get("hwnd")}
    except Exception as e:
        log.warning(f"Failed to load workers.json: {e}")
        return {}


def _hwnd_alive(hwnd: int) -> bool:
    return bool(ctypes.windll.user32.IsWindow(hwnd))


# ── UIA Dialog Detection & Clearing ────────────────────────────────

def _build_dialog_scan_ps(hwnd: int) -> str:
    """Build PowerShell that scans a worker window for blocking dialogs and handles them."""
    return f'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$hwnd = [IntPtr]{hwnd}
$root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
if (-not $root) {{ Write-Host "NO_ROOT"; exit }}

$btnType = [System.Windows.Automation.ControlType]::Button
$btnCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty, $btnType)
$buttons = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $btnCond)

$handled = $false

foreach ($btn in $buttons) {{
    $name = $btn.Current.Name
    
    # 1. "Copy Changes" — Uncommitted Changes dialog
    if ($name -eq "Copy Changes") {{
        try {{
            $pat = $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
            $pat.Invoke()
            Write-Host "CLEARED:UNCOMMITTED_CHANGES"
            $handled = $true
            break
        }} catch {{ }}
    }}
    
    # 2. "Remove Pending Requests" — Pending requests dialog
    if ($name -match "Remove Pending") {{
        try {{
            $pat = $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
            $pat.Invoke()
            Write-Host "CLEARED:PENDING_REQUESTS"
            $handled = $true
            break
        }} catch {{ }}
    }}
    
    # 3. "Cancel (Alt+Backspace)" — STEERING state
    if ($name -match "Cancel.*Alt.*Backspace") {{
        try {{
            $pat = $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
            $pat.Invoke()
            Write-Host "CLEARED:STEERING"
            $handled = $true
            break
        }} catch {{ }}
    }}
    
    # 4. Error dialog OK buttons (VS Code native dialogs)
    if ($name -eq "OK" -and $btn.Current.ClassName -ne "Chrome_RenderWidgetHostHWND") {{
        # Verify it's inside a dialog context (not a regular button)
        $parent = [System.Windows.Automation.TreeWalker]::RawViewWalker.GetParent($btn)
        if ($parent) {{
            $pName = $parent.Current.Name
            if ($pName -match "Error|Exception|Failed|Warning|Assert") {{
                try {{
                    $pat = $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
                    $pat.Invoke()
                    Write-Host "CLEARED:ERROR_DIALOG:$pName"
                    $handled = $true
                    break
                }} catch {{ }}
            }}
        }}
    }}
}}

if (-not $handled) {{
    Write-Host "CLEAN"
}}
'''


def _scan_and_clear_worker(name: str, hwnd: int) -> str | None:
    """Scan one worker for blocking dialogs. Returns action taken or None."""
    if not _hwnd_alive(hwnd):
        return None

    ps_script = _build_dialog_scan_ps(hwnd)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=12,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        output = result.stdout.strip()

        if output.startswith("CLEARED:"):
            action = output.split(":", 1)[1]
            log.info(f"✅ {name} (HWND={hwnd}): Auto-cleared {action}")
            return action
        elif output == "CLEAN":
            return None
        else:
            return None

    except subprocess.TimeoutExpired:
        log.warning(f"⚠️ {name}: UIA scan timed out (12s)")
        return None
    except Exception as e:
        log.warning(f"⚠️ {name}: scan error: {e}")
        return None


_apply_cleared_recently: dict[int, float] = {}  # hwnd → last_cleared_time

def _check_apply_stuck(name: str, hwnd: int) -> bool:
    """Check if worker has a stuck Apply button and clear it with F6+Ctrl+L.
    Skips if Apply was already cleared within 120s (prevents repeat-clear loops)."""
    # Skip if we already cleared this worker's Apply recently
    last = _apply_cleared_recently.get(hwnd, 0)
    if time.time() - last < 120:
        return False  # Already handled, don't re-clear
    ps_apply = f'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
$btnCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Button)
$buttons = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $btnCond)
$found = $false
foreach ($b in $buttons) {{
    if ($b.Current.Name -match "^Apply$" -and $b.Current.Name -notmatch "Editor") {{
        $found = $true
        break
    }}
}}
if ($found) {{ Write-Host "APPLY_STUCK" }} else {{ Write-Host "NO_APPLY" }}
'''
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_apply],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000,
        )
        if "APPLY_STUCK" in r.stdout:
            log.info(f"🔧 {name}: Apply button detected — clearing with F6+Ctrl+L")
            _clear_apply_with_keyboard(hwnd)
            _apply_cleared_recently[hwnd] = time.time()
            return True
    except Exception:
        pass
    return False


def _clear_apply_with_keyboard(hwnd: int):
    """Clear stuck Apply button using F6×4 + Ctrl+L (known working method)."""
    user32 = ctypes.windll.user32
    # Focus the window
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)

    VK_F6 = 0x75
    VK_CONTROL = 0x11
    VK_L = 0x4C

    # F6 × 4 to cycle panel focus to chat
    for _ in range(4):
        user32.PostMessageW(hwnd, 0x0100, VK_F6, 0)  # WM_KEYDOWN
        time.sleep(0.05)
        user32.PostMessageW(hwnd, 0x0101, VK_F6, 0)  # WM_KEYUP
        time.sleep(0.15)

    time.sleep(0.3)

    # Ctrl+L to start new conversation (dismisses Apply)
    user32.PostMessageW(hwnd, 0x0100, VK_CONTROL, 0)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, 0x0100, VK_L, 0)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, 0x0101, VK_L, 0)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, 0x0101, VK_CONTROL, 0)
    time.sleep(0.3)

    log.info(f"  → F6×4 + Ctrl+L sent to HWND={hwnd}")


# ── Bus notification ───────────────────────────────────────────────

def _notify_bus(worker_name: str, action: str):
    """Post proactive action to bus so orchestrator knows."""
    try:
        from tools.skynet_spam_guard import guarded_publish
        guarded_publish({
            "sender": "proactive_handler",
            "topic": "orchestrator",
            "type": "proactive_clear",
            "content": f"AUTO-CLEARED {action} on {worker_name} -- no orchestrator intervention needed",
        })
    except Exception:
        # Fallback: log only, don't crash the daemon
        log.warning(f"Bus notify failed for {worker_name}:{action}")


# ── Main scan loop ─────────────────────────────────────────────────

_running = True


def _signal_handler(sig, frame):
    global _running
    log.info(f"Signal {sig} received — shutting down")
    _running = False


def run_daemon():
    """Main proactive handler loop."""
    global _running

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_handler)

    _write_pid()
    log.info(f"Proactive Handler started (PID={os.getpid()}, interval={SCAN_INTERVAL}s)")

    actions_taken = 0
    scans = 0

    try:
        while _running:
            workers = _load_worker_hwnds()
            if not workers:
                time.sleep(SCAN_INTERVAL)
                continue

            scans += 1
            for name, hwnd in workers.items():
                if not _running:
                    break
                if not _hwnd_alive(hwnd):
                    continue

                # Phase 1: Dialog scan (Uncommitted, Pending, Steering, Errors)
                action = _scan_and_clear_worker(name, hwnd)
                if action:
                    _notify_bus(name, action)
                    actions_taken += 1
                    time.sleep(1)  # brief pause after clearing
                    continue  # don't also check Apply on same cycle

                # Phase 2: Apply button check (only if no dialog found)
                if _check_apply_stuck(name, hwnd):
                    _notify_bus(name, "APPLY_STUCK")
                    actions_taken += 1
                    time.sleep(1)

            # Periodic status log
            if scans % 20 == 0:
                log.info(f"📊 Scan #{scans}: {len(workers)} workers monitored, {actions_taken} total actions taken")

            time.sleep(SCAN_INTERVAL)

    finally:
        _remove_pid()
        log.info(f"Proactive Handler stopped. {scans} scans, {actions_taken} actions taken.")


# ── CLI ─────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("run", "foreground"):
        run_daemon()
        return

    cmd = sys.argv[1]

    if cmd == "start":
        pid = _read_pid()
        if pid and _pid_alive(pid):
            print(f"Already running (PID={pid})")
            return
        proc = subprocess.Popen(
            [PYTHON, __file__, "run"],
            cwd=str(ROOT),
            creationflags=0x00000008 | 0x00000200,  # DETACHED + CREATE_NEW_PROCESS_GROUP
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"Started proactive handler (PID={proc.pid})")

    elif cmd == "stop":
        pid = _read_pid()
        if pid and _pid_alive(pid):
            import psutil
            psutil.Process(pid).terminate()
            print(f"Stopped (PID={pid})")
            _remove_pid()
        else:
            print("Not running")
            _remove_pid()

    elif cmd == "status":
        pid = _read_pid()
        if pid and _pid_alive(pid):
            print(f"Running (PID={pid})")
        else:
            print("Not running")
            _remove_pid()

    elif cmd == "scan":
        # One-shot scan
        workers = _load_worker_hwnds()
        print(f"Scanning {len(workers)} workers...")
        for name, hwnd in workers.items():
            if not _hwnd_alive(hwnd):
                print(f"  {name}: DEAD (HWND={hwnd})")
                continue
            action = _scan_and_clear_worker(name, hwnd)
            if action:
                print(f"  {name}: CLEARED {action}")
            else:
                applied = _check_apply_stuck(name, hwnd)
                if applied:
                    print(f"  {name}: CLEARED APPLY_STUCK")
                else:
                    print(f"  {name}: CLEAN [OK]")

    else:
        print(f"Usage: {sys.argv[0]} [start|stop|status|scan|run]")


if __name__ == "__main__":
    main()
