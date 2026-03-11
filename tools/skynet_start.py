#!/usr/bin/env python3
"""
Skynet Start — Unified orchestrator bootstrap.

Trigger words: "skynet-start", "orchestrator-start", "Orch-Start"

Starts Skynet backend, GOD Console, opens worker chat windows (ghost mouse),
prompts each, registers with Skynet, and connects ScreenMemory engines.

Learned from new_chat.ps1 activation sequence:
- Grid slots use 930x500, bottom row at y=540 (y+h=1040, taskbar safe)
- Each worker must be prompted BEFORE opening the next window
  (new_chat.ps1 blocks if any chat has no first prompt)
- Failure tracker (chat_open_failures.json) must be cleared between opens
- Model guard uses keyboard filter ("fast" + Down+Enter), not UIA list clicks

Usage:
    python tools/skynet_start.py                  # Full bootstrap
    python tools/skynet_start.py --fresh          # Skip session restore, fresh windows only
    python tools/skynet_start.py --workers 2      # Only 2 workers
    python tools/skynet_start.py --reconnect      # Reconnect to existing workers
    python tools/skynet_start.py --status         # Show system status
"""

import json
import os
import sys
import time
import socket
import urllib.request
import subprocess
import ctypes
import ctypes.wintypes
import atexit
from pathlib import Path
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError

# Force UTF-8 output on Windows to handle emoji and box-drawing characters
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "chrome_bridge"))

# ─── Win32 Constants ───────────────────────────────────
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP   = 0x0202
MK_LBUTTON     = 0x0001

user32 = ctypes.windll.user32

# ─── Config ────────────────────────────────────────────
SKYNET_PORT    = 8420
GOD_PORT       = 8421
SKYNET_EXE     = str(ROOT / "Skynet" / "skynet.exe")

# Resolve the REAL Python interpreter to avoid venv trampoline double-process.
# On Python 3.13+ Windows, the venv's python.exe is a launcher stub that spawns
# the real interpreter as a child — doubling process count for every daemon.
def _resolve_real_python():
    """Return (real_python_path, env_dict) that bypasses the venv trampoline."""
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
        base_python = sys.executable
    # Build env that activates the venv for the base interpreter
    env = os.environ.copy()
    site_packages = str(venv_dir / "Lib" / "site-packages")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{site_packages};{existing}" if existing else site_packages
    env["VIRTUAL_ENV"] = str(venv_dir)
    return base_python, env

PYTHON, _DAEMON_ENV = _resolve_real_python()
DATA_DIR       = ROOT / "data"
WORKERS_FILE   = DATA_DIR / "workers.json"
ORCH_FILE      = DATA_DIR / "orchestrator.json"
BACKGROUND_SPAWN_FLAGS = (
    subprocess.CREATE_NEW_PROCESS_GROUP
    | subprocess.DETACHED_PROCESS
    | subprocess.CREATE_NO_WINDOW
)
NEW_CHAT_PS1   = str(ROOT / "tools" / "new_chat.ps1")

BOOT_IN_PROGRESS_FILE = DATA_DIR / "boot_in_progress.json"
WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]
GRID_SLOTS = [
    {"name": "alpha", "x": 1930, "y": 20,  "w": 930, "h": 500, "grid": "top-left"},
    {"name": "beta",  "x": 2870, "y": 20,  "w": 930, "h": 500, "grid": "top-right"},
    {"name": "gamma", "x": 1930, "y": 540, "w": 930, "h": 500, "grid": "bottom-left"},
    {"name": "delta", "x": 2870, "y": 540, "w": 930, "h": 500, "grid": "bottom-right"},
]


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"INFO": "🔵", "OK": "🟢", "WARN": "🟡", "ERR": "🔴", "SYS": "⚡"}.get(level, "  ")
    print(f"[{ts}] {prefix} {msg}", flush=True)


def _set_boot_phase(phase_name):
    """Write boot_in_progress.json so other daemons (self-prompt) back off during boot."""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        BOOT_IN_PROGRESS_FILE.write_text(json.dumps({
            "phase": phase_name,
            "pid": os.getpid(),
            "started": datetime.now().isoformat(),
            "t": time.time(),
        }))
    except Exception:
        pass


def _clear_boot_phase():
    """Remove boot_in_progress.json — signals boot is complete and daemons may resume."""
    try:
        BOOT_IN_PROGRESS_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ─── Network Helpers ───────────────────────────────────

