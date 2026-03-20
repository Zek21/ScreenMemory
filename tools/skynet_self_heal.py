"""Skynet Self-Healing — comprehensive automated recovery system.

Detects stuck tasks, checks all critical systems, auto-fixes issues,
and generates health reports.

Usage:
    python tools/skynet_self_heal.py                # Full check + fix (default)
    python tools/skynet_self_heal.py --check-only   # Diagnose without fixing
    python tools/skynet_self_heal.py --fix          # Explicit fix mode
    python tools/skynet_self_heal.py --json         # JSON output
    python tools/skynet_self_heal.py --daemon       # Run every 5 minutes
    python tools/skynet_self_heal.py detect         # Legacy: show stuck tasks
    python tools/skynet_self_heal.py heal           # Legacy: auto-heal stuck tasks
    python tools/skynet_self_heal.py report         # Legacy: dispatch health report
    python tools/skynet_self_heal.py run            # Legacy: continuous monitor loop
"""
# signed: delta

import argparse
import ctypes
import datetime
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

DATA = ROOT / "data"
LOGS = ROOT / "logs"
SKYNET_DIR = ROOT / "Skynet"

DISPATCH_LOG = DATA / "dispatch_log.json"
WORKER_PERF = DATA / "worker_performance.json"
REALTIME_FILE = DATA / "realtime.json"
HEAL_LOG = DATA / "heal_log.json"

BACKEND_PORT = 8420
GOD_CONSOLE_PORT = 8421
DAEMON_INTERVAL = 300  # 5 minutes
BUS_ARCHIVE_MAX_MB = 10
LOG_ARCHIVE_AGE_DAYS = 1

STUCK_THRESHOLDS = {
    "simple": 120,    # 2 min
    "standard": 180,  # 3 min
    "complex": 300,   # 5 min
}

ALL_WORKERS = ["alpha", "beta", "gamma", "delta"]

# Known daemons and their PID file paths
KNOWN_DAEMONS = {
    "sse_daemon": DATA / "sse_daemon.pid",
    "monitor": DATA / "monitor.pid",
    "self_prompt": DATA / "self_prompt.pid",
    "self_improve": DATA / "self_improve.pid",
    "bus_relay": DATA / "bus_relay.pid",
    "learner": DATA / "learner.pid",
    "watchdog": DATA / "watchdog.pid",
    "overseer": DATA / "overseer.pid",
    "god_console": DATA / "god_console.pid",
    "bus_persist": DATA / "bus_persist.pid",
    "idle_monitor": DATA / "idle_monitor.pid",
    "consultant_consumer": DATA / "consultant_consumer.pid",
    "consultant_bridge": DATA / "consultant_bridge.pid",
    "gemini_consultant_bridge": DATA / "gemini_consultant_bridge.pid",
    "proactive_handler": DATA / "proactive_handler.pid",
    "knowledge_distill": DATA / "knowledge_distill.pid",
    "self_heal": DATA / "self_heal.pid",
}


def _load_json(path: Path) -> dict | list:
    """Load JSON file, return empty dict/list on failure."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json(path: Path, data) -> None:
    """Atomically save JSON data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _ts() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _is_pid_alive(pid: int) -> bool:
    """Check if a process is alive using Windows kernel API."""
    try:
        PROCESS_QUERY_LIMITED = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == STILL_ACTIVE
    except Exception:
        return False


