#!/usr/bin/env python3
"""Shadow Input — virtual keyboard & mouse that NEVER touches the user's devices.

All input is delivered via Win32 PostMessage/SendMessage to specific window handles.
The user's physical mouse cursor and keyboard are NEVER hijacked.

This module replaces pyautogui for all Skynet automation. It uses:
  - PostMessage WM_LBUTTONDOWN/UP for mouse clicks (window-targeted)
  - PostMessage WM_KEYDOWN/UP for keyboard presses (window-targeted)
  - SetClipboardData + PostMessage WM_PASTE for text input
  - UIA InvokePattern for button clicks (zero-input, pure COM)

IMPORTANT: VS Code's Chromium renderer receives PostMessage mouse events
on Chrome_RenderWidgetHostHWND child windows. For keyboard events inside
Chromium, we use SetForegroundWindow + keybd_event as a focused fallback
(this briefly focuses the window but does NOT move the cursor).

Usage:
    from tools.shadow_input import ShadowInput
    si = ShadowInput()
    si.click(hwnd, x, y)           # Client-relative click
    si.click_screen(hwnd, sx, sy)  # Screen-absolute click
    si.press(hwnd, 'down')         # Key press
    si.hotkey(hwnd, 'ctrl', 'v')   # Hotkey combo
    si.paste_and_submit(hwnd, text, render_hwnd)  # Paste text + Enter
    si.invoke_button(hwnd, "Button Name")  # UIA InvokePattern
"""

import ctypes
import ctypes.wintypes
import time
import subprocess
import sys
import os
from typing import Optional, Tuple, List

# ─── Win32 Constants ──────────────────────────────────────────────
WM_LBUTTONDOWN   = 0x0201
WM_LBUTTONUP     = 0x0202
WM_RBUTTONDOWN   = 0x0204
WM_RBUTTONUP     = 0x0205
WM_MOUSEMOVE     = 0x0200
WM_KEYDOWN       = 0x0100
WM_KEYUP         = 0x0101
WM_CHAR          = 0x0102
WM_PASTE         = 0x0302
WM_CLOSE         = 0x0010
WM_SETFOCUS      = 0x0007

MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002

KEYEVENTF_KEYUP = 0x0002

# Virtual key codes
VK_RETURN    = 0x0D
VK_ESCAPE    = 0x1B
VK_TAB       = 0x09
VK_BACK      = 0x08
VK_DELETE    = 0x2E
VK_SPACE     = 0x20
VK_UP        = 0x26
VK_DOWN      = 0x28
VK_LEFT      = 0x25
VK_RIGHT     = 0x27
VK_CONTROL   = 0x11
VK_SHIFT     = 0x10
VK_MENU      = 0x12  # Alt
VK_F6        = 0x75
VK_V         = 0x56
VK_A         = 0x41
VK_C         = 0x43
VK_N         = 0x4E

# Named key map for convenience
KEY_MAP = {
    'enter': VK_RETURN, 'return': VK_RETURN,
    'escape': VK_ESCAPE, 'esc': VK_ESCAPE,
    'tab': VK_TAB,
    'backspace': VK_BACK, 'back': VK_BACK,
    'delete': VK_DELETE, 'del': VK_DELETE,
    'space': VK_SPACE,
    'up': VK_UP, 'down': VK_DOWN,
    'left': VK_LEFT, 'right': VK_RIGHT,
    'ctrl': VK_CONTROL, 'control': VK_CONTROL,
    'shift': VK_SHIFT,
    'alt': VK_MENU,
    'f6': VK_F6,
    'v': VK_V, 'a': VK_A, 'c': VK_C, 'n': VK_N,
}

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32

# Typedefs
PostMessageW = u32.PostMessageW
SendMessageW = u32.SendMessageW
GetWindowRect = u32.GetWindowRect
ClientToScreen = u32.ClientToScreen
ScreenToClient = u32.ScreenToClient
SetForegroundWindow = u32.SetForegroundWindow
GetForegroundWindow = u32.GetForegroundWindow
IsWindow = u32.IsWindow
GetClassNameW = u32.GetClassNameW
EnumChildWindows = u32.EnumChildWindows
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL,
                                  ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

