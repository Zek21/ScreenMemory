#!/usr/bin/env python
"""
set_autopilot.py — Keyboard-based Autopilot permission switcher for VS Code Copilot Chat.

Uses SendInput physical keyboard events (not PostMessage, not UIA) to switch
from "Default Approvals" to "Autopilot (Preview)" on worker windows.

Method:
  1. ForceForeground the target worker window
  2. Physical mouse click on the permissions button area to open dropdown
  3. Physical keyboard END key to jump to last item (Autopilot)
  4. Physical keyboard ENTER to confirm selection
  5. Screenshot + OCR verification
  6. Restore orchestrator focus

Why keyboard + Enter:
  - PostMessage keys are ignored by Electron dropdown menus
  - UIA ExpandCollapsePattern fails when multiple VS Code windows are open
  - Physical mouse click on dropdown items is coordinate-fragile
  - Physical keyboard END+ENTER after opening dropdown is reliable:
    END always jumps to the last item regardless of dropdown height
    ENTER always selects the focused item

Usage:
  python tools/set_autopilot.py                    # All workers from workers.json
  python tools/set_autopilot.py --worker alpha      # Specific worker
  python tools/set_autopilot.py --hwnd 721342       # By HWND
  python tools/set_autopilot.py --verify-only       # Just check, don't switch
  python tools/set_autopilot.py --wait-processing   # Wait for each worker to finish processing
"""

import argparse
import ctypes
import ctypes.wintypes
import json
import os
import sys
import time

# ── Win32 constants ──
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001

VK_END = 0x23
VK_RETURN = 0x0D
VK_MENU = 0x12  # ALT key

SW_MINIMIZE = 6
SW_RESTORE = 9

WS_MINIMIZE = 0x20000000


# ── ctypes structures ──
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("union", INPUT_UNION)]


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def send_mouse_click(x: int, y: int):
    """Physical mouse click at absolute screen coordinates using SendInput."""
    ctypes.windll.user32.SetCursorPos(x, y)
    time.sleep(0.05)

    inputs = (INPUT * 2)()
    # Mouse down
    inputs[0].type = INPUT_MOUSE
    inputs[0].union.mi.dwFlags = MOUSEEVENTF_LEFTDOWN
    # Mouse up
    inputs[1].type = INPUT_MOUSE
    inputs[1].union.mi.dwFlags = MOUSEEVENTF_LEFTUP

    user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))


def send_key(vk: int, extended: bool = False):
    """Physical key press+release using SendInput."""
    inputs = (INPUT * 2)()

    flags_down = KEYEVENTF_EXTENDEDKEY if extended else 0
    flags_up = KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if extended else 0)

    # Key down
    inputs[0].type = INPUT_KEYBOARD
    inputs[0].union.ki.wVk = vk
    inputs[0].union.ki.dwFlags = flags_down
    # Key up
    inputs[1].type = INPUT_KEYBOARD
    inputs[1].union.ki.wVk = vk
    inputs[1].union.ki.dwFlags = flags_up

    user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))


def force_foreground(hwnd: int) -> bool:
    """Force a window to foreground using ALT key trick + minimize/restore."""
    hwnd_ptr = ctypes.wintypes.HWND(hwnd)

    if not user32.IsWindow(hwnd_ptr):
        return False

    # AllowSetForegroundWindow(-1) lets any process set foreground
    user32.AllowSetForegroundWindow(ctypes.wintypes.DWORD(-1))

    # ALT key trick — pressing and releasing ALT allows SetForegroundWindow to work
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)

    # If minimized, restore first
    style = user32.GetWindowLongW(hwnd_ptr, -16)  # GWL_STYLE
    if style & WS_MINIMIZE:
        user32.ShowWindow(hwnd_ptr, SW_RESTORE)
        time.sleep(0.3)

    result = user32.SetForegroundWindow(hwnd_ptr)
    time.sleep(0.2)

    # Verify
    fg = user32.GetForegroundWindow()
    return fg == hwnd


