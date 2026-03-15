#!/usr/bin/env python
"""
set_copilot_cli.py — Switch VS Code chat windows from Local to Copilot CLI mode.

This is the MANDATORY session target switch. All Skynet workers MUST run in
"Copilot CLI" mode, not "Local" or "Cloud" mode.

Method:
  1. Move worker window to primary monitor (pyautogui only captures primary)
  2. Focus the worker window
  3. Click the session target button ("Local" / "Cloud" / "Copilot CLI") at bottom-left
  4. Wait for dropdown to appear
  5. Click "Copilot CLI" option in the dropdown
  6. Verify the switch via screenshot + text check
  7. Move worker back to its grid position
  8. Restore orchestrator focus

Why pyautogui:
  - The session target dropdown is a Chromium quickpick overlay (INCIDENT 013)
  - Win32/UIA/clipboard input cannot reach Chromium overlays
  - Only hardware-level input (pyautogui) works reliably

Usage:
  python tools/set_copilot_cli.py                     # All workers from workers.json
  python tools/set_copilot_cli.py --worker alpha       # Specific worker
  python tools/set_copilot_cli.py --hwnd 396010        # By HWND
  python tools/set_copilot_cli.py --verify-only        # Just check, don't switch

Called by: boot protocol (skynet_start.py, Orch-Start.ps1, manual boot)
# signed: orchestrator
"""

import argparse
import ctypes
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

u32 = ctypes.windll.user32
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002

# Grid positions for workers on right monitor (1920+ offset)
GRID_POSITIONS = {
    "alpha": (1930, 20, 930, 500),
    "beta": (2860, 20, 930, 500),
    "gamma": (1930, 540, 930, 500),
    "delta": (2860, 540, 930, 500),
}

# Window position when moved to primary monitor for pyautogui
PRIMARY_POS = (200, 50, 930, 700)


def load_workers():
    path = os.path.join(ROOT, "data", "workers.json")
    with open(path, "r") as f:
        raw = json.load(f)
    if isinstance(raw, dict):
        return raw.get("workers", [])
    return raw


def load_orch_hwnd():
    path = os.path.join(ROOT, "data", "orchestrator.json")
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return int(data.get("hwnd", 0))
    except Exception:
        return 0


def focus_window(hwnd):
    """Focus a window using the Alt-key trick."""
    u32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)
    u32.SetForegroundWindow(hwnd)
    time.sleep(0.3)


