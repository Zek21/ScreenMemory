"""
Chrome Bridge — Windows Controller (winctl)
Full Windows desktop automation: screen capture, window management,
UI Automation, virtual input, process control, clipboard, and CDP integration.

All input is API-level (SendInput/UIAutomation patterns) — zero physical mouse movement
unless explicitly requested.

Usage:
    from winctl import Desktop

    desk = Desktop()

    # Screen
    desk.screenshot('screenshots/screen.png')          # full screen
    desk.screenshot('screenshots/window.png', window='Chrome')
    desk.screenshot('screenshots/region.png', region=(0,0,800,600))

    # Windows
    windows = desk.windows()                         # list all
    desk.focus('Chrome')                              # bring to front
    desk.minimize('Notepad')
    desk.maximize('Chrome')
    desk.resize('Chrome', 1920, 1080)
    desk.move('Chrome', 0, 0)
    desk.close('Notepad')

    # UI Automation (no mouse)
    elements = desk.find_elements('Chrome', name='Submit')
    desk.click_element('Chrome', name='Submit')      # InvokePattern, no mouse
    desk.set_value('Chrome', name='Search', value='hello')
    desk.toggle('Chrome', name='Developer mode')
    tree = desk.ui_tree('Chrome', depth=3)

    # Virtual keyboard (SendInput)
    desk.type_text('Hello World')
    desk.press_key('enter')
    desk.hotkey('ctrl', 'c')

    # Processes
    procs = desk.processes(name='chrome')
    desk.launch('notepad.exe')
    desk.kill(pid=1234)

    # Clipboard
    desk.clip_set('hello')
    text = desk.clip_get()

    # CDP (Chrome DevTools Protocol)
    chrome = desk.chrome(port=9222)
    chrome.eval(tab_id, 'document.title')
"""

import ctypes
import ctypes.wintypes
import json
import os
import sys
import time
import subprocess
import base64
import struct
import io
import re
import threading
from collections import defaultdict

# ─── Win32 API Bindings ─────────────────────────────────────

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32
shell32 = ctypes.windll.shell32

# Window functions
user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM), ctypes.wintypes.LPARAM]
user32.GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [ctypes.wintypes.HWND]
user32.IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
user32.GetClassNameW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
user32.ShowWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
user32.MoveWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.wintypes.BOOL]
user32.GetWindowRect.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.RECT)]
user32.GetForegroundWindow.argtypes = []
user32.PostMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.c_uint, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
user32.SendMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.c_uint, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetDC.argtypes = [ctypes.wintypes.HWND]
user32.ReleaseDC.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HDC]
user32.GetDesktopWindow.argtypes = []
user32.PrintWindow.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HDC, ctypes.c_uint]

# SendInput structures
INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort), ("wParamH", ctypes.c_ushort)]

class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", INPUT_UNION)]

user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = ctypes.c_uint

# Virtual key codes
VK_MAP = {
    'enter': 0x0D, 'return': 0x0D, 'tab': 0x09, 'escape': 0x1B, 'esc': 0x1B,
    'backspace': 0x08, 'delete': 0x2E, 'insert': 0x2D,
    'up': 0x26, 'down': 0x28, 'left': 0x25, 'right': 0x27,
    'home': 0x24, 'end': 0x23, 'pageup': 0x21, 'pagedown': 0x22,
    'space': 0x20, 'ctrl': 0x11, 'control': 0x11, 'alt': 0x12, 'shift': 0x10,
    'win': 0x5B, 'windows': 0x5B, 'lwin': 0x5B, 'rwin': 0x5C,
    'f1': 0x70, 'f2': 0x71, 'f3': 0x72, 'f4': 0x73, 'f5': 0x74, 'f6': 0x75,
    'f7': 0x76, 'f8': 0x77, 'f9': 0x78, 'f10': 0x79, 'f11': 0x7A, 'f12': 0x7B,
    'capslock': 0x14, 'numlock': 0x90, 'scrolllock': 0x91,
    'printscreen': 0x2C, 'prtsc': 0x2C, 'pause': 0x13,
    'apps': 0x5D, 'menu': 0x5D,
    'a': 0x41, 'b': 0x42, 'c': 0x43, 'd': 0x44, 'e': 0x45, 'f': 0x46,
    'g': 0x47, 'h': 0x48, 'i': 0x49, 'j': 0x4A, 'k': 0x4B, 'l': 0x4C,
    'm': 0x4D, 'n': 0x4E, 'o': 0x4F, 'p': 0x50, 'q': 0x51, 'r': 0x52,
    's': 0x53, 't': 0x54, 'u': 0x55, 'v': 0x56, 'w': 0x57, 'x': 0x58,
    'y': 0x59, 'z': 0x5A,
    '0': 0x30, '1': 0x31, '2': 0x32, '3': 0x33, '4': 0x34,
    '5': 0x35, '6': 0x36, '7': 0x37, '8': 0x38, '9': 0x39,
}