def port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def http_get(path, port=SKYNET_PORT, timeout=5):
    try:
        with urlopen(f"http://localhost:{port}{path}", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def http_post(path, body, port=SKYNET_PORT, timeout=10):
    try:
        data = json.dumps(body).encode()
        req = Request(f"http://localhost:{port}{path}", data=data, method="POST",
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"POST {path} failed: {e}", "WARN")
        return None


# ─── Win32 Helpers ─────────────────────────────────────

def get_orchestrator_hwnd():
    """Read orchestrator HWND from data/orchestrator.json."""
    if ORCH_FILE.exists():
        data = json.loads(ORCH_FILE.read_text())
        return data.get("orchestrator_hwnd")
    return None


def find_vscode_windows():
    """Find all visible VS Code windows."""
    results = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def enum_cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if "Visual Studio Code" in buf.value:
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            results.append({
                "hwnd": hwnd,
                "title": buf.value,
                "x": rect.left, "y": rect.top,
                "w": rect.right - rect.left, "h": rect.bottom - rect.top,
            })
        return True

    user32.EnumWindows(enum_cb, 0)
    return results


def get_chat_windows(orch_hwnd):
    """Get detached chat windows (VS Code windows that aren't the orchestrator)."""
    all_wins = find_vscode_windows()
    chats = []
    for w in all_wins:
        if w["hwnd"] == orch_hwnd:
            continue
        if w["w"] < 1200:  # Detached chat windows are narrow
            chats.append(w)
    return chats


def ghost_click(hwnd, screen_x, screen_y):
    """Click inside a window via PostMessage — zero cursor movement."""
    render = ctypes.windll.user32.FindWindowExW(hwnd, None, "Chrome_RenderWidgetHostHWND", None)
    if not render:
        child = ctypes.windll.user32.FindWindowExW(hwnd, None, None, None)
        while child:
            render = ctypes.windll.user32.FindWindowExW(child, None, "Chrome_RenderWidgetHostHWND", None)
            if render:
                break
            child = ctypes.windll.user32.FindWindowExW(hwnd, child, None, None)
    if not render:
        render = hwnd

    pt = ctypes.wintypes.POINT(screen_x, screen_y)
    user32.ScreenToClient(render, ctypes.byref(pt))
    lparam = (pt.y << 16) | (pt.x & 0xFFFF)
    user32.PostMessageW(render, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
    time.sleep(0.05)
    user32.PostMessageW(render, WM_LBUTTONUP, 0, lparam)


def move_window(hwnd, x, y, w, h):
    user32.MoveWindow(hwnd, x, y, w, h, True)


def focus_window(hwnd):
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)


def chat_has_messages(hwnd):
    """Check if a chat window has conversation messages using UIA."""
    ps = f'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
$items = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::ListItem
    ))
)
$count = 0
foreach ($li in $items) {{
    if ($li.Current.ClassName -match 'monaco-list-row') {{ $count++ }}
}}
Write-Host $count
'''
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=10
        )
        return int(r.stdout.strip()) > 0
    except Exception:
        return False


def open_chat_window(orch_hwnd):
    """Open a new detached chat window using tools/new_chat.ps1.

    new_chat.ps1 is the proven, battle-tested script that:
      1. Finds the narrow dropdown ▾ button (right side of the chat panel header)
      2. Ghost-clicks it via PostMessage (no cursor movement)
      3. Invokes 'New Chat Window' menu item via UIA InvokePattern
      4. Waits 4000ms for the window to appear
      5. Runs model guard (ensures Claude Opus 4.6 fast + Copilot CLI)

    We skip new_chat.ps1's built-in grid positioning (we handle that ourselves)
    and its empty-chat blocker (we manage sequential opens).

    Returns the new HWND on success, None on failure.
    """
    before = {w["hwnd"] for w in find_vscode_windows()}

    script_path = str(ROOT / "tools" / "new_chat.ps1")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", script_path, "-Monitor", "2", "-SkipEmptyCheck"],
            capture_output=True, text=True, timeout=45,
            cwd=str(ROOT)
        )
        if r.returncode != 0:
            stderr = r.stderr.strip()[:200] if r.stderr else ""
            stdout = r.stdout.strip()[:200] if r.stdout else ""
            if "BLOCKED" in (r.stdout or ""):
                log(f"new_chat.ps1: blocked (all slots full or consecutive failures)", "WARN")
            else:
                log(f"new_chat.ps1 failed (rc={r.returncode}): {stdout} {stderr}", "ERR")
            return None

        # rc=0 but still blocked (e.g. BLOCKED: all slots full)
        if "BLOCKED" in (r.stdout or "") and "OK HWND=" not in (r.stdout or ""):
            log(f"new_chat.ps1: blocked — {(r.stdout or '').strip()[:120]}", "WARN")
            return None

        log(f"Chat window opened via new_chat.ps1", "OK")

        # Parse HWND directly from new_chat.ps1 output: "OK HWND=<n> pos=..."
        import re as _re
        m = _re.search(r'OK HWND=(\d+)', r.stdout or "")
        if m:
            hwnd = int(m.group(1))
            log(f"New chat window: HWND={hwnd}", "OK")
            return hwnd
    except subprocess.TimeoutExpired:
        log("new_chat.ps1 timed out after 45s", "ERR")
        return None
    except Exception as e:
        log(f"open_chat_window failed: {e}", "ERR")
        return None

    # Poll for new window (up to 8 seconds — new_chat.ps1 already waits 4s internally)
    for _ in range(8):
        time.sleep(1)
        after = {w["hwnd"] for w in find_vscode_windows()}
        new_hwnds = after - before - {orch_hwnd}
        if new_hwnds:
            new_hwnd = new_hwnds.pop()
            log(f"New chat window detected: HWND={new_hwnd}", "OK")
            return new_hwnd

    log("New chat window not detected after 8s", "ERR")
    return None


MAX_SESSION_RESTORE_ATTEMPTS = 2


def restore_session_from_panel(session_name, orch_hwnd, slot):
    """Right-click a session in the SESSIONS panel → Open in New Window.
    
    Returns the new HWND on success, None on failure.
    Max 2 attempts — if both fail, reports failure immediately.
    """
    for attempt in range(1, MAX_SESSION_RESTORE_ATTEMPTS + 1):
        log(f"Restoring session '{session_name}' (attempt {attempt}/{MAX_SESSION_RESTORE_ATTEMPTS})...", "SYS")
        
        before = {w["hwnd"] for w in find_vscode_windows()}
        
        ps = f'''
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type @"
using System; using System.Runtime.InteropServices;
public class SR {{
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr wp, IntPtr lp);
    [DllImport("user32.dll")] public static extern IntPtr FindWindowEx(IntPtr p, IntPtr a, string c, string w);
    [DllImport("user32.dll")] public static extern bool ScreenToClient(IntPtr h, ref POINT p);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [StructLayout(LayoutKind.Sequential)] public struct POINT {{ public int X, Y; }}
    public static void RClick(IntPtr rh, int sx, int sy) {{
        POINT pt; pt.X=sx; pt.Y=sy; ScreenToClient(rh,ref pt);
        IntPtr lp=(IntPtr)((pt.Y<<16)|(pt.X&0xFFFF));
        PostMessage(rh,0x0204,(IntPtr)2,lp); System.Threading.Thread.Sleep(60);
        PostMessage(rh,0x0205,IntPtr.Zero,lp);
    }}
    public delegate bool EnumWP(IntPtr h,IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p,EnumWP cb,IntPtr l);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h,System.Text.StringBuilder sb,int n);
    private static IntPtr _found;
    private static bool _enumCb(IntPtr h,IntPtr l){{var sb=new System.Text.StringBuilder(256);GetClassName(h,sb,256);if(sb.ToString()=="Chrome_RenderWidgetHostHWND"){{_found=h;return false;}}return true;}}
    public static IntPtr FindRender(IntPtr p) {{
        _found=IntPtr.Zero;
        EnumChildWindows(p,new EnumWP(_enumCb),IntPtr.Zero);
        return _found!=IntPtr.Zero?_found:p;
    }}
}}
"@

$orch=[IntPtr]{orch_hwnd}
$render=[SR]::FindRender($orch)
[SR]::SetForegroundWindow($orch)
Start-Sleep -Milliseconds 500

