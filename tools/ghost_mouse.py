#!/usr/bin/env python3
"""Ghost Mouse — cursor-free window interaction via PostMessage/UIA/CDP.

NEVER moves the real cursor. All clicks are delivered via Win32 PostMessage,
UIA InvokePattern, or CDP JavaScript evaluation. Safe for use while the user
is actively working — zero focus theft, zero cursor movement.

Usage:
    python tools/ghost_mouse.py --hwnd HWND --x X --y Y --action click
    python tools/ghost_mouse.py --hwnd HWND --x X --y Y --action right
    python tools/ghost_mouse.py --hwnd HWND --x X --y Y --action double
    python tools/ghost_mouse.py --hwnd HWND --x X --y Y --action scroll --delta -120
    python tools/ghost_mouse.py --hwnd HWND --x1 X1 --y1 Y1 --x2 X2 --y2 Y2 --action drag
    python tools/ghost_mouse.py --hwnd HWND --name "Button Name" --action invoke
    python tools/ghost_mouse.py --cdp-port 9222 --tab-id TAB_ID --selector "#btn" --action cdp-click
    python tools/ghost_mouse.py --hwnd HWND --action find-render

Forbidden (NEVER used): pyautogui, SendInput, SetCursorPos, mouse_event.
"""
# signed: alpha

import ctypes
import ctypes.wintypes
import time
import argparse
import json
import sys
import os
from typing import Optional, Tuple, List

# ─── Win32 Constants ──────────────────────────────────────────────
WM_LBUTTONDOWN   = 0x0201
WM_LBUTTONUP     = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN   = 0x0204
WM_RBUTTONUP     = 0x0205
WM_MBUTTONDOWN   = 0x0207
WM_MBUTTONUP     = 0x0208
WM_MOUSEWHEEL    = 0x020A
WM_MOUSEMOVE     = 0x0200

MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002

GW_CHILD = 5

user32 = ctypes.windll.user32

# ─── Win32 Function Signatures ────────────────────────────────────
PostMessageW = user32.PostMessageW
PostMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.c_uint,
                         ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
PostMessageW.restype = ctypes.wintypes.BOOL

SendMessageW = user32.SendMessageW
SendMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.c_uint,
                         ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
SendMessageW.restype = ctypes.wintypes.LPARAM

GetWindowTextW = user32.GetWindowTextW
GetClassNameW = user32.GetClassNameW
GetWindow = user32.GetWindow
IsWindow = user32.IsWindow
GetWindowRect = user32.GetWindowRect
ClientToScreen = user32.ClientToScreen
ScreenToClient = user32.ScreenToClient

EnumChildWindows = user32.EnumChildWindows
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL,
                                  ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)


def _make_lparam(x: int, y: int) -> int:
    """Pack (x, y) into LPARAM as MAKELPARAM(x, y)."""
    return (y & 0xFFFF) << 16 | (x & 0xFFFF)  # signed: alpha


def _make_wheel_lparam(x: int, y: int, hwnd: int) -> int:
    """WM_MOUSEWHEEL needs SCREEN coordinates in lParam, not client."""
    pt = ctypes.wintypes.POINT(x, y)
    ClientToScreen(hwnd, ctypes.byref(pt))
    return (pt.y & 0xFFFF) << 16 | (pt.x & 0xFFFF)  # signed: alpha


# ─── Core Ghost Mouse Functions ──────────────────────────────────

def ghost_click(hwnd: int, x: int, y: int, pause: float = 0.05) -> bool:
    """Left-click at (x, y) in client coordinates via PostMessage.

    Does NOT move the physical cursor. Does NOT steal focus.
    Works on background windows.
    """
    if not IsWindow(hwnd):
        return False
    lp = _make_lparam(x, y)
    PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    time.sleep(pause)
    PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)
    return True  # signed: alpha