def get_window_rect(hwnd: int) -> tuple:
    """Get window (x, y, width, height)."""
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(ctypes.wintypes.HWND(hwnd), ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def ocr_region(x: int, y: int, w: int, h: int) -> list:
    """Capture a screen region and OCR it. Returns list of (text, y_center) tuples."""
    try:
        import mss
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return []

    sct = mss.mss()
    monitor = {"left": x, "top": y, "width": w, "height": h}
    img = sct.grab(monitor)
    path = os.path.join(os.path.dirname(__file__), "..", "screenshots", "_autopilot_verify.png")
    path = os.path.abspath(path)
    mss.tools.to_png(img.rgb, img.size, output=path)

    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore")
        ocr = RapidOCR()
        result, _ = ocr(path)

    if not result:
        return []

    texts = []
    for box, text, conf in result:
        yc = (box[0][1] + box[2][1]) / 2
        texts.append((text, int(yc)))
    return texts


def check_autopilot(hwnd: int) -> str:
    """Check current permission mode via OCR. Returns 'AUTOPILOT', 'DEFAULT', or 'UNKNOWN'."""
    x, y, w, h = get_window_rect(hwnd)
    if w == 0 or h == 0:
        return "UNKNOWN"

    # Scan bottom 120px where the permission button lives
    texts = ocr_region(x, y + h - 120, w, 120)

    for text, _ in texts:
        if "Autopilot" in text:
            return "AUTOPILOT"
        if "Default" in text and "Approvals" in text:
            return "DEFAULT"
        if "Bypass" in text:
            return "BYPASS"

    return "UNKNOWN"


def switch_to_autopilot(hwnd: int, name: str, orch_hwnd: int = 0) -> str:
    """
    Switch a worker window from Default Approvals to Autopilot using keyboard.
    
    Method:
      1. ForceForeground the worker
      2. Click permissions button area to open dropdown
      3. Keyboard END to jump to last item (Autopilot)
      4. Keyboard ENTER to select
      5. Restore orchestrator focus
    
    Returns: 'OK', 'ALREADY_AUTOPILOT', 'FOCUS_FAILED', 'VERIFY_FAILED'
    """
    print(f"  [{name}] Checking current state...")
    current = check_autopilot(hwnd)
    if current == "AUTOPILOT":
        print(f"  [{name}] Already Autopilot - skipping")
        return "ALREADY_AUTOPILOT"

    print(f"  [{name}] Current: {current} - switching to Autopilot...")

    # Step 1: Focus the worker window
    focused = force_foreground(hwnd)
    if not focused:
        print(f"  [{name}] WARNING: ForceForeground returned False, attempting anyway")
    time.sleep(0.3)

    # Step 2: Click the permissions button area
    # The permissions button is at the bottom of the chat window
    # In a 930x500 window, the button is approximately at local coords (220, 484)
    wx, wy, ww, wh = get_window_rect(hwnd)
    btn_x = wx + 220
    btn_y = wy + wh - 16  # 16px from bottom edge
    print(f"  [{name}] Clicking permissions button at ({btn_x}, {btn_y})...")
    send_mouse_click(btn_x, btn_y)
    time.sleep(0.6)  # Wait for dropdown to open

    # Step 3: Keyboard END to jump to last item (Autopilot is last in the list)
    print(f"  [{name}] Pressing END key...")
    send_key(VK_END, extended=True)
    time.sleep(0.3)

    # Step 4: Keyboard ENTER to select
    print(f"  [{name}] Pressing ENTER...")
    send_key(VK_RETURN)
    time.sleep(0.5)

    # Step 5: Verify
    print(f"  [{name}] Verifying...")
    result = check_autopilot(hwnd)

    # Step 6: Restore orchestrator focus
    if orch_hwnd:
        force_foreground(orch_hwnd)

    if result == "AUTOPILOT":
        print(f"  [{name}] SUCCESS - Autopilot confirmed")
        return "OK"
    else:
        print(f"  [{name}] VERIFY_FAILED - got '{result}' after switch attempt")
        return "VERIFY_FAILED"


def wait_for_processing(hwnd: int, name: str, timeout: int = 60) -> str:
    """Wait for a worker to finish processing (no more 'Apply' button visible)."""
    start = time.time()
    while time.time() - start < timeout:
        x, y, w, h = get_window_rect(hwnd)
        texts = ocr_region(x, y, w, h)
        has_apply = any("Apply" in t for t, _ in texts)
        if not has_apply:
            return "IDLE"
        print(f"  [{name}] Still processing (Apply pending)... waiting")
        time.sleep(5)
    return "TIMEOUT"


def load_workers() -> list:
    """Load workers from data/workers.json."""
    path = os.path.join(os.path.dirname(__file__), "..", "data", "workers.json")
    path = os.path.abspath(path)
    with open(path) as f:
        data = json.load(f)
    return data.get("workers", [])


def load_orchestrator_hwnd() -> int:
    """Load orchestrator HWND from data/orchestrator.json."""
    path = os.path.join(os.path.dirname(__file__), "..", "data", "orchestrator.json")
    path = os.path.abspath(path)
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("hwnd", 0)
    except Exception:
        return 0


def main():
    parser = argparse.ArgumentParser(description="Keyboard-based Autopilot switcher")
    parser.add_argument("--worker", help="Specific worker name (alpha/beta/gamma/delta)")
    parser.add_argument("--hwnd", type=int, help="Specific window HWND")
    parser.add_argument("--verify-only", action="store_true", help="Only verify, don't switch")
    parser.add_argument("--wait-processing", action="store_true", help="Wait for each worker to finish")
    args = parser.parse_args()

    orch_hwnd = load_orchestrator_hwnd()
    workers = load_workers()

    if args.hwnd:
        workers = [{"name": "target", "hwnd": args.hwnd}]
    elif args.worker:
        workers = [w for w in workers if w["name"] == args.worker]
        if not workers:
            print(f"Worker '{args.worker}' not found in workers.json")
            sys.exit(1)

    print(f"Processing {len(workers)} worker(s)...")
    print(f"Orchestrator HWND: {orch_hwnd}")
    print()

    results = {}
    for w in workers:
        name = w["name"]
        hwnd = w["hwnd"]
        print(f"[{name.upper()}] HWND={hwnd}")

        if not user32.IsWindow(ctypes.wintypes.HWND(hwnd)):
            print(f"  [{name}] HWND invalid - window not found")
            results[name] = "INVALID_HWND"
            continue

        if args.verify_only:
            status = check_autopilot(hwnd)
            print(f"  [{name}] Permission mode: {status}")
            results[name] = status
        else:
            # Switch to Autopilot
            result = switch_to_autopilot(hwnd, name, orch_hwnd)
            results[name] = result

            # Optionally wait for processing
            if args.wait_processing and result in ("OK", "ALREADY_AUTOPILOT"):
                print(f"  [{name}] Waiting for processing to complete...")
                proc_status = wait_for_processing(hwnd, name)
                print(f"  [{name}] Processing status: {proc_status}")

        print()

    # Summary
    print("=" * 50)
    print("SUMMARY:")
    for name, result in results.items():
        print(f"  {name}: {result}")

    # Restore orchestrator focus
    if orch_hwnd:
        force_foreground(orch_hwnd)

    return results


if __name__ == "__main__":
    main()