# Find the session ListItem by name via UIA
$orchEl=[System.Windows.Automation.AutomationElement]::FromHandle($orch)
$allEls=$orchEl.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
$found=$false
foreach($el in $allEls) {{
    try {{
        if($el.Current.ControlType -eq [System.Windows.Automation.ControlType]::ListItem -and $el.Current.Name -match '{session_name}') {{
            $r=$el.Current.BoundingRectangle
            $cx=[int]($r.X+$r.Width/2); $cy=[int]($r.Y+$r.Height/2)
            [SR]::RClick($render,$cx,$cy)
            Start-Sleep -Milliseconds 1500
            $desktop=[System.Windows.Automation.AutomationElement]::RootElement
            $mis=$desktop.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::MenuItem)))
            foreach($mi in $mis) {{
                if($mi.Current.Name -eq 'Open in New Window') {{
                    $mi.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
                    Write-Host "OPENED"
                    $found=$true; break
                }}
            }}
            break
        }}
    }} catch {{}}
}}
if(-not $found) {{ Write-Host "NOT_FOUND" }}
[SR]::SetForegroundWindow($orch)
'''
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=30
            )
        except Exception as e:
            log(f"Session restore attempt {attempt} failed: {e}", "ERR")
            continue
        
        if "OPENED" not in r.stdout:
            log(f"Session '{session_name}' not found in SESSIONS panel (attempt {attempt})", "WARN")
            continue
        
        # Poll for new window (up to 8 seconds)
        new_hwnd = None
        for _ in range(8):
            time.sleep(1)
            after = {w["hwnd"] for w in find_vscode_windows()}
            new_hwnds = after - before
            if new_hwnds:
                new_hwnd = new_hwnds.pop()
                break
        
        if new_hwnd:
            move_window(new_hwnd, slot["x"], slot["y"], slot["w"], slot["h"])
            guard_model(new_hwnd, orch_hwnd)
            focus_window(orch_hwnd)
            log(f"Session '{session_name}' restored: HWND={new_hwnd} → ({slot['x']},{slot['y']})", "OK")
            return new_hwnd
        else:
            log(f"Window did not appear after restore (attempt {attempt})", "WARN")
    
    log(f"FAILED: Could not restore session '{session_name}' after {MAX_SESSION_RESTORE_ATTEMPTS} attempts", "ERR")
    return None


def guard_model(hwnd, orch_hwnd):
    """Ensure a chat window is on Claude Opus 4.6 (fast mode) + Copilot CLI.
    
    Reads the 'Pick Model' button text via UIA. If it doesn't say 'Opus 4.6.*fast',
    opens the model picker and selects the correct model.
    """
    ps = f'''
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type @"
using System; using System.Runtime.InteropServices;
public class MG {{
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr wp, IntPtr lp);
    [DllImport("user32.dll")] public static extern IntPtr FindWindowEx(IntPtr p, IntPtr a, string c, string w);
    [DllImport("user32.dll")] public static extern bool ScreenToClient(IntPtr h, ref POINT p);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [StructLayout(LayoutKind.Sequential)] public struct POINT {{ public int X, Y; }}
    public static void Click(IntPtr rh, int sx, int sy) {{
        POINT pt; pt.X=sx; pt.Y=sy; ScreenToClient(rh,ref pt);
        IntPtr lp=(IntPtr)((pt.Y<<16)|(pt.X&0xFFFF));
        PostMessage(rh,0x0201,(IntPtr)1,lp); System.Threading.Thread.Sleep(50);
        PostMessage(rh,0x0202,IntPtr.Zero,lp);
    }}
    public delegate bool EnumWP(IntPtr h,IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p,EnumWP cb,IntPtr l);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h,System.Text.StringBuilder sb,int n);
    private static IntPtr _found;
    private static bool _enumCb(IntPtr h,IntPtr l){{var sb=new System.Text.StringBuilder(256);GetClassName(h,sb,256);if(sb.ToString()=="Chrome_RenderWidgetHostHWND"){{_found=h;return false;}}return true;}}
    public static IntPtr FindRender(IntPtr p) {{
        _found=IntPtr.Zero;
        EnumChildWindows(p,new EnumWP(_enumCb),IntPtr.Zero);
        return _found!=IntPtr.Zero?_found:p;
    }}
}}
"@

$hwnd=[IntPtr]{hwnd}
$render=[MG]::FindRender($hwnd)
$root=[System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
$btns=$root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button)))

$modelOk=$false; $targetOk=$false
foreach($b in $btns) {{
    $n=$b.Current.Name
    if($n -match 'Pick Model.*Opus 4\\.6.*fast') {{ $modelOk=$true }}
    if($n -match 'Copilot CLI') {{ $targetOk=$true }}
}}

if(-not $modelOk) {{
    foreach($b in $btns) {{
        if($b.Current.Name -match 'Pick Model') {{
            [MG]::SetForegroundWindow($hwnd)
            Start-Sleep -Milliseconds 300
            $r=$b.Current.BoundingRectangle
            [MG]::Click($render,[int]($r.X+$r.Width/2),[int]($r.Y+$r.Height/2))
            Start-Sleep -Milliseconds 1500
            # Type "opus" to filter to Claude Opus only (avoids "Grok Code Fast" false match)
            Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
            [System.Windows.Forms.SendKeys]::SendWait("opus")
            Start-Sleep -Milliseconds 1000
            [System.Windows.Forms.SendKeys]::SendWait("{{DOWN}}{{ENTER}}")
            Start-Sleep -Milliseconds 800
            Write-Host "MODEL_FIXED"
            break
        }}
    }}
}}

if(-not $targetOk) {{
    # Re-read buttons after model change
    $root2=[System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
    $btns2=$root2.FindAll([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button)))
    foreach($b in $btns2) {{
        if($b.Current.Name -match 'Session Target' -and $b.Current.Name -notmatch 'Copilot CLI') {{
            $r=$b.Current.BoundingRectangle
            [MG]::Click($render,[int]($r.X+$r.Width/2),[int]($r.Y+$r.Height/2))
            Start-Sleep -Milliseconds 1500
            $desktop=[System.Windows.Automation.AutomationElement]::RootElement
            $cbs=$desktop.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                (New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                    [System.Windows.Automation.ControlType]::CheckBox)))
            foreach($cb in $cbs) {{
                if($cb.Current.Name -eq 'Copilot CLI' -and $cb.Current.BoundingRectangle.Width -gt 0) {{
                    $cb.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern).Toggle()
                    Write-Host "TARGET_FIXED"
                    break
                }}
            }}
            Start-Sleep -Milliseconds 800
            break
        }}
    }}
}}

if($modelOk -and $targetOk) {{ Write-Host "GUARD_OK" }}
[MG]::SetForegroundWindow([IntPtr]{orch_hwnd})
'''
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=20
        )
        if "MODEL_FIXED" in r.stdout:
            log(f"Model guard: fixed model for HWND={hwnd}", "OK")
        if "TARGET_FIXED" in r.stdout:
            log(f"Model guard: fixed target for HWND={hwnd}", "OK")
        if "GUARD_OK" in r.stdout:
            log(f"Model guard: HWND={hwnd} already correct", "OK")
    except Exception as e:
        log(f"Model guard failed for HWND={hwnd}: {e}", "WARN")


def guard_permissions(hwnd, orch_hwnd):
    """Ensure a chat window has Bypass Approvals set.
    
    Calls tools/guard_bypass.ps1 — the single source of truth for permission
    switching. Uses the same Ghost class and PostMessage approach as new_chat.ps1.
    
    SetForegroundWindow is called from Python (which has foreground rights)
    because subprocess PowerShell cannot steal focus on Windows.
    """
    import ctypes
    # Give worker window real OS focus from orchestrator process
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)

    script_path = str(ROOT / "tools" / "guard_bypass.ps1")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", script_path, "-Hwnd", str(hwnd)],
            capture_output=True, text=True, timeout=20
        )
        out = r.stdout.strip()
        if "PERMS_FIXED" in out:
            log(f"Permissions: Bypass Approvals set for HWND={hwnd}", "OK")
        elif "PERMS_OK" in out:
            log(f"Permissions: HWND={hwnd} already Bypass Approvals", "OK")
        elif "PERMS_FAILED" in out:
            perm_state = out.split("PERMS_FAILED:")[-1].strip() if "PERMS_FAILED:" in out else "unknown"
            log(f"Permissions: FAILED to set Bypass for HWND={hwnd} (still '{perm_state}')", "WARN")
        else:
            log(f"Permissions: Unexpected result for HWND={hwnd}: {out[:100]}", "WARN")
    except Exception as e:
        log(f"Permissions guard failed for HWND={hwnd}: {e}", "WARN")
    finally:
        # Restore orchestrator focus from Python (has foreground rights)
        ctypes.windll.user32.SetForegroundWindow(orch_hwnd)
        time.sleep(0.2)


def prompt_worker(hwnd, worker_name, orch_hwnd, boot_memories=None):
    """Send initialization prompt to a worker chat window via clipboard paste."""
    # Build memory context from persistent store if available
    memory_context = _format_memory_context(worker_name, boot_memories) if boot_memories else ""

    prompt = (
        f"You are Worker {worker_name.upper()} in the Skynet system. "
        f"Backend: http://localhost:{SKYNET_PORT} | Worker ID: {worker_name} | "
        f"Workspace: D:\\Prospects\\ScreenMemory. "
        f"Reply with ONLY a single short line: "
        f"'Worker {worker_name.upper()} online.' "
        f"Do NOT run any commands, do NOT call any APIs, do NOT check status. "
        f"Just reply with that one line."
    )
    if memory_context:
        prompt += memory_context

    ps = f'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms

$hwnd = [IntPtr]{hwnd}
$orch = [IntPtr]{orch_hwnd}

# Focus worker
[System.Windows.Automation.AutomationElement]::FromHandle($hwnd) | Out-Null
$wnd = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)

# Bring to front briefly
Add-Type @"
using System; using System.Runtime.InteropServices;
public class FW {{ [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h); }}
"@
[FW]::SetForegroundWindow($hwnd)
Start-Sleep -Milliseconds 600

# Find edit control and focus it
$edit = $wnd.FindFirst(
    [System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Edit
    ))
)
if ($edit) {{
    try {{ $edit.SetFocus() }} catch {{}}
    Start-Sleep -Milliseconds 300
    
    [System.Windows.Forms.Clipboard]::SetText("{prompt.replace(chr(34), '`"')}")
    Start-Sleep -Milliseconds 200
    [System.Windows.Forms.SendKeys]::SendWait("^v")
    Start-Sleep -Milliseconds 400
    [System.Windows.Forms.SendKeys]::SendWait("{{ENTER}}")
    Write-Host "OK"
}} else {{
    Write-Host "NO_EDIT"
}}

Start-Sleep -Milliseconds 500
[FW]::SetForegroundWindow($orch)
'''
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15
        )
        return "OK" in r.stdout
    except Exception as e:
        log(f"Prompt failed for {worker_name}: {e}", "ERR")
        return False


