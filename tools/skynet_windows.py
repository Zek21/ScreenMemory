#!/usr/bin/env python3
"""
skynet_windows.py -- Skynet Window Awareness Registry.

Tracks all windows, processes, and services that Skynet has opened.
Auto-discovers Skynet-related windows via Win32 enumeration and known patterns.

Usage:
    python tools/skynet_windows.py scan       # Scan and print all Skynet windows
    python tools/skynet_windows.py --json     # Output as JSON
    python tools/skynet_windows.py --save     # Save to data/skynet_windows.json
"""

import argparse
import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import psutil
except Exception:
    psutil = None

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "chrome_bridge"))

REGISTRY_FILE = DATA / "skynet_windows.json"

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def _hidden_subprocess_kwargs(**kwargs):
    merged = dict(kwargs)
    if sys.platform == "win32":
        merged["creationflags"] = merged.get("creationflags", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = merged.get("startupinfo")
        if startupinfo is None and hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            merged["startupinfo"] = startupinfo
    return merged


def _hidden_run(args, **kwargs):
    return subprocess.run(args, **_hidden_subprocess_kwargs(**kwargs))


# ── Win32 Helpers ──────────────────────────────────────────────

def _enum_windows(visible_only=True):
    """Enumerate all windows with hwnd, title, class, rect, pid."""
    results = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def cb(hwnd, _):
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
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        results.append({
            "hwnd": hwnd,
            "title": buf.value,
            "class": cls.value,
            "x": rect.left,
            "y": rect.top,
            "w": rect.right - rect.left,
            "h": rect.bottom - rect.top,
            "pid": pid.value,
        })
        return True

    user32.EnumWindows(cb, 0)
    return results


def _is_window_valid(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd)) and bool(user32.IsWindowVisible(hwnd))


def _get_window_state(hwnd: int) -> str:
    if not user32.IsWindow(hwnd):
        return "closed"
    if not user32.IsWindowVisible(hwnd):
        return "hidden"

    class WINDOWPLACEMENT(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint),
            ("flags", ctypes.c_uint),
            ("showCmd", ctypes.c_uint),
            ("ptMinPosition", ctypes.wintypes.POINT),
            ("ptMaxPosition", ctypes.wintypes.POINT),
            ("rcNormalPosition", ctypes.wintypes.RECT),
        ]

    placement = WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(placement)
    user32.GetWindowPlacement(hwnd, ctypes.byref(placement))
    SW_SHOWMINIMIZED = 2
    SW_SHOWMAXIMIZED = 3
    if placement.showCmd == SW_SHOWMINIMIZED:
        return "minimized"
    if placement.showCmd == SW_SHOWMAXIMIZED:
        return "maximized"
    return "visible"


def _get_window_rect(hwnd: int) -> dict:
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return {
        "x": rect.left, "y": rect.top,
        "w": rect.right - rect.left,
        "h": rect.bottom - rect.top,
    }


def _get_monitor_index(x: int) -> int:
    """Estimate monitor index from x coordinate."""
    if x < 0:
        return 0
    if x < 1920:
        return 1
    return 2


def _pid_alive(pid: int) -> bool:
    try:
        PROCESS_QUERY_LIMITED = 0x1000
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
        if h:
            kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


def _get_process_name(pid: int) -> str:
    """Get process name via Win32 API (no subprocess)."""
    try:
        PROCESS_QUERY_LIMITED = 0x1000
        PROCESS_VM_READ = 0x0010
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED | PROCESS_VM_READ, False, pid)
        if h:
            try:
                import ctypes.wintypes
                psapi = ctypes.windll.psapi
                buf = ctypes.create_unicode_buffer(260)
                if psapi.GetModuleBaseNameW(h, None, buf, 260):
                    return buf.value
            finally:
                kernel32.CloseHandle(h)
        return "unknown"
    except Exception:
        return "unknown"


# ── Skynet Window Discovery ──────────────────────────────────

# Patterns that identify Skynet-related windows
SKYNET_PATTERNS = [
    {"match": "Visual Studio Code - Insiders", "role": "vscode"},
    {"match": "skynet", "role": "backend"},
    {"match": "GOD Console", "role": "god_console"},
    {"match": "localhost:8421", "role": "dashboard"},
    {"match": "localhost:8420", "role": "backend_ui"},
    {"match": "ScreenMemory", "role": "project"},
]