# ShowWindow constants
SW_HIDE = 0
SW_NORMAL = 1
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9
WM_CLOSE = 0x0010

# Screenshot constants
SRCCOPY = 0x00CC0020
BI_RGB = 0
DIB_RGB_COLORS = 0
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
PW_CLIENTONLY = 1
PW_RENDERFULLCONTENT = 2


# ─── BMP/PNG helpers ────────────────────────────────────────

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ('biSize', ctypes.c_uint), ('biWidth', ctypes.c_long), ('biHeight', ctypes.c_long),
        ('biPlanes', ctypes.c_ushort), ('biBitCount', ctypes.c_ushort), ('biCompression', ctypes.c_uint),
        ('biSizeImage', ctypes.c_uint), ('biXPelsPerMeter', ctypes.c_long), ('biYPelsPerMeter', ctypes.c_long),
        ('biClrUsed', ctypes.c_uint), ('biClrImportant', ctypes.c_uint),
    ]

class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', ctypes.c_ulong * 3)]


def _capture_screen(region=None):
    """Capture screen region as raw BGRA bytes + dimensions."""
    if region:
        x, y, w, h = region
    else:
        x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    old = gdi32.SelectObject(hdc_mem, hbmp)
    gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y, SRCCOPY)
    gdi32.SelectObject(hdc_mem, old)

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB

    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)

    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(0, hdc_screen)

    return bytes(buf), w, h


def _capture_window(hwnd):
    """Capture a specific window as raw BGRA bytes."""
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None, 0, 0

    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    old = gdi32.SelectObject(hdc_mem, hbmp)

    user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB

    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)

    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(0, hdc_screen)

    return bytes(buf), w, h


def _save_bmp(raw_bgra, w, h, path):
    """Save raw BGRA as BMP file."""
    row_size = w * 4
    pixel_size = row_size * h
    file_size = 54 + pixel_size

    with open(path, 'wb') as f:
        # BMP header
        f.write(b'BM')
        f.write(struct.pack('<I', file_size))
        f.write(struct.pack('<HH', 0, 0))
        f.write(struct.pack('<I', 54))
        # DIB header
        f.write(struct.pack('<I', 40))
        f.write(struct.pack('<i', w))
        f.write(struct.pack('<i', h))  # positive = bottom-up for BMP
        f.write(struct.pack('<HH', 1, 32))
        f.write(struct.pack('<I', 0))
        f.write(struct.pack('<I', pixel_size))
        f.write(struct.pack('<ii', 0, 0))
        f.write(struct.pack('<II', 0, 0))
        # Flip rows (our data is top-down, BMP wants bottom-up)
        for y_idx in range(h - 1, -1, -1):
            f.write(raw_bgra[y_idx * row_size:(y_idx + 1) * row_size])


def _bgra_to_png_bytes(raw_bgra, w, h):
    """Convert BGRA to PNG using pure Python (no PIL needed)."""
    try:
        from PIL import Image
        img = Image.frombytes('RGBA', (w, h), raw_bgra, 'raw', 'BGRA')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    except ImportError:
        pass

    # Fallback: save as BMP
    return None


def _save_image(raw_bgra, w, h, path):
    """Save screenshot to file (PNG if PIL available, BMP otherwise)."""
    if path.lower().endswith('.png'):
        png_data = _bgra_to_png_bytes(raw_bgra, w, h)
        if png_data:
            with open(path, 'wb') as f:
                f.write(png_data)
            return
        # Fallback to BMP with .png extension warning
        path = path[:-4] + '.bmp'
        print(f'  (PIL not installed, saving as {os.path.basename(path)})')

    _save_bmp(raw_bgra, w, h, path)


# ─── UIAutomation via COM ───────────────────────────────────

_uia = None
_uia_available = False

