#!/usr/bin/env python3
"""
daemon_health.py -- Quick diagnostic: checks all Skynet daemons are alive.

Usage:
    python tools/daemon_health.py              # Report alive/dead for all 9 daemons
    python tools/daemon_health.py --fix        # Auto-start dead daemons
    python tools/daemon_health.py --json       # Machine-readable JSON output
    python tools/daemon_health.py --validate   # Pre-flight environment validation
    python tools/daemon_health.py --validate --json  # Validation as JSON

Exit codes: 0 = all healthy / validation pass, 1 = one or more dead / validation fail.
Designed to be called from Orch-Start.ps1 for comprehensive daemon verification.
Daemons should call validate_daemon_environment() on startup to fail fast.
"""
# signed: delta

import argparse
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# ── Resolve real Python to avoid venv launcher duplicate-process issues ──
def _real_python():
    cfg = ROOT.parent / "env" / "pyvenv.cfg"
    if cfg.exists():
        for line in cfg.read_text().splitlines():
            if line.strip().startswith("executable"):
                _, _, val = line.partition("=")
                p = val.strip()
                if Path(p).exists():
                    return p
    return sys.executable
# signed: delta

PYTHON = _real_python()
_BG = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS

# ── Daemon registry: name → (pid_file, start_cmd, description) ──
DAEMONS = {
    "monitor":      ("monitor.pid",      ["skynet_monitor.py"],                "Worker window health monitor"),
    "watchdog":     ("watchdog.pid",      ["skynet_watchdog.py", "start"],      "Service health watchdog"),
    "self_prompt":  ("self_prompt.pid",   ["skynet_self_prompt.py", "start"],   "Orchestrator heartbeat"),
    "self_improve": ("self_improve.pid",  ["skynet_self_improve.py", "start"],  "Self-improvement scanner"),
    "bus_relay":    ("bus_relay.pid",     ["skynet_bus_relay.py"],              "Bus message relay"),
    "sse_daemon":   ("sse_daemon.pid",   ["skynet_sse_daemon.py"],             "SSE real-time streamer"),
    "learner":      ("learner.pid",      ["skynet_learner.py", "--daemon"],    "Learning engine"),
    "overseer":     ("overseer.pid",     ["skynet_overseer.py"],               "Autonomous overseer"),
    "realtime":     ("realtime.pid",     ["skynet_realtime.py"],               "Real-time state writer"),
}
# signed: delta


def _alive(pid: int) -> bool:
    """Windows-safe process alive check via kernel32."""
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    return False
# signed: delta


def check_daemon(name: str) -> dict:
    """Check one daemon. Returns {name, pid, alive, pid_file, stale}."""
    pid_file_name, _, desc = DAEMONS[name]
    pid_path = DATA / pid_file_name
    result = {"name": name, "description": desc, "pid": None,
              "alive": False, "stale": False, "pid_file": str(pid_path)}
    if not pid_path.exists():
        return result
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        result["stale"] = True
        return result
    result["pid"] = pid
    if _alive(pid):
        result["alive"] = True
    else:
        result["stale"] = True
    return result
# signed: delta


def fix_daemon(name: str) -> bool:
    """Start a dead daemon. Returns True on success. Respects .disabled sentinel."""
    # Check disabled sentinel -- do not restart disabled daemons
    disabled_file = ROOT / "data" / f"{name}.disabled"
    if disabled_file.exists():
        print(f"  {name}: DISABLED -- skipping fix (remove {disabled_file} to enable)")
        return False
    _, cmd_parts, _ = DAEMONS[name]
    full_cmd = [PYTHON] + [str(ROOT / "tools" / c) if c.endswith(".py") else c for c in cmd_parts]
    env = os.environ.copy()
    site_pkg = str(ROOT.parent / "env" / "Lib" / "site-packages")
    env["PYTHONPATH"] = f"{site_pkg};{env.get('PYTHONPATH', '')}"
    try:
        proc = subprocess.Popen(full_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=env, creationflags=_BG, cwd=str(ROOT))
        import time; time.sleep(1.5)
        return proc.poll() is None
    except Exception as e:
        print(f"  FAILED to start {name}: {e}", file=sys.stderr)
        return False
# signed: delta


