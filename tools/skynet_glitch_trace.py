#!/usr/bin/env python3
"""
skynet_glitch_trace.py -- Fast transient window/focus tracer for Skynet glitches.

Captures visible top-level window appearances/disappearances and foreground
changes that are too brief to identify by eye.

Usage:
    python tools/skynet_glitch_trace.py trace --seconds 120
    python tools/skynet_glitch_trace.py trace --seconds 300 --output data/glitch_trace_latest.jsonl
    python tools/skynet_glitch_trace.py summary
    python tools/skynet_glitch_trace.py summary --input data/glitch_trace_latest.jsonl
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DEFAULT_OUTPUT = DATA_DIR / "glitch_trace_latest.jsonl"

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [ctypes.wintypes.HWND]
user32.GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.RECT)]
user32.GetForegroundWindow.argtypes = []
user32.GetWindowThreadProcessId.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)]

kernel32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]

psapi.GetModuleBaseNameW.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.HANDLE, ctypes.wintypes.LPWSTR, ctypes.wintypes.DWORD]
psapi.GetModuleBaseNameW.restype = ctypes.wintypes.DWORD

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010

CONSOLE_PROCESSES = {
    "cmd.exe",
    "conhost.exe",
    "powershell.exe",
    "pwsh.exe",
    "windowsterminal.exe",
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _get_process_name(pid: int) -> str:
    if not pid:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        if psapi.GetModuleBaseNameW(handle, None, buf, 260):
            return buf.value
    except Exception:
        return ""
    finally:
        kernel32.CloseHandle(handle)
    return ""


def _get_window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _get_window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _get_rect(hwnd: int) -> dict[str, int]:
    rect = ctypes.wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return {"x": 0, "y": 0, "w": 0, "h": 0}
    return {
        "x": rect.left,
        "y": rect.top,
        "w": rect.right - rect.left,
        "h": rect.bottom - rect.top,
    }


def _window_info(hwnd: int) -> dict[str, object]:
    if not hwnd:
        return {
            "hwnd": 0,
            "title": "",
            "class": "",
            "pid": 0,
            "process": "",
            "rect": {"x": 0, "y": 0, "w": 0, "h": 0},
            "kind": "none",
            "suspicious": False,
        }
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    title = _get_window_title(hwnd)
    class_name = _get_window_class(hwnd)
    process_name = _get_process_name(pid.value)
    rect = _get_rect(hwnd)

    title_l = title.lower()
    class_l = class_name.lower()
    proc_l = process_name.lower()
    kind = "other"
    if class_name == "ConsoleWindowClass" or proc_l in CONSOLE_PROCESSES:
        kind = "console"
    elif "visual studio code" in title_l:
        kind = "vscode"
    elif "god console" in title_l or "8421" in title_l:
        kind = "god_console"
    elif proc_l == "skynet.exe":
        kind = "skynet"

    suspicious = bool(
        kind == "console"
        or class_l == "consolewindowclass"
        or "command prompt" in title_l
        or "powershell" in title_l
        or "cmd.exe" in title_l
    )
    return {
        "hwnd": int(hwnd),
        "title": title,
        "class": class_name,
        "pid": int(pid.value),
        "process": process_name,
        "rect": rect,
        "kind": kind,
        "suspicious": suspicious,
    }


def _enum_visible_windows() -> dict[int, dict[str, object]]:
    windows: dict[int, dict[str, object]] = {}

    @WNDENUMPROC
    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        rect = _get_rect(hwnd)
        if rect["w"] < 20 or rect["h"] < 20:
            return True
        info = _window_info(hwnd)
        if not info["title"] and info["class"] in ("MSCTFIME UI", "IME"):
            return True
        windows[int(hwnd)] = info
        return True

    user32.EnumWindows(cb, 0)
    return windows


def _write_event(handle, trace_start: float, event_type: str, info: dict[str, object], **extra) -> None:
    entry = {
        "ts": _now_iso(),
        "elapsed_ms": round((time.perf_counter() - trace_start) * 1000.0, 1),
        "event": event_type,
        **info,
    }
    entry.update(extra)
    handle.write(json.dumps(entry, ensure_ascii=True) + "\n")
    handle.flush()


_EMPTY_TRACE_INFO = {"hwnd": 0, "title": "", "class": "", "pid": 0, "process": "",
                     "rect": {"x": 0, "y": 0, "w": 0, "h": 0}, "kind": "trace", "suspicious": False}


def _trace_poll_cycle(handle, trace_start: float, visible: dict, first_seen: dict,
                      last_foreground: int, next_enum: float, enum_s: float) -> tuple:
    """Run one poll cycle: check foreground + enum windows. Returns updated state."""
    now = time.perf_counter()
    foreground = int(user32.GetForegroundWindow() or 0)
    if foreground != last_foreground:
        _write_event(handle, trace_start, "foreground_changed",
                     _window_info(foreground), previous_hwnd=last_foreground)
        last_foreground = foreground

    if now >= next_enum:
        current = _enum_visible_windows()
        current_set = set(current)
        visible_set = set(visible)

        for hwnd in sorted(current_set - visible_set):
            first_seen[hwnd] = now
            _write_event(handle, trace_start, "window_shown", current[hwnd])

        for hwnd in sorted(visible_set - current_set):
            info = visible[hwnd]
            shown_at = first_seen.get(hwnd, now)
            _write_event(handle, trace_start, "window_hidden", info,
                         lifetime_ms=round((now - shown_at) * 1000.0, 1))
            first_seen.pop(hwnd, None)

        visible = current
        next_enum = now + enum_s

    return visible, first_seen, last_foreground, next_enum


def trace(seconds: float, poll_ms: int, enum_ms: int, output: Path) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    trace_start = time.perf_counter()
    deadline = trace_start + seconds
    visible = _enum_visible_windows()
    first_seen = {hwnd: 0.0 for hwnd in visible}
    last_foreground = int(user32.GetForegroundWindow() or 0)

    with output.open("w", encoding="utf-8") as handle:
        _write_event(handle, trace_start, "trace_start", dict(_EMPTY_TRACE_INFO),
                     seconds=seconds, poll_ms=poll_ms, enum_ms=enum_ms,
                     initial_visible=len(visible))
        if last_foreground:
            _write_event(handle, trace_start, "foreground_changed",
                         _window_info(last_foreground), reason="initial")

        next_enum = trace_start
        poll_s = max(poll_ms, 1) / 1000.0
        enum_s = max(enum_ms, 1) / 1000.0

        while time.perf_counter() < deadline:
            visible, first_seen, last_foreground, next_enum = _trace_poll_cycle(
                handle, trace_start, visible, first_seen,
                last_foreground, next_enum, enum_s)
            time.sleep(poll_s)

        _write_event(handle, trace_start, "trace_end", dict(_EMPTY_TRACE_INFO),
                     final_visible=len(visible))
    return 0


def _parse_trace_entries(input_path: Path) -> list:
    """Load and parse all JSON entries from a trace file."""
    entries = []
    with input_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _classify_trace_events(entries: list) -> tuple[dict, list, list]:
    """Classify entries into event counts, suspicious events, and transient windows."""
    event_counts: dict[str, int] = {}
    suspicious = []
    transient = []
    for entry in entries:
        event = str(entry.get("event", ""))
        event_counts[event] = event_counts.get(event, 0) + 1
        if entry.get("suspicious"):
            suspicious.append(entry)
        if event == "window_hidden" and float(entry.get("lifetime_ms", 0) or 0) <= 1000:
            transient.append(entry)
    return event_counts, suspicious, transient


def summary(input_path: Path) -> int:
    if not input_path.exists():
        print(f"Trace file not found: {input_path}")
        return 1

    entries = _parse_trace_entries(input_path)
    if not entries:
        print("No trace entries found.")
        return 1

    event_counts, suspicious, transient = _classify_trace_events(entries)

    print(f"Trace file: {input_path}")
    print(f"Entries: {len(entries)}")
    print(f"Events: {event_counts}")

    if suspicious:
        print("\nSuspicious events:")
        for entry in suspicious[-10:]:
            print(
                f"  {entry.get('ts')} | {entry.get('event')} | "
                f"pid={entry.get('pid')} proc={entry.get('process')} "
                f"class={entry.get('class')} title={entry.get('title')!r}"
            )

    if transient:
        print("\nShort-lived windows (<=1000ms):")
        for entry in transient[-10:]:
            print(
                f"  {entry.get('ts')} | pid={entry.get('pid')} proc={entry.get('process')} "
                f"life={entry.get('lifetime_ms')}ms class={entry.get('class')} title={entry.get('title')!r}"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Trace transient focus/window glitches on Windows")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_trace = sub.add_parser("trace", help="Capture transient window/focus events")
    p_trace.add_argument("--seconds", type=float, default=120.0, help="How long to trace")
    p_trace.add_argument("--poll-ms", type=int, default=20, help="Foreground poll interval")
    p_trace.add_argument("--enum-ms", type=int, default=50, help="Visible-window enumeration interval")
    p_trace.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="JSONL output file")

    p_summary = sub.add_parser("summary", help="Summarize a trace file")
    p_summary.add_argument("--input", type=Path, default=DEFAULT_OUTPUT, help="JSONL input file")

    args = parser.parse_args()

    if args.cmd == "trace":
        return trace(args.seconds, args.poll_ms, args.enum_ms, args.output)
    if args.cmd == "summary":
        return summary(args.input)
    return 1


if __name__ == "__main__":
    sys.exit(main())