def _init_uia():
    """Initialize UIAutomation COM interface."""
    global _uia, _uia_available
    if _uia is not None:
        return _uia_available
    try:
        import comtypes
        import comtypes.client
        _uia = comtypes.client.CreateObject('{ff48dba4-60ef-4201-aa87-54103eef594e}')
        _uia_available = True
    except Exception:
        try:
            # Fallback: use .NET UIAutomation via pythonnet
            import clr
            clr.AddReference('UIAutomationClient')
            clr.AddReference('UIAutomationTypes')
            from System.Windows.Automation import AutomationElement
            _uia = AutomationElement
            _uia_available = True
        except Exception:
            _uia_available = False
    return _uia_available


# ─── Desktop Controller ─────────────────────────────────────

class Desktop:
    """
    Windows Desktop automation controller.
    All operations are API-level — zero physical mouse/keyboard interference
    unless explicitly using move_mouse/physical_click.
    """

    def __init__(self):
        self._cdp = None

    # ─── Window Discovery ──────────────────────────────

    def windows(self, visible_only=True):
        """List all windows with their properties."""
        results = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def enum_cb(hwnd, lparam):
            if visible_only and not user32.IsWindowVisible(hwnd):
                return True
            title_len = user32.GetWindowTextLengthW(hwnd)
            if title_len == 0:
                return True
            buf = ctypes.create_unicode_buffer(title_len + 1)
            user32.GetWindowTextW(hwnd, buf, title_len + 1)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))

            results.append({
                'hwnd': hwnd,
                'title': buf.value,
                'class': cls.value,
                'x': rect.left, 'y': rect.top,
                'width': rect.right - rect.left,
                'height': rect.bottom - rect.top,
                'pid': self._get_window_pid(hwnd),
            })
            return True

        user32.EnumWindows(enum_cb, 0)
        return results

    def find_window(self, title_match):
        """Find window by title substring. Returns hwnd."""
        if isinstance(title_match, int):
            return title_match
        for w in self.windows():
            if title_match.lower() in w['title'].lower():
                return w['hwnd']
        return None

    def _get_window_pid(self, hwnd):
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    # ─── Window Management ─────────────────────────────

    def focus(self, window):
        """Bring window to foreground. No mouse movement."""
        hwnd = self.find_window(window) if isinstance(window, str) else window
        if not hwnd:
            raise ValueError(f'Window not found: {window}')
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        return True

    def minimize(self, window):
        hwnd = self.find_window(window) if isinstance(window, str) else window
        if hwnd:
            user32.ShowWindow(hwnd, SW_MINIMIZE)

    def maximize(self, window):
        hwnd = self.find_window(window) if isinstance(window, str) else window
        if hwnd:
            user32.ShowWindow(hwnd, SW_MAXIMIZE)

    def restore(self, window):
        hwnd = self.find_window(window) if isinstance(window, str) else window
        if hwnd:
            user32.ShowWindow(hwnd, SW_RESTORE)

    def close(self, window):
        """Close a window via WM_CLOSE (graceful, no mouse)."""
        hwnd = self.find_window(window) if isinstance(window, str) else window
        if hwnd:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

    def resize(self, window, width, height):
        hwnd = self.find_window(window) if isinstance(window, str) else window
        if not hwnd:
            return
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        user32.MoveWindow(hwnd, rect.left, rect.top, width, height, True)

    def move(self, window, x, y):
        hwnd = self.find_window(window) if isinstance(window, str) else window
        if not hwnd:
            return
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        user32.MoveWindow(hwnd, x, y, w, h, True)

    def get_rect(self, window):
        hwnd = self.find_window(window) if isinstance(window, str) else window
        if not hwnd:
            return None
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return {'x': rect.left, 'y': rect.top,
                'width': rect.right - rect.left, 'height': rect.bottom - rect.top}

    def foreground(self):
        """Get the currently focused window."""
        hwnd = user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        return {'hwnd': hwnd, 'title': buf.value}

    # ─── Screen Capture ────────────────────────────────

    def screenshot(self, path=None, window=None, region=None):
        if path is None:
            path = os.path.join('screenshots', 'screenshot.png')
            os.makedirs('screenshots', exist_ok=True)
        """
        Take screenshot.
        - No args: full virtual screen (all monitors)
        - window='Chrome': capture specific window
        - region=(x, y, w, h): capture screen region
        """
        if window:
            hwnd = self.find_window(window) if isinstance(window, str) else window
            if not hwnd:
                raise ValueError(f'Window not found: {window}')
            raw, w, h = _capture_window(hwnd)
        else:
            raw, w, h = _capture_screen(region)

        if not raw or w <= 0 or h <= 0:
            raise RuntimeError('Screenshot capture failed')

        _save_image(raw, w, h, path)
        return {'path': path, 'width': w, 'height': h, 'size': os.path.getsize(path)}

    def screenshot_base64(self, window=None, region=None):
        """Take screenshot and return as base64 PNG/BMP."""
        if window:
            hwnd = self.find_window(window) if isinstance(window, str) else window
            raw, w, h = _capture_window(hwnd)
        else:
            raw, w, h = _capture_screen(region)

        png = _bgra_to_png_bytes(raw, w, h)
        if png:
            return base64.b64encode(png).decode(), 'image/png'

        # BMP fallback
        buf = io.BytesIO()
        row_size = w * 4
        pixel_size = row_size * h
        file_size = 54 + pixel_size
        buf.write(b'BM')
        buf.write(struct.pack('<I', file_size))
        buf.write(struct.pack('<HH', 0, 0))
        buf.write(struct.pack('<I', 54))
        buf.write(struct.pack('<I', 40))
        buf.write(struct.pack('<i', w))
        buf.write(struct.pack('<i', h))
        buf.write(struct.pack('<HH', 1, 32))
        buf.write(struct.pack('<I', 0))
        buf.write(struct.pack('<I', pixel_size))
        buf.write(struct.pack('<ii', 0, 0))
        buf.write(struct.pack('<II', 0, 0))
        for y_idx in range(h - 1, -1, -1):
            buf.write(raw[y_idx * row_size:(y_idx + 1) * row_size])
        return base64.b64encode(buf.getvalue()).decode(), 'image/bmp'

    def screen_size(self):
        """Get virtual screen dimensions (all monitors)."""
        return {
            'x': user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
            'y': user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            'width': user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            'height': user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
            'primary_width': user32.GetSystemMetrics(SM_CXSCREEN),
            'primary_height': user32.GetSystemMetrics(SM_CYSCREEN),
        }

    # ─── Virtual Keyboard (SendInput — no physical key) ─

    def type_text(self, text, interval=0):
        """Type text using SendInput UNICODE events. No physical keyboard."""
        for char in text:
            inputs = (INPUT * 2)()
            # Key down
            inputs[0].type = INPUT_KEYBOARD
            inputs[0].union.ki.wScan = ord(char)
            inputs[0].union.ki.dwFlags = KEYEVENTF_UNICODE
            # Key up
            inputs[1].type = INPUT_KEYBOARD
            inputs[1].union.ki.wScan = ord(char)
            inputs[1].union.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
            user32.SendInput(2, inputs, ctypes.sizeof(INPUT))
            if interval:
                time.sleep(interval)

    def press_key(self, key):
        """Press and release a key. Use VK names: 'enter', 'tab', 'f5', etc."""
        vk = VK_MAP.get(key.lower(), 0)
        if not vk:
            if len(key) == 1:
                vk = ord(key.upper())
            else:
                raise ValueError(f'Unknown key: {key}. Available: {", ".join(sorted(VK_MAP.keys()))}')

        inputs = (INPUT * 2)()
        inputs[0].type = INPUT_KEYBOARD
        inputs[0].union.ki.wVk = vk
        inputs[1].type = INPUT_KEYBOARD
        inputs[1].union.ki.wVk = vk
        inputs[1].union.ki.dwFlags = KEYEVENTF_KEYUP
        user32.SendInput(2, inputs, ctypes.sizeof(INPUT))

    def hotkey(self, *keys):
        """Press a key combination. E.g. hotkey('ctrl', 'shift', 't')"""
        vks = []
        for k in keys:
            vk = VK_MAP.get(k.lower(), 0)
            if not vk:
                vk = ord(k.upper()) if len(k) == 1 else 0
            if not vk:
                raise ValueError(f'Unknown key: {k}')
            vks.append(vk)

        n = len(vks)
        inputs = (INPUT * (n * 2))()
        # Press all keys
        for i, vk in enumerate(vks):
            inputs[i].type = INPUT_KEYBOARD
            inputs[i].union.ki.wVk = vk
        # Release all keys (reverse order)
        for i, vk in enumerate(reversed(vks)):
            inputs[n + i].type = INPUT_KEYBOARD
            inputs[n + i].union.ki.wVk = vk
            inputs[n + i].union.ki.dwFlags = KEYEVENTF_KEYUP
        user32.SendInput(n * 2, inputs, ctypes.sizeof(INPUT))

    def key_down(self, key):
        vk = VK_MAP.get(key.lower(), ord(key.upper()) if len(key) == 1 else 0)
        inp = (INPUT * 1)()
        inp[0].type = INPUT_KEYBOARD
        inp[0].union.ki.wVk = vk
        user32.SendInput(1, inp, ctypes.sizeof(INPUT))

    def key_up(self, key):
        vk = VK_MAP.get(key.lower(), ord(key.upper()) if len(key) == 1 else 0)
        inp = (INPUT * 1)()
        inp[0].type = INPUT_KEYBOARD
        inp[0].union.ki.wVk = vk
        inp[0].union.ki.dwFlags = KEYEVENTF_KEYUP
        user32.SendInput(1, inp, ctypes.sizeof(INPUT))

    # ─── Clipboard ─────────────────────────────────────

    def clip_set(self, text):
        """Set clipboard text. No mouse/keyboard."""
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

        data = text.encode('utf-16-le') + b'\x00\x00'
        user32.OpenClipboard(0)
        user32.EmptyClipboard()
        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        ptr = kernel32.GlobalLock(h_mem)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(h_mem)
        user32.SetClipboardData(CF_UNICODETEXT, h_mem)
        user32.CloseClipboard()

    def clip_get(self):
        """Get clipboard text. No mouse/keyboard."""
        CF_UNICODETEXT = 13
        user32.GetClipboardData.restype = ctypes.c_void_p
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]

        user32.OpenClipboard(0)
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            user32.CloseClipboard()
            return ''
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            user32.CloseClipboard()
            return ''
        text = ctypes.wstring_at(ptr)
        kernel32.GlobalUnlock(handle)
        user32.CloseClipboard()
        return text

    def clip_paste(self, text):
        """Set clipboard and paste (Ctrl+V). Combines clip_set + hotkey."""
        self.clip_set(text)
        time.sleep(0.05)
        self.hotkey('ctrl', 'v')

    # ─── Process Management ────────────────────────────

    def processes(self, name=None):
        """List processes. Optionally filter by name."""
        try:
            out = subprocess.check_output(
                ['powershell', '-NoProfile', '-Command',
                 'Get-Process | Select-Object Id,ProcessName,MainWindowTitle,WorkingSet64 | ConvertTo-Json -Compress'],
                text=True, timeout=10, stderr=subprocess.DEVNULL
            )
            procs = json.loads(out)
            if isinstance(procs, dict):
                procs = [procs]
            if name:
                name_lower = name.lower()
                procs = [p for p in procs if name_lower in p.get('ProcessName', '').lower()]
            return procs
        except Exception:
            return []

    def launch(self, command, *args, wait=False, shell=False, cwd=None):
        """Launch a process."""
        cmd = [command] + list(args)
        if wait:
            result = subprocess.run(cmd, capture_output=True, text=True, shell=shell, cwd=cwd, timeout=300)
            return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
        else:
            proc = subprocess.Popen(cmd, shell=shell, cwd=cwd,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {'pid': proc.pid}

    def kill(self, pid):
        """Kill process by PID."""
        try:
            subprocess.run(['powershell', '-NoProfile', '-Command',
                          f'Stop-Process -Id {pid} -Force'], timeout=10,
                         capture_output=True)
            return True
        except Exception:
            return False

    def kill_name(self, name):
        """Kill processes by name (careful!)."""
        procs = self.processes(name)
        killed = []
        for p in procs:
            if self.kill(p['Id']):
                killed.append(p['Id'])
        return killed

    # ─── UI Automation (accessible elements) ───────────

    def ui_tree(self, window, depth=2, max_children=50):
        """
        Get UI automation tree of a window.
        Uses PowerShell + .NET UIAutomation (no external dependencies).
        """
        hwnd = self.find_window(window) if isinstance(window, str) else window
        if not hwnd:
            raise ValueError(f'Window not found: {window}')

        ps_script = f'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})

function Get-Tree($el, $depth, $maxD, $maxC) {{
    $node = @{{
        name = $el.Current.Name
        type = $el.Current.ControlType.ProgrammaticName -replace 'ControlType\\.',''
        id = $el.Current.AutomationId
        cls = $el.Current.ClassName
        enabled = $el.Current.IsEnabled
    }}
    $r = $el.Current.BoundingRectangle
    if (-not [double]::IsInfinity($r.X)) {{
        $node.rect = @{{ x=[int]$r.X; y=[int]$r.Y; w=[int]$r.Width; h=[int]$r.Height }}
    }}
    if ($depth -lt $maxD) {{
        $children = $el.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
        $node.children = @()
        $count = 0
        foreach ($c in $children) {{
            if ($count -ge $maxC) {{ break }}
            $node.children += (Get-Tree $c ($depth+1) $maxD $maxC)
            $count++
        }}
    }}
    return $node
}}

$tree = Get-Tree $root 0 {depth} {max_children}
$tree | ConvertTo-Json -Depth 20 -Compress
'''
        try:
            result = subprocess.run(
                ['powershell', '-NoProfile', '-Command', ps_script],
                capture_output=True, text=True, timeout=15
            )
            if result.stdout.strip():
                return json.loads(result.stdout)
            return {'error': result.stderr.strip()[:500]}
        except Exception as e:
            return {'error': str(e)}

    def find_elements(self, window, name=None, type=None, id=None, cls=None):
        """Find UI elements in a window by properties."""
        hwnd = self.find_window(window) if isinstance(window, str) else window
        if not hwnd:
            return []

        conditions = []
        if name:
            conditions.append(f'$el.Current.Name -like "*{name}*"')
        if type:
            conditions.append(f'$el.Current.ControlType.ProgrammaticName -match "{type}"')
        if id:
            conditions.append(f'$el.Current.AutomationId -eq "{id}"')
        if cls:
            conditions.append(f'$el.Current.ClassName -eq "{cls}"')

        filter_expr = ' -and '.join(conditions) if conditions else '$true'

        ps_script = f'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
$all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
$results = @()
foreach ($el in $all) {{
    if ({filter_expr}) {{
        $r = $el.Current.BoundingRectangle
        $results += @{{
            name = $el.Current.Name
            type = $el.Current.ControlType.ProgrammaticName -replace 'ControlType\\.',''
            id = $el.Current.AutomationId
            cls = $el.Current.ClassName
            enabled = $el.Current.IsEnabled
            rect = if (-not [double]::IsInfinity($r.X)) {{ @{{ x=[int]$r.X; y=[int]$r.Y; w=[int]$r.Width; h=[int]$r.Height }} }} else {{ $null }}
        }}
        if ($results.Count -ge 50) {{ break }}
    }}
}}
$results | ConvertTo-Json -Compress
'''
        try:
            result = subprocess.run(
                ['powershell', '-NoProfile', '-Command', ps_script],
                capture_output=True, text=True, timeout=15
            )
            if result.stdout.strip():
                data = json.loads(result.stdout)
                return data if isinstance(data, list) else [data]
            return []
        except Exception:
            return []

    def click_element(self, window, name=None, id=None, type=None):
        """
        Click a UI element using InvokePattern (no mouse!).
        Falls back to expanding/toggling if invoke isn't available.
        """
        hwnd = self.find_window(window) if isinstance(window, str) else window
        if not hwnd:
            raise ValueError(f'Window not found: {window}')

        find_by = ''
        if id:
            find_by = f'$el.Current.AutomationId -eq "{id}"'
        elif name:
            find_by = f'$el.Current.Name -like "*{name}*"'
        elif type:
            find_by = f'$el.Current.ControlType.ProgrammaticName -match "{type}"'
        else:
            raise ValueError('Provide name, id, or type')

        ps_script = f'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
$all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
foreach ($el in $all) {{
    if ({find_by}) {{
        try {{
            $ip = $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
            $ip.Invoke()
            Write-Host "invoked:$($el.Current.Name)"
            exit 0
        }} catch {{}}
        try {{
            $tp = $el.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
            $tp.Toggle()
            Write-Host "toggled:$($el.Current.Name)"
            exit 0
        }} catch {{}}
        try {{
            $ep = $el.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
            $ep.Expand()
            Write-Host "expanded:$($el.Current.Name)"
            exit 0
        }} catch {{}}
        try {{
            $sp = $el.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
            $sp.Select()
            Write-Host "selected:$($el.Current.Name)"
            exit 0
        }} catch {{}}
        Write-Host "no_pattern:$($el.Current.Name)"
        exit 1
    }}
}}
Write-Host "not_found"
exit 2
'''
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps_script],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout.strip()
        if output.startswith('invoked:') or output.startswith('toggled:') or \
           output.startswith('expanded:') or output.startswith('selected:'):
            return {'action': output.split(':')[0], 'element': output.split(':', 1)[1]}
        raise RuntimeError(f'Click failed: {output}')

    def set_value(self, window, name=None, id=None, value=''):
        """Set value on a UI element using ValuePattern (no mouse/keyboard)."""
        hwnd = self.find_window(window) if isinstance(window, str) else window
        if not hwnd:
            raise ValueError(f'Window not found: {window}')

        find_by = ''
        if id:
            find_by = f'$el.Current.AutomationId -eq "{id}"'
        elif name:
            find_by = f'$el.Current.Name -like "*{name}*"'

        value_escaped = value.replace("'", "''").replace('"', '`"')

        ps_script = f'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
$all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
foreach ($el in $all) {{
    if ({find_by}) {{
        try {{
            $vp = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
            $vp.SetValue("{value_escaped}")
            Write-Host "set:$($el.Current.Name)"
            exit 0
        }} catch {{
            Write-Host "error:$($_.Exception.Message)"
            exit 1
        }}
    }}
}}
Write-Host "not_found"
exit 2
'''
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps_script],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip()

    def toggle(self, window, name=None, id=None):
        """Toggle a checkbox/switch using TogglePattern (no mouse)."""
        return self.click_element(window, name=name, id=id)

    # ─── File Dialog Helper ────────────────────────────

    def fill_file_dialog(self, path, dialog_title=None):
        """
        Fill a file open/save/folder dialog with a path and click OK.
        Uses keyboard shortcuts — no mouse.
        """
        time.sleep(0.5)
        # Alt+D focuses the address bar in Explorer dialogs
        self.hotkey('alt', 'd')
        time.sleep(0.3)
        self.hotkey('ctrl', 'a')
        time.sleep(0.1)
        self.type_text(path)
        time.sleep(0.2)
        self.press_key('enter')
        time.sleep(1)
        # For folder picker, press Alt+S (Select Folder shortcut)
        self.hotkey('alt', 's')
        time.sleep(0.3)
        # Fallback: press Enter
        self.press_key('enter')

    # ─── System Info ───────────────────────────────────

    def monitors(self):
        """Get monitor information."""
        try:
            out = subprocess.check_output(
                ['powershell', '-NoProfile', '-Command',
                 'Add-Type -AssemblyName System.Windows.Forms; '
                 '[System.Windows.Forms.Screen]::AllScreens | ForEach-Object { '
                 '@{Name=$_.DeviceName; Primary=$_.Primary; '
                 'X=$_.Bounds.X; Y=$_.Bounds.Y; '
                 'Width=$_.Bounds.Width; Height=$_.Bounds.Height} '
                 '} | ConvertTo-Json -Compress'],
                text=True, timeout=5
            )
            data = json.loads(out)
            return data if isinstance(data, list) else [data]
        except Exception:
            return []

    def cursor_pos(self):
        """Get current cursor position (read-only, doesn't move it)."""
        point = ctypes.wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        return {'x': point.x, 'y': point.y}

    # ─── CDP Integration ───────────────────────────────

    def chrome(self, port=9222):
        """Get CDP Chrome controller."""
        if self._cdp:
            return self._cdp
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from cdp import CDP
            self._cdp = CDP(port=port)
            return self._cdp
        except Exception as e:
            raise RuntimeError(f'CDP connection failed: {e}')

    def chrome_launch(self, port=9222, url=None, headless=False):
        """Launch Chrome with remote debugging."""
        from cdp import CDP
        self._cdp = CDP.launch(port=port, headless=headless)
        if url:
            tabs = self._cdp.tabs()
            if tabs:
                self._cdp.navigate(tabs[0]['id'], url)
        return self._cdp

    # ─── Hub Integration ───────────────────────────────

    def bridge(self):
        """Get Chrome Bridge hub connection."""
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from bridge import Hub
        return Hub()

    # ─── Wait Utilities ────────────────────────────────

    def wait(self, seconds):
        time.sleep(seconds)

    def wait_for_window(self, title, timeout=30):
        """Wait for a window to appear."""
        start = time.time()
        while time.time() - start < timeout:
            hwnd = self.find_window(title)
            if hwnd:
                return hwnd
            time.sleep(0.5)
        raise TimeoutError(f'Window not found: {title}')

    def wait_for_window_gone(self, title, timeout=30):
        """Wait for a window to close."""
        start = time.time()
        while time.time() - start < timeout:
            hwnd = self.find_window(title)
            if not hwnd:
                return True
            time.sleep(0.5)
        raise TimeoutError(f'Window still present: {title}')