# ─── ScreenMemory Engine Integration ──────────────────

def connect_engines():
    """Initialize ScreenMemory's core engines for orchestration."""
    engines = {}

    # DAAORouter — difficulty-aware task routing
    try:
        from core.difficulty_router import DAAORouter
        engines["router"] = DAAORouter()
        log("DAAORouter connected — difficulty-aware routing active", "OK")
    except Exception as e:
        log(f"DAAORouter unavailable: {e}", "WARN")

    # DAGEngine — workflow decomposition
    try:
        from core.dag_engine import DAGBuilder, DAGExecutor
        engines["dag_builder"] = DAGBuilder()
        engines["dag_executor"] = DAGExecutor(max_feedback_loops=3)
        log("DAGEngine connected — workflow decomposition active", "OK")
    except Exception as e:
        log(f"DAGEngine unavailable: {e}", "WARN")

    # InputGuard — safety filtering
    try:
        from core.input_guard import InputGuard
        engines["guard"] = InputGuard(block_threshold=0.75, warn_threshold=0.40)
        log("InputGuard connected — directive safety filtering active", "OK")
    except Exception as e:
        log(f"InputGuard unavailable: {e}", "WARN")

    # HybridRetriever — memory-augmented context
    try:
        from core.hybrid_retrieval import HybridRetriever
        engines["retriever"] = HybridRetriever()
        log("HybridRetriever connected — memory-augmented context active", "OK")
    except Exception as e:
        log(f"HybridRetriever unavailable: {e}", "WARN")

    # Desktop (winctl) — window management
    try:
        from winctl import Desktop
        engines["desktop"] = Desktop()
        log("Desktop (winctl) connected — API-level window control active", "OK")
    except Exception as e:
        log(f"Desktop unavailable: {e}", "WARN")

    # Orchestrator — full pipeline
    try:
        from core.orchestrator import Orchestrator
        engines["orchestrator"] = Orchestrator()
        log("Orchestrator brain connected — full pipeline active", "OK")
    except Exception as e:
        log(f"Orchestrator unavailable: {e}", "WARN")

    return engines


# ─── Task Dispatch ─────────────────────────────────────

def dispatch_task(directive, worker=None, engines=None):
    """
    Dispatch a task through the ScreenMemory pipeline → Skynet → worker.

    Pipeline:
    1. InputGuard scans directive
    2. DAAORouter determines difficulty
    3. Route to optimal worker (or specified worker)
    4. POST to Skynet /directive
    """
    # Step 1: Safety scan
    if engines and "guard" in engines:
        scan = engines["guard"].scan(directive)
        if scan.blocked:
            log(f"BLOCKED by InputGuard: {scan.triggers}", "ERR")
            return {"status": "blocked", "reason": scan.triggers}

    # Step 2: Route
    if engines and "router" in engines and not worker:
        try:
            plan = engines["router"].route(directive)
            log(f"DAAO: difficulty={plan.difficulty.level.name}, "
                f"operator={plan.operator.value}", "SYS")
        except Exception:
            pass

    # Step 3: Dispatch to Skynet
    body = {"goal": directive, "priority": 5}
    if worker:
        result = http_post(f"/directive?route={worker}", body)
    else:
        result = http_post("/directive", body)

    if result:
        log(f"Dispatched to {'worker ' + worker if worker else 'auto-route'}", "OK")
    return result


# ─── Bootstrap Phases ─────────────────────────────────

# Global: persistent memory store instance (initialized in Phase 0)
_memory_store = None


def _phase_0_memory_preload():
    """Phase 0: Load persistent memories from SQLite before workers boot.

    Returns dict mapping worker names to their top recalled memories.
    These are injected into worker boot prompts in Phase 3.
    """
    global _memory_store
    try:
        from core.persistent_memory import PersistentMemoryStore
        _memory_store = PersistentMemoryStore()
        stats = _memory_store.get_stats()
        log(f"Memory store: {stats['episodes']} episodes, {stats['semantics']} semantics, "
            f"{stats['sessions']} sessions", "OK")

        # Pre-recall top memories for each worker
        worker_memories = {}
        for name in WORKER_NAMES:
            memories = _memory_store.recall(name, top_k=5)
            worker_memories[name] = memories
            if memories:
                log(f"  {name}: {len(memories)} memories recalled (top utility={memories[0].get('effective_utility', 0):.3f})", "OK")
            else:
                log(f"  {name}: no prior memories", "INFO")

        # Also load general cross-session semantics
        general = _memory_store.load_session(top_k=10)
        worker_memories["_general"] = general
        if general:
            log(f"  general: {len(general)} cross-session semantics loaded", "OK")

        return worker_memories
    except Exception as e:
        log(f"Memory preload skipped: {e}", "WARN")
        return {}