def _is_port_open(port: int, timeout: float = 1.0) -> bool:
    """Check if a TCP port is listening."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def _http_get(url: str, timeout: float = 2.0):
    """Quick HTTP GET, returns parsed JSON or None."""
    try:
        import urllib.request
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


# ── Comprehensive system checks ──────────────────────────────────

def check_backend() -> dict:
    """Check Skynet backend on port 8420."""
    result = {"name": "backend", "port": BACKEND_PORT, "status": "DEAD", "details": {}}
    if _is_port_open(BACKEND_PORT):
        result["status"] = "ALIVE"
        status = _http_get(f"http://127.0.0.1:{BACKEND_PORT}/status")
        if status:
            result["details"]["uptime_s"] = status.get("uptime_s", 0)
            result["details"]["version"] = status.get("version", "unknown")
    return result


def check_god_console() -> dict:
    """Check GOD Console on port 8421."""
    result = {"name": "god_console", "port": GOD_CONSOLE_PORT, "status": "DEAD", "details": {}}
    if _is_port_open(GOD_CONSOLE_PORT):
        result["status"] = "ALIVE"
        health = _http_get(f"http://127.0.0.1:{GOD_CONSOLE_PORT}/health")
        if health:
            result["details"] = health
    return result


def check_workers_hwnd() -> dict:
    """Check worker HWNDs via IsWindow."""
    result = {"total": len(ALL_WORKERS), "alive": 0, "dead": [], "workers": {}}
    health_file = DATA / "worker_health.json"
    if not health_file.exists():
        result["error"] = "worker_health.json missing"
        return result
    try:
        health = json.loads(health_file.read_text(encoding="utf-8"))
    except Exception as e:
        result["error"] = str(e)
        return result
    for name in ALL_WORKERS:
        w = health.get(name, {})
        hwnd = int(w.get("hwnd", 0))
        alive = bool(hwnd and ctypes.windll.user32.IsWindow(hwnd))
        result["workers"][name] = {
            "hwnd": hwnd, "alive": alive,
            "status": w.get("status", "UNKNOWN"),
            "model": w.get("model", "unknown"),
        }
        if alive:
            result["alive"] += 1
        else:
            result["dead"].append(name)
    return result


def check_daemons() -> dict:
    """Cross-check PID files against live processes."""
    result = {"total": 0, "alive": 0, "stale": [], "missing": [], "daemons": {}}
    for name, pid_path in KNOWN_DAEMONS.items():
        result["total"] += 1
        if not pid_path.exists():
            result["missing"].append(name)
            result["daemons"][name] = {"status": "NO_PID", "pid": None}
            continue
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            result["stale"].append(name)
            result["daemons"][name] = {"status": "CORRUPT_PID", "pid": None}
            continue
        if _is_pid_alive(pid):
            result["alive"] += 1
            result["daemons"][name] = {"status": "ALIVE", "pid": pid}
        else:
            result["stale"].append(name)
            result["daemons"][name] = {"status": "STALE", "pid": pid}

    # Also check for unknown PID files
    for f in DATA.glob("*.pid"):
        daemon_name = f.stem
        if daemon_name not in KNOWN_DAEMONS:
            try:
                pid = int(f.read_text(encoding="utf-8").strip())
                alive = _is_pid_alive(pid)
            except (ValueError, OSError):
                pid, alive = None, False
            result["daemons"][daemon_name] = {
                "status": "ALIVE" if alive else "STALE",
                "pid": pid, "unknown": True,
            }
            if alive:
                result["alive"] += 1
            else:
                result["stale"].append(daemon_name)
    return result


def check_disk() -> dict:
    """Check disk space and stale/large files in data/."""
    result = {"data_dir_mb": 0, "stale_files": [], "large_files": []}
    try:
        usage = shutil.disk_usage(str(ROOT))
        result["disk_free_gb"] = round(usage.free / (1024 ** 3), 2)
        result["disk_total_gb"] = round(usage.total / (1024 ** 3), 2)
        result["disk_used_pct"] = round((usage.used / usage.total) * 100, 1)
    except Exception:
        pass
    total_size = 0
    now = time.time()
    for f in DATA.iterdir():
        if f.is_file():
            sz = f.stat().st_size
            total_size += sz
            age_days = (now - f.stat().st_mtime) / 86400
            if sz > 10 * 1024 * 1024:
                result["large_files"].append({"name": f.name, "size_mb": round(sz / (1024 ** 2), 2)})
            if age_days > 30 and f.suffix in (".tmp", ".bak", ".old"):
                result["stale_files"].append(f.name)
    result["data_dir_mb"] = round(total_size / (1024 ** 2), 2)
    bus_archive = DATA / "bus_archive.jsonl"
    if bus_archive.exists():
        sz_mb = bus_archive.stat().st_size / (1024 ** 2)
        result["bus_archive_mb"] = round(sz_mb, 2)
        result["bus_archive_needs_compact"] = sz_mb > BUS_ARCHIVE_MAX_MB
    return result


def check_logs() -> dict:
    """Check log directory for old/large files."""
    result = {"total_files": 0, "total_mb": 0, "old_logs": [], "old_images": []}
    if not LOGS.exists():
        return result
    now = time.time()
    total = 0
    for f in LOGS.iterdir():
        if not f.is_file():
            continue
        result["total_files"] += 1
        sz = f.stat().st_size
        total += sz
        age_days = (now - f.stat().st_mtime) / 86400
        if age_days > LOG_ARCHIVE_AGE_DAYS:
            if f.suffix == ".log":
                result["old_logs"].append(f.name)
            elif f.suffix in (".png", ".jpg"):
                result["old_images"].append(f.name)
    result["total_mb"] = round(total / (1024 ** 2), 2)
    return result


# ── Fix functions ────────────────────────────────────────────────

def fix_stale_pids(daemon_result: dict) -> list:
    """Remove PID files for dead processes."""
    fixed = []
    for name in daemon_result.get("stale", []):
        pid_path = KNOWN_DAEMONS.get(name, DATA / f"{name}.pid")
        if pid_path.exists():
            try:
                pid_path.unlink()
                fixed.append({"action": "removed_stale_pid", "daemon": name})
            except OSError as e:
                fixed.append({"action": "failed_remove_pid", "daemon": name, "error": str(e)})
    return fixed


def fix_archive_logs(log_result: dict) -> list:
    """Move old log files to logs/archive/."""
    fixed = []
    archive_dir = LOGS / "archive"
    old_files = log_result.get("old_logs", []) + log_result.get("old_images", [])
    if not old_files:
        return fixed
    archive_dir.mkdir(parents=True, exist_ok=True)
    for name in old_files:
        src = LOGS / name
        dst = archive_dir / name
        if src.exists():
            try:
                shutil.move(str(src), str(dst))
                fixed.append({"action": "archived_log", "file": name})
            except OSError as e:
                fixed.append({"action": "failed_archive", "file": name, "error": str(e)})
    return fixed


def fix_compact_bus_archive(disk_result: dict) -> list:
    """Compact bus archive by keeping only last 5000 lines."""
    fixed = []
    if not disk_result.get("bus_archive_needs_compact"):
        return fixed
    archive = DATA / "bus_archive.jsonl"
    if not archive.exists():
        return fixed
    try:
        lines = archive.read_text(encoding="utf-8", errors="replace").splitlines()
        original_count = len(lines)
        keep = 5000
        if len(lines) > keep:
            compacted = lines[-keep:]
            tmp = archive.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(compacted) + "\n", encoding="utf-8")
            backup = archive.with_suffix(".jsonl.bak")
            if backup.exists():
                backup.unlink()
            archive.rename(backup)
            tmp.rename(archive)
            new_sz = round(archive.stat().st_size / (1024 ** 2), 2)
            fixed.append({"action": "compacted_bus_archive",
                          "lines_before": original_count, "lines_after": keep,
                          "size_mb": new_sz})
        else:
            fixed.append({"action": "bus_archive_ok", "lines": original_count})
    except Exception as e:
        fixed.append({"action": "compact_failed", "error": str(e)})
    return fixed


def fix_restart_backend() -> list:
    """Restart crashed backend if skynet.exe exists."""
    fixed = []
    exe = SKYNET_DIR / "skynet.exe"
    if not exe.exists():
        fixed.append({"action": "skip_backend_restart", "reason": "skynet.exe not found"})
        return fixed
    try:
        proc = subprocess.Popen(
            [str(exe)], cwd=str(SKYNET_DIR),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x00000008 | 0x00000200,
        )
        time.sleep(3)
        if _is_port_open(BACKEND_PORT, timeout=2.0):
            fixed.append({"action": "restarted_backend", "pid": proc.pid})
        else:
            fixed.append({"action": "backend_restart_pending", "pid": proc.pid})
    except Exception as e:
        fixed.append({"action": "backend_restart_failed", "error": str(e)})
    return fixed


def fix_restart_god_console() -> list:
    """Restart GOD Console if dead."""
    fixed = []
    script = ROOT / "god_console.py"
    if not script.exists():
        fixed.append({"action": "skip_god_console_restart", "reason": "god_console.py not found"})
        return fixed
    try:
        proc = subprocess.Popen(
            [sys.executable, str(script)], cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x00000008 | 0x00000200,
        )
        time.sleep(2)
        if _is_port_open(GOD_CONSOLE_PORT, timeout=2.0):
            fixed.append({"action": "restarted_god_console", "pid": proc.pid})
        else:
            fixed.append({"action": "god_console_restart_pending", "pid": proc.pid})
    except Exception as e:
        fixed.append({"action": "god_console_restart_failed", "error": str(e)})
    return fixed


# ── Comprehensive check + fix orchestration ──────────────────────

def run_system_checks() -> dict:
    """Run all health checks and return structured report."""
    report = {
        "timestamp": _ts(),
        "backend": check_backend(),
        "god_console": check_god_console(),
        "workers": check_workers_hwnd(),
        "daemons": check_daemons(),
        "disk": check_disk(),
        "logs": check_logs(),
        "stuck_tasks": detect_stuck_tasks(),
        "issues": [],
        "summary": {},
    }
    issues = report["issues"]
    if report["backend"]["status"] == "DEAD":
        issues.append({"severity": "CRITICAL", "system": "backend", "msg": "Backend is DOWN"})
    if report["god_console"]["status"] == "DEAD":
        issues.append({"severity": "HIGH", "system": "god_console", "msg": "GOD Console is DOWN"})
    for w in report["workers"].get("dead", []):
        issues.append({"severity": "HIGH", "system": "workers", "msg": f"Worker {w} HWND dead"})
    for d in report["daemons"].get("stale", []):
        issues.append({"severity": "MEDIUM", "system": "daemons", "msg": f"Stale PID: {d}"})
    if report["disk"].get("bus_archive_needs_compact"):
        sz = report["disk"].get("bus_archive_mb", 0)
        issues.append({"severity": "LOW", "system": "disk",
                        "msg": f"Bus archive {sz}MB > {BUS_ARCHIVE_MAX_MB}MB"})
    if len(report["logs"].get("old_logs", [])) > 10:
        issues.append({"severity": "LOW", "system": "logs",
                        "msg": f"{len(report['logs']['old_logs'])} old log files"})
    if report["stuck_tasks"]:
        issues.append({"severity": "HIGH", "system": "dispatch",
                        "msg": f"{len(report['stuck_tasks'])} stuck tasks"})

    report["summary"] = {
        "critical": sum(1 for i in issues if i["severity"] == "CRITICAL"),
        "high": sum(1 for i in issues if i["severity"] == "HIGH"),
        "medium": sum(1 for i in issues if i["severity"] == "MEDIUM"),
        "low": sum(1 for i in issues if i["severity"] == "LOW"),
        "total_issues": len(issues),
        "backend": report["backend"]["status"],
        "god_console": report["god_console"]["status"],
        "workers_alive": report["workers"]["alive"],
        "workers_total": report["workers"]["total"],
        "daemons_alive": report["daemons"]["alive"],
        "daemons_stale": len(report["daemons"].get("stale", [])),
    }
    return report


def run_system_fixes(report: dict) -> list:
    """Apply all auto-fixes based on check results."""
    all_fixes = []
    all_fixes.extend(fix_stale_pids(report["daemons"]))
    all_fixes.extend(fix_archive_logs(report["logs"]))
    all_fixes.extend(fix_compact_bus_archive(report["disk"]))
    if report["backend"]["status"] == "DEAD":
        all_fixes.extend(fix_restart_backend())
    if report["god_console"]["status"] == "DEAD":
        all_fixes.extend(fix_restart_god_console())
    # Auto-heal stuck tasks
    if report.get("stuck_tasks"):
        actions = auto_heal()
        for a in actions:
            all_fixes.append({"action": f"heal_{a['action']}", "worker": a["worker"]})
    return all_fixes


def save_health_report(report: dict, fixes: list = None):
    """Save health report to data/health_report.json."""
    report["fixes_applied"] = fixes or []
    report["generated_at"] = _ts()
    out = DATA / "health_report.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return out


def print_system_report(report: dict, fixes: list = None, as_json: bool = False):
    """Print human-readable or JSON report."""
    if as_json:
        print(json.dumps({**report, "fixes_applied": fixes or []}, indent=2, default=str))
        return

    s = report["summary"]
    print("=" * 60)
    print(f"  SKYNET SELF-HEAL REPORT  --  {report['timestamp']}")
    print("=" * 60)

    be = report["backend"]
    icon = "+" if be["status"] == "ALIVE" else "X"
    uptime = be["details"].get("uptime_s", "?")
    print(f"\n  Backend:      [{icon}] {be['status']}  (port {be['port']}, uptime {uptime}s)")

    gc = report["god_console"]
    icon = "+" if gc["status"] == "ALIVE" else "X"
    print(f"  GOD Console:  [{icon}] {gc['status']}  (port {gc['port']})")

    wk = report["workers"]
    print(f"\n  Workers: {wk['alive']}/{wk['total']} alive")
    for name, info in wk.get("workers", {}).items():
        icon = "+" if info["alive"] else "X"
        print(f"    [{icon}] {name:8s}  HWND={info['hwnd']}  {info['status']}")

    dm = report["daemons"]
    print(f"\n  Daemons: {dm['alive']} alive, {len(dm.get('stale', []))} stale, "
          f"{len(dm.get('missing', []))} no PID")
    for name, info in sorted(dm.get("daemons", {}).items()):
        st = info["status"]
        icon = "+" if st == "ALIVE" else ("X" if st == "STALE" else "-")
        pid_str = f"PID={info['pid']}" if info.get("pid") else ""
        print(f"    [{icon}] {name:30s}  {pid_str} {st}")

    dk = report["disk"]
    print(f"\n  Disk: {dk.get('disk_free_gb', '?')}GB free ({dk.get('disk_used_pct', '?')}% used)")
    print(f"  Data dir: {dk.get('data_dir_mb', '?')}MB")
    if dk.get("bus_archive_mb"):
        flag = " !! NEEDS COMPACT" if dk.get("bus_archive_needs_compact") else ""
        print(f"  Bus archive: {dk['bus_archive_mb']}MB{flag}")

    lg = report["logs"]
    print(f"\n  Logs: {lg['total_files']} files, {lg['total_mb']}MB total")
    if lg.get("old_logs"):
        print(f"    Old logs: {len(lg['old_logs'])} files (>{LOG_ARCHIVE_AGE_DAYS}d)")

    if report.get("stuck_tasks"):
        print(f"\n  Stuck tasks: {len(report['stuck_tasks'])}")
        for st in report["stuck_tasks"]:
            print(f"    {st['worker']}: {st['stuck_seconds']}s ({st['severity']})")

    if report["issues"]:
        print(f"\n  Issues ({s['total_issues']}):")
        for iss in report["issues"]:
            print(f"    [{iss['severity']:8s}] {iss['msg']}")
    else:
        print("\n  [+] No issues detected")

    if fixes:
        print(f"\n  Fixes applied ({len(fixes)}):")
        for fix in fixes:
            target = fix.get("daemon", fix.get("file", fix.get("worker", "")))
            print(f"    -> {fix['action']}: {target}")

    print("\n" + "=" * 60)


def daemon_loop(fix_mode: bool = True, as_json: bool = False):
    """Run self-heal every DAEMON_INTERVAL seconds."""
    pid_file = DATA / "self_heal.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    def _cleanup(signum=None, frame=None):
        pid_file.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    try:
        signal.signal(signal.SIGBREAK, _cleanup)
    except AttributeError:
        pass  # SIGBREAK is Windows-only

    print(f"[SELF-HEAL] Daemon started (PID {os.getpid()}, interval {DAEMON_INTERVAL}s)")
    try:
        while True:
            report = run_system_checks()
            fixes = run_system_fixes(report) if fix_mode else []
            save_health_report(report, fixes)
            ts = _ts()
            iss_count = report["summary"]["total_issues"]
            fix_count = len(fixes)
            print(f"[{ts}] Check: {iss_count} issues, {fix_count} fixes")
            time.sleep(DAEMON_INTERVAL)
    except KeyboardInterrupt:
        _cleanup()


def _get_worker_states() -> dict[str, dict]:
    """Read worker states from realtime.json (zero-network)."""
    data = _load_json(REALTIME_FILE)
    if not data:
        return {}
    workers = data.get("workers", {})
    result = {}
    for name, info in workers.items():
        if isinstance(info, dict):
            result[name] = {
                "state": info.get("state", "UNKNOWN"),
                "since": info.get("since", 0),
                "task": info.get("task", ""),
            }
        else:
            result[name] = {"state": str(info), "since": 0, "task": ""}
    return result


def _get_dispatch_log() -> list[dict]:
    """Read dispatch log entries."""
    data = _load_json(DISPATCH_LOG)
    if isinstance(data, list):
        return data
    return data.get("dispatches", data.get("log", []))


def detect_stuck_tasks(threshold_s: Optional[float] = None) -> list[dict]:
    """Detect tasks that appear stuck based on worker state and timing.

    Returns list of {worker, state, stuck_seconds, task, severity}.
    """
    threshold = threshold_s or STUCK_THRESHOLDS["standard"]
    workers = _get_worker_states()
    now = time.time()
    stuck = []

    for name, info in workers.items():
        state = info.get("state", "UNKNOWN")
        if state != "PROCESSING":
            continue

        since = info.get("since", 0)
        if since <= 0:
            continue

        elapsed = now - since
        if elapsed > threshold:
            severity = "critical" if elapsed > threshold * 2 else "warning"
            stuck.append({
                "worker": name,
                "state": state,
                "stuck_seconds": round(elapsed, 1),
                "task": info.get("task", "unknown"),
                "severity": severity,
                "threshold": threshold,
            })

    return stuck


def _try_cancel_worker(worker: str) -> tuple[bool, str | None]:
    """Attempt to cancel a stuck worker via UIA. Returns (cancelled, error)."""
    try:
        from tools.uia_engine import get_engine
        workers_json = ROOT / "data" / "workers.json"
        if workers_json.exists():
            wdata = json.loads(workers_json.read_text(encoding="utf-8"))
            hwnd = wdata.get(worker, {}).get("hwnd")
            if hwnd:
                engine = get_engine()
                engine.cancel_generation(hwnd)
                return True, None
    except Exception as e:
        return False, str(e)
    return False, None


def _log_and_broadcast_heals(actions: list[dict]):
    """Log heal actions to file and broadcast summary to bus."""
    log_data = _load_json(HEAL_LOG)
    if not isinstance(log_data, list):
        log_data = []
    log_data.append({
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "actions": actions,
    })
    _save_json(HEAL_LOG, log_data[-100:])

    try:
        from tools.skynet_spam_guard import guarded_publish
        summary = "; ".join(f"{a['worker']}:{a['action']}" for a in actions)
        guarded_publish({  # signed: gamma
            "sender": "self_heal",
            "topic": "orchestrator",
            "type": "alert",
            "content": f"AUTO-HEAL: {summary}",
        })
    except Exception:
        pass


def auto_heal(dry_run: bool = False) -> list[dict]:
    """Auto-heal stuck tasks by cancelling and optionally re-dispatching.

    Args:
        dry_run: If True, report what would be done without acting.

    Returns list of {worker, action, result} dicts.
    """
    stuck = detect_stuck_tasks()
    actions = []

    for task in stuck:
        worker = task["worker"]
        action = {
            "worker": worker,
            "stuck_seconds": task["stuck_seconds"],
            "severity": task["severity"],
            "task_text": task.get("task", ""),
        }

        if dry_run:
            action["action"] = "would_cancel"
            action["result"] = "dry_run"
        else:
            cancelled, err = _try_cancel_worker(worker)
            if err:
                action["cancel_error"] = err
            action["action"] = "cancelled" if cancelled else "cancel_failed"
            action["result"] = "success" if cancelled else "manual_intervention_needed"

        actions.append(action)

    if actions and not dry_run:
        _log_and_broadcast_heals(actions)

    return actions


def health_report() -> dict:
    """Generate comprehensive dispatch health report.

    Returns dict with worker_states, stuck_tasks, recent_heals,
    dispatch_stats, and recommendations.
    """
    workers = _get_worker_states()
    stuck = detect_stuck_tasks()
    heal_log = _load_json(HEAL_LOG)
    if not isinstance(heal_log, list):
        heal_log = []
    perf = _load_json(WORKER_PERF)

    # Worker state summary
    state_counts = {}
    for w in workers.values():
        st = w.get("state", "UNKNOWN")
        state_counts[st] = state_counts.get(st, 0) + 1

    # Dispatch stats
    perf_workers = perf.get("workers", {})
    total_completed = sum(w.get("tasks_completed", 0) for w in perf_workers.values())
    total_failed = sum(w.get("tasks_failed", 0) for w in perf_workers.values())

    # Recommendations
    recommendations = []
    if len(stuck) > 0:
        recommendations.append(f"URGENT: {len(stuck)} stuck task(s) detected -- run auto_heal()")
    if total_failed > total_completed * 0.3 and total_completed > 0:
        recommendations.append("HIGH FAILURE RATE: >30% tasks failing. Investigate root cause.")

    idle_count = state_counts.get("IDLE", 0)
    processing_count = state_counts.get("PROCESSING", 0)
    if idle_count > 2 and processing_count == 0:
        recommendations.append("UNDERUTILIZED: Multiple idle workers. Dispatch more tasks.")

    if not recommendations:
        recommendations.append("System healthy. No issues detected.")

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "worker_states": workers,
        "state_summary": state_counts,
        "stuck_tasks": stuck,
        "recent_heals": heal_log[-5:],
        "dispatch_stats": {
            "total_completed": total_completed,
            "total_failed": total_failed,
            "success_rate": round(
                total_completed / max(1, total_completed + total_failed) * 100, 1
            ),
        },
        "recommendations": recommendations,
    }


def run_continuous(interval_s: float = 30.0, max_iterations: int = 0):
    """Run continuous self-healing monitoring loop.

    Args:
        interval_s: Check interval in seconds.
        max_iterations: Max loops (0 = infinite).
    """
    iteration = 0
    print(f"[SELF-HEAL] Starting continuous monitor (interval={interval_s}s)")

    while max_iterations == 0 or iteration < max_iterations:
        iteration += 1
        try:
            stuck = detect_stuck_tasks()
            if stuck:
                print(f"[SELF-HEAL] Iteration {iteration}: {len(stuck)} stuck task(s) found")
                actions = auto_heal()
                for a in actions:
                    print(f"  {a['worker']}: {a['action']} ({a.get('result', 'unknown')})")
            else:
                if iteration % 10 == 0:
                    print(f"[SELF-HEAL] Iteration {iteration}: all clear")
        except Exception as e:
            print(f"[SELF-HEAL] Error in iteration {iteration}: {e}")

        time.sleep(interval_s)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    # Legacy subcommand interface (detect, heal, report, run)
    if len(sys.argv) > 1 and sys.argv[1] in ("detect", "heal", "report", "run"):
        cmd = sys.argv[1]
        if cmd == "detect":
            stuck = detect_stuck_tasks()
            if stuck:
                print(f"Found {len(stuck)} stuck task(s):")
                for s in stuck:
                    print(f"  {s['worker']}: {s['stuck_seconds']}s ({s['severity']})")
            else:
                print("No stuck tasks detected.")
        elif cmd == "heal":
            dry = "--dry-run" in sys.argv
            actions = auto_heal(dry_run=dry)
            if actions:
                for a in actions:
                    print(f"  {a['worker']}: {a['action']} ({a.get('result', 'unknown')})")
            else:
                print("Nothing to heal.")
        elif cmd == "report":
            report = health_report()
            print(json.dumps(report, indent=2))
        elif cmd == "run":
            interval = 30.0
            for arg in sys.argv[2:]:
                if arg.startswith("--interval="):
                    interval = float(arg.split("=")[1])
            run_continuous(interval_s=interval)
        return

    # New comprehensive interface
    parser = argparse.ArgumentParser(description="Skynet Self-Heal -- automated recovery system")
    parser.add_argument("--check-only", action="store_true", help="Diagnose without fixing")
    parser.add_argument("--fix", action="store_true", help="Explicit fix mode (default)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--daemon", action="store_true", help="Run every 5 minutes")
    args = parser.parse_args()

    if args.daemon:
        daemon_loop(fix_mode=not args.check_only, as_json=args.json)
        return

    report = run_system_checks()
    fixes = [] if args.check_only else run_system_fixes(report)
    save_health_report(report, fixes)
    print_system_report(report, fixes, as_json=args.json)


if __name__ == "__main__":
    main()
