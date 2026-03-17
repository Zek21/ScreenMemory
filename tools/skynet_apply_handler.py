#!/usr/bin/env python3
"""
skynet_apply_handler.py — Handles stuck Apply buttons in VS Code Copilot Chat.

ROOT CAUSE (INCIDENT 016):
  - Chromium webview buttons (Apply, View All Changes) are unreachable by ALL
    programmatic input methods: UIA InvokePattern (NULL COM pointer), PostMessage,
    SendMessage, SendInput, pyautogui mouse (webview blocks it), keyboard Enter/Space.
  - pyautogui mouse CANNOT reach workers on the right monitor (x>1920) at all.

SOLUTION:
  1. Focus worker window via SetForegroundWindow
  2. Press F6 ×4 to cycle VS Code panel focus into the chat panel
  3. Press Ctrl+L to start a new conversation (dismisses Apply panel)
  4. Optionally re-inject worker identity

DISCOVERY: Ctrl+L only works when the chat panel has keyboard focus.
F6 (cycle panel focus) reliably moves focus through VS Code panels.
4 presses consistently lands on the chat panel.

Usage:
  python tools/skynet_apply_handler.py                    # Clear all workers with Apply
  python tools/skynet_apply_handler.py --worker beta      # Clear specific worker
  python tools/skynet_apply_handler.py --scan              # Just scan for Apply buttons
  python tools/skynet_apply_handler.py --reinject          # Clear + re-inject identity
"""
# signed: orchestrator

import argparse
import ctypes
import json
import os
import sys
import time

# Lazy imports for pyautogui (not always available)
_pyautogui = None

def _get_pyautogui():
    global _pyautogui
    if _pyautogui is None:
        import pyautogui
        _pyautogui = pyautogui
    return _pyautogui


def _get_uia():
    """Get UI Automation COM interface."""
    import comtypes
    import comtypes.client
    try:
        return comtypes.client.CreateObject(
            '{ff48dba4-60ef-4201-aa87-54103eef594e}',
            interface=comtypes.gen.UIAutomationClient.IUIAutomation
        )
    except Exception:
        comtypes.client.GetModule('UIAutomationCore.dll')
        return comtypes.client.CreateObject(
            '{ff48dba4-60ef-4201-aa87-54103eef594e}',
            interface=comtypes.gen.UIAutomationClient.IUIAutomation
        )


def scan_for_apply_buttons(workers=None):
    """Scan worker windows for Apply buttons. Returns dict of {name: count}."""
    if workers is None:
        workers = _load_workers()

    UIA = _get_uia()
    results = {}

    for w in workers:
        name = w['name']
        hwnd = w['hwnd']
        try:
            el = UIA.ElementFromHandle(hwnd)
            cond = UIA.CreatePropertyCondition(30003, 50000)  # ControlType.Button
            btns = el.FindAll(4, cond)  # TreeScope.Descendants
            count = 0
            for i in range(btns.Length):
                btn_name = str(btns.GetElement(i).CurrentName)
                if 'Apply' in btn_name and 'Editor' not in btn_name:
                    count += 1
            results[name] = count
        except Exception as e:
            results[name] = -1  # Error scanning
            print(f"  ⚠ Error scanning {name}: {e}")

    return results


def clear_apply_panel(hwnd, name="worker", f6_count=4, retries=2):
    """
    Clear an Apply panel from a worker window using F6 + Ctrl+L.

    Steps:
      1. Focus window via SetForegroundWindow
      2. Press F6 × f6_count to cycle panel focus to chat
      3. Press Ctrl+L to start new conversation (dismisses Apply)

    Returns True if Apply was cleared, False if it persists.
    """
    pyautogui = _get_pyautogui()
    u = ctypes.windll.user32

    for attempt in range(retries):
        # Focus the window
        u.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.2)
        u.SetForegroundWindow(hwnd)
        time.sleep(0.5)

        # Verify focus
        fg = u.GetForegroundWindow()
        if fg != hwnd:
            print(f"  ⚠ {name}: focus failed (got {fg}, need {hwnd}), retrying...")
            time.sleep(0.5)
            u.SetForegroundWindow(hwnd)
            time.sleep(0.5)

        # F6 to cycle panel focus into chat panel
        for _ in range(f6_count):
            pyautogui.press('f6')
            time.sleep(0.2)

        # Ctrl+L to start new conversation
        pyautogui.hotkey('ctrl', 'l')
        time.sleep(1.5)

        # Check if Apply is gone
        apply_count = scan_for_apply_buttons([{'name': name, 'hwnd': hwnd}])
        if apply_count.get(name, 1) == 0:
            print(f"  ✅ {name}: Apply cleared (attempt {attempt + 1})")
            return True
        else:
            print(f"  ⚠ {name}: Apply persists (attempt {attempt + 1}/{retries})")

    return False