def _format_memory_context(worker_name, boot_memories):
    """Format recalled memories into a prompt context string for a worker."""
    if not boot_memories:
        return ""

    worker_mems = boot_memories.get(worker_name, [])
    general_mems = boot_memories.get("_general", [])

    # Combine worker-specific + general, deduplicate, take top 5
    seen = set()
    combined = []
    for m in worker_mems + general_mems:
        content = m.get("content", "")
        if content not in seen and len(combined) < 5:
            seen.add(content)
            combined.append(content)

    if not combined:
        return ""

    lines = [f"  - {c[:150]}" for c in combined]
    return "\n[PERSISTENT MEMORY -- recalled from prior sessions]\n" + "\n".join(lines) + "\n"


def _save_session_memories(session_id=None):
    """Shutdown hook: save current session memories to persistent store."""
    global _memory_store
    if _memory_store is None:
        return

    if session_id is None:
        session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    try:
        # Store boot event as episodic memory
        _memory_store.store_episode(
            session_id,
            f"Skynet session ended at {datetime.now().isoformat()}",
            importance=0.3,
            tags=["session", "shutdown"],
        )

        # Run consolidation to promote repeated patterns
        result = _memory_store.consolidate()
        if result.get("consolidated", 0) > 0:
            log(f"Memory consolidation: {result['consolidated']} patterns promoted", "OK")

        stats = _memory_store.get_stats()
        log(f"Session saved: {stats['episodes']} episodes, {stats['semantics']} semantics", "OK")
        _memory_store.close()
    except Exception as e:
        log(f"Session save failed: {e}", "WARN")


_consolidation_timer = None

def _schedule_memory_consolidation(interval_s=600):
    """Schedule periodic memory consolidation every `interval_s` seconds (default 10 min)."""
    import threading
    global _consolidation_timer, _memory_store
    if _memory_store is None:
        return
    try:
        result = _memory_store.consolidate()
        consolidated = result.get("consolidated", 0)
        if consolidated > 0:
            log(f"Memory consolidation: {consolidated} patterns promoted", "OK")
    except Exception as e:
        log(f"Memory consolidation error: {e}", "WARN")
    _consolidation_timer = threading.Timer(interval_s, _schedule_memory_consolidation, [interval_s])
    _consolidation_timer.daemon = True
    _consolidation_timer.start()