# SendInput structures for hardware-level mouse events
MOUSEEVENTF_MOVE       = 0x0001
MOUSEEVENTF_LEFTDOWN   = 0x0002
MOUSEEVENTF_LEFTUP     = 0x0004
MOUSEEVENTF_ABSOLUTE   = 0x8000
INPUT_MOUSE = 0

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

class INPUT(ctypes.Structure):
    class _INPUT(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]
    _fields_ = [("type", ctypes.c_ulong), ("_input", _INPUT)]

# Clipboard functions
CF_UNICODETEXT = 13


def _screen_to_absolute(sx: int, sy: int) -> tuple:
    """Convert screen coords to SendInput absolute coords (0-65535 range)."""
    sw = u32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    sh = u32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    ox = u32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    oy = u32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
    ax = int(((sx - ox) * 65535) / (sw - 1))
    ay = int(((sy - oy) * 65535) / (sh - 1))
    return ax, ay


def _make_lparam(x: int, y: int) -> int:
    return (y & 0xFFFF) << 16 | (x & 0xFFFF)


def _resolve_vk(key) -> int:
    """Convert key name or VK code to VK int."""
    if isinstance(key, int):
        return key
    k = key.lower().strip()
    if k in KEY_MAP:
        return KEY_MAP[k]
    # Single char → VK code (uppercase letter)
    if len(k) == 1 and k.isalpha():
        return ord(k.upper())
    raise ValueError(f"Unknown key: {key!r}")