def _load_known_hwnds() -> dict:
    """Load known HWNDs from workers.json and orchestrator.json."""
    known = {}

    # Workers
    workers_file = DATA / "workers.json"
    if workers_file.exists():
        try:
            wdata = json.loads(workers_file.read_text())
            for w in wdata.get("workers", []):
                hwnd = w.get("hwnd")
                name = w.get("name", "unknown")
                if hwnd:
                    known[hwnd] = {
                        "role": f"worker_{name}",
                        "name": name,
                        "grid": w.get("grid", ""),
                        "expected_pos": {"x": w.get("x"), "y": w.get("y"), "w": w.get("w"), "h": w.get("h")},
                    }
        except Exception:
            pass

    # Orchestrator
    orch_file = DATA / "orchestrator.json"
    if orch_file.exists():
        try:
            odata = json.loads(orch_file.read_text())
            hwnd = odata.get("orchestrator_hwnd")
            if hwnd:
                known[hwnd] = {
                    "role": "orchestrator",
                    "name": "orchestrator",
                    "model": odata.get("model", ""),
                }
        except Exception:
            pass

    return known


def _find_processes() -> list:
    """Find Skynet-related background processes without shelling out."""
    procs = []

    found_ports = set()

    if psutil is not None:
        try:
            for conn in psutil.net_connections(kind="tcp"):
                laddr = getattr(conn, "laddr", None)
                if not laddr:
                    continue
                port = getattr(laddr, "port", None)
                if port not in (8420, 8421):
                    continue
                if conn.status != psutil.CONN_LISTEN or not conn.pid:
                    continue
                service = "skynet_backend" if port == 8420 else "god_console"
                procs.append({"pid": int(conn.pid), "service": service, "port": port})
                found_ports.add(port)
        except Exception:
            pass

    if psutil is not None:
        try:
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    name = (proc.info.get("name") or "").lower()
                    cmdline = " ".join(proc.info.get("cmdline") or [])
                    if name == "skynet.exe" and 8420 not in found_ports:
                        procs.append({"pid": int(proc.info["pid"]), "service": "skynet_backend", "port": 8420})
                        found_ports.add(8420)
                    elif "god_console.py" in cmdline and 8421 not in found_ports:
                        procs.append({"pid": int(proc.info["pid"]), "service": "god_console", "port": 8421})
                        found_ports.add(8421)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except Exception:
            pass

    # Watchdog
    pid_file = DATA / "watchdog.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if _pid_alive(pid):
                procs.append({"pid": pid, "service": "watchdog", "port": None})
        except Exception:
            pass

    # Monitor
    mon_file = DATA / "monitor.pid"
    if mon_file.exists():
        try:
            pid = int(mon_file.read_text().strip())
            if _pid_alive(pid):
                procs.append({"pid": pid, "service": "monitor", "port": None})
        except Exception:
            pass

    return procs


def _match_window(w, known, processes, process_pids):
    """Try to match a window to known HWNDs, title patterns, or process PIDs."""
    hwnd = w["hwnd"]

    # Known HWND match
    if hwnd in known:
        info = known[hwnd]
        state = _get_window_state(hwnd)
        rect = _get_window_rect(hwnd)
        entry = {
            "hwnd": hwnd, "role": info["role"], "name": info.get("name", ""),
            "title": w["title"], "pid": w["pid"], "state": state,
            "monitor": _get_monitor_index(rect["x"]), "position": rect,
            "source": "known_hwnd",
        }
        if "expected_pos" in info:
            ep = info["expected_pos"]
            entry["position_drift"] = (
                abs(rect["x"] - (ep.get("x") or 0)) > 5 or
                abs(rect["y"] - (ep.get("y") or 0)) > 5
            )
        return entry

    # Title pattern match
    for pat in SKYNET_PATTERNS:
        if pat["match"].lower() in w["title"].lower():
            state = _get_window_state(hwnd)
            rect = _get_window_rect(hwnd)
            return {
                "hwnd": hwnd, "role": pat["role"], "name": pat["role"],
                "title": w["title"], "pid": w["pid"], "state": state,
                "monitor": _get_monitor_index(rect["x"]), "position": rect,
                "source": "pattern_match",
            }

    # Process PID match
    if w["pid"] in process_pids:
        state = _get_window_state(hwnd)
        rect = _get_window_rect(hwnd)
        svc = next((p for p in processes if p["pid"] == w["pid"]), {})
        return {
            "hwnd": hwnd, "role": svc.get("service", "skynet_process"),
            "name": svc.get("service", "unknown"),
            "title": w["title"], "pid": w["pid"], "state": state,
            "monitor": _get_monitor_index(rect["x"]), "position": rect,
            "source": "process_match",
        }

    return None