def check_current_mode(hwnd):
    """Check if window is already in Copilot CLI mode via UIA button scan."""
    try:
        import System.Windows.Automation as SWA  # noqa
    except ImportError:
        pass

    # Fallback: use PowerShell UIA scan
    import subprocess
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
        if ($nm -match 'Copilot CLI|Local|Cloud') {{
            Write-Output $nm
        }}
    }}
    '''
    try:
        result = subprocess.run(
            ["powershell", "-STA", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if "Copilot CLI" in line:
                return "copilot_cli"
            elif "Cloud" in line:
                return "cloud"
            elif "Local" in line:
                return "local"
    except Exception:
        pass
    return "unknown"


def switch_to_copilot_cli(hwnd, name="worker", orch_hwnd=0, grid_pos=None):
    """Switch a single worker window to Copilot CLI mode.
    
    Returns True if successfully switched or already in CLI mode.
    """
    import pyautogui

    # Check current mode first
    mode = check_current_mode(hwnd)
    if mode == "copilot_cli":
        print(f"  ✅ {name} already in Copilot CLI mode")
        return True

    print(f"  🔄 {name} currently in '{mode}' mode, switching to Copilot CLI...")

    # Move to primary monitor for pyautogui access
    u32.MoveWindow(hwnd, *PRIMARY_POS, True)
    time.sleep(0.3)

    # Focus the window
    focus_window(hwnd)
    time.sleep(0.3)

    # Click the session target button at bottom-left of chat window
    # Position: approximately x=55, y=685 relative to window at PRIMARY_POS
    btn_x = PRIMARY_POS[0] + 55
    btn_y = PRIMARY_POS[1] + PRIMARY_POS[3] - 15  # 15px from bottom
    pyautogui.click(btn_x, btn_y)
    time.sleep(1.5)

    # Take screenshot to find "Copilot CLI" in dropdown
    ss = pyautogui.screenshot(region=(
        PRIMARY_POS[0] - 50,
        PRIMARY_POS[1] + PRIMARY_POS[3] - 230,
        400, 250
    ))

    # Find "Copilot CLI" position in the dropdown
    # The dropdown always shows: New Chat Session / Continue In: Local, Copilot CLI, Cloud
    # "Copilot CLI" is the 2nd option under "Continue In", approximately 110px from dropdown top
    cli_x = PRIMARY_POS[0] + 85
    cli_y = PRIMARY_POS[1] + PRIMARY_POS[3] - 90  # Copilot CLI position in dropdown

    pyautogui.click(cli_x, cli_y)
    time.sleep(2)

    # Verify the switch
    mode_after = check_current_mode(hwnd)
    
    if mode_after == "copilot_cli":
        print(f"  ✅ {name} switched to Copilot CLI mode")
        success = True
    else:
        # Retry with adjusted coordinates
        print(f"  ⚠️  First attempt got '{mode_after}', retrying...")
        pyautogui.press("escape")
        time.sleep(0.5)
        
        # Reopen dropdown
        pyautogui.click(btn_x, btn_y)
        time.sleep(1.5)
        
        # Screenshot for debugging
        ss2 = pyautogui.screenshot(region=(
            PRIMARY_POS[0] - 50,
            PRIMARY_POS[1] + PRIMARY_POS[3] - 250,
            400, 280
        ))
        debug_path = os.path.join(ROOT, "data", f"{name}_cli_debug.png")
        ss2.save(debug_path)
        
        # Try clicking slightly higher (Copilot CLI might be at different y)
        pyautogui.click(cli_x, cli_y - 20)
        time.sleep(2)
        
        mode_after2 = check_current_mode(hwnd)
        if mode_after2 == "copilot_cli":
            print(f"  ✅ {name} switched to Copilot CLI mode (retry)")
            success = True
            # Clean debug screenshot
            try:
                os.remove(debug_path)
            except OSError:
                pass
        else:
            print(f"  ❌ {name} FAILED to switch — still in '{mode_after2}' mode")
            print(f"     Debug screenshot: {debug_path}")
            success = False

    # Move back to grid position
    if grid_pos:
        u32.MoveWindow(hwnd, *grid_pos, True)
        time.sleep(0.2)

    return success


def main():
    parser = argparse.ArgumentParser(description="Switch workers to Copilot CLI mode")
    parser.add_argument("--worker", help="Specific worker name (alpha/beta/gamma/delta)")
    parser.add_argument("--hwnd", type=int, help="Specific window HWND")
    parser.add_argument("--verify-only", action="store_true", help="Only check mode, don't switch")
    args = parser.parse_args()

    orch_hwnd = load_orch_hwnd()

    if args.hwnd:
        # Single HWND mode
        name = args.worker or "worker"
        grid = GRID_POSITIONS.get(name)
        if args.verify_only:
            mode = check_current_mode(args.hwnd)
            status = "✅" if mode == "copilot_cli" else "❌"
            print(f"{status} {name} (HWND={args.hwnd}): {mode}")
        else:
            ok = switch_to_copilot_cli(args.hwnd, name, orch_hwnd, grid)
            if orch_hwnd:
                focus_window(orch_hwnd)
            sys.exit(0 if ok else 1)
    else:
        # All workers mode
        workers = load_workers()
        if args.worker:
            workers = [w for w in workers if w.get("name") == args.worker]

        if not workers:
            print("No workers found in workers.json")
            sys.exit(1)

        results = {}
        for w in workers:
            name = w.get("name", "unknown")
            hwnd = int(w.get("hwnd", 0))
            if not hwnd or not u32.IsWindow(hwnd):
                print(f"  ⚠️  {name} HWND={hwnd} is dead, skipping")
                results[name] = False
                continue

            grid = GRID_POSITIONS.get(name)
            if args.verify_only:
                mode = check_current_mode(hwnd)
                status = "✅" if mode == "copilot_cli" else "❌"
                print(f"{status} {name} (HWND={hwnd}): {mode}")
                results[name] = mode == "copilot_cli"
            else:
                print(f"--- {name} (HWND={hwnd}) ---")
                ok = switch_to_copilot_cli(hwnd, name, orch_hwnd, grid)
                results[name] = ok

        # Restore orchestrator focus
        if orch_hwnd and not args.verify_only:
            focus_window(orch_hwnd)

        # Summary
        total = len(results)
        passed = sum(1 for v in results.values() if v)
        print(f"\n{'='*40}")
        print(f"Copilot CLI mode: {passed}/{total} workers")
        if passed < total:
            failed = [n for n, v in results.items() if not v]
            print(f"FAILED: {', '.join(failed)}")
            sys.exit(1)
        else:
            print("ALL WORKERS IN COPILOT CLI MODE ✅")


if __name__ == "__main__":
    main()