def phase_1_backend():
    """Start Skynet backend if not running."""
    if port_open(SKYNET_PORT):
        status = http_get("/status")
        if status:
            v = status.get("version", "?")
            log(f"Skynet v{v} already running on port {SKYNET_PORT}", "OK")
            return True

    log(f"Starting Skynet backend on port {SKYNET_PORT}...", "SYS")
    if not os.path.exists(SKYNET_EXE):
        log(f"skynet.exe not found at {SKYNET_EXE}", "ERR")
        return False

    subprocess.Popen(
        [SKYNET_EXE],
        cwd=str(ROOT / "Skynet"),
        creationflags=BACKGROUND_SPAWN_FLAGS,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    for i in range(15):
        time.sleep(1)
        if port_open(SKYNET_PORT):
            status = http_get("/status")
            if status:
                log(f"Skynet v{status.get('version', '?')} started", "OK")
                return True
    log("Skynet failed to start", "ERR")
    return False


def phase_2_dashboard():
    """Start GOD Console if not running."""
    if port_open(GOD_PORT):
        log(f"GOD Console already running on port {GOD_PORT}", "OK")
        return True

    log(f"Starting GOD Console on port {GOD_PORT}...", "SYS")
    god_script = str(ROOT / "god_console.py")
    if not os.path.exists(god_script):
        log("god_console.py not found", "ERR")
        return False

    subprocess.Popen(
        [PYTHON, god_script],
        cwd=str(ROOT),
        env=_DAEMON_ENV,
        creationflags=BACKGROUND_SPAWN_FLAGS,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    for i in range(10):
        time.sleep(1)
        if port_open(GOD_PORT):
            log("GOD Console started", "OK")
            return True
    log("GOD Console failed to start", "WARN")
    return False


def phase_3_workers(num_workers=4, orch_hwnd=None, fresh=False, boot_memories=None):
    """Open worker chat windows, one at a time, with ghost mouse automation.
    
    Flow:
    1. Try restoring sessions from SESSIONS panel (skip if --fresh)
    2. Fall back to open_chat_window() which uses multi-strategy UIA automation
    3. For each worker: open window → guard model → prompt → wait for response → open next
    """
    if not orch_hwnd:
        orch_hwnd = get_orchestrator_hwnd()
    if not orch_hwnd:
        log("No orchestrator HWND found in data/orchestrator.json", "ERR")
        return []

    # Clear failure tracker to ensure clean start
    fail_file = DATA_DIR / "chat_open_failures.json"
    if fail_file.exists():
        fail_file.unlink()
        log("Cleared chat_open_failures.json", "SYS")

    existing_chats = get_chat_windows(orch_hwnd)
    workers_created = []

    # Check if workers already exist (reconnect mode) — skip if fresh
    if not fresh and WORKERS_FILE.exists():
        saved = json.loads(WORKERS_FILE.read_text())
        saved_workers = saved.get("workers", [])
        still_alive = []
        for idx, sw in enumerate(saved_workers):
            hwnd = sw.get("hwnd")
            if hwnd and user32.IsWindowVisible(hwnd):
                still_alive.append(sw)
                guard_model(hwnd, orch_hwnd)
                log(f"Worker {sw['name']}: HWND={hwnd} still alive", "OK")
                # Stagger guard_model calls — VS Code freezes when UIA operations
                # hit multiple windows in rapid succession
                if idx < len(saved_workers) - 1:
                    time.sleep(3)
        if len(still_alive) >= num_workers:
            log(f"All {len(still_alive)} workers still connected", "OK")
            return still_alive

    start_idx = len(existing_chats)
    num_to_open = min(num_workers, 4) - start_idx
    if num_to_open <= 0 and existing_chats:
        log(f"{len(existing_chats)} chat windows already open", "OK")
        return _map_chats_to_workers(existing_chats, orch_hwnd)

    log(f"Opening {num_to_open} worker chat window(s)...", "SYS")

    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 2

    for i in range(num_to_open):
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            log(f"Stopping: {consecutive_failures} consecutive failures opening chat windows", "ERR")
            break

        worker_idx = start_idx + i
        if worker_idx >= 4:
            break
        worker_name = WORKER_NAMES[worker_idx]
        slot = GRID_SLOTS[worker_idx]

        log(f"Opening worker {worker_name.upper()}...", "SYS")

        new_hwnd = None
        opened_fresh = False

        # Try restoring from SESSIONS panel first (skip if --fresh)
        if not fresh:
            new_hwnd = restore_session_from_panel(f"Worker {worker_name.upper()}", orch_hwnd, slot)
        
        if not new_hwnd:
            if not fresh:
                log(f"Session restore failed for {worker_name}, opening fresh window...", "WARN")

            new_hwnd = open_chat_window(orch_hwnd)
            opened_fresh = True

            if not new_hwnd:
                log(f"Could not open window for {worker_name}", "ERR")
                consecutive_failures += 1
                continue

            move_window(new_hwnd, slot["x"], slot["y"], slot["w"], slot["h"])
            time.sleep(0.5)

        # Success — reset failure counter
        consecutive_failures = 0

        # Guard model + permissions ONLY for restored sessions.
        # Fresh windows from new_chat.ps1 already have model guard + permission
        # guard built in — calling them again can revert the state.
        if not opened_fresh:
            guard_model(new_hwnd, orch_hwnd)
            time.sleep(0.5)
            guard_permissions(new_hwnd, orch_hwnd)
            time.sleep(1)

        # Prompt the worker
        log(f"Prompting {worker_name.upper()}...", "SYS")
        ok = prompt_worker(new_hwnd, worker_name, orch_hwnd, boot_memories=boot_memories)
        if ok:
            log(f"Worker {worker_name.upper()} prompted", "OK")
        else:
            log(f"Prompt may have failed for {worker_name}", "WARN")

        # Brief wait for response (don't block long — open_chat_window has no empty-chat blocker)
        for wait in range(6):
            time.sleep(1)
            if chat_has_messages(new_hwnd):
                log(f"Worker {worker_name.upper()} responded", "OK")
                break

        worker_info = {
            "name": worker_name,
            "hwnd": new_hwnd,
            "grid": slot["grid"],
            "x": slot["x"], "y": slot["y"],
            "w": slot["w"], "h": slot["h"],
        }
        workers_created.append(worker_info)
        existing_chats.append({"hwnd": new_hwnd, "w": slot["w"]})

        # Register with Skynet
        http_post(f"/directive?route={worker_name}", {
            "goal": f"Worker {worker_name} initialized -- CLI chat HWND={new_hwnd} connected",
            "priority": 1,
        })

    # Restore orchestrator focus
    focus_window(orch_hwnd)
    return workers_created


def _map_chats_to_workers(chats, orch_hwnd):
    """Map existing chat windows to worker names by grid position."""
    mapped = []
    for i, cw in enumerate(chats[:4]):
        name = WORKER_NAMES[i] if i < len(WORKER_NAMES) else f"worker_{i}"
        slot = GRID_SLOTS[i] if i < len(GRID_SLOTS) else GRID_SLOTS[0]
        mapped.append({
            "name": name,
            "hwnd": cw["hwnd"],
            "grid": slot["grid"],
            "x": cw.get("x", slot["x"]),
            "y": cw.get("y", slot["y"]),
            "w": cw.get("w", slot["w"]),
            "h": cw.get("h", slot["h"]),
        })
    return mapped


def phase_4_register(workers):
    """Register all workers with Skynet backend."""
    for w in workers:
        http_post(f"/directive?route={w['name']}", {
            "goal": f"Worker {w['name']} active — HWND={w['hwnd']}",
            "priority": 1,
        })
        log(f"Registered {w['name'].upper()} with Skynet", "OK")


def phase_4b_identity(workers):
    """Inject identity prompts into each worker so they know who they are."""
    orch_hwnd = get_orchestrator_hwnd()
    profiles_file = DATA_DIR / "agent_profiles.json"
    profiles = {}
    try:
        if profiles_file.exists():
            profiles = json.loads(profiles_file.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"Could not load agent_profiles.json: {e}", "WARN")

    dispatched = 0
    for w in workers:
        name = w.get("name", "unknown")
        hwnd = w.get("hwnd", 0)
        profile = profiles.get(name, {})
        role = profile.get("role", "worker")
        specs = profile.get("specializations", [])
        specs_str = ", ".join(specs) if specs else "general"

        identity_prompt = (
            f'You are {name.upper()} -- {role} in the Skynet multi-agent network. '
            f'Your HWND is {hwnd}. You are running Claude Opus 4.6 fast in Copilot CLI mode. '
            f'Connected to Skynet backend on port {SKYNET_PORT}. '
            f'Your orchestrator is the main VS Code window (HWND {orch_hwnd}). '
            f'Report results by posting to http://localhost:{SKYNET_PORT}/bus/publish with sender={name}. '
            f'Your specializations: {specs_str}. '
            f"Acknowledge your identity by posting: "
            f"Invoke-RestMethod -Uri http://localhost:{SKYNET_PORT}/bus/publish "
            f"-Method POST -ContentType 'application/json' "
            f"-Body (ConvertTo-Json @{{sender='{name}';topic='orchestrator';"
            f"type='identity_ack';content='{name.upper()} identity confirmed -- ready for tasks'}})"
        )

        try:
            result = subprocess.run(
                [PYTHON, str(ROOT / "tools" / "skynet_dispatch.py"),
                 "--worker", name, "--task", identity_prompt],
                cwd=str(ROOT), timeout=30, capture_output=True, text=True,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
            )
            if result.returncode == 0:
                log(f"Identity dispatched to {name.upper()}", "OK")
                dispatched += 1
            else:
                log(f"Identity dispatch to {name.upper()} failed: {result.stderr[:100]}", "WARN")
        except Exception as e:
            log(f"Identity dispatch to {name.upper()} error: {e}", "WARN")

        time.sleep(3)

    # Post orchestrator's own identity
    http_post("/bus/publish", {
        "sender": "orchestrator",
        "topic": "system",
        "type": "identity_ack",
        "content": (
            f"ORCHESTRATOR identity confirmed -- Command & Synthesis node. "
            f"HWND={orch_hwnd}. Claude Opus 4.6 fast, Copilot CLI. "
            f"Specializations: decomposition, synthesis, routing, delegation, monitoring. "
            f"Ready to command."
        ),
    })
    log(f"Identity injection complete: {dispatched}/{len(workers)} workers + orchestrator", "OK")


def phase_5_save(workers, engines):
    """Save worker state and engine status."""
    DATA_DIR.mkdir(exist_ok=True)

    state = {
        "workers": workers,
        "layout": "2x2",
        "monitor": 2,
        "skynet_port": SKYNET_PORT,
        "god_console_port": GOD_PORT,
        "engines": list(engines.keys()),
        "created": datetime.now().isoformat(),
    }
    WORKERS_FILE.write_text(json.dumps(state, indent=2, default=str))
    log(f"State saved to {WORKERS_FILE}", "OK")


# ─── Status Report ────────────────────────────────────

def show_status():
    """Show complete system status."""
    print("\n" + "=" * 60)
    print("  SKYNET SYSTEM STATUS")
    print("=" * 60)

    # Backend
    if port_open(SKYNET_PORT):
        status = http_get("/status")
        if status:
            print(f"\n🟢 Skynet v{status.get('version', '?')} — port {SKYNET_PORT}")
            agents = status.get("agents", {})
            for name, info in agents.items():
                s = info.get("status", "?")
                tc = info.get("tasks_completed", 0)
                qd = info.get("queue_depth", 0)
                emoji = "🟢" if s == "IDLE" else "🔵" if s == "BUSY" else "🔴"
                print(f"  {emoji} {name.upper()}: {s} | tasks={tc} | queue={qd}")
    else:
        print(f"\n🔴 Skynet — port {SKYNET_PORT} NOT running")

    # Dashboard
    if port_open(GOD_PORT):
        print(f"\n🟢 GOD Console — port {GOD_PORT}")
    else:
        print(f"\n🔴 GOD Console — port {GOD_PORT} NOT running")

    # Workers
    if WORKERS_FILE.exists():
        data = json.loads(WORKERS_FILE.read_text())
        workers = data.get("workers", [])
        print(f"\n📋 Workers ({len(workers)}):")
        for w in workers:
            hwnd = w.get("hwnd", 0)
            alive = user32.IsWindowVisible(hwnd) if hwnd else False
            emoji = "🟢" if alive else "🔴"
            print(f"  {emoji} {w['name'].upper()}: HWND={hwnd} | {w.get('grid', '?')}")
        engines = data.get("engines", [])
        if engines:
            print(f"\n⚡ Engines: {', '.join(engines)}")
    else:
        print("\n📋 No workers.json found")

    # Orchestrator
    orch = get_orchestrator_hwnd()
    if orch:
        alive = user32.IsWindowVisible(orch)
        emoji = "🟢" if alive else "🔴"
        print(f"\n{emoji} Orchestrator: HWND={orch}")

    print("\n" + "=" * 60)


# ─── Reconnect Mode ──────────────────────────────────

def reconnect():
    """Reconnect to existing workers without opening new windows."""
    log("Reconnecting to existing workers...", "SYS")

    if not phase_1_backend():
        log("Reconnect aborted — Skynet backend required", "ERR")
        return False
    phase_2_dashboard()

    orch_hwnd = get_orchestrator_hwnd()
    if not orch_hwnd:
        log("No orchestrator HWND", "ERR")
        return False

    if not WORKERS_FILE.exists():
        log("No workers.json — nothing to reconnect", "ERR")
        return False

    data = json.loads(WORKERS_FILE.read_text())
    workers = data.get("workers", [])
    alive = []
    dead = []

    for w in workers:
        hwnd = w.get("hwnd", 0)
        if hwnd and user32.IsWindowVisible(hwnd):
            alive.append(w)
            log(f"Worker {w['name'].upper()}: HWND={hwnd} ✓", "OK")
        else:
            dead.append(w)
            log(f"Worker {w['name'].upper()}: HWND={hwnd} ✗ (dead)", "WARN")

    if dead:
        log(f"{len(dead)} worker(s) dead — need to reopen", "WARN")

    # Connect engines
    engines = connect_engines()

    # Re-register alive workers
    phase_4_register(alive)

    # Identity injection for reconnected workers
    if alive:
        log("Reconnect: Identity Injection", "SYS")
        phase_4b_identity(alive)

    _start_post_boot_daemons()

    # Update state
    data["engines"] = list(engines.keys())
    data["reconnected"] = datetime.now().isoformat()
    WORKERS_FILE.write_text(json.dumps(data, indent=2, default=str))

    return True


# ─── Window Hygiene ───────────────────────────────────

WM_CLOSE = 0x0010

def close_non_essential_windows():
    """Close all windows that are not Skynet-essential.
    
    Essential windows: orchestrator VS Code, worker VS Code windows,
    GOD Console browser tab, and any window whose title contains
    'ScreenMemory' or 'Skynet'.
    
    Uses Win32 API only — no pyautogui, no SendKeys, no mouse simulation.
    Can be called standalone or as part of bootstrap.
    """
    orch_hwnd = get_orchestrator_hwnd()
    
    # Load worker HWNDs
    worker_hwnds = set()
    if WORKERS_FILE.exists():
        try:
            wdata = json.loads(WORKERS_FILE.read_text())
            for w in wdata.get("workers", []):
                h = w.get("hwnd")
                if h:
                    worker_hwnds.add(int(h))
        except Exception:
            pass
    
    essential_hwnds = set()
    if orch_hwnd:
        essential_hwnds.add(int(orch_hwnd))
    essential_hwnds.update(worker_hwnds)
    
    # Essential title substrings (case-insensitive)
    essential_titles = [
        "visual studio code",
        "screenmemory",
        "skynet",
        "god console",
        "windows terminal",
        "powershell",
        "cmd.exe",
        "task manager",
        "explorer",  # Windows Explorer shell
    ]
    
    # System process classes to never touch
    system_classes = [
        "Shell_TrayWnd",          # Taskbar
        "Shell_SecondaryTrayWnd",  # Secondary taskbar
        "Progman",                 # Desktop
        "WorkerW",                 # Desktop wallpaper
        "NotifyIconOverflowWindow",
        "Windows.UI.Core.CoreWindow",
        "ApplicationFrameWindow",  # UWP host (Settings, etc.)
    ]
    
    closed = []
    skipped = []
    
    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def enum_cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        
        # Skip essential HWNDs
        if int(hwnd) in essential_hwnds:
            return True
        
        # Get window title
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value.strip()
        if not title:
            return True
        
        # Get window class
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        cls_name = cls_buf.value
        
        # Skip system classes
        if cls_name in system_classes:
            return True
        
        # Skip essential titles
        title_lower = title.lower()
        for et in essential_titles:
            if et in title_lower:
                skipped.append(f"  [KEEP] {title[:60]}")
                return True
        
        # Skip windows with no meaningful size (background/hidden)
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w < 50 or h < 50:
            return True
        
        # This window is non-essential — close it via WM_CLOSE
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        closed.append(f"  [CLOSED] {title[:60]} (class={cls_name})")
        return True
    
    user32.EnumWindows(enum_cb, 0)
    
    if closed:
        log(f"Window hygiene: closed {len(closed)} non-essential window(s)", "OK")
        for c in closed:
            log(c, "INFO")
    else:
        log("Window hygiene: all windows are Skynet-essential", "OK")



# ─── Daemon Lifecycle ──────────────────────────────────

def _is_daemon_running(pid_file):
    """Check if a daemon is already running by its PID file.
    Returns (running: bool, pid: int|None)."""
    if not pid_file.exists():
        return False, None
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        return False, None
    if pid == os.getpid():
        return True, pid
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True, pid
        return False, None
    try:
        os.kill(pid, 0)  # check alive
        return True, pid
    except OSError:
        return False, None


def _process_commandline(pid):
    """Best-effort process command line lookup for PID ownership checks."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if pid <= 0:
        return ""
    if os.name == "nt":
        try:
            r = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f'$p = Get-CimInstance Win32_Process -Filter "ProcessId = {pid}" -ErrorAction SilentlyContinue; if ($p) {{ $p.CommandLine }}',
                ],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return (r.stdout or "").strip()
        except Exception:
            return ""
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _pid_matches_script(pid, script_path):
    """Confirm the PID still belongs to the expected daemon script."""
    cmdline = _process_commandline(pid)
    if not cmdline:
        return False
    cmd_norm = cmdline.lower().replace("\\", "/")
    script_name = Path(script_path).name.lower()
    script_norm = str(Path(script_path).resolve()).lower().replace("\\", "/")
    return script_norm in cmd_norm or script_name in cmd_norm


def _start_daemon_safe(script_path, pid_file, label, extra_args=None):
    """Start a daemon only if not already running (PID file guard).
    Returns the Popen object or None if already running / failed."""
    running, pid = _is_daemon_running(pid_file)
    if running:
        if _pid_matches_script(pid, script_path):
            log(f"{label} already running (PID {pid}) -- skipping", "OK")
            return None
        log(f"{label} PID file points to a different process (PID {pid}) -- repairing", "WARN")
        try:
            pid_file.unlink()
        except Exception:
            pass

    if not os.path.exists(script_path):
        log(f"{label} script not found: {script_path}", "WARN")
        return None

    cmd = [PYTHON, script_path]
    if extra_args:
        cmd.extend(extra_args)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=_DAEMON_ENV,
            creationflags=BACKGROUND_SPAWN_FLAGS,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log(f"{label} started (PID {proc.pid})", "OK")
        return proc
    except Exception as e:
        log(f"{label} failed to start: {e}", "ERR")
        return None


def _json_get(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _consultant_endpoint_status(api_port):
    data = _json_get(f"http://localhost:{api_port}/consultants", timeout=2)
    if not isinstance(data, dict):
        return {"reachable": False, "live": False, "promptable": False}
    consultant = data.get("consultant")
    if not isinstance(consultant, dict):
        return {"reachable": False, "live": False, "promptable": False}
    return {
        "reachable": True,
        "live": bool(consultant.get("live")) or str(consultant.get("status", "")).upper() == "LIVE",
        "promptable": bool(consultant.get("accepts_prompts")),
        "routable": bool(consultant.get("routable")),
        "prompt_transport": consultant.get("prompt_transport"),
    }


def _consultant_endpoint_live(api_port):
    return _consultant_endpoint_status(api_port).get("live", False)


def _ensure_consultant_bridge():
    primary_script = str(ROOT / "tools" / "skynet_consultant_bridge.py")
    primary_pid = DATA_DIR / "consultant_bridge.pid"
    fallback_pid = DATA_DIR / "consultant_bridge_8424.pid"

    _start_daemon_safe(
        primary_script,
        primary_pid,
        "Codex Consultant bridge",
    )

    status = _consultant_endpoint_status(8422)
    if status.get("live"):
        if not status.get("promptable"):
            log("Consultant bridge on port 8422 is live but not promptable", "WARN")
        return

    log("Consultant bridge on port 8422 is not reporting live — starting fallback bridge on 8424", "WARN")
    _start_daemon_safe(
        primary_script,
        fallback_pid,
        "Codex Consultant bridge fallback",
        extra_args=["--api-port", "8424", "--pid-file", str(fallback_pid)],
    )


def _ensure_gemini_consultant_bridge():
    primary_script = str(ROOT / "tools" / "skynet_consultant_bridge.py")
    primary_pid = DATA_DIR / "gemini_consultant_bridge.pid"

    _start_daemon_safe(
        primary_script,
        primary_pid,
        "Gemini Consultant bridge",
        extra_args=[
            "--id", "gemini_consultant",
            "--display-name", "Gemini Consultant",
            "--model", "Gemini 3 Pro",
            "--source", "GC-Start",
            "--api-port", "8425",
        ],
    )

    status = _consultant_endpoint_status(8425)
    if status.get("live"):
        if not status.get("promptable"):
            log("Gemini Consultant bridge on port 8425 is live but not promptable", "WARN")
        return

    log("Gemini Consultant bridge on port 8425 is not reporting live", "WARN")


def _start_post_boot_daemons():
    """Ensure the shared post-boot daemon set is running."""
    _start_daemon_safe(
        str(ROOT / "tools" / "skynet_self_prompt.py"),
        DATA_DIR / "self_prompt.pid",
        "Self-prompt daemon",
        extra_args=["start"],
    )
    _start_daemon_safe(
        str(ROOT / "tools" / "skynet_self_improve.py"),
        DATA_DIR / "self_improve.pid",
        "Self-improvement engine",
        extra_args=["start"],
    )
    _start_daemon_safe(
        str(ROOT / "tools" / "skynet_bus_relay.py"),
        DATA_DIR / "bus_relay.pid",
        "Bus relay daemon",
    )
    _start_daemon_safe(
        str(ROOT / "tools" / "skynet_learner.py"),
        DATA_DIR / "learner.pid",
        "Learner daemon",
        extra_args=["--daemon"],
    )
    _ensure_consultant_bridge()
    _ensure_gemini_consultant_bridge()


# ─── Main ─────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skynet Start — Unified Orchestrator Bootstrap")
    parser.add_argument("--workers", type=int, default=4, help="Number of workers (1-4)")
    parser.add_argument("--reconnect", action="store_true", help="Reconnect to existing workers")
    parser.add_argument("--fresh", action="store_true", help="Skip session restore, open fresh windows via new_chat.ps1")
    parser.add_argument("--status", action="store_true", help="Show system status")
    parser.add_argument("--dispatch", type=str, help="Dispatch a task to Skynet")
    parser.add_argument("--worker", type=str, help="Target worker for dispatch")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    print()
    print("╔══════════════════════════════════════════╗")
    print("║       ⚡ SKYNET ORCHESTRATOR v3.0 ⚡      ║")
    print("║   ScreenMemory-Powered Multi-Agent AI    ║")
    print("╚══════════════════════════════════════════╝")
    print()

    if args.dispatch:
        engines = connect_engines()
        result = dispatch_task(args.dispatch, worker=args.worker, engines=engines)
        print(json.dumps(result, indent=2, default=str) if result else "Dispatch failed")
        return

    if args.reconnect:
        reconnect()
        show_status()
        return

    t0 = time.time()

    # Phase 0: Persistent Memory Preload
    log("Phase 0: Persistent Memory Preload", "SYS")
    boot_memories = _phase_0_memory_preload()
    atexit.register(_save_session_memories)
    _schedule_memory_consolidation(interval_s=600)

    # Phase 1: Backend
    log("Phase 1: Skynet Backend", "SYS")
    if not phase_1_backend():
        log("ABORT — Skynet backend required", "ERR")
        return

    # Phase 2: Dashboard
    log("Phase 2: GOD Console", "SYS")
    phase_2_dashboard()

    try:
        # Phase 3: Workers (set boot lock — prevents self-prompt daemon from firing during UIA-heavy phases)
        _set_boot_phase("phase_3_workers")
        log("Phase 3: Worker Chat Windows", "SYS")
        workers = phase_3_workers(num_workers=args.workers, fresh=args.fresh, boot_memories=boot_memories)

        # Phase 4: Register
        _set_boot_phase("phase_4_register")
        log("Phase 4: Skynet Registration", "SYS")
        if workers:
            phase_4_register(workers)

        # Phase 4b: Identity Injection
        _set_boot_phase("phase_4b_identity")
        log("Phase 4b: Identity Injection", "SYS")
        if workers:
            phase_4b_identity(workers)

        # Phase 5: Engines
        _set_boot_phase("phase_5_engines")
        log("Phase 5: ScreenMemory Engines", "SYS")
        engines = connect_engines()

        # Phase 6: Save state
        _set_boot_phase("phase_6_save")
        log("Phase 6: Save State", "SYS")
        phase_5_save(workers, engines)
    finally:
        # Boot lock released — UIA-heavy phases complete, daemons may resume
        # In a finally block so the lock is always cleared, even on crash
        _clear_boot_phase()
        log("Boot phase lock released", "OK")

    # Phase 7: Window hygiene
    log("Phase 7: Window Hygiene", "SYS")
    close_non_essential_windows()

    elapsed = time.time() - t0
    log(f"SKYNET ONLINE — {len(workers)} workers, {len(engines)} engines — {elapsed:.1f}s", "OK")

    # Phase 8: Background daemons
    log("Phase 8: Background Daemons", "SYS")
    _start_post_boot_daemons()

    print()
    show_status()


if __name__ == "__main__":
    main()