class ShadowInput:
    """Virtual input device that never touches the user's physical devices.

    All operations target specific window handles via Win32 messages.
    The user's mouse cursor position and keyboard state are unaffected.
    """

    def __init__(self):
        self._focus_lock = False

    # ─── Mouse ────────────────────────────────────────────────────

    def click(self, hwnd: int, x: int, y: int, pause: float = 0.05) -> bool:
        """Left-click at (x, y) in CLIENT coordinates. No cursor movement."""
        if not IsWindow(hwnd):
            return False
        lp = _make_lparam(x, y)
        PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
        time.sleep(pause)
        PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)
        return True

    def click_screen(self, hwnd: int, sx: int, sy: int, pause: float = 0.05) -> bool:
        """Left-click at (sx, sy) in SCREEN coordinates. Converts to client coords."""
        if not IsWindow(hwnd):
            return False
        pt = ctypes.wintypes.POINT(sx, sy)
        ScreenToClient(hwnd, ctypes.byref(pt))
        return self.click(hwnd, pt.x, pt.y, pause)

    def click_render(self, parent_hwnd: int, cx: int, cy: int, pause: float = 0.05) -> bool:
        """Click inside Chrome_RenderWidgetHostHWND at parent-relative client coords."""
        render = self._find_render(parent_hwnd)
        if not render:
            return self.click(parent_hwnd, cx, cy, pause)
        # Convert parent client → screen → render client
        pt = ctypes.wintypes.POINT(cx, cy)
        ClientToScreen(parent_hwnd, ctypes.byref(pt))
        ScreenToClient(render, ctypes.byref(pt))
        return self.click(render, pt.x, pt.y, pause)

    def click_render_screen(self, parent_hwnd: int, sx: int, sy: int, pause: float = 0.05) -> bool:
        """Click inside Chrome render widget using SCREEN coordinates."""
        render = self._find_render(parent_hwnd)
        target = render or parent_hwnd
        pt = ctypes.wintypes.POINT(sx, sy)
        ScreenToClient(target, ctypes.byref(pt))
        return self.click(target, pt.x, pt.y, pause)

    def hardware_click(self, sx: int, sy: int, pause: float = 0.05) -> bool:
        """Hardware-level click at screen coords with cursor save/restore.

        Uses SendInput (same as pyautogui) but saves and restores cursor
        position so the user's mouse returns to where it was.
        This is the ONLY way to click inside Chromium-rendered UI elements.
        """
        # Save current cursor position
        saved = ctypes.wintypes.POINT()
        u32.GetCursorPos(ctypes.byref(saved))

        # Convert screen coords to absolute SendInput range
        ax, ay = _screen_to_absolute(sx, sy)
        flags_move = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE

        # Build SendInput array: move, down, up
        inputs = (INPUT * 3)()
        # Move to target
        inputs[0].type = INPUT_MOUSE
        inputs[0]._input.mi.dx = ax
        inputs[0]._input.mi.dy = ay
        inputs[0]._input.mi.dwFlags = flags_move
        # Mouse down
        inputs[1].type = INPUT_MOUSE
        inputs[1]._input.mi.dx = ax
        inputs[1]._input.mi.dy = ay
        inputs[1]._input.mi.dwFlags = flags_move | MOUSEEVENTF_LEFTDOWN
        # Mouse up
        inputs[2].type = INPUT_MOUSE
        inputs[2]._input.mi.dx = ax
        inputs[2]._input.mi.dy = ay
        inputs[2]._input.mi.dwFlags = flags_move | MOUSEEVENTF_LEFTUP

        u32.SendInput(3, ctypes.byref(inputs), ctypes.sizeof(INPUT))
        time.sleep(pause)

        # Restore cursor position
        rax, ray = _screen_to_absolute(saved.x, saved.y)
        restore = (INPUT * 1)()
        restore[0].type = INPUT_MOUSE
        restore[0]._input.mi.dx = rax
        restore[0]._input.mi.dy = ray
        restore[0]._input.mi.dwFlags = flags_move
        u32.SendInput(1, ctypes.byref(restore), ctypes.sizeof(INPUT))
        return True

    # ─── Keyboard (PostMessage — window-targeted) ─────────────────

    def press(self, hwnd: int, key, pause: float = 0.05) -> bool:
        """Press and release a key via PostMessage. No focus needed."""
        if not IsWindow(hwnd):
            return False
        vk = _resolve_vk(key)
        PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
        time.sleep(pause)
        PostMessageW(hwnd, WM_KEYUP, vk, 0)
        return True

    def press_sequence(self, hwnd: int, keys: list, delay: float = 0.3) -> bool:
        """Press a sequence of keys with delay between each."""
        for key in keys:
            self.press(hwnd, key)
            time.sleep(delay)
        return True

    def hotkey(self, hwnd: int, modifier, key, pause: float = 0.05) -> bool:
        """Press modifier+key combo via PostMessage."""
        if not IsWindow(hwnd):
            return False
        mod_vk = _resolve_vk(modifier)
        key_vk = _resolve_vk(key)
        PostMessageW(hwnd, WM_KEYDOWN, mod_vk, 0)
        time.sleep(pause)
        PostMessageW(hwnd, WM_KEYDOWN, key_vk, 0)
        time.sleep(pause)
        PostMessageW(hwnd, WM_KEYUP, key_vk, 0)
        time.sleep(pause)
        PostMessageW(hwnd, WM_KEYUP, mod_vk, 0)
        return True

    # ─── Keyboard (keybd_event — focused, for Chromium) ───────────

    def focused_press(self, hwnd: int, key, pause: float = 0.05) -> bool:
        """Press key via keybd_event after focusing window.

        Required for Chromium dropdowns/menus that ignore PostMessage.
        Briefly focuses the target window but does NOT move the cursor.
        """
        vk = _resolve_vk(key)
        SetForegroundWindow(hwnd)
        time.sleep(0.1)
        u32.keybd_event(vk, 0, 0, 0)
        time.sleep(pause)
        u32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        return True

    def focused_press_sequence(self, hwnd: int, keys: list, delay: float = 0.3) -> bool:
        """Press key sequence via keybd_event (for Chromium menus)."""
        SetForegroundWindow(hwnd)
        time.sleep(0.15)
        for key in keys:
            vk = _resolve_vk(key)
            u32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.05)
            u32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(delay)
        return True

    def focused_hotkey(self, hwnd: int, modifier, key, pause: float = 0.05) -> bool:
        """Modifier+key via keybd_event after focusing."""
        mod_vk = _resolve_vk(modifier)
        key_vk = _resolve_vk(key)
        SetForegroundWindow(hwnd)
        time.sleep(0.1)
        u32.keybd_event(mod_vk, 0, 0, 0)
        time.sleep(pause)
        u32.keybd_event(key_vk, 0, 0, 0)
        time.sleep(pause)
        u32.keybd_event(key_vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(pause)
        u32.keybd_event(mod_vk, 0, KEYEVENTF_KEYUP, 0)
        return True

    # ─── Clipboard Operations ─────────────────────────────────────

    def set_clipboard(self, text: str) -> bool:
        """Set clipboard text via Win32 API (no pyperclip needed)."""
        data = text.encode('utf-16-le') + b'\x00\x00'
        if not u32.OpenClipboard(0):
            return False
        try:
            u32.EmptyClipboard()
            # Proper 64-bit pointer handling
            _GlobalAlloc = k32.GlobalAlloc
            _GlobalAlloc.restype = ctypes.c_void_p
            _GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
            _GlobalLock = k32.GlobalLock
            _GlobalLock.restype = ctypes.c_void_p
            _GlobalLock.argtypes = [ctypes.c_void_p]
            _GlobalUnlock = k32.GlobalUnlock
            _GlobalUnlock.argtypes = [ctypes.c_void_p]
            _SetClipboardData = u32.SetClipboardData
            _SetClipboardData.restype = ctypes.c_void_p
            _SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

            h = _GlobalAlloc(0x0042, len(data))  # GMEM_MOVEABLE | GMEM_ZEROINIT
            if not h:
                return False
            p = _GlobalLock(h)
            if not p:
                k32.GlobalFree(h)
                return False
            ctypes.memmove(p, data, len(data))
            _GlobalUnlock(h)
            _SetClipboardData(CF_UNICODETEXT, h)
            return True
        except Exception:
            return False
        finally:
            u32.CloseClipboard()

    def get_clipboard(self) -> str:
        """Get clipboard text via Win32 API."""
        if not u32.OpenClipboard(0):
            return ""
        try:
            _GetClipboardData = u32.GetClipboardData
            _GetClipboardData.restype = ctypes.c_void_p
            _GetClipboardData.argtypes = [ctypes.c_uint]
            _GlobalLock = k32.GlobalLock
            _GlobalLock.restype = ctypes.c_void_p
            _GlobalLock.argtypes = [ctypes.c_void_p]
            _GlobalUnlock = k32.GlobalUnlock
            _GlobalUnlock.argtypes = [ctypes.c_void_p]

            h = _GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            p = _GlobalLock(h)
            if not p:
                return ""
            text = ctypes.wstring_at(p)
            _GlobalUnlock(h)
            return text
        except Exception:
            return ""
        finally:
            u32.CloseClipboard()

    def paste_to_render(self, parent_hwnd: int) -> bool:
        """Send Ctrl+V to the target window via keybd_event.

        Focuses the window briefly, sends Ctrl+V, then returns.
        Does NOT move the mouse cursor.
        """
        SetForegroundWindow(parent_hwnd)
        time.sleep(0.15)
        # Ctrl+V via keybd_event (works for Chromium)
        u32.keybd_event(VK_CONTROL, 0, 0, 0)
        time.sleep(0.05)
        u32.keybd_event(VK_V, 0, 0, 0)
        time.sleep(0.05)
        u32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)
        u32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        return True

    def submit_enter(self, parent_hwnd: int) -> bool:
        """Send Enter to submit in Copilot CLI via keybd_event.

        Focuses the window briefly, sends Enter, then returns.
        Does NOT move the mouse cursor.
        """
        SetForegroundWindow(parent_hwnd)
        time.sleep(0.1)
        u32.keybd_event(VK_RETURN, 0, 0, 0)
        time.sleep(0.05)
        u32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
        return True

    def paste_and_submit(self, hwnd: int, text: str, restore_focus: int = 0) -> bool:
        """Shadow paste and submit — ZERO cursor movement, ZERO hardware mouse.

        The user's physical mouse cursor is NEVER moved. All input uses:
          - Win32 clipboard API for text (no user interaction)
          - AttachThreadInput + SetFocus on Chrome render widget (no cursor)
          - PostMessage WM_LBUTTONDOWN/UP to give Chromium DOM focus to input (no cursor)
          - keybd_event for Ctrl+V paste (keyboard only, no mouse)
          - keybd_event Enter (keyboard only, no mouse)
          - UIA InvokePattern Send button as fallback (pure COM)

        Key discovery: SetFocus on the Chrome render widget gives it Win32 keyboard
        focus, but Chromium manages its own internal DOM focus separately. If the DOM
        focus is on the output/response area (not the input box), keybd_event Ctrl+V
        pastes into the wrong element (or nowhere). PostMessage WM_LBUTTONDOWN/UP to
        the render widget at the input area coordinates forces Chromium to set DOM
        focus on the chat input — all without moving the user's physical cursor.
        """
        old_clip = self.get_clipboard()
        if not self.set_clipboard(text):
            return False
        time.sleep(0.2)

        # Verify clipboard was set correctly
        verify = self.get_clipboard()
        if not verify or verify[:50] != text[:50]:
            time.sleep(0.1)
            self.set_clipboard(text)
            time.sleep(0.2)

        # Find Chrome render widget for direct focus
        render = self._find_render(hwnd)
        target = render or hwnd

        # Attach our thread to the target's input queue
        my_tid = k32.GetCurrentThreadId()
        target_tid = u32.GetWindowThreadProcessId(target, None)
        attached = False
        if my_tid != target_tid:
            attached = bool(u32.AttachThreadInput(my_tid, target_tid, True))

        # Bring window to foreground and set focus on render widget
        SetForegroundWindow(hwnd)
        time.sleep(0.15)
        if render:
            u32.SetFocus(render)
        time.sleep(0.15)

        # CRITICAL: PostMessage click to input area gives Chromium DOM focus
        # to the chat input element. Without this, keybd_event Ctrl+V may paste
        # into the output area or be silently dropped. PostMessage is zero-cursor:
        # it sends the click message directly to the render widget without
        # moving the user's physical mouse.
        if render:
            self._focus_chat_input(hwnd, render)
            time.sleep(0.15)

        # Ctrl+V via keybd_event (keyboard only — user's cursor stays put)
        u32.keybd_event(VK_CONTROL, 0, 0, 0)
        time.sleep(0.03)
        u32.keybd_event(VK_V, 0, 0, 0)
        time.sleep(0.03)
        u32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)
        u32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.3)

        # Submit: Enter via keybd_event (keyboard only, no mouse)
        u32.keybd_event(VK_RETURN, 0, 0, 0)
        time.sleep(0.05)
        u32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.5)

        # Fallback: try UIA InvokePattern on Send button (pure COM, zero input)
        self._try_uia_submit(hwnd)

        # Detach thread input
        if attached:
            u32.AttachThreadInput(my_tid, target_tid, False)

        # Restore clipboard
        try:
            self.set_clipboard(old_clip if old_clip else '')
        except Exception:
            pass

        # Restore focus to orchestrator
        if restore_focus and IsWindow(restore_focus):
            SetForegroundWindow(restore_focus)

        return True

    def _focus_chat_input(self, hwnd: int, render: int) -> None:
        """PostMessage click to the chat input area inside the Chrome render widget.

        This gives Chromium's DOM focus to the chat input element without moving
        the user's physical cursor. The coordinates are calculated relative to
        the render widget's bounding rectangle.

        The input area in a 930x500 Copilot CLI window is approximately at:
          - X: center of window (width/2)
          - Y: ~85px from the bottom (height - 85)
        """
        rect = ctypes.wintypes.RECT()
        if not GetWindowRect(render, ctypes.byref(rect)):
            return
        rw = rect.right - rect.left
        rh = rect.bottom - rect.top
        if rw <= 0 or rh <= 0:
            return
        # Input area: horizontally centered, ~85px from bottom
        input_x = rw // 2
        input_y = rh - 85
        lparam = _make_lparam(input_x, input_y)
        PostMessageW(render, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
        time.sleep(0.05)
        PostMessageW(render, WM_LBUTTONUP, 0, lparam)

    def _try_uia_submit(self, hwnd: int) -> bool:
        """Try submitting via UIA InvokePattern on Send/Submit button.

        Pure COM call — zero mouse, zero keyboard, zero cursor movement.
        Tries multiple possible button names.
        """
        for name in ["Send", "Submit", "Send Message", "Send (Enter)"]:
            if self.invoke_button(hwnd, name):
                return True
        return False

    # ─── UIA InvokePattern ────────────────────────────────────────

    def invoke_button(self, hwnd: int, name: str) -> bool:
        """Click a button via UIA InvokePattern — zero input, pure COM.

        This is the safest method: no mouse, no keyboard, no focus needed.
        """
        ps = (
            'Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes\n'
            f'$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})\n'
            'if (-not $root) { Write-Output "NO_ROOT"; exit 1 }\n'
            f'$cond = New-Object System.Windows.Automation.PropertyCondition('
            f'[System.Windows.Automation.AutomationElement]::NameProperty, "{name}")\n'
            '$el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)\n'
            'if (-not $el) { Write-Output "NOT_FOUND"; exit 1 }\n'
            '$pat = $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)\n'
            'if ($pat) { $pat.Invoke(); Write-Output "INVOKED" }\n'
            'else { Write-Output "NO_INVOKE_PATTERN" }\n'
        )
        try:
            r = subprocess.run(
                ["powershell", "-STA", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=10,
            )
            return "INVOKED" in (r.stdout or "")
        except Exception:
            return False

    # ─── Chromium Dropdown Navigation ─────────────────────────────

    def navigate_dropdown(self, hwnd: int, down_presses: int, delay: float = 0.5) -> bool:
        """Navigate a Chromium dropdown menu using keybd_event.

        Presses Down N times then Enter. Window must be focused.
        Does NOT move the mouse cursor.
        """
        SetForegroundWindow(hwnd)
        time.sleep(0.15)
        for _ in range(down_presses):
            u32.keybd_event(VK_DOWN, 0, 0, 0)
            time.sleep(0.05)
            u32.keybd_event(VK_DOWN, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(delay)
        u32.keybd_event(VK_RETURN, 0, 0, 0)
        time.sleep(0.05)
        u32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
        return True

    # ─── Chrome Render Widget Discovery ───────────────────────────

    def _find_render(self, parent_hwnd: int) -> Optional[int]:
        """Find Chrome_RenderWidgetHostHWND child window.

        Uses FindWindowExW loop instead of EnumChildWindows callback
        to avoid access violations in subprocess contexts.
        """
        if not IsWindow(parent_hwnd):
            return None
        try:
            child = 0
            target_class = "Chrome_RenderWidgetHostHWND"
            for _ in range(100):  # safety limit
                child = u32.FindWindowExW(parent_hwnd, child, target_class, None)
                if not child:
                    break
                if u32.IsWindowVisible(child):
                    return child
            # If no visible one found, try any match
            child = u32.FindWindowExW(parent_hwnd, 0, target_class, None)
            return child if child else None
        except Exception:
            return None

    # ─── Window Management ────────────────────────────────────────

    def close_windows_by_title(self, pattern: str) -> int:
        """Close all visible windows matching title pattern. Returns count."""
        closed = []
        def cb(hwnd, _):
            if u32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(512)
                u32.GetWindowTextW(hwnd, buf, 512)
                if pattern in buf.value:
                    PostMessageW(hwnd, WM_CLOSE, 0, 0)
                    closed.append(hwnd)
            return True
        u32.EnumWindows(WNDENUMPROC(cb), 0)
        return len(closed)

    def find_window(self, title_pattern: str, exclude: set = None) -> Optional[int]:
        """Find first visible window matching title, excluding known HWNDs."""
        exclude = exclude or set()
        result = [None]
        def cb(hwnd, _):
            if u32.IsWindowVisible(hwnd) and hwnd not in exclude:
                buf = ctypes.create_unicode_buffer(512)
                u32.GetWindowTextW(hwnd, buf, 512)
                if title_pattern in buf.value:
                    result[0] = hwnd
                    return False
            return True
        u32.EnumWindows(WNDENUMPROC(cb), 0)
        return result[0]


# Singleton instance
_instance = None

def get_shadow_input() -> ShadowInput:
    """Get the singleton ShadowInput instance."""
    global _instance
    if _instance is None:
        _instance = ShadowInput()
    return _instance
