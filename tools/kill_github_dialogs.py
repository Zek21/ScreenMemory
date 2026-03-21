"""
GitHub Auth Dialog Killer — Background daemon that auto-closes
'Connect to GitHub' sign-in dialogs that spam from worker windows.

Usage:
    python tools/kill_github_dialogs.py          # Run once
    python tools/kill_github_dialogs.py --daemon  # Run continuously in background
"""
import ctypes, ctypes.wintypes, time, sys

u32 = ctypes.windll.user32
WM_CLOSE = 0x0010
WINFUNC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)


def kill_github_dialogs():
    """Find and close all 'Connect to GitHub' windows. Returns count killed."""
    killed = []
    def cb(hwnd, _):
        if u32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(512)
            u32.GetWindowTextW(hwnd, buf, 512)
            t = buf.value
            if 'Connect to GitHub' in t:
                u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                killed.append(hwnd)
        return True
    u32.EnumWindows(WINFUNC(cb), 0)
    return len(killed)


def daemon_loop(interval=2):
    """Continuously kill GitHub dialogs every `interval` seconds."""
    total = 0
    print(f"[GitHub Dialog Killer] Daemon started (interval={interval}s)")
    try:
        while True:
            n = kill_github_dialogs()
            if n:
                total += n
                print(f"[GitHub Dialog Killer] Killed {n} dialogs (total: {total})")
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n[GitHub Dialog Killer] Stopped. Total killed: {total}")


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        daemon_loop()
    else:
        n = kill_github_dialogs()
        print(f"Killed {n} GitHub dialog(s)")