def ghost_right_click(hwnd: int, x: int, y: int, pause: float = 0.05) -> bool:
    """Right-click at (x, y) in client coordinates via PostMessage."""
    if not IsWindow(hwnd):
        return False
    lp = _make_lparam(x, y)
    PostMessageW(hwnd, WM_RBUTTONDOWN, MK_RBUTTON, lp)
    time.sleep(pause)
    PostMessageW(hwnd, WM_RBUTTONUP, 0, lp)
    return True  # signed: alpha


def ghost_double_click(hwnd: int, x: int, y: int, pause: float = 0.05) -> bool:
    """Double-click at (x, y) via PostMessage WM_LBUTTONDBLCLK."""
    if not IsWindow(hwnd):
        return False
    lp = _make_lparam(x, y)
    # Standard double-click sequence: DOWN, UP, DBLCLK, UP
    PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    time.sleep(pause)
    PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)
    time.sleep(pause)
    PostMessageW(hwnd, WM_LBUTTONDBLCLK, MK_LBUTTON, lp)
    time.sleep(pause)
    PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)
    return True  # signed: alpha


def ghost_scroll(hwnd: int, x: int, y: int, delta: int = -120) -> bool:
    """Scroll at (x, y) via PostMessage WM_MOUSEWHEEL.

    delta: positive = scroll up, negative = scroll down.
    Standard unit is 120 (one notch). Use -120 for one notch down.
    """
    if not IsWindow(hwnd):
        return False
    # WM_MOUSEWHEEL wParam: HIWORD = delta, LOWORD = key state
    wparam = (delta & 0xFFFF) << 16
    # lParam must be screen coordinates for WM_MOUSEWHEEL
    lp = _make_wheel_lparam(x, y, hwnd)
    PostMessageW(hwnd, WM_MOUSEWHEEL, wparam, lp)
    return True  # signed: alpha


def ghost_drag(hwnd: int, x1: int, y1: int, x2: int, y2: int,
               steps: int = 10, step_delay: float = 0.01) -> bool:
    """Drag from (x1,y1) to (x2,y2) via PostMessage series.

    Sends: LBUTTONDOWN at start → MOUSEMOVE interpolated → LBUTTONUP at end.
    """
    if not IsWindow(hwnd):
        return False

    lp_start = _make_lparam(x1, y1)
    PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp_start)
    time.sleep(step_delay)

    # Interpolate movement
    for i in range(1, steps + 1):
        t = i / steps
        cx = int(x1 + (x2 - x1) * t)
        cy = int(y1 + (y2 - y1) * t)
        lp = _make_lparam(cx, cy)
        PostMessageW(hwnd, WM_MOUSEMOVE, MK_LBUTTON, lp)
        time.sleep(step_delay)

    lp_end = _make_lparam(x2, y2)
    PostMessageW(hwnd, WM_LBUTTONUP, 0, lp_end)
    return True  # signed: alpha


def ghost_hover(hwnd: int, x: int, y: int) -> bool:
    """Move the virtual mouse to (x, y) without clicking."""
    if not IsWindow(hwnd):
        return False
    lp = _make_lparam(x, y)
    PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
    return True  # signed: alpha


# ─── Chrome Render Widget Discovery ──────────────────────────────

