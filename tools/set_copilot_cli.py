#!/usr/bin/env python
"""
set_copilot_cli.py — Enforce Copilot CLI mode + Bypass Approvals on worker windows.

Two MANDATORY settings enforced by this script:
  1. Session Target: Local/Cloud → Copilot CLI
  2. Approval Permissions: Default Approvals → Bypass Approvals

Both are Chromium-rendered dropdowns that ONLY respond to pyautogui
hardware-level input (INCIDENT 013). Win32/UIA/clipboard cannot reach them.

Coordinates (proven working, window at 200,50,930x700):
  - "Default Approvals" button: absolute (380, 740) — bottom bar, right of Copilot CLI
  - "Bypass Approvals" in dropdown: absolute (345, 655) — 2nd option in 3-item list
  - "Copilot CLI" button: absolute (280, 740) — bottom bar, left side

Usage:
  python tools/set_copilot_cli.py                     # All workers: CLI + Bypass
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
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

u32 = ctypes.windll.user32
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002

GRID_POSITIONS = {
    "alpha": (1930, 20, 930, 500),
    "beta": (2860, 20, 930, 500),
    "gamma": (1930, 540, 930, 500),
    "delta": (2860, 540, 930, 500),
}

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


def _uia_scan_buttons(hwnd):
    """Scan UIA buttons for session target and approval mode. Returns dict."""
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
        if ($nm -match 'Copilot CLI|Local|Cloud|Default Approvals|Bypass Approvals|Autopilot') {{
            Write-Output $nm
        }}
    }}
    '''
    result = {"session_target": "unknown", "approvals": "unknown"}
    try:
        r = subprocess.run(
            ["powershell", "-STA", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=10
        )
        for line in r.stdout.strip().split("\n"):
            line = line.strip()
            if "Copilot CLI" in line and "Approvals" not in line:
                result["session_target"] = "copilot_cli"
            elif "Local" in line and "Approvals" not in line:
                result["session_target"] = "local"
            elif "Cloud" in line and "Approvals" not in line:
                result["session_target"] = "cloud"
            if "Bypass Approvals" in line:
                result["approvals"] = "bypass"
            elif "Default Approvals" in line:
                result["approvals"] = "default"
            elif "Autopilot" in line:
                result["approvals"] = "autopilot"
    except Exception:
        pass
    return result


def _move_to_primary(hwnd):
    """Move window to primary monitor for pyautogui access."""
    u32.MoveWindow(hwnd, *PRIMARY_POS, True)
    time.sleep(0.4)
    focus_window(hwnd)
    time.sleep(0.3)


def _switch_approvals_to_bypass(name="worker"):
    """Click Default Approvals → Bypass Approvals. Window must be at PRIMARY_POS and focused."""
    import pyautogui

    # Click "Default Approvals ∨" button at bottom bar
    # Proven coordinates: absolute (380, 740) when window at (200, 50, 930, 700)
    pyautogui.click(380, 740)
    time.sleep(1.5)

    # Click "Bypass Approvals" — 2nd item in dropdown
    # Proven coordinates: absolute (345, 655)
    pyautogui.click(345, 655)
    time.sleep(1.0)


def _switch_session_to_cli(name="worker"):
    """Click Local/Cloud → Copilot CLI. Window must be at PRIMARY_POS and focused."""
    import pyautogui

    # Click session target button at bottom-left
    # "Local ∨" or "Cloud ∨" at approximately (280, 740)
    pyautogui.click(280, 740)
    time.sleep(1.5)

    # In the dropdown, "Copilot CLI" is one of the "Continue In" options
    # It appears at approximately (285, 660) in the dropdown
    pyautogui.click(285, 660)
    time.sleep(1.0)


def enforce_settings(hwnd, name="worker", orch_hwnd=0, grid_pos=None):
    """Ensure a worker window has both Copilot CLI + Bypass Approvals.

    Returns dict with 'session_target_ok' and 'approvals_ok' booleans.
    """
    state = _uia_scan_buttons(hwnd)
    cli_ok = state["session_target"] == "copilot_cli"
    bypass_ok = state["approvals"] == "bypass"

    if cli_ok and bypass_ok:
        print(f"  ✅ {name}: Copilot CLI ✅ + Bypass Approvals ✅")
        return {"session_target_ok": True, "approvals_ok": True}

    # Need to fix something — move to primary
    _move_to_primary(hwnd)

    # Fix approvals first (if needed)
    if not bypass_ok:
        print(f"  🔄 {name}: switching to Bypass Approvals (was '{state['approvals']}')")
        _switch_approvals_to_bypass(name)

    # Fix session target (if needed)
    if not cli_ok:
        print(f"  🔄 {name}: switching to Copilot CLI (was '{state['session_target']}')")
        _switch_session_to_cli(name)

    # Verify
    state_after = _uia_scan_buttons(hwnd)
    cli_ok = state_after["session_target"] == "copilot_cli"
    bypass_ok = state_after["approvals"] == "bypass"

    if cli_ok:
        print(f"  ✅ {name}: Copilot CLI ✅")
    else:
        print(f"  ❌ {name}: session target still '{state_after['session_target']}'")

    if bypass_ok:
        print(f"  ✅ {name}: Bypass Approvals ✅")
    else:
        print(f"  ❌ {name}: approvals still '{state_after['approvals']}'")

    # Move back to grid
    if grid_pos:
        u32.MoveWindow(hwnd, *grid_pos, True)
        time.sleep(0.2)

    return {"session_target_ok": cli_ok, "approvals_ok": bypass_ok}


def main():
    parser = argparse.ArgumentParser(description="Enforce Copilot CLI + Bypass Approvals")
    parser.add_argument("--worker", help="Specific worker name (alpha/beta/gamma/delta)")
    parser.add_argument("--hwnd", type=int, help="Specific window HWND")
    parser.add_argument("--verify-only", action="store_true", help="Only check, don't switch")
    args = parser.parse_args()

    orch_hwnd = load_orch_hwnd()

    if args.hwnd:
        name = args.worker or "worker"
        grid = GRID_POSITIONS.get(name)
        if args.verify_only:
            state = _uia_scan_buttons(args.hwnd)
            cli = "✅" if state["session_target"] == "copilot_cli" else "❌"
            byp = "✅" if state["approvals"] == "bypass" else "❌"
            print(f"{cli} {name} session: {state['session_target']}")
            print(f"{byp} {name} approvals: {state['approvals']}")
        else:
            result = enforce_settings(args.hwnd, name, orch_hwnd, grid)
            if orch_hwnd:
                focus_window(orch_hwnd)
            ok = result["session_target_ok"] and result["approvals_ok"]
            sys.exit(0 if ok else 1)
    else:
        workers = load_workers()
        if args.worker:
            workers = [w for w in workers if w.get("name") == args.worker]

        if not workers:
            print("No workers found in workers.json")
            sys.exit(1)

        all_ok = True
        for w in workers:
            name = w.get("name", "unknown")
            hwnd = int(w.get("hwnd", 0))
            if not hwnd or not u32.IsWindow(hwnd):
                print(f"  ⚠️  {name} HWND={hwnd} is dead, skipping")
                all_ok = False
                continue

            grid = GRID_POSITIONS.get(name)
            if args.verify_only:
                state = _uia_scan_buttons(hwnd)
                cli = "✅" if state["session_target"] == "copilot_cli" else "❌"
                byp = "✅" if state["approvals"] == "bypass" else "❌"
                print(f"{cli} {name} (HWND={hwnd}): session={state['session_target']}")
                print(f"{byp} {name} (HWND={hwnd}): approvals={state['approvals']}")
                if state["session_target"] != "copilot_cli" or state["approvals"] != "bypass":
                    all_ok = False
            else:
                print(f"--- {name} (HWND={hwnd}) ---")
                result = enforce_settings(hwnd, name, orch_hwnd, grid)
                if not (result["session_target_ok"] and result["approvals_ok"]):
                    all_ok = False

        if orch_hwnd and not args.verify_only:
            focus_window(orch_hwnd)

        print(f"\n{'='*40}")
        if all_ok:
            print("ALL WORKERS: Copilot CLI ✅ + Bypass Approvals ✅")
        else:
            print("SOME WORKERS NEED ATTENTION — check output above")
            sys.exit(1)


if __name__ == "__main__":
    main()
