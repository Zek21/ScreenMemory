#!/usr/bin/env python3
"""
skynet_watchdog.py -- Lightweight daemon that monitors and auto-restarts Skynet services.

Checks:
  - GOD Console (port 8421) every 30s -- auto-restarts if down
  - Skynet backend (port 8420) every 60s -- logs alert if down

Usage:
    python tools/skynet_watchdog.py start    # Run daemon (blocking)
    python tools/skynet_watchdog.py status   # Show last check results
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import psutil
except Exception:
    psutil = None

ROOT = Path(__file__).resolve().parent.parent
# Ensure repo root is on sys.path so 'tools.*' imports work when run standalone
sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "data"
LOG_FILE = DATA_DIR / "watchdog.log"
STATUS_FILE = DATA_DIR / "watchdog_status.json"
PID_FILE = DATA_DIR / "watchdog.pid"

SKYNET_URL = "http://localhost:8420"
GOD_CONSOLE_URL = "http://localhost:8421"

# Resolve real Python interpreter to avoid venv trampoline double-process.
# On Python 3.13+ Windows, the venv python.exe is a launcher that spawns
# the real interpreter as a child process, doubling every daemon's PID count.
def _resolve_real_python():
    """Return (real_python_path, env_dict) bypassing the venv trampoline."""
    venv_dir = ROOT.parent / "env"
    cfg = venv_dir / "pyvenv.cfg"
    base_python = None
    if cfg.exists():
        for line in cfg.read_text().splitlines():
            if line.strip().startswith("executable"):
                _, _, val = line.partition("=")
                candidate = val.strip()
                if Path(candidate).exists():
                    base_python = candidate
                    break
    if not base_python:
        base_python = str(Path(sys.executable))
    import os as _os
    env = _os.environ.copy()
    site_packages = str(venv_dir / "Lib" / "site-packages")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{site_packages};{existing}" if existing else site_packages
    env["VIRTUAL_ENV"] = str(venv_dir)
    return base_python, env

PYTHON, _DAEMON_ENV = _resolve_real_python()

GOD_CHECK_INTERVAL = 30
SKYNET_CHECK_INTERVAL = 60


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


def _hidden_check_output(args, **kwargs):
    return subprocess.check_output(args, **_hidden_subprocess_kwargs(**kwargs))


def _read_state_timestamp_age(path: Path, *keys: str) -> tuple[object | None, float | None]:
    """Read a JSON timestamp field and return (raw_value, age_seconds)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, None

    raw_value = None
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            raw_value = value
            break

    if raw_value in (None, ""):
        return None, None

    try:
        if isinstance(raw_value, str):
            dt = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            if dt.tzinfo:
                age = (datetime.now(timezone.utc) - dt).total_seconds()
            else:
                age = (datetime.now() - dt).total_seconds()
        else:
            age = time.time() - float(raw_value)
        return raw_value, max(age, 0.0)
    except Exception:
        return raw_value, None


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    DATA_DIR.mkdir(exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def check_url(url: str, timeout: int = 5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def restart_god_console():
    """Restart god_console.py as a hidden background process."""
    log("GOD Console DOWN -- attempting restart")
    old_pid = _get_service_pid("god_console")
    try:
        subprocess.Popen(
            [PYTHON, "god_console.py", "--no-open"],
            cwd=str(ROOT),
            env=_DAEMON_ENV,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )
        time.sleep(3)
        if check_url(f"{GOD_CONSOLE_URL}/health"):
            new_pid = _get_service_pid("god_console")
            log(f"GOD Console restarted (old={old_pid}, new={new_pid})")
            _post_restart_alert("god_console.py", old_pid, new_pid)
            _refresh_protected_registry()
            _log_incident("god_console_restart", old_pid, new_pid)
            return True
        else:
            log("GOD Console restart FAILED -- still not responding")
            return False
    except Exception as e:
        log(f"GOD Console restart error: {e}")
        return False


def restart_skynet():
    """Restart skynet.exe as a hidden background process."""
    log("Skynet backend DOWN -- attempting restart")
    old_pid = _get_service_pid("skynet")
    try:
        subprocess.Popen(
            [str(ROOT / "Skynet" / "skynet.exe")],
            cwd=str(ROOT / "Skynet"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )
        time.sleep(3)
        if check_url(f"{SKYNET_URL}/status"):
            new_pid = _get_service_pid("skynet")
            log(f"Skynet backend restarted (old={old_pid}, new={new_pid})")
            _post_restart_alert("skynet.exe", old_pid, new_pid)
            _refresh_protected_registry()
            _log_incident("skynet_restart", old_pid, new_pid)
            return True
        else:
            log("Skynet backend restart FAILED -- still not responding")
            return False
    except Exception as e:
        log(f"Skynet backend restart error: {e}")
        return False


def restart_sse_daemon():
    """Restart skynet_sse_daemon.py as a hidden background process."""
    # ── PID guard: check if SSE daemon is already running via its PID file ──
    sse_pid_file = DATA_DIR / "sse_daemon.pid"
    if sse_pid_file.exists():
        try:
            old_pid_val = int(sse_pid_file.read_text().strip())
            import os
            os.kill(old_pid_val, 0)  # check if alive (signal 0 = no-op)
            log(f"SSE daemon already running (PID {old_pid_val}) -- skipping restart")
            return True
        except (OSError, ValueError):
            pass  # Stale PID file -- proceed with restart

    log("SSE daemon DOWN -- attempting restart")
    old_pid = _get_service_pid("sse_daemon")
    try:
        subprocess.Popen(
            [PYTHON, str(ROOT / "tools" / "skynet_sse_daemon.py")],
            cwd=str(ROOT),
            env=_DAEMON_ENV,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )
        time.sleep(3)
        # Verify by checking realtime.json freshness
        rt_file = DATA_DIR / "realtime.json"
        if rt_file.exists():
            try:
                _, age = _read_state_timestamp_age(rt_file, "last_update", "timestamp")
                if age is not None and age < 10:
                    new_pid = _get_service_pid("sse_daemon")
                    log(f"SSE daemon restarted (old={old_pid}, new={new_pid})")
                    _post_restart_alert("skynet_sse_daemon.py", old_pid, new_pid)
                    _refresh_protected_registry()
                    _log_incident("sse_daemon_restart", old_pid, new_pid)
                    return True
            except Exception:
                pass
        log("SSE daemon restart -- cannot verify (may be starting)")
        return True  # optimistic; next check cycle will verify
    except Exception as e:
        log(f"SSE daemon restart error: {e}")
        return False


def restart_learner():
    """Restart skynet_learner.py as a hidden background daemon."""
    learner_pid_file = DATA_DIR / "learner.pid"
    if learner_pid_file.exists():
        try:
            old_pid_val = int(learner_pid_file.read_text().strip())
            import os
            os.kill(old_pid_val, 0)
            log(f"Learner daemon already running (PID {old_pid_val}) -- skipping restart")
            return True
        except (OSError, ValueError):
            pass  # Stale PID file -- proceed with restart

    log("Learner daemon DOWN -- attempting restart")
    old_pid = _get_service_pid("learner")
    try:
        subprocess.Popen(
            [PYTHON, str(ROOT / "tools" / "skynet_learner.py"), "--daemon"],
            cwd=str(ROOT),
            env=_DAEMON_ENV,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )
        time.sleep(3)
        if learner_pid_file.exists():
            try:
                new_pid_val = int(learner_pid_file.read_text().strip())
                import os
                os.kill(new_pid_val, 0)
                log(f"Learner daemon restarted (old={old_pid}, new={new_pid_val})")
                _post_restart_alert("skynet_learner.py", old_pid, new_pid_val)
                _refresh_protected_registry()
                _log_incident("learner_restart", old_pid, new_pid_val)
                return True
            except (OSError, ValueError):
                pass
        log("Learner daemon restart -- cannot verify (may be starting)")
        return True
    except Exception as e:
        log(f"Learner daemon restart error: {e}")
        return False


def _get_service_pid(service_name):
    """Get the PID of a known service by name. Returns 0 if not found."""
    try:
        if service_name == "skynet":
            pid = _port_to_pid(8420)
            if pid:
                return pid
            if psutil is not None:
                for proc in psutil.process_iter(["pid", "name"]):
                    try:
                        if (proc.info.get("name") or "").lower() == "skynet.exe":
                            return int(proc.info["pid"])
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
        elif service_name == "god_console":
            pid = _port_to_pid(8421)
            if pid:
                return pid
            if psutil is not None:
                for proc in psutil.process_iter(["pid", "cmdline"]):
                    try:
                        cmdline = " ".join(proc.info.get("cmdline") or [])
                        if "god_console.py" in cmdline:
                            return int(proc.info["pid"])
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
        elif service_name == "sse_daemon":
            # Find python processes running skynet_sse_daemon
            out = _hidden_check_output(
                ["wmic", "process", "where", "Name like '%python%'",
                 "get", "ProcessId,CommandLine", "/format:csv"],
                text=True, timeout=10, stderr=subprocess.DEVNULL
            )
            for line in out.strip().split("\n"):
                if "skynet_sse_daemon" in line:
                    parts = line.strip().split(",")
                    if parts:
                        try:
                            return int(parts[-1].strip())
                        except ValueError:
                            pass
        elif service_name == "learner":
            learner_pid_file = DATA_DIR / "learner.pid"
            if learner_pid_file.exists():
                try:
                    pid_val = int(learner_pid_file.read_text().strip())
                    import os
                    os.kill(pid_val, 0)
                    return pid_val
                except (OSError, ValueError):
                    pass
    except Exception:
        pass
    return 0


def _port_to_pid(port):
    """Get PID listening on a port without shelling out."""
    if psutil is not None:
        try:
            for conn in psutil.net_connections(kind="tcp"):
                laddr = getattr(conn, "laddr", None)
                if not laddr:
                    continue
                if getattr(laddr, "port", None) != port:
                    continue
                if conn.status != psutil.CONN_LISTEN:
                    continue
                if conn.pid:
                    return int(conn.pid)
        except Exception:
            pass
    return 0


def _post_restart_alert(service_name, old_pid, new_pid):
    """Post a SERVICE_RESTARTED alert to the bus."""
    try:
        data = json.dumps({
            "sender": "watchdog",
            "topic": "orchestrator",
            "type": "alert",
            "content": f"SERVICE_RESTARTED: {service_name}, old_pid={old_pid}, new_pid={new_pid}"
        }).encode()
        req = urllib.request.Request(
            "http://localhost:8420/bus/publish", data=data,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        log(f"Failed to post restart alert for {service_name}")


def _refresh_protected_registry():
    """Refresh critical_processes.json after a service restart."""
    try:
        from tools.skynet_process_guard import refresh_registry
        refresh_registry()
        log("Protected process registry refreshed")
    except Exception as e:
        log(f"Registry refresh failed: {e}")


def _log_incident(incident_id, old_pid, new_pid):
    """Log a restart incident to data/incidents.json and knowledge system."""
    try:
        from tools.skynet_knowledge import learn_incident
        learn_incident(
            incident_id=f"auto_restart_{incident_id}_{int(time.time())}",
            what_happened=f"Service {incident_id} died (PID {old_pid})",
            root_cause="Service process exited or became unresponsive",
            fix_applied=f"Watchdog auto-restarted service (new PID {new_pid})",
            rule_created="Auto-restart on health check failure"
        )
    except Exception as e:
        log(f"Incident logging failed: {e}")
    # Also append to data/incidents.json directly as backup
    try:
        incidents_file = DATA_DIR / "incidents.json"
        incidents = []
        if incidents_file.exists():
            incidents = json.loads(incidents_file.read_text(encoding="utf-8"))
        incidents.append({
            "id": incident_id,
            "timestamp": datetime.now().isoformat(),
            "old_pid": old_pid,
            "new_pid": new_pid,
            "type": "auto_restart",
        })
        incidents_file.write_text(json.dumps(incidents[-100:], indent=2), encoding="utf-8")
    except Exception:
        pass


def _check_worker_hwnds():
    """Check if worker window HWNDs are still valid using Win32 IsWindow().

    Returns list of dead workers (name, hwnd).
    """
    import ctypes
    user32 = ctypes.windll.user32
    dead = []
    workers_file = DATA_DIR / "workers.json"
    if not workers_file.exists():
        return dead
    try:
        wdata = json.loads(workers_file.read_text(encoding="utf-8"))
        workers = wdata.get("workers", [])
        if isinstance(workers, list):
            for w in workers:
                hwnd = w.get("hwnd", 0)
                name = w.get("name", "unknown")
                if hwnd and not user32.IsWindow(int(hwnd)):
                    dead.append({"name": name, "hwnd": hwnd})
    except Exception:
        pass
    return dead


# ── Heartbeat tracking ──────────────────────────────────────

HEARTBEAT_FILE = DATA_DIR / "service_heartbeats.json"
HEARTBEAT_TIMEOUT = 60  # seconds before a service is considered dead

def _update_heartbeat(service_name, is_alive):
    """Update the heartbeat timestamp for a service."""
    try:
        heartbeats = {}
        if HEARTBEAT_FILE.exists():
            heartbeats = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
        if is_alive:
            heartbeats[service_name] = {
                "last_seen": time.time(),
                "last_seen_ts": datetime.now().isoformat(),
                "status": "alive",
            }
        else:
            entry = heartbeats.get(service_name, {})
            entry["status"] = "dead"
            heartbeats[service_name] = entry
        HEARTBEAT_FILE.write_text(json.dumps(heartbeats, indent=2), encoding="utf-8")
    except Exception:
        pass


def _is_service_stale(service_name):
    """Check if a service heartbeat is stale (>HEARTBEAT_TIMEOUT seconds old)."""
    try:
        if HEARTBEAT_FILE.exists():
            heartbeats = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
            entry = heartbeats.get(service_name, {})
            last_seen = entry.get("last_seen", 0)
            if last_seen and (time.time() - last_seen) > HEARTBEAT_TIMEOUT:
                return True
    except Exception:
        pass
    return False


def write_status(status: dict):
    DATA_DIR.mkdir(exist_ok=True)
    status["updated"] = datetime.now().isoformat()
    STATUS_FILE.write_text(json.dumps(status, indent=2))


def run_daemon(args=None):
    """Main watchdog loop."""
    import os
    max_runtime = getattr(args, 'max_runtime', 0) if args else 0
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            os.kill(old_pid, 0)  # check if alive
            # Verify it's actually a watchdog process (not a recycled PID)
            import subprocess
            result = _hidden_run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {old_pid}\").CommandLine"],
                capture_output=True, text=True, timeout=5
            )
            if "watchdog" in result.stdout.lower():
                log(f"Watchdog already running (PID {old_pid}) -- exiting")
                return
            else:
                log(f"Stale PID file (PID {old_pid} is not a watchdog) -- taking over")
        except (OSError, ValueError, Exception):
            pass
    PID_FILE.write_text(str(os.getpid()))

    log("Skynet Watchdog v2 starting (auto-recovery + heartbeat + HWND checks)")
    start_time = time.time()
    cycle = 0
    last_god_check = 0.0
    last_skynet_check = 0.0
    last_sse_check = 0.0
    last_awareness = 0.0
    last_window_scan = 0.0
    last_stuck_check = 0.0
    last_guard_refresh = 0.0
    last_hwnd_check = 0.0
    last_learner_check = 0.0
    AWARENESS_INTERVAL = 60
    WINDOW_SCAN_INTERVAL = 30
    STUCK_CHECK_INTERVAL = 30
    GUARD_REFRESH_INTERVAL = 60
    SSE_CHECK_INTERVAL = 30
    HWND_CHECK_INTERVAL = 30
    LEARNER_CHECK_INTERVAL = 60
    stuck_detector = None
    status = {"god_console": "unknown", "skynet": "unknown", "sse_daemon": "unknown", "learner": "unknown"}

    try:
        while True:
            now = time.time()
            cycle += 1

            # Max runtime guard
            if max_runtime and (now - start_time) >= max_runtime:
                log(f"Max runtime {max_runtime}s reached -- shutting down gracefully")
                break

            # ── GOD Console check + auto-restart ──
            if now - last_god_check >= GOD_CHECK_INTERVAL:
                god_ok = check_url(f"{GOD_CONSOLE_URL}/health")
                _update_heartbeat("god_console", god_ok)
                if god_ok:
                    status["god_console"] = "ok"
                else:
                    restarted = restart_god_console()
                    status["god_console"] = "restarted" if restarted else "down"
                status["god_last_check"] = datetime.now().isoformat()
                last_god_check = now

            # ── Skynet backend check + auto-restart ──
            if now - last_skynet_check >= SKYNET_CHECK_INTERVAL:
                skynet_ok = check_url(f"{SKYNET_URL}/status")
                _update_heartbeat("skynet", skynet_ok)
                if skynet_ok:
                    status["skynet"] = "ok"
                else:
                    restarted = restart_skynet()
                    status["skynet"] = "restarted" if restarted else "down"
                    if not restarted:
                        log("Skynet backend restart FAILED -- manual intervention required")
                status["skynet_last_check"] = datetime.now().isoformat()
                last_skynet_check = now

            # ── SSE daemon check + auto-restart ──
            if now - last_sse_check >= SSE_CHECK_INTERVAL:
                sse_ok = False
                # Check 1: PID file alive (process exists even if reconnecting)
                sse_pid_file = DATA_DIR / "sse_daemon.pid"
                if sse_pid_file.exists():
                    try:
                        sse_pid_val = int(sse_pid_file.read_text().strip())
                        import os
                        os.kill(sse_pid_val, 0)
                        sse_ok = True  # process alive, might be in reconnect backoff
                    except (OSError, ValueError):
                        pass  # stale PID

                # Check 2: realtime.json freshness (confirms active streaming)
                if not sse_ok:
                    rt_file = DATA_DIR / "realtime.json"
                    if rt_file.exists():
                        try:
                            _, age = _read_state_timestamp_age(rt_file, "last_update", "timestamp")
                            sse_ok = age is not None and age < 15
                        except Exception:
                            pass

                _update_heartbeat("sse_daemon", sse_ok)
                if sse_ok:
                    status["sse_daemon"] = "ok"
                else:
                    restarted = restart_sse_daemon()
                    status["sse_daemon"] = "restarted" if restarted else "down"
                status["sse_last_check"] = datetime.now().isoformat()
                last_sse_check = now

            # ── Learner daemon check + auto-restart ──
            if now - last_learner_check >= LEARNER_CHECK_INTERVAL:
                learner_ok = False
                learner_pid_file = DATA_DIR / "learner.pid"
                if learner_pid_file.exists():
                    try:
                        learner_pid_val = int(learner_pid_file.read_text().strip())
                        import os
                        os.kill(learner_pid_val, 0)
                        learner_ok = True
                    except (OSError, ValueError):
                        pass
                _update_heartbeat("learner", learner_ok)
                if learner_ok:
                    status["learner"] = "ok"
                else:
                    restarted = restart_learner()
                    status["learner"] = "restarted" if restarted else "down"
                status["learner_last_check"] = datetime.now().isoformat()
                last_learner_check = now

            # ── Worker HWND health check ──
            if now - last_hwnd_check >= HWND_CHECK_INTERVAL:
                try:
                    dead_workers = _check_worker_hwnds()
                    status["dead_worker_windows"] = len(dead_workers)
                    status["hwnd_last_check"] = datetime.now().isoformat()
                    if dead_workers:
                        names = [d["name"] for d in dead_workers]
                        log(f"CRITICAL: Worker window(s) GONE: {names}")
                        try:
                            alert = json.dumps({
                                "sender": "watchdog",
                                "topic": "orchestrator",
                                "type": "alert",
                                "content": f"WORKER_WINDOW_DEAD: {', '.join(names)} -- HWNDs invalid"
                            }).encode()
                            req = urllib.request.Request(
                                "http://localhost:8420/bus/publish", data=alert,
                                headers={"Content-Type": "application/json"}
                            )
                            urllib.request.urlopen(req, timeout=3)
                        except Exception:
                            pass
                except Exception as e:
                    log(f"Worker HWND check failed: {e}")
                last_hwnd_check = now

            # Periodic window scan
            if now - last_window_scan >= WINDOW_SCAN_INTERVAL:
                try:
                    from tools.skynet_windows import scan_windows, save_registry
                    registry = scan_windows()
                    save_registry(registry)
                    s = registry["summary"]
                    status["windows"] = s["total_windows"]
                    status["dead_windows"] = s["dead_windows"]
                    status["window_scan"] = datetime.now().isoformat()
                    if s["dead_windows"] > 0:
                        log(f"WINDOW ALERT: {s['dead_windows']} dead window(s) detected")
                except Exception as e:
                    log(f"Window scan failed: {e}")
                last_window_scan = now

            # Periodic stuck worker detection
            if now - last_stuck_check >= STUCK_CHECK_INTERVAL:
                try:
                    from tools.skynet_stuck_detector import StuckDetector
                    if stuck_detector is None:
                        stuck_detector = StuckDetector()
                    issues = stuck_detector.check_all()
                    stuck_detector.save_history()
                    status["stuck_issues"] = len(issues)
                    status["stuck_check"] = datetime.now().isoformat()
                    if issues:
                        names = [i["worker"] for i in issues]
                        log(f"STUCK DETECTOR: {len(issues)} issue(s) -- {names}")
                except Exception as e:
                    log(f"Stuck detection failed: {e}")
                last_stuck_check = now

            # Periodic self-awareness broadcast
            if now - last_awareness >= AWARENESS_INTERVAL:
                try:
                    # Direct import since we added ROOT to sys.path at module level
                    from tools.skynet_self import SkynetSelf
                    skynet_self = SkynetSelf()
                    pulse = skynet_self.broadcast_awareness()
                    status["last_awareness"] = datetime.now().isoformat()
                    status["iq"] = pulse.get("iq", 0)
                    log(f"Awareness broadcast OK (IQ={pulse.get('iq', 0):.3f})")
                except Exception as e:
                    log(f"Awareness broadcast failed: {e} (ROOT={ROOT}, sys.path[0]={sys.path[0]})")
                last_awareness = now

            # Periodic process guard registry refresh + dedup detection
            if now - last_guard_refresh >= GUARD_REFRESH_INTERVAL:
                try:
                    from tools.skynet_process_guard import refresh_registry
                    reg = refresh_registry()
                    status["protected_processes"] = reg.get("process_count", 0)
                    status["guard_refresh"] = datetime.now().isoformat()
                    # Detect duplicate services
                    from collections import Counter
                    role_counts = Counter(p["role"] for p in reg.get("processes", [])
                                         if p["role"] not in ("worker", "orchestrator"))
                    dups = {r: c for r, c in role_counts.items() if c > 1}
                    if dups:
                        dup_str = ", ".join(f"{r}={c}" for r, c in dups.items())
                        log(f"DUPLICATE ALERT: {dup_str}")
                        status["duplicate_services"] = dups
                        try:
                            alert = json.dumps({
                                "sender": "watchdog", "topic": "orchestrator",
                                "type": "alert",
                                "content": f"DUPLICATE_SERVICES: {dup_str} -- orchestrator should clean up"
                            }).encode()
                            req = urllib.request.Request(
                                "http://localhost:8420/bus/publish", data=alert,
                                headers={"Content-Type": "application/json"}
                            )
                            urllib.request.urlopen(req, timeout=3)
                        except Exception:
                            pass
                except Exception as e:
                    log(f"Process guard refresh failed: {e}")
                last_guard_refresh = now

            # Dispatch timeout check (every 60s)
            if now - last_skynet_check >= SKYNET_CHECK_INTERVAL:
                try:
                    dispatch_log = DATA_DIR / "dispatch_log.json"
                    if dispatch_log.exists():
                        entries = json.loads(dispatch_log.read_text(encoding="utf-8"))
                        stale = []
                        for e in entries:
                            if e.get("success") and not e.get("result_received"):
                                ts = datetime.fromisoformat(e["timestamp"])
                                age_s = (datetime.now() - ts).total_seconds()
                                if age_s > 300:  # 5 minutes
                                    stale.append({"worker": e["worker"], "task": e["task_summary"][:60], "age_min": round(age_s / 60, 1)})
                        if stale:
                            status["stale_dispatches"] = len(stale)
                            log(f"TIMEOUT ALERT: {len(stale)} dispatch(es) without result >5min: " +
                                ", ".join(f"{s['worker']}({s['age_min']}m)" for s in stale[:5]))
                            # Post alert to bus
                            try:
                                data = json.dumps({"sender": "watchdog", "topic": "orchestrator", "type": "alert",
                                                   "content": f"DISPATCH_TIMEOUT: {len(stale)} workers silent >5min: " +
                                                              ", ".join(f"{s['worker']}({s['age_min']}m)" for s in stale[:5])}).encode()
                                req = urllib.request.Request("http://localhost:8420/bus/publish", data=data,
                                                            headers={"Content-Type": "application/json"})
                                urllib.request.urlopen(req, timeout=3)
                            except Exception:
                                pass
                        else:
                            status.pop("stale_dispatches", None)
                except Exception as e:
                    log(f"Dispatch timeout check failed: {e}")

            write_status(status)
            time.sleep(10)
    except KeyboardInterrupt:
        log("Watchdog shutting down (Ctrl+C)")
        try:
            data = json.dumps({"sender": "watchdog", "topic": "orchestrator", "type": "lifecycle",
                "content": f"Watchdog shutdown: KeyboardInterrupt after {cycle} cycles"}).encode()
            req = urllib.request.Request(f"{SKYNET_URL}/bus/publish", data=data,
                                        headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass
    finally:
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass


def show_status():
    if STATUS_FILE.exists():
        print(STATUS_FILE.read_text())
    else:
        print("No watchdog status yet -- run 'python skynet_watchdog.py start' first")


def main():
    parser = argparse.ArgumentParser(description="Skynet Watchdog Daemon")
    parser.add_argument("action", choices=["start", "status"], help="start daemon or show status")
    parser.add_argument("--max-runtime", type=int, default=0,
                        help="Max runtime in seconds (0=unlimited)")
    args = parser.parse_args()

    if args.action == "status":
        show_status()
    else:
        run_daemon(args)


if __name__ == "__main__":
    main()