def find_render_widget(parent_hwnd: int) -> Optional[int]:
    """Find Chrome_RenderWidgetHostHWND child of a window (DFS).

    Used for targeting clicks into VS Code, Chrome, Electron apps.
    Returns the HWND of the render widget, or None if not found.
    """
    results: List[int] = []

    def _enum_callback(child_hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(256)
        GetClassNameW(child_hwnd, buf, 256)
        class_name = buf.value
        if class_name.startswith("Chrome_RenderWidgetHost"):
            results.append(child_hwnd)
            return False  # stop enumeration
        return True  # continue

    cb = WNDENUMPROC(_enum_callback)
    EnumChildWindows(parent_hwnd, cb, 0)

    if results:
        return results[0]

    # DFS through child windows
    child = GetWindow(parent_hwnd, GW_CHILD)
    while child:
        found = find_render_widget(child)
        if found:
            return found
        child = GetWindow(child, 2)  # GW_HWNDNEXT = 2

    return None  # signed: alpha


def find_all_render_widgets(parent_hwnd: int) -> List[int]:
    """Find ALL Chrome_RenderWidgetHostHWND children (for multi-pane windows)."""
    results: List[int] = []

    def _enum_callback(child_hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(256)
        GetClassNameW(child_hwnd, buf, 256)
        if buf.value.startswith("Chrome_RenderWidgetHost"):
            results.append(child_hwnd)
        return True  # continue to find all

    cb = WNDENUMPROC(_enum_callback)
    EnumChildWindows(parent_hwnd, cb, 0)
    return results  # signed: alpha


def ghost_click_render(parent_hwnd: int, x: int, y: int) -> bool:
    """Click inside a Chrome render widget at (x, y) parent-relative coords.

    Finds the render widget, converts coordinates, and clicks.
    """
    render = find_render_widget(parent_hwnd)
    if not render:
        return False

    # Convert parent client coords to render widget client coords
    pt = ctypes.wintypes.POINT(x, y)
    ClientToScreen(parent_hwnd, ctypes.byref(pt))
    ScreenToClient(render, ctypes.byref(pt))

    return ghost_click(render, pt.x, pt.y)  # signed: alpha


# ─── UIA InvokePattern ────────────────────────────────────────────

def invoke_by_name(hwnd: int, name: str, control_type: str = "Button") -> bool:
    """Invoke a UIA element by name using InvokePattern.

    No cursor movement, no focus theft. Uses COM-based UI Automation.
    Falls back to PowerShell UIA if COM import fails.

    Args:
        hwnd: Target window handle
        name: Element name/label to find (e.g., "Cancel (Alt+Backspace)")
        control_type: UIA control type filter (default: "Button")

    Returns:
        True if element was found and invoked, False otherwise.
    """
    # Use PowerShell with .NET UIA — reliable and doesn't need comtypes
    ps_script = f'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
$nameCondition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty, "{name}")
$el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $nameCondition)
if ($el) {{
    $invokePattern = $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $invokePattern.Invoke()
    Write-Output "INVOKED"
}} else {{
    Write-Output "NOT_FOUND"
}}
'''
    import subprocess
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True, text=True, timeout=10,
        creationflags=0x08000000  # CREATE_NO_WINDOW
    )
    return "INVOKED" in result.stdout  # signed: alpha


def invoke_by_automation_id(hwnd: int, automation_id: str) -> bool:
    """Invoke a UIA element by AutomationId."""
    ps_script = f'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
$cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::AutomationIdProperty, "{automation_id}")
$el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
if ($el) {{
    $invokePattern = $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $invokePattern.Invoke()
    Write-Output "INVOKED"
}} else {{
    Write-Output "NOT_FOUND"
}}
'''
    import subprocess
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True, text=True, timeout=10,
        creationflags=0x08000000
    )
    return "INVOKED" in result.stdout  # signed: alpha


def find_uia_element_coords(hwnd: int, name: str) -> Optional[Tuple[int, int]]:
    """Find UIA element center coordinates (client-relative) by name.

    Useful when you need to PostMessage click a UIA-identified element.
    """
    ps_script = f'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
$cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty, "{name}")
$el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
if ($el) {{
    $rect = $el.Current.BoundingRectangle
    $cx = [int]($rect.X + $rect.Width / 2)
    $cy = [int]($rect.Y + $rect.Height / 2)
    Write-Output "COORDS:$cx,$cy"
}} else {{
    Write-Output "NOT_FOUND"
}}
'''
    import subprocess
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True, text=True, timeout=10,
        creationflags=0x08000000
    )
    for line in result.stdout.strip().split('\n'):
        if line.startswith("COORDS:"):
            parts = line.split(":")[1].split(",")
            sx, sy = int(parts[0]), int(parts[1])
            # Convert screen coords to client coords
            pt = ctypes.wintypes.POINT(sx, sy)
            ScreenToClient(hwnd, ctypes.byref(pt))
            return (pt.x, pt.y)
    return None  # signed: alpha


# ─── CDP Integration ──────────────────────────────────────────────

def cdp_click(port: int, tab_id: str, selector: str) -> bool:
    """Click an element via CDP JavaScript evaluation — zero cursor movement.

    Uses DOM.querySelector → getBoundingClientRect → dispatchEvent click.
    No physical mouse involved at all.

    Args:
        port: Chrome DevTools Protocol port (usually 9222)
        tab_id: Tab ID from CDP /json endpoint
        selector: CSS selector for the element to click
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from chrome_bridge.cdp import CDP
        cdp = CDP(port=port)
        # Use Runtime.evaluate with a click simulation
        js = f'''
        (() => {{
            const el = document.querySelector('{selector}');
            if (!el) return JSON.stringify({{ok: false, error: 'not_found'}});
            const rect = el.getBoundingClientRect();
            const x = rect.x + rect.width / 2;
            const y = rect.y + rect.height / 2;
            el.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true, clientX: x, clientY: y}}));
            el.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true, clientX: x, clientY: y}}));
            el.dispatchEvent(new MouseEvent('click', {{bubbles: true, clientX: x, clientY: y}}));
            return JSON.stringify({{ok: true, x: x, y: y, tag: el.tagName}});
        }})()
        '''
        result = cdp.eval(tab_id, js)
        if isinstance(result, str):
            data = json.loads(result)
            return data.get('ok', False)
        return False
    except Exception as e:
        print(f"cdp_click error: {e}", file=sys.stderr)
        return False  # signed: alpha


def cdp_click_text(port: int, tab_id: str, text: str) -> bool:
    """Click the first element containing the given text via CDP JS.

    Searches all visible elements and clicks the first text match.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from chrome_bridge.cdp import CDP
        cdp = CDP(port=port)
        js = f'''
        (() => {{
            const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_ELEMENT, null);
            let node;
            const target = '{text}';
            while (node = walker.nextNode()) {{
                if (node.textContent.trim() === target ||
                    node.innerText?.trim() === target ||
                    node.getAttribute('aria-label') === target) {{
                    const rect = node.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {{
                        node.click();
                        return JSON.stringify({{ok: true, tag: node.tagName}});
                    }}
                }}
            }}
            return JSON.stringify({{ok: false, error: 'text_not_found'}});
        }})()
        '''
        result = cdp.eval(tab_id, js)
        if isinstance(result, str):
            data = json.loads(result)
            return data.get('ok', False)
        return False
    except Exception as e:
        print(f"cdp_click_text error: {e}", file=sys.stderr)
        return False  # signed: alpha


def cdp_input_dispatch(port: int, tab_id: str, x: float, y: float,
                       click_type: str = "click") -> bool:
    """Click at exact page coordinates via CDP Input.dispatchMouseEvent.

    This uses the DevTools Input domain — the most reliable CDP click method.
    Still zero physical cursor movement.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from chrome_bridge.cdp import CDP
        cdp = CDP(port=port)
        tab = cdp.tab(tab_id)
        tab.connect()

        tab.send('Input.dispatchMouseEvent', {
            'type': 'mousePressed',
            'x': x, 'y': y,
            'button': 'left',
            'clickCount': 2 if click_type == "double" else 1
        })
        time.sleep(0.05)
        tab.send('Input.dispatchMouseEvent', {
            'type': 'mouseReleased',
            'x': x, 'y': y,
            'button': 'left',
            'clickCount': 2 if click_type == "double" else 1
        })
        return True
    except Exception as e:
        print(f"cdp_input_dispatch error: {e}", file=sys.stderr)
        return False  # signed: alpha


# ─── Convenience Helpers ──────────────────────────────────────────

def ghost_click_element(hwnd: int, element_name: str) -> bool:
    """Click a named UI element: try UIA Invoke first, fall back to PostMessage.

    This is the highest-level ghost click — finds the element by name via UIA,
    tries InvokePattern (most reliable), falls back to coordinate-based click.
    """
    # Try UIA InvokePattern first (most reliable, no coords needed)
    if invoke_by_name(hwnd, element_name):
        return True

    # Fall back: find element coords via UIA, then PostMessage click
    coords = find_uia_element_coords(hwnd, element_name)
    if coords:
        return ghost_click(hwnd, coords[0], coords[1])

    return False  # signed: alpha


# ─── PostMessage Key Functions ────────────────────────────────────
# signed: alpha

WM_KEYDOWN = 0x0100
WM_KEYUP   = 0x0101
WM_CHAR    = 0x0102
WM_PASTE   = 0x0302

VK_RETURN  = 0x0D
VK_ESCAPE  = 0x1B
VK_CONTROL = 0x11
VK_SHIFT   = 0x10
VK_F6      = 0x75
VK_L       = 0x4C
VK_V       = 0x56

KEYEVENTF_KEYUP = 0x0002


def ghost_key_press(hwnd: int, vk_code: int, pause: float = 0.05) -> bool:
    """Press a key via PostMessage WM_KEYDOWN + WM_KEYUP.

    Window-targeted — works on background windows. No cursor movement.
    Note: May not work for Chrome render widgets (use ghost_keybd_press instead).
    """
    if not IsWindow(hwnd):
        return False
    PostMessageW(hwnd, WM_KEYDOWN, vk_code, 0)
    time.sleep(pause)
    PostMessageW(hwnd, WM_KEYUP, vk_code, 0)
    return True  # signed: alpha


def ghost_hotkey(hwnd: int, modifier_vk: int, key_vk: int, pause: float = 0.05) -> bool:
    """Press modifier+key combo via PostMessage. E.g., Ctrl+L, Ctrl+V.

    Window-targeted — works on background windows. No cursor movement.
    Note: May not work for Chrome render widgets (use ghost_keybd_hotkey instead).
    """
    if not IsWindow(hwnd):
        return False
    PostMessageW(hwnd, WM_KEYDOWN, modifier_vk, 0)
    time.sleep(pause)
    PostMessageW(hwnd, WM_KEYDOWN, key_vk, 0)
    time.sleep(pause)
    PostMessageW(hwnd, WM_KEYUP, key_vk, 0)
    time.sleep(pause)
    PostMessageW(hwnd, WM_KEYUP, modifier_vk, 0)
    return True  # signed: alpha


def ghost_enter(hwnd: int) -> bool:
    """Send Enter key via PostMessage to a specific window."""
    return ghost_key_press(hwnd, VK_RETURN)  # signed: alpha


def ghost_f6(hwnd: int) -> bool:
    """Send F6 key via PostMessage to a specific window."""
    return ghost_key_press(hwnd, VK_F6)  # signed: alpha


def ghost_keybd_press(vk_code: int, pause: float = 0.05) -> bool:
    """Press a key via keybd_event. Goes to the focused window.

    No cursor movement. Works with Chrome/Electron render widgets
    (unlike PostMessage which Chrome may ignore for keyboard input).
    """
    user32.keybd_event(vk_code, 0, 0, 0)
    time.sleep(pause)
    user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)
    return True  # signed: alpha


def ghost_keybd_hotkey(modifier_vk: int, key_vk: int, pause: float = 0.05) -> bool:
    """Press modifier+key combo via keybd_event. Goes to the focused window.

    No cursor movement. Works with Chrome/Electron render widgets.
    """
    user32.keybd_event(modifier_vk, 0, 0, 0)
    time.sleep(pause)
    user32.keybd_event(key_vk, 0, 0, 0)
    time.sleep(pause)
    user32.keybd_event(key_vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(pause)
    user32.keybd_event(modifier_vk, 0, KEYEVENTF_KEYUP, 0)
    return True  # signed: alpha


# ─── CLI Entry Point ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Ghost Mouse — click windows without moving the cursor",
        epilog="NEVER uses pyautogui, SendInput, SetCursorPos, or mouse_event."
    )
    parser.add_argument("--hwnd", type=int, help="Target window handle")
    parser.add_argument("--x", type=int, default=0, help="X coordinate (client)")
    parser.add_argument("--y", type=int, default=0, help="Y coordinate (client)")
    parser.add_argument("--x1", type=int, help="Drag start X")
    parser.add_argument("--y1", type=int, help="Drag start Y")
    parser.add_argument("--x2", type=int, help="Drag end X")
    parser.add_argument("--y2", type=int, help="Drag end Y")
    parser.add_argument("--delta", type=int, default=-120, help="Scroll delta")
    parser.add_argument("--name", type=str, help="UIA element name for invoke")
    parser.add_argument("--action", required=True,
                        choices=["click", "right", "double", "scroll", "drag",
                                 "hover", "invoke", "find-render", "cdp-click"],
                        help="Action to perform")
    parser.add_argument("--cdp-port", type=int, default=9222, help="CDP port")
    parser.add_argument("--tab-id", type=str, help="CDP tab ID")
    parser.add_argument("--selector", type=str, help="CSS selector for CDP click")

    args = parser.parse_args()

    if args.action == "click":
        if not args.hwnd:
            parser.error("--hwnd required for click")
        ok = ghost_click(args.hwnd, args.x, args.y)
        print(json.dumps({"action": "click", "hwnd": args.hwnd,
                          "x": args.x, "y": args.y, "ok": ok}))

    elif args.action == "right":
        if not args.hwnd:
            parser.error("--hwnd required for right-click")
        ok = ghost_right_click(args.hwnd, args.x, args.y)
        print(json.dumps({"action": "right_click", "hwnd": args.hwnd,
                          "x": args.x, "y": args.y, "ok": ok}))

    elif args.action == "double":
        if not args.hwnd:
            parser.error("--hwnd required for double-click")
        ok = ghost_double_click(args.hwnd, args.x, args.y)
        print(json.dumps({"action": "double_click", "hwnd": args.hwnd,
                          "x": args.x, "y": args.y, "ok": ok}))

    elif args.action == "scroll":
        if not args.hwnd:
            parser.error("--hwnd required for scroll")
        ok = ghost_scroll(args.hwnd, args.x, args.y, args.delta)
        print(json.dumps({"action": "scroll", "hwnd": args.hwnd,
                          "delta": args.delta, "ok": ok}))

    elif args.action == "drag":
        if not args.hwnd or args.x1 is None or args.y1 is None or \
           args.x2 is None or args.y2 is None:
            parser.error("--hwnd, --x1, --y1, --x2, --y2 required for drag")
        ok = ghost_drag(args.hwnd, args.x1, args.y1, args.x2, args.y2)
        print(json.dumps({"action": "drag", "hwnd": args.hwnd,
                          "from": [args.x1, args.y1],
                          "to": [args.x2, args.y2], "ok": ok}))

    elif args.action == "hover":
        if not args.hwnd:
            parser.error("--hwnd required for hover")
        ok = ghost_hover(args.hwnd, args.x, args.y)
        print(json.dumps({"action": "hover", "hwnd": args.hwnd,
                          "x": args.x, "y": args.y, "ok": ok}))

    elif args.action == "invoke":
        if not args.hwnd or not args.name:
            parser.error("--hwnd and --name required for invoke")
        ok = invoke_by_name(args.hwnd, args.name)
        print(json.dumps({"action": "invoke", "hwnd": args.hwnd,
                          "name": args.name, "ok": ok}))

    elif args.action == "find-render":
        if not args.hwnd:
            parser.error("--hwnd required for find-render")
        render = find_render_widget(args.hwnd)
        all_renders = find_all_render_widgets(args.hwnd)
        print(json.dumps({"action": "find_render", "parent_hwnd": args.hwnd,
                          "render_hwnd": render,
                          "all_render_hwnds": all_renders,
                          "count": len(all_renders)}))

    elif args.action == "cdp-click":
        if not args.selector or not args.tab_id:
            parser.error("--tab-id and --selector required for cdp-click")
        ok = cdp_click(args.cdp_port, args.tab_id, args.selector)
        print(json.dumps({"action": "cdp_click", "port": args.cdp_port,
                          "selector": args.selector, "ok": ok}))

    sys.exit(0)


if __name__ == "__main__":
    main()