def scan_windows() -> dict:
    """Full window scan: discover all Skynet-related windows and processes."""
    scan_start = time.time()
    all_windows = _enum_windows(visible_only=False)
    known = _load_known_hwnds()
    processes = _find_processes()
    process_pids = {p["pid"] for p in processes}

    registry = {
        "scan_time": datetime.now().isoformat(),
        "windows": [],
        "processes": processes,
        "summary": {},
    }

    for w in all_windows:
        entry = _match_window(w, known, processes, process_pids)
        if entry:
            registry["windows"].append(entry)

    # Check for dead known windows
    dead = []
    for hwnd, info in known.items():
        if not user32.IsWindow(hwnd):
            dead.append({
                "hwnd": hwnd,
                "role": info["role"],
                "name": info.get("name", ""),
                "state": "dead",
                "source": "known_hwnd",
            })
    registry["dead_windows"] = dead

    # Summary
    roles = {}
    for w in registry["windows"]:
        r = w["role"]
        roles[r] = roles.get(r, 0) + 1
    total_area = sum(w["position"]["w"] * w["position"]["h"] for w in registry["windows"] if "position" in w)
    monitors_used = set(w.get("monitor", 0) for w in registry["windows"])

    registry["summary"] = {
        "total_windows": len(registry["windows"]),
        "dead_windows": len(dead),
        "total_processes": len(processes),
        "roles": roles,
        "monitors_used": sorted(monitors_used),
        "total_screen_area_px": total_area,
        "scan_ms": round((time.time() - scan_start) * 1000, 1),
    }

    return registry


def save_registry(registry: dict):
    DATA.mkdir(exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(registry, indent=2, default=str), encoding="utf-8")


def format_report(registry: dict) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append("  SKYNET WINDOW AWARENESS REPORT")
    lines.append(f"  Scanned: {registry['scan_time']}")
    lines.append("=" * 60)

    s = registry["summary"]
    lines.append(f"\n  Windows: {s['total_windows']} | Dead: {s['dead_windows']} | Processes: {s['total_processes']}")
    lines.append(f"  Monitors: {s['monitors_used']} | Screen area: {s['total_screen_area_px']:,} px")
    lines.append(f"  Scan time: {s['scan_ms']}ms")

    lines.append(f"\n-- Windows --")
    for w in registry["windows"]:
        pos = w.get("position", {})
        drift = " DRIFTED" if w.get("position_drift") else ""
        lines.append(
            f"  {w['role']:20s} | hwnd={w['hwnd']:>10} | pid={w['pid']:>6} | "
            f"{w['state']:9s} | mon={w.get('monitor', '?')} | "
            f"({pos.get('x', 0):>5},{pos.get('y', 0):>5}) {pos.get('w', 0)}x{pos.get('h', 0)}{drift}"
        )

    if registry.get("dead_windows"):
        lines.append(f"\n-- Dead Windows (known HWNDs no longer valid) --")
        for d in registry["dead_windows"]:
            lines.append(f"  {d['role']:20s} | hwnd={d['hwnd']:>10} | DEAD")

    lines.append(f"\n-- Background Processes --")
    for p in registry.get("processes", []):
        alive = "alive" if _pid_alive(p["pid"]) else "DEAD"
        port = f":{p['port']}" if p.get("port") else ""
        lines.append(f"  {p['service']:20s} | pid={p['pid']:>6} | {alive}{port}")

    lines.append(f"\n{'=' * 60}")
    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────

def get_window_summary() -> dict:
    """Quick summary for embedding in pulse/self-awareness."""
    registry = scan_windows()
    s = registry["summary"]
    workers_alive = sum(1 for w in registry["windows"] if w["role"].startswith("worker_") and w["state"] != "closed")
    orch_alive = any(w["role"] == "orchestrator" and w["state"] != "closed" for w in registry["windows"])
    return {
        "total_windows": s["total_windows"],
        "dead_windows": s["dead_windows"],
        "workers_alive": workers_alive,
        "orchestrator_alive": orch_alive,
        "processes": s["total_processes"],
        "monitors": s["monitors_used"],
        "screen_area_px": s["total_screen_area_px"],
    }


def main():
    parser = argparse.ArgumentParser(description="Skynet Window Awareness")
    parser.add_argument("action", nargs="?", default="scan", choices=["scan"], help="Action")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--save", action="store_true", help="Save to data/skynet_windows.json")
    args = parser.parse_args()

    registry = scan_windows()

    if args.json:
        print(json.dumps(registry, indent=2, default=str))
    else:
        print(format_report(registry))

    if args.save:
        save_registry(registry)
        print(f"\nSaved to {REGISTRY_FILE}")


if __name__ == "__main__":
    main()