def validate_daemon_environment(quiet=False) -> dict:
    """Pre-flight environment validator. Call before any daemon starts.

    Checks: Python version, required modules, data/ writable, backend alive,
    workers.json valid. Returns dict with 'ok' bool and per-check details.
    """
    checks = {}

    # 1. Python version >= 3.8
    ver = sys.version_info
    py_ok = ver >= (3, 8)
    checks["python_version"] = {
        "ok": py_ok,
        "value": f"{ver.major}.{ver.minor}.{ver.micro}",
        "required": ">=3.8",
    }

    # 2. Required modules importable (ensure ROOT is on sys.path for tools.*)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    required_modules = [
        "tools.skynet_spam_guard",
        "tools.skynet_scoring",
        "tools.skynet_knowledge",
        "json", "pathlib", "hashlib", "urllib.request",
    ]
    optional_modules = [
        "tools.uia_engine",
        "tools.skynet_collective",
        "tools.skynet_convene",
    ]
    mod_results = {}
    for mod_name in required_modules:
        try:
            __import__(mod_name)
            mod_results[mod_name] = {"ok": True, "required": True}
        except Exception as e:
            mod_results[mod_name] = {"ok": False, "required": True, "error": str(e)[:100]}
    for mod_name in optional_modules:
        try:
            __import__(mod_name)
            mod_results[mod_name] = {"ok": True, "required": False}
        except Exception:
            mod_results[mod_name] = {"ok": False, "required": False}
    req_ok = all(v["ok"] for v in mod_results.values() if v["required"])
    checks["modules"] = {"ok": req_ok, "details": mod_results}

    # 3. data/ directory writable
    test_file = DATA / ".write_test_delta"
    try:
        DATA.mkdir(exist_ok=True)
        test_file.write_text("ok")
        test_file.unlink()
        data_ok = True
    except Exception as e:
        data_ok = False
        checks["data_writable"] = {"ok": False, "error": str(e)[:100]}
    if data_ok:
        checks["data_writable"] = {"ok": True, "path": str(DATA)}

    # 4. Port 8420 reachable (backend alive)
    import socket
    backend_ok = False
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", 8420))
        s.close()
        backend_ok = True
    except Exception:
        pass
    checks["backend_alive"] = {"ok": backend_ok, "port": 8420}

    # 5. data/workers.json exists and has valid HWND entries
    workers_file = DATA / "workers.json"
    workers_ok = False
    worker_detail = {}
    if workers_file.exists():
        try:
            raw = json.loads(workers_file.read_text(encoding="utf-8"))
            workers = raw.get("workers", raw) if isinstance(raw, dict) else raw
            if isinstance(workers, list) and len(workers) > 0:
                valid = 0
                for w in workers:
                    hwnd = w.get("hwnd", 0)
                    name = w.get("name", "?")
                    is_valid = isinstance(hwnd, int) and hwnd > 0
                    if is_valid:
                        valid += 1
                    worker_detail[name] = {"hwnd": hwnd, "valid": is_valid}
                workers_ok = valid > 0
                checks["workers_json"] = {
                    "ok": workers_ok, "total": len(workers),
                    "valid_hwnds": valid, "workers": worker_detail,
                }
            else:
                checks["workers_json"] = {"ok": False, "reason": "empty or not a list"}
        except Exception as e:
            checks["workers_json"] = {"ok": False, "error": str(e)[:100]}
    else:
        checks["workers_json"] = {"ok": False, "reason": "file not found"}

    all_ok = all(c["ok"] for c in checks.values())

    if not quiet:
        print(f"\n  Daemon Environment Validation ({'PASS' if all_ok else 'FAIL'})")
        print(f"  {'='*45}")
        for check_name, detail in checks.items():
            icon = "OK" if detail["ok"] else "FAIL"
            extra = ""
            if check_name == "python_version":
                extra = f" ({detail['value']})"
            elif check_name == "modules":
                failed = [m for m, v in detail["details"].items() if not v["ok"] and v["required"]]
                if failed:
                    extra = f" (missing: {', '.join(failed)})"
                else:
                    extra = f" ({len(detail['details'])} checked)"
            elif check_name == "backend_alive":
                extra = f" (port {detail['port']})"
            elif check_name == "workers_json" and detail["ok"]:
                extra = f" ({detail.get('valid_hwnds', 0)}/{detail.get('total', 0)} valid)"
            elif check_name == "workers_json":
                extra = f" ({detail.get('reason', detail.get('error', ''))})"
            print(f"  {icon:>4}  {check_name:<20}{extra}")
        print()

    return {"ok": all_ok, "checks": checks}
    # signed: delta


def main():
    parser = argparse.ArgumentParser(description="Skynet Daemon Health Check")
    parser.add_argument("--fix", action="store_true", help="Auto-start dead daemons")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--validate", action="store_true",
                        help="Pre-flight environment validation for daemon startup")
    args = parser.parse_args()

    if args.validate:
        result = validate_daemon_environment(quiet=args.json)
        if args.json:
            print(json.dumps(result))
        sys.exit(0 if result["ok"] else 1)

    results = {name: check_daemon(name) for name in DAEMONS}
    alive_count = sum(1 for r in results.values() if r["alive"])
    total = len(DAEMONS)
    dead_names = [n for n, r in results.items() if not r["alive"]]

    if args.json:
        print(json.dumps({"daemons": results, "alive": alive_count,
                          "total": total, "healthy": alive_count == total}))
    else:
        print(f"\n  Skynet Daemon Health ({alive_count}/{total})\n  {'='*40}")
        for name, r in results.items():
            icon = "OK" if r["alive"] else ("STALE" if r["stale"] else "DOWN")
            pid_s = str(r["pid"]) if r["pid"] else "-"
            print(f"  {icon:>5}  {name:<15} PID {pid_s:>7}  {r['description']}")
        print()

    fixed = 0
    if args.fix and dead_names:
        print(f"  Fixing {len(dead_names)} dead daemon(s)...")
        for name in dead_names:
            ok = fix_daemon(name)
            status = "STARTED" if ok else "FAILED"
            print(f"    {name}: {status}")
            if ok:
                fixed += 1
        print(f"  Fixed {fixed}/{len(dead_names)}\n")

    sys.exit(0 if alive_count + fixed >= total else 1)


if __name__ == "__main__":
    main()
# signed: delta