def clear_all_apply_panels(workers=None, reinject=False):
    """Clear Apply panels from all workers that have them."""
    if workers is None:
        workers = _load_workers()

    # First scan
    apply_status = scan_for_apply_buttons(workers)
    stuck = [w for w in workers if apply_status.get(w['name'], 0) > 0]

    if not stuck:
        print("✅ No workers have Apply panels — all clear")
        return True

    print(f"🔧 Found {len(stuck)} workers with Apply panels: {[w['name'] for w in stuck]}")

    all_cleared = True
    for w in stuck:
        success = clear_apply_panel(w['hwnd'], w['name'])
        if not success:
            all_cleared = False
            print(f"  ❌ {w['name']}: Apply could NOT be cleared after retries")

    if reinject and all_cleared:
        _reinject_identities([w for w in stuck])

    # Restore orchestrator focus
    _restore_orchestrator_focus()

    return all_cleared


def _reinject_identities(workers):
    """Re-inject worker identities after clearing conversations."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from tools.skynet_dispatch import ghost_type_to_worker
        orch_hwnd = _load_orch_hwnd()

        for w in workers:
            identity = f"You are {w['name'].upper()}, a Skynet worker. Acknowledge with: IDENTITY_ACK {w['name']}"
            print(f"  📤 Re-injecting identity to {w['name']}...")
            time.sleep(2)  # Clipboard cooldown
            ghost_type_to_worker(w['hwnd'], identity, orch_hwnd)
            time.sleep(1)
    except Exception as e:
        print(f"  ⚠ Identity re-injection failed: {e}")


def _load_workers():
    """Load worker list from data/workers.json."""
    workers_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'data', 'workers.json')
    with open(workers_path) as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get('workers', [])


def _load_orch_hwnd():
    """Load orchestrator HWND."""
    orch_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             'data', 'orchestrator.json')
    with open(orch_path) as f:
        return json.load(f).get('hwnd', 0)


def _restore_orchestrator_focus():
    """Restore focus to orchestrator window."""
    try:
        orch_hwnd = _load_orch_hwnd()
        if orch_hwnd:
            ctypes.windll.user32.SetForegroundWindow(orch_hwnd)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description='Handle stuck Apply buttons in worker windows')
    parser.add_argument('--worker', help='Target specific worker by name')
    parser.add_argument('--scan', action='store_true', help='Just scan for Apply buttons')
    parser.add_argument('--reinject', action='store_true', help='Re-inject worker identities after clearing')
    parser.add_argument('--f6-count', type=int, default=4, help='F6 presses to cycle focus (default: 4)')
    args = parser.parse_args()

    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    workers = _load_workers()
    if args.worker:
        workers = [w for w in workers if w['name'] == args.worker]
        if not workers:
            print(f"❌ Worker '{args.worker}' not found")
            sys.exit(1)

    if args.scan:
        results = scan_for_apply_buttons(workers)
        for name, count in results.items():
            if count > 0:
                print(f"  ⚠ {name}: {count} Apply button(s)")
            elif count == 0:
                print(f"  ✅ {name}: clear")
            else:
                print(f"  ❌ {name}: scan error")
        sys.exit(0)

    success = clear_all_apply_panels(workers, reinject=args.reinject)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