# ─── CLI Entry Point ────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(prog='winctl', description='Windows Desktop Controller')
    sub = parser.add_subparsers(dest='command')

    sub.add_parser('windows', help='List windows')
    sub.add_parser('screen', help='Screen info')
    sub.add_parser('monitors', help='Monitor info')
    sub.add_parser('foreground', help='Foreground window')

    p_shot = sub.add_parser('screenshot', help='Take screenshot')
    p_shot.add_argument('--output', '-o', default='screenshot.bmp')
    p_shot.add_argument('--window', '-w', default=None)

    p_focus = sub.add_parser('focus', help='Focus window')
    p_focus.add_argument('title')

    p_close = sub.add_parser('close', help='Close window')
    p_close.add_argument('title')

    p_tree = sub.add_parser('tree', help='UI automation tree')
    p_tree.add_argument('title')
    p_tree.add_argument('--depth', '-d', type=int, default=2)

    p_find = sub.add_parser('find', help='Find UI elements')
    p_find.add_argument('title')
    p_find.add_argument('--name', '-n', default=None)
    p_find.add_argument('--id', default=None)
    p_find.add_argument('--type', '-t', default=None)

    p_click = sub.add_parser('click', help='Click UI element')
    p_click.add_argument('title')
    p_click.add_argument('--name', '-n', default=None)
    p_click.add_argument('--id', default=None)

    p_type = sub.add_parser('type', help='Type text')
    p_type.add_argument('text')

    p_key = sub.add_parser('key', help='Press key')
    p_key.add_argument('key')

    p_hotkey = sub.add_parser('hotkey', help='Press key combination')
    p_hotkey.add_argument('keys', nargs='+')

    p_clip = sub.add_parser('clip', help='Clipboard operations')
    p_clip.add_argument('action', choices=['get', 'set'])
    p_clip.add_argument('text', nargs='?', default='')

    p_procs = sub.add_parser('procs', help='List processes')
    p_procs.add_argument('--name', '-n', default=None)

    p_launch = sub.add_parser('launch', help='Launch process')
    p_launch.add_argument('cmd', nargs='+')

    p_kill = sub.add_parser('kill', help='Kill process')
    p_kill.add_argument('pid', type=int)

    args = parser.parse_args()
    desk = Desktop()

    if args.command == 'windows':
        for w in desk.windows():
            print(f"  [{w['hwnd']:>8}] {w['title'][:60]:60s} {w['class'][:25]}")
    elif args.command == 'screen':
        print(json.dumps(desk.screen_size(), indent=2))
    elif args.command == 'monitors':
        print(json.dumps(desk.monitors(), indent=2))
    elif args.command == 'foreground':
        print(json.dumps(desk.foreground(), indent=2))
    elif args.command == 'screenshot':
        r = desk.screenshot(args.output, window=args.window)
        print(f"Saved: {r['path']} ({r['width']}x{r['height']}, {r['size']} bytes)")
    elif args.command == 'focus':
        desk.focus(args.title)
        print(f'Focused: {args.title}')
    elif args.command == 'close':
        desk.close(args.title)
        print(f'Closed: {args.title}')
    elif args.command == 'tree':
        print(json.dumps(desk.ui_tree(args.title, depth=args.depth), indent=2, default=str))
    elif args.command == 'find':
        elements = desk.find_elements(args.title, name=args.name, id=args.id, type=args.type)
        for el in elements:
            rect = el.get('rect') or {}
            print(f"  [{el.get('type',''):15s}] {el.get('name','')[:40]:40s} id={el.get('id',''):20s} ({rect.get('x',0)},{rect.get('y',0)} {rect.get('w',0)}x{rect.get('h',0)})")
    elif args.command == 'click':
        r = desk.click_element(args.title, name=args.name, id=args.id)
        print(f"Clicked: {r}")
    elif args.command == 'type':
        desk.type_text(args.text)
        print(f'Typed: {args.text[:40]}')
    elif args.command == 'key':
        desk.press_key(args.key)
        print(f'Pressed: {args.key}')
    elif args.command == 'hotkey':
        desk.hotkey(*args.keys)
        print(f'Hotkey: {"+".join(args.keys)}')
    elif args.command == 'clip':
        if args.action == 'set':
            desk.clip_set(args.text)
            print(f'Clipboard set: {args.text[:40]}')
        else:
            print(desk.clip_get())
    elif args.command == 'procs':
        for p in desk.processes(args.name):
            print(f"  [{p.get('Id',0):>6}] {p.get('ProcessName',''):25s} {p.get('MainWindowTitle','')[:40]}")
    elif args.command == 'launch':
        r = desk.launch(args.cmd[0], *args.cmd[1:])
        print(f"Launched: PID {r.get('pid')}")
    elif args.command == 'kill':
        desk.kill(args.pid)
        print(f'Killed: {args.pid}')
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
